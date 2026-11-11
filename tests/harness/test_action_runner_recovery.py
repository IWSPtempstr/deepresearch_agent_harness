"""T-404：多动作 intent 与耐久恢复测试。

验收（tasks.md T-404）：在 intent 后、调用后/提交前、部分批次提交后、retry 中
注入中断；新 Runtime 恢复；断言工具调用/证据/预算/事件（真实持久层行，
不仅测试 helper predicate）。

崩溃注入方式：以 RunLeaseLost 传播模拟"进程死亡"——Runtime 的租约易主
处理器不做终态转换、不写 checkpoint（run 留在 executing，最后一份 checkpoint
即崩溃现场），与真实 kill -9 后的恢复输入一致。
"""
import json

import pytest
from sqlalchemy import select

from deepresearch_agent.harness.contracts import SourceMode, WorkflowMode
from deepresearch_agent.harness.coreact_state import (
    CoreActLoopState,
    PendingAction,
    loop_state_from_workflow_state,
)
from deepresearch_agent.harness.errors import RunLeaseLost
from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver
from deepresearch_agent.harness.runtime import HarnessRuntime
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.artifact_store import ArtifactStore
from deepresearch_agent.persistence.models import PlanModel, TaskModel, ToolCallModel
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository,
    CheckpointRepository,
    ContractRepository,
    EventRepository,
    EvidenceRepository,
    MessageRepository,
    PlanTaskToolRepository,
    RunRepository,
    SessionRepository,
)

from test_coreact_e2e import FakeOrchestrator, ScriptedLLM, action_by_id, ready


# ---------------------------------------------------------------- fixtures


@pytest.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'coreact-runner-recovery.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


def make_runtime(database, tmp_path, factory) -> HarnessRuntime:
    return HarnessRuntime(
        run_repository=RunRepository(database), message_repository=MessageRepository(database),
        event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
        evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
        trajectory_repository=PlanTaskToolRepository(database), workflow_factory=factory,
        artifact_store=ArtifactStore(tmp_path / "artifacts"), artifact_repository=ArtifactRepository(database),
    )


async def create_run(database) -> str:
    from backend.app.schemas import MessageCreate, RunCreate, SessionCreate

    session = await SessionRepository(database).create(SessionCreate(title="runner-recovery"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="调研检索增强技术", client_message_id="atomic"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
            config_snapshot={"orchestration": "coreact_v1", "min_evidence": 1},
        ),
    )
    return run.run_id


def make_driver(context, orchestrator):
    return PlanExecuteReportDriver(context, orchestrator, events=None)


async def latest_checkpoint_state(database, run_id: str) -> CoreActLoopState | None:
    checkpoint = await CheckpointRepository(database).latest(run_id)
    assert checkpoint is not None
    workflow_state = json.loads(checkpoint.state_json or "{}").get("workflow_state") or {}
    return loop_state_from_workflow_state(workflow_state)


async def stored_rows(database, run_id: str):
    async with database.sessions() as session:
        plans = list((await session.execute(select(PlanModel).where(PlanModel.run_id == run_id))).scalars())
        tasks = list((await session.execute(select(TaskModel).where(PlanModel.run_id == run_id))).scalars())
        calls = list((await session.execute(select(ToolCallModel).where(ToolCallModel.run_id == run_id))).scalars())
    evidence = await EvidenceRepository(database).list_for_run(run_id)
    return plans, tasks, calls, evidence


