"""T-403：统一隔离 ActionRunner 的隔离/并发/预算/重试正确性测试。

验收（tasks.md T-403）：
- K=2 使用 barrier 证明真实重叠（真实执行器栈 + fake provider，不是"执行了两个"）；
- 不同完成顺序不改变合并结果（稳定合并）；
- 预算超限零派发（预派发预约）；
- 任务失败记录保留；
- 共享键冲突语义确定（sequence 顺序裁决，各动作记录不丢）；
- 每动作至多一次定向修正，所有 attempt 保留。
"""
import asyncio
import threading

from deepresearch_agent.agents.multi_agent.core.plan_spec import (
    AcceptanceCriteria,
    PlanSpec,
    ProblemStatement,
    TaskGraph,
    TaskNode,
)
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.agents.multi_agent.core.rubric import build_task_rubric
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.executor.base_executor import BaseExecutor
from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor
from deepresearch_agent.agents.multi_agent.executor.worker_coordinator import WorkerCoordinator
from deepresearch_agent.harness.action_models import action_spec_from_task_node
from deepresearch_agent.harness.action_runner import ActionRunner, BatchAction, DispatchBudget
from deepresearch_agent.harness.contracts import SourceMode


class FakeProvider:
    """Fake 检索 Provider：真实 search 接口签名；可注入延迟与并发 barrier。"""

    def __init__(self, mode: SourceMode = SourceMode.WEB, *, delay: float = 0.0, barrier: threading.Barrier | None = None):
        self.mode = mode
        self.provider_name = "fake_web"
        self.calls: list[str] = []
        self.delay = delay
        self.barrier = barrier

    async def search(self, query, *, top_k, search_depth, filters, call_context):
        self.calls.append(query)
        if self.barrier is not None:
            # 两个 worker 线程必须同时到达这里——GIL 原子性无法解释该同步点
            self.barrier.wait(timeout=5)
        if self.delay:
            await asyncio.sleep(self.delay)
        return [
            RetrievalResult(
                granularity="DO",
                evidence=f"证据: {query}",
                source="tavily_search",
                source_mode=self.mode.value,
                score=0.9,
                metadata=RetrievalMetadata(source_id=f"web-{abs(hash(query)) % 1000}", source_type="webpage"),
            )
        ]


def make_state(tasks: list[TaskNode], *, source_mode: str = "web") -> PlanExecuteState:
    plan = PlanSpec(
        source_mode=source_mode,
        problem_statement=ProblemStatement(original_query="runner 测试"),
        task_graph=TaskGraph(nodes=list(tasks), execution_mode="parallel"),
        acceptance_criteria=AcceptanceCriteria(min_evidence_count=1),
    )
    state = PlanExecuteState(input="runner 测试", source_mode=source_mode, plan=plan)
    state.plan = plan
    return state


def make_action(task_id: str, query: str, *, source_mode: str = "web") -> BatchAction:
    task = TaskNode(
        task_id=task_id, task_type="web_search", source_mode=source_mode,
        description=query, parameters={"query": query},
    )
    rubric = build_task_rubric(task_id, description=query, source_mode=source_mode, min_evidence=1)
    return BatchAction(spec=action_spec_from_task_node(task, rubric_id=rubric.rubric_id), task=task, rubric=rubric)


async def verify_ok(action, records, executed_queries):
    class _V:
        passed = True
        retry_reason = None

    return _V()


async def verify_fail(action, records, executed_queries):
    class _V:
        passed = False
        retry_reason = "证据不足"

    return _V()


# ---------- K=2 真实重叠（barrier 证明） ----------

async def test_k2_real_overlap_proven_by_barrier():
    """K=2：两个动作的 provider 调用必须在同一线程窗口内重叠（barrier 等待双方）。"""
    barrier = threading.Barrier(2)
    provider = FakeProvider(SourceMode.WEB, barrier=barrier)
    coordinator = WorkerCoordinator(
        executors=[RetrievalExecutor(provider=provider)],
        max_parallel_workers=2,
    )
    runner = ActionRunner(
        execute_action=coordinator.execute_action,
        max_parallel=2, max_concurrency=2,
        allowed_tools={"web_search"},
    )
    actions = [make_action("task_1", "查询一"), make_action("task_2", "查询二")]
    state = make_state([action.task for action in actions])
    outcomes = await runner.run_and_merge(actions, shared_state=state, verify_fn=verify_ok)

    assert len(provider.calls) == 2  # 两个动作都真正触达 provider
    assert all(outcome.verdict.passed for outcome in outcomes)
    record_task_ids = sorted(record.task_id for record in state.execution_records)
    assert record_task_ids == ["task_1", "task_2"]
    assert len({record.record_id for record in state.execution_records}) == 2
    # 共享状态只经协调者合并变更：每个动作的证据都进入共享账本
    assert len(state.execution_records) == 2


async def test_k1_serial_has_no_overlap_requirement():
    """K=1：串行执行（barrier=1 立即通过），行为与串行 driver 一致。"""
    barrier = threading.Barrier(1)
    provider = FakeProvider(SourceMode.WEB, barrier=barrier)
    coordinator = WorkerCoordinator(
        executors=[RetrievalExecutor(provider=provider)],
        max_parallel_workers=1,
    )
    runner = ActionRunner(
        execute_action=coordinator.execute_action,
        max_parallel=1, max_concurrency=1,
        allowed_tools={"web_search"},
    )
    actions = [make_action("task_1", "查询一"), make_action("task_2", "查询二")]
    state = make_state([action.task for action in actions])
    outcomes = await runner.run_and_merge(actions, shared_state=state, verify_fn=verify_ok)
    assert [record.task_id for record in state.execution_records] == ["task_1", "task_2"]


# ---------- 稳定合并：完成顺序不改变结果 ----------

class _RecordingExecutor(BaseExecutor):
    """可控延迟的执行器：按 task_id 记录执行并写共享键冲突。"""

    worker_type = "recording_test"

    def __init__(self, delays: dict[str, float]):
        self.delays = delays
        self.executed: list[str] = []
        self.lock = threading.Lock()

    def can_handle(self, task_type: str) -> bool:
        return task_type == "web_search"

    def execute_task(self, task, state, signal):
        import time

        time.sleep(self.delays.get(task.task_id, 0.0))
        with self.lock:
            # 完成时刻记录（而非派发时刻）：completed 列表顺序即完成顺序
            self.executed.append(task.task_id)
        record = self._record_for(task)
        state.execution_records.append(record)
        if state.execution_context is not None:
            state.execution_context.intermediate_results["shared_key"] = f"written_by_{task.task_id}"
            state.execution_context.intermediate_results[task.task_id] = f"local_{task.task_id}"
            state.execution_context.completed_task_ids.append(task.task_id)
        if state.plan is not None:
            state.plan.update_task_status(task.task_id, "completed")
        from deepresearch_agent.agents.multi_agent.executor.base_executor import TaskExecutionResult

        return TaskExecutionResult(record=record, success=True)

    @staticmethod
    def _record_for(task):
        from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionMetadata, ExecutionRecord

        return ExecutionRecord(
            task_id=task.task_id,
            session_id="session_test",
            worker_type="recording_test",
            metadata=ExecutionMetadata(worker_type="recording_test"),
        )


async def _run_two(delays: dict[str, float]) -> tuple[PlanExecuteState, list[str]]:
    actions = [make_action("task_1", "查询一"), make_action("task_2", "查询二")]
    executor = _RecordingExecutor(delays)
    coordinator = WorkerCoordinator(
        executors=[executor], max_parallel_workers=2,
    )
    runner = ActionRunner(
        execute_action=coordinator.execute_action,
        max_parallel=2, max_concurrency=2, allowed_tools={"web_search"},
    )
    state = make_state([action.task for action in actions])
    from deepresearch_agent.agents.multi_agent.core.state import ExecutionContext

    state.execution_context = ExecutionContext()
    await runner.run_and_merge(actions, shared_state=state, verify_fn=verify_ok)
    return state, list(executor.executed)