async def count_all_events(database, run_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    after = 0
    repo = EventRepository(database)
    while True:
        batch = await repo.list_after(run_id, after_event_id=after, limit=1000)
        if not batch:
            break
        for item in batch:
            counts[item.event_type] = counts.get(item.event_type, 0) + 1
        after = batch[-1].event_id
        if len(batch) < 1000:
            break
    return counts



@pytest.fixture
def parallel_two(monkeypatch):
    monkeypatch.setattr("deepresearch_agent.harness.coreact_loop.COREACT_MAX_PARALLEL", 2)


# ---------------------------------------------------------------- tests


class TestFullBatchIntent:
    async def test_intent_checkpoint_carries_whole_batch(self, database, tmp_path, parallel_two):
        """K=2 intent：派发前 checkpoint 携带整个批次（主动作+补位动作），不是只有主动作。"""
        run_id = await create_run(database)
        llm = ScriptedLLM([action_by_id("plan_1", "检索增强 基础概念"), ready()])
        orchestrator = FakeOrchestrator(llm)

        class CrashAfterIntentDriver(PlanExecuteReportDriver):
            async def _coreact_persist_intent(self, plan):
                await super()._coreact_persist_intent(plan)
                raise RunLeaseLost("crash: intent 已落盘、动作未派发")

        runtime = make_runtime(
            database, tmp_path,
            lambda context, events: CrashAfterIntentDriver(context, orchestrator, events=events),
        )
        context = await runtime.execute_run(run_id)
        assert context.status.value == "executing"  # 租约易主：不做终态转换

        state = await latest_checkpoint_state(database, run_id)
        assert state is not None and state.continuation_pending()
        assert [item.task.get("task_id") for item in state.pending_batch] == ["plan_1", "plan_2"]
        assert state.next_operation == "execute"
        # intent 阶段不写 tool/evidence 行（动作尚未执行）
        _plans, tasks, calls, evidence = await stored_rows(database, run_id)
        running_tasks = [task for task in tasks if task.status == "running"]
        assert {task.task_id for task in running_tasks} == {"plan_1", "plan_2"}
        assert calls == [] and evidence == []


class TestCrashRecovery:
    async def test_crash_after_intent_resumes_without_losing_secondary_action(self, database, tmp_path, parallel_two):
        """intent 后崩溃 → 新 Runtime 恢复 → 批次两个动作都执行，次要动作不丢。"""
        run_id = await create_run(database)
        llm_a = ScriptedLLM([action_by_id("plan_1", "检索增强 基础概念"), ready()])
        orchestrator_a = FakeOrchestrator(llm_a)

        class CrashAfterIntentDriver(PlanExecuteReportDriver):
            async def _coreact_persist_intent(self, plan):
                await super()._coreact_persist_intent(plan)
                raise RunLeaseLost("crash after intent")

        runtime_a = make_runtime(
            database, tmp_path,
            lambda context, events: CrashAfterIntentDriver(context, orchestrator_a, events=events),
        )
        await runtime_a.execute_run(run_id)
        assert orchestrator_a.executed == []  # 崩溃于派发前

        # 新 Runtime 恢复：scripted 只决定 ready（批次从游标重放，不重新决策）
        llm_b = ScriptedLLM([ready("重放完成")])
        orchestrator_b = FakeOrchestrator(llm_b)
        runtime_b = make_runtime(
            database, tmp_path, lambda context, events: make_driver(context, orchestrator_b),
        )
        final_context = await runtime_b.execute_run(run_id)
        assert final_context.status.value == "completed", (await RunRepository(database).get(run_id)).error_message
        # 两个动作都被重放（恢复路径不走 controller 决策：llm_b 只用了 ready）
        assert [task.task_id for task in orchestrator_b.executed] == ["plan_1", "plan_2"]
        plans, tasks, calls, evidence = await stored_rows(database, run_id)
        assert len(calls) == 2 and len(evidence) == 2
        assert {call.status for call in calls} == {"completed"}

    async def test_crash_between_rows_and_checkpoint_replays_with_stable_tool_id(self, database, tmp_path):
        """行已落库、checkpoint 未更新的真实危险窗口：恢复重放同一动作时，
        稳定 tool_call_id（(run_id, task_id, attempt) 派生）保证行幂等——
        不产生重复 tool 行、不重复计费；外部调用重复一次（不承诺 exactly-once）。"""
        run_id = await create_run(database)
        external_calls: list[str] = []
        llm_a = ScriptedLLM([action_by_id("plan_1", "检索增强 基础概念"), ready()])
        orchestrator_a = FakeOrchestrator(llm_a)
        leg_a_execute = orchestrator_a.execute_action

        def execute_and_count(self, state, task, *, progress_callback=None):
            external_calls.append(task.task_id)
            return leg_a_execute(state, task, progress_callback=progress_callback)

        orchestrator_a.execute_action = execute_and_count.__get__(orchestrator_a)
        runtime_a = make_runtime(
            database, tmp_path, lambda context, events: make_driver(context, orchestrator_a),
        )
        # 在"动作行已持久化、checkpoint 尚未写"的窗口注入崩溃：
        # executing 阶段的 save 次序 = intent(1) → 动作提交(2) → 批次收尾(3)…；
        # （此前还有 context_building/planning 阶段的 save，不计入）
        original_save = runtime_a.checkpoints.save
        executing_saves = {"n": 0}

        async def save_then_die(context, stage):
            if stage == "executing":
                executing_saves["n"] += 1
                if executing_saves["n"] == 2:
                    raise RunLeaseLost("crash: rows persisted, checkpoint stale")
            return await original_save(context, stage)

        runtime_a.checkpoints.save = save_then_die
        await runtime_a.execute_run(run_id)
        assert external_calls == ["plan_1"]  # 崩溃前外部调用已发生

        llm_b = ScriptedLLM([action_by_id("plan_1", "检索增强 基础概念"), ready()])
        orchestrator_b = FakeOrchestrator(llm_b)
        leg_b_execute = orchestrator_b.execute_action

        def record_recovery_call(self, state, task, *, progress_callback=None):
            external_calls.append(task.task_id)  # 恢复段同样计外部调用
            return leg_b_execute(state, task, progress_callback=progress_callback)

        orchestrator_b.execute_action = record_recovery_call.__get__(orchestrator_b)
        runtime_b = make_runtime(
            database, tmp_path, lambda context, events: make_driver(context, orchestrator_b),
        )
        final_context = await runtime_b.execute_run(run_id)
        assert final_context.status.value == "completed"
        # 外部调用重复（崩溃前 1 次 + 恢复后重做 1 次）——残余窗口如实存在
        assert external_calls == ["plan_1", "plan_1"]
        # 稳定 tool ID 使持久层行幂等：tool/evidence 各恰好 1 份，事件不重复
        _plans, _tasks, calls, evidence = await stored_rows(database, run_id)
        assert len(calls) == 1 and len(evidence) == 1
        run_row = await RunRepository(database).get(run_id)
        usage = json.loads(run_row.usage_json or "{}")
        # 计费跟随首次行写入且不重复：该窗口内的内存计费随崩溃丢失（低估），
        # 重放按既有行不再计费（不双重计费）——与"不承诺恰好一次"一致。
        assert usage.get("usage", {}).get("tool_calls", 0) <= 1
        counts = await count_all_events(database, run_id)
        assert counts.get("tool.completed") == 1  # 不重复发布

    async def test_crash_after_partial_batch_commit_replays_only_remaining(self, database, tmp_path, parallel_two):
        """部分批次提交后崩溃：已提交动作不重做，仅重放剩余 in-flight 动作。"""
        run_id = await create_run(database)
        llm_a = ScriptedLLM([action_by_id("plan_1", "检索增强 基础概念"), ready()])
        orchestrator_a = FakeOrchestrator(llm_a)

        class CrashAfterFirstCommitDriver(PlanExecuteReportDriver):
            @property
            def action_commit(self):
                return getattr(self, "_wrapped_action_commit", None)

            @action_commit.setter
            def action_commit(self, callback):
                if callback is None:
                    self._wrapped_action_commit = None
                    return

                async def _wrapped(driver):
                    await callback(driver)  # 先真实提交（行 + checkpoint）
                    if len(driver.coreact_loop.pending_batch) == 1:
                        raise RunLeaseLost("crash: 动作1已提交、动作2未执行")

                self._wrapped_action_commit = _wrapped

        runtime_a = make_runtime(
            database, tmp_path,
            lambda context, events: CrashAfterFirstCommitDriver(context, orchestrator_a, events=events),
        )
        await runtime_a.execute_run(run_id)

        state = await latest_checkpoint_state(database, run_id)
        assert [item.task.get("task_id") for item in state.pending_batch] == ["plan_2"]
        assert state.committed_action_ids == ["action_plan_1"]

        llm_b = ScriptedLLM([ready("重放完成")])
        orchestrator_b = FakeOrchestrator(llm_b)
        runtime_b = make_runtime(
            database, tmp_path, lambda context, events: make_driver(context, orchestrator_b),
        )
        final_context = await runtime_b.execute_run(run_id)
        assert final_context.status.value == "completed"
        assert [task.task_id for task in orchestrator_b.executed] == ["plan_2"]  # 不重做动作 1
        _plans, tasks, calls, evidence = await stored_rows(database, run_id)
        assert len(calls) == 2 and len(evidence) == 2
        counts = await count_all_events(database, run_id)
        assert counts.get("tool.completed") == 2  # 动作 1 的事件不重复发布
        # T-404 review：已提交动作的记录必须携带 rubric/verdict 元数据（提交前
        # 附加）——恢复后最终 rubric_coverage 门不得被静默削弱
        checks = await ContractRepository(database).list_for_run(run_id)
        kinds = {check.kind: check.passed for check in checks}
        assert bool(kinds.get("rubric_coverage")) is True
        assert bool(kinds.get("min_evidence")) is True


class TestRetryCrashRecovery:
    async def test_crash_during_retry_replays_retry_cursor(self, database, tmp_path):
        """retry 中崩溃：修正游标（attempt=2）已持久化——恢复重放重试动作而非重新决策。"""
        run_id = await create_run(database)
        external_calls: list[str] = []
        # 首次执行产出无关证据（rubric 失败）→ controller 定向修正 → 重试执行中崩溃
        llm_a = ScriptedLLM([
            action_by_id("plan_1", "无关查询"),
            action_by_id("plan_1", "检索增强 修正后查询"),
        ])
        orchestrator_a = FakeOrchestrator(llm_a)
        original_execute = FakeOrchestrator.execute_action

        def execute_retry_die(self, state, task, *, progress_callback=None):
            query = str(task.parameters.get("query", ""))
            if "修正后查询" in query:
                external_calls.append(query)
                raise RunLeaseLost("crash during retry execution")
            if getattr(self, "_first_done", False):
                return original_execute(state, task, progress_callback=progress_callback)
            self._first_done = True
            return failing_execute(self, state, task)

        def failing_execute(self, state, task, *, progress_callback=None):
            from deepresearch_agent.agents.multi_agent.core.execution_record import (
                ExecutionMetadata,
                ExecutionRecord,
                ToolCall,
            )
            from deepresearch_agent.agents.multi_agent.core.retrieval_result import (
                RetrievalMetadata,
                RetrievalResult,
            )

            self.executed.append(task.model_copy(deep=True))
            result = RetrievalResult(
                granularity="Chunk", evidence="完全无关的内容",
                metadata=RetrievalMetadata(source_id="https://x.example/a", source_type="webpage"),
                source="tavily_search", source_mode="web",
            )
            record = ExecutionRecord(
                task_id=task.task_id, session_id=state.session_id, worker_type="retrieval_executor",
                tool_calls=[ToolCall(
                    tool_name="web_search",
                    tool_call_id=f"call-{state.run_id}-{task.task_id}-a{getattr(task, 'attempt', 1)}",
                    source_mode="web", args=dict(task.parameters),
                    result={"answer": str(result.evidence)}, status="success",
                )],
                evidence=[result],
                metadata=ExecutionMetadata(worker_type="retrieval_executor", tool_calls_count=1),
            )
            record.metadata.environment["coreact"] = {}
            state.execution_records.append(record)
            if state.execution_context is not None:
                if task.task_id not in state.execution_context.completed_task_ids:
                    state.execution_context.completed_task_ids.append(task.task_id)
            return True, None, record

        orchestrator_a.execute_action = execute_retry_die.__get__(orchestrator_a)
        runtime_a = make_runtime(
            database, tmp_path, lambda context, events: make_driver(context, orchestrator_a),
        )
        await runtime_a.execute_run(run_id)

        state = await latest_checkpoint_state(database, run_id)
        assert state.pending_action is not None
        assert state.pending_action.attempt == 2
        assert "检索增强 修正后查询" in str(state.pending_action.task.get("parameters", {}).get("query", ""))

        # 恢复：重放重试动作（不走 controller），然后 ready 收尾
        llm_b = ScriptedLLM([ready("重放完成")])
        orchestrator_b = FakeOrchestrator(llm_b)
        runtime_b = make_runtime(
            database, tmp_path, lambda context, events: make_driver(context, orchestrator_b),
        )
        final_context = await runtime_b.execute_run(run_id)
        assert final_context.status.value == "completed", (await RunRepository(database).get(run_id)).error_message
        assert [task.parameters["query"] for task in orchestrator_b.executed] == ["检索增强 修正后查询"]
        # 重试 attempt=2 产生新 tool ID：两次尝试的行都保留（不删失败历史）
        tool_repo = PlanTaskToolRepository(database)
        assert await tool_repo.get_tool_call(f"call-{run_id}-plan_1-a1") is not None
        assert await tool_repo.get_tool_call(f"call-{run_id}-plan_1-a2") is not None
        # 预算：task_retries 在崩溃前已计一次（首次失败后的修正），恢复段不重复计
        run_row = await RunRepository(database).get(run_id)
        usage = json.loads(run_row.usage_json or "{}")
        assert usage.get("usage", {}).get("task_retries") == 1


class TestBatchCursorValidation:
    def test_invalid_batch_item_dropped_without_losing_siblings(self):
        """畸形批内单动作被剔除，其余 in-flight 动作保留（恢复不丢次要动作）。"""
        rubric = {"rubric_id": "r1", "task_id": "t1", "criteria": [
            {"criterion_id": "c", "kind": "evidence_presence", "description": "d"}]}
        good = PendingAction(
            task={"task_id": "t1", "task_type": "web_search", "description": "d", "source_mode": "web"},
            rubric=rubric, attempt=1,
        )
        bad = PendingAction(task={"task_id": "t2"}, rubric=rubric, attempt=1)  # 缺必填字段
        state = CoreActLoopState(phase="act", pending_batch=[bad, good])
        restored = loop_state_from_workflow_state({"coreact": state.model_dump(mode="json")})
        assert restored is not None
        assert [item.task.get("task_id") for item in restored.pending_batch] == ["t1"]
        assert restored.pending_batch[0].attempt == 1

    def test_all_invalid_batch_falls_back_to_fresh_decision(self):
        rubric = {"rubric_id": "r1", "task_id": "t1", "criteria": [
            {"criterion_id": "c", "kind": "evidence_presence", "description": "d"}]}
        bad = PendingAction(task={"task_id": "t2"}, rubric=rubric, attempt=1)
        state = CoreActLoopState(phase="act", pending_batch=[bad])
        restored = loop_state_from_workflow_state({"coreact": state.model_dump(mode="json")})
        assert restored is not None
        assert restored.pending_batch == [] and restored.pending_action is None
        assert restored.phase == "decide"

    def test_legacy_single_pending_snapshot_roundtrips_unchanged(self):
        """旧单动作快照（无 pending_batch）：恢复后 pending_batch 归一化、语义不变。"""
        rubric = {"rubric_id": "r1", "task_id": "t1", "criteria": [
            {"criterion_id": "c", "kind": "evidence_presence", "description": "d"}]}
        state = CoreActLoopState(
            phase="verify",
            pending_action=PendingAction(
                task={"task_id": "t1", "task_type": "web_search", "description": "d", "source_mode": "web"},
                rubric=rubric, attempt=2,
            ),
        )
        restored = loop_state_from_workflow_state({"coreact": state.model_dump(mode="json")})
        assert restored is not None
        # 兼容读取：单动作经 inflight_items() 回退可见
        assert [item.attempt for item in restored.inflight_items()] == [2]
        assert restored.next_operation == "execute"