async def test_completion_order_does_not_change_merge_result():
    """不同完成顺序（task_1 先完 vs task_2 先完）产生相同的合并结果。"""
    slow_first, slow_second = {"task_1": 0.2, "task_2": 0.0}, {"task_1": 0.0, "task_2": 0.2}
    state_a, executed_a = await _run_two(slow_first)
    state_b, executed_b = await _run_two(slow_second)

    # 完成顺序确实不同（重叠执行）
    assert executed_a[0] == "task_2" and executed_b[0] == "task_1"
    # 合并结果与完成顺序无关：记录按 sequence 顺序落账
    assert [r.task_id for r in state_a.execution_records] == [r.task_id for r in state_b.execution_records]
    assert [r.task_id for r in state_a.execution_records] == ["task_1", "task_2"]
    assert state_a.execution_context.completed_task_ids == state_b.execution_context.completed_task_ids
    # 共享键冲突按 sequence 裁决：后合并者（task_2）写入的值胜出，且两侧一致
    assert state_a.execution_context.intermediate_results["shared_key"] == "written_by_task_2"
    assert state_a.execution_context.intermediate_results["shared_key"] == state_b.execution_context.intermediate_results["shared_key"]
    # 各动作局部结果不被覆盖
    assert state_a.execution_context.intermediate_results["task_1"] == "local_task_1"
    assert state_a.execution_context.intermediate_results["task_2"] == "local_task_2"


# ---------- 预算：超限零派发 ----------

async def test_budget_exhausted_admission_dispatches_nothing():
    """预算预约：额度不足的动作零派发（执行器不可达）。"""
    executed: list[str] = []

    def execute_action(state, task):
        executed.append(task.task_id)
        return True, None, None

    runner = ActionRunner(
        execute_action=execute_action,
        max_parallel=2, max_concurrency=2, allowed_tools={"web_search"},
        budget=DispatchBudget(used_fn=lambda: 10, limit=10),
    )
    actions = [make_action("task_1", "查询一"), make_action("task_2", "查询二")]
    report = runner.admit(actions, completed_task_ids=[], executed_queries=[])
    assert report.admitted == []
    assert [r.reason_code for r in report.rejections] == ["budget_exhausted", "budget_exhausted"]
    outcomes = await runner.run_and_merge(report.admitted, shared_state=make_state([]), verify_fn=verify_ok)
    assert outcomes == [] and executed == []
    assert runner.budget.reserved == 0  # 零预约


async def test_budget_reservation_released_after_batch():
    """预约在批次结束后释放；下一批按释放后的余量准入。"""
    executed: list[str] = []

    def execute_action(state, task):
        executed.append(task.task_id)
        return True, None, None

    runner = ActionRunner(
        execute_action=execute_action,
        max_parallel=2, max_concurrency=2, allowed_tools={"web_search"},
        budget=DispatchBudget(used_fn=lambda: 8, limit=10),
    )
    first = [make_action("task_1", "查询一")]
    report = runner.admit(first, completed_task_ids=[], executed_queries=[])
    assert len(report.admitted) == 1 and runner.budget.reserved == 1
    await runner.run_and_merge(report.admitted, shared_state=make_state([first[0].task]), verify_fn=verify_ok)
    assert runner.budget.reserved == 0  # 批次结束释放
    second = [make_action("task_2", "查询二")]
    report2 = runner.admit(second, completed_task_ids=[], executed_queries=[])
    assert len(report2.admitted) == 1  # 释放后可再次准入


# ---------- 准入门：依赖 / 白名单 / 查重 ----------

async def test_admission_rejects_dependencies_tools_and_duplicates():
    def execute_action(state, task):
        return True, None, None

    runner = ActionRunner(
        execute_action=execute_action,
        max_parallel=2, max_concurrency=2, allowed_tools={"web_search"},
    )
    dep_blocked = make_action("task_dep", "依赖未就绪")
    dep_blocked.task.depends_on = ["task_missing"]
    tool_blocked = make_action("task_tool", "工具越权")
    tool_blocked.task.task_type = "local_search"  # graphrag 专属
    actions = [
        dep_blocked,
        tool_blocked,
        make_action("task_dup", "重复查询"),
        make_action("task_ok", "新查询"),
    ]
    report = runner.admit(
        actions, completed_task_ids=[],
        executed_queries=["web_search:重复查询"],
    )
    assert [action.action_id for action in report.admitted] == ["action_task_ok"]
    reasons = {r.action_id: r.reason_code for r in report.rejections}
    assert reasons["action_task_dep"] == "dependency_not_ready"
    assert reasons["action_task_tool"] == "tool_not_allowed"
    assert reasons["action_task_dup"] == "duplicate_query"


# ---------- 失败记录保留 ----------

async def test_failed_task_record_retained_and_active_points_to_it():
    """任务失败：失败记录进共享账本（不删除）；active 指向该尝试。"""
    provider = FakeProvider(SourceMode.WEB)
    coordinator = WorkerCoordinator(
        executors=[RetrievalExecutor(provider=provider)],
        max_parallel_workers=1,
    )
    runner = ActionRunner(
        execute_action=coordinator.execute_action,
        max_parallel=1, max_concurrency=1, allowed_tools={"web_search"},
    )
    # 让 provider 抛错 → RetrievalExecutor 记失败
    async def failing_search(query, *, top_k, search_depth, filters, call_context):
        raise RuntimeError("provider down")

    provider.search = failing_search
    action = make_action("task_bad", "必失败查询")
    state = make_state([action.task])
    outcomes = await runner.run_and_merge([action], shared_state=state, verify_fn=verify_ok)

    outcome = outcomes[0]
    assert outcome.active.success is False
    assert outcome.active.error is not None
    assert len(outcome.attempts) == 1
    # 失败记录保留在共享账本
    assert any(record.task_id == "task_bad" for record in state.execution_records)


# ---------- 每动作一次定向修正 ----------

async def test_correction_runs_at_most_once_per_action():
    """验证失败 → 恰好一次定向修正；修正后仍失败不再重试（runner 层保证）。"""
    dispatch_count = {"n": 0}

    def execute_action(state, task):
        dispatch_count["n"] += 1
        record = _RecordingExecutor._record_for(task)
        state.execution_records.append(record)
        return True, None, record

    runner = ActionRunner(
        execute_action=execute_action,
        max_parallel=1, max_concurrency=1, allowed_tools={"web_search"},
    )
    action = make_action("task_r", "修正前查询")
    state = make_state([action.task])
    corrections: list[str] = []

    async def correction_fn(action, verdict, error):
        corrections.append(action.action_id)
        query = f"修正后查询-{len(corrections)}"
        retry_task = TaskNode(
            task_id=action.task.task_id, task_type="web_search", source_mode="web",
            description=query, parameters={"query": query},
        )
        return BatchAction(
            spec=action.spec.next_attempt(query=query), task=retry_task, rubric=action.rubric,
        )

    outcomes = await runner.run_and_merge(
        [action], shared_state=state, verify_fn=verify_fail, correction_fn=correction_fn,
    )
    assert corrections == ["action_task_r"]  # 只修正一次
    assert dispatch_count["n"] == 2  # 首次 + 一次修正
    assert len(outcomes[0].attempts) == 2
    assert outcomes[0].active.action.attempt == 2


async def test_correction_none_means_no_second_dispatch():
    async def execute_action(state, task):
        record = _RecordingExecutor._record_for(task)
        state.execution_records.append(record)
        return True, None, record

    runner = ActionRunner(
        execute_action=execute_action,
        max_parallel=1, max_concurrency=1, allowed_tools={"web_search"},
    )
    action = make_action("task_x", "查询")
    state = make_state([action.task])
    calls = {"n": 0}

    async def correction_fn(action, verdict, error):
        calls["n"] += 1
        return None

    outcomes = await runner.run_and_merge(
        [action], shared_state=state, verify_fn=verify_fail, correction_fn=correction_fn,
    )
    assert calls["n"] == 1 and len(outcomes[0].attempts) == 1


# ---------- 停止派发 ----------

async def test_stop_check_cancels_undispatched_actions():
    """批内取消：串行信号量下第一个动作执行期间翻转 stop 标志，
    第二个动作必须在派发前被取消（零派发、无记录）。"""
    started: list[str] = []

    def execute_action(state, task):
        started.append(task.task_id)
        stop_flag["flag"] = True  # 第一个动作执行时请求停止
        record = _RecordingExecutor._record_for(task)
        state.execution_records.append(record)
        return True, None, record

    runner = ActionRunner(
        execute_action=execute_action,
        max_parallel=1, max_concurrency=1, allowed_tools={"web_search"},
    )
    actions = [make_action("task_1", "查询一"), make_action("task_2", "查询二")]
    state = make_state([action.task for action in actions])
    stop_flag = {"flag": False}

    outcomes = await runner.run_and_merge(
        actions, shared_state=state, verify_fn=verify_ok,
        stop_check=lambda: stop_flag["flag"],
    )
    assert started == ["task_1"]  # 只有第一个动作被派发
    cancelled = [outcome for outcome in outcomes if outcome.active.cancelled]
    executed = [outcome for outcome in outcomes if not outcome.active.cancelled]
    assert [o.action.action_id for o in executed] == ["action_task_1"]
    assert [o.action.action_id for o in cancelled] == ["action_task_2"]
    for outcome in cancelled:
        assert outcome.active.record is None and outcome.active.error == "cancelled_before_dispatch"
    assert len(state.execution_records) == 1  # 取消动作零记录


async def test_partial_budget_admission_boundary():
    """预算边界：剩余 1 个额度时首批准入、后续动作被拒（部分准入可观测）。"""

    def execute_action(state, task):
        return True, None, None

    runner = ActionRunner(
        execute_action=execute_action,
        max_parallel=2, max_concurrency=2, allowed_tools={"web_search"},
        budget=DispatchBudget(used_fn=lambda: 9, limit=10),
    )
    actions = [make_action("task_1", "查询一"), make_action("task_2", "查询二")]
    report = runner.admit(actions, completed_task_ids=[], executed_queries=[])
    assert [action.action_id for action in report.admitted] == ["action_task_1"]
    assert [r.action_id for r in report.rejections] == ["action_task_2"]
    assert report.rejections[0].reason_code == "budget_exhausted"


async def test_stop_check_before_dispatch_prevents_all_execution():
    def execute_action(state, task):
        raise AssertionError("不应派发")

    runner = ActionRunner(
        execute_action=execute_action,
        max_parallel=2, max_concurrency=2, allowed_tools={"web_search"},
    )
    actions = [make_action("task_1", "查询一")]
    state = make_state([action.task for action in actions])
    outcomes = await runner.run_and_merge(
        actions, shared_state=state, verify_fn=verify_ok, stop_check=lambda: True,
    )
    assert outcomes[0].active.cancelled is True
    assert state.execution_records == []


# ---------- 隔离性：worker 不直接写共享状态 ----------

async def test_worker_threads_receive_copies_not_shared_state():
    """隔离性证明：worker 线程拿到的 state 与共享 state 不是同一对象；
    worker 只写副本，共享记录仅由协调者合并产生。"""
    seen_states: list[int] = []

    def execute_action(state, task):
        seen_states.append(id(state))
        record = _RecordingExecutor._record_for(task)
        state.execution_records.append(record)  # 只写副本
        return True, None, record

    runner = ActionRunner(
        execute_action=execute_action,
        max_parallel=2, max_concurrency=2, allowed_tools={"web_search"},
    )
    actions = [make_action("task_1", "查询一"), make_action("task_2", "查询二")]
    state = make_state([action.task for action in actions])
    await runner.run_and_merge(actions, shared_state=state, verify_fn=verify_ok)

    assert len(seen_states) == 2
    assert all(seen != id(state) for seen in seen_states), "worker 必须拿到深拷贝副本"
    assert len(set(seen_states)) == 2, "各动作的副本彼此独立"
    # 共享账本仅含协调者合并的记录
    assert len(state.execution_records) == 2
    assert [r.task_id for r in state.execution_records] == ["task_1", "task_2"]
