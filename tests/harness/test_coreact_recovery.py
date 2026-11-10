"""T-205: step-level persistence and version-aware recovery for the coreact loop."""
import json

import pytest

from deepresearch_agent.agents.multi_agent.core.execution_record import (
    ExecutionMetadata,
    ExecutionRecord,
    ToolCall,
)
from deepresearch_agent.harness.budgets import BudgetManager
from deepresearch_agent.harness.checkpoints import CheckpointManager
from deepresearch_agent.harness.contracts import RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.harness.coreact_state import CoreActLoopState
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.harness.runtime import HarnessRuntime
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.artifact_store import ArtifactStore
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository,
    CheckpointRepository,
    ContractRepository,
    EventRepository,
    EvidenceRepository,
    MessageRepository,
    RunRepository,
    SessionRepository,
)
from deepresearch_agent.persistence.repositories.trajectory import PlanTaskToolRepository


@pytest.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'coreact-recovery.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


def build_runtime(database, tmp_path, factory):
    return HarnessRuntime(
        run_repository=RunRepository(database), message_repository=MessageRepository(database),
        event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
        evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
        trajectory_repository=PlanTaskToolRepository(database), workflow_factory=factory,
        artifact_store=ArtifactStore(tmp_path / "artifacts"), artifact_repository=ArtifactRepository(database),
    )


async def create_run(database, run_id: str = "run-coreact") -> str:
    """Persist a real Run so foreign keys hold for trajectory/event rows."""
    from backend.app.schemas import MessageCreate, RunCreate, SessionCreate

    session = await SessionRepository(database).create(SessionCreate(title="coreact"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(
            session_id=session.session_id, role="user", content="研究问题",
            client_message_id=f"client-{session.session_id}",
        ),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
            config_snapshot={"orchestration": "coreact_v1"}, min_evidence=1,
        ),
    )
    return run.run_id


def make_context(run_id="run-coreact", **overrides):
    values = dict(
        run_id=run_id, session_id="session-coreact", trigger_message_id="msg-coreact",
        source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        status=RunStatus.EXECUTING, original_query="研究问题",
        config_snapshot={"orchestration": "coreact_v1"},
    )
    values.update(overrides)
    return RunContext(**values)


class StepDriver:
    """Step-driven fake driver: emits one action record and a durable marker."""

    supports_step_commit = True

    def __init__(self, context, events=None):
        self.context = context
        self.events = events
        self.records = []
        self.coreact_loop = CoreActLoopState()
        self.step_commit = None

    def snapshot(self):
        return {
            "state": {},
            "orchestration": "coreact_v1",
            "coreact": self.coreact_loop.model_dump(mode="json"),
        }

    def execution_records(self):
        return list(self.records)

    def evidence_results(self):
        return []

    def plan_record(self):
        tasks = [{
            "task_id": record.task_id,
            "task_type": "web_search",
            "source_mode": "web",
            "status": "completed",
            "description": "probe",
        } for record in self.records]
        return ({"plan_id": f"plan-{self.context.run_id}", "status": "executing", "tasks": tasks}, tasks)

    def add_action(self, index: int) -> ExecutionRecord:
        record = ExecutionRecord(
            task_id=f"task-{self.context.run_id}-{index}",
            session_id=self.context.session_id,
            worker_type="fake",
            tool_calls=[ToolCall(
                tool_name="web_search",
                tool_call_id=f"call-{self.context.run_id}-{index}",
                source_mode="web",
                args={"query": f"q{index}"},
                result={"ok": True},
            )],
            evidence=[],
            metadata=ExecutionMetadata(worker_type="fake", tool_calls_count=1),
        )
        self.records.append(record)
        return record


class TestRecoveryMapping:
    def test_legacy_executing_checkpoint_still_resumes_at_reporting(self):
        context = make_context(status=RunStatus.EXECUTING)
        assert HarnessRuntime._safe_stage_after(RunStatus.EXECUTING) is RunStatus.REPORTING
        assert HarnessRuntime._coreact_continuation_pending(context) is False

    def test_unfinished_coreact_checkpoint_resumes_at_executing(self):
        context = make_context(status=RunStatus.EXECUTING)
        context.workflow_state = {
            "coreact": CoreActLoopState(step_index=2, decisions_used=3).model_dump(mode="json")
        }
        assert HarnessRuntime._coreact_continuation_pending(context) is True

    def test_finished_coreact_checkpoint_resumes_at_reporting(self):
        context = make_context(status=RunStatus.EXECUTING)
        context.workflow_state = {
            "coreact": CoreActLoopState(done=True, step_index=2).model_dump(mode="json")
        }
        assert HarnessRuntime._coreact_continuation_pending(context) is False

    def test_malformed_coreact_marker_is_treated_as_legacy(self):
        context = make_context(status=RunStatus.EXECUTING)
        context.workflow_state = {"coreact": {"engine": "coreact_v1", "step_index": "not-a-number"}}
        assert HarnessRuntime._coreact_continuation_pending(context) is False


class TestStepCommit:
    async def test_step_commit_writes_checkpoint_and_is_idempotent(self, database, tmp_path):
        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda context, events: None)
        context = make_context(run_id=run_id)
        driver = StepDriver(context)
        driver.add_action(1)
        budget = BudgetManager(context.budget_limits, context.budget_usage)

        await runtime._commit_coreact_step(context, driver, budget)

        checkpoint = await CheckpointRepository(database).latest(context.run_id)
        assert checkpoint is not None
        assert checkpoint.stage == "executing"

        # 幂等：重复提交同一动作不得产生重复 tool_call 行
        await runtime._commit_coreact_step(context, driver, budget)
        stored = await PlanTaskToolRepository(database).get_tool_call(
            f"call-{context.run_id}-1"
        )
        assert stored is not None
        assert stored.status == "completed"

    async def test_step_commit_updates_budget_usage_on_context(self, database, tmp_path):
        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda context, events: None)
        context = make_context(run_id=run_id)
        driver = StepDriver(context)
        driver.add_action(1)
        budget = BudgetManager(context.budget_limits, context.budget_usage)
        budget.consume_tool(tavily=True)
        await runtime._commit_coreact_step(context, driver, budget)
        assert context.budget_usage.tool_calls == budget.usage.tool_calls
        assert context.budget_usage.tavily_calls == budget.usage.tavily_calls

    async def test_step_commit_snapshot_carries_loop_cursor(self, database, tmp_path):
        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda context, events: None)
        context = make_context(run_id=run_id)
        driver = StepDriver(context)
        driver.add_action(1)
        driver.coreact_loop.step_index = 1
        driver.coreact_loop.decisions_used = 2
        budget = BudgetManager(context.budget_limits, context.budget_usage)
        await runtime._commit_coreact_step(context, driver, budget)
        assert context.workflow_state["coreact"]["step_index"] == 1
        assert context.workflow_state["coreact"]["decisions_used"] == 2
        assert context.workflow_state["orchestration"] == "coreact_v1"


class TestPauseAtActionBoundary:
    async def test_paused_coreact_run_resumes_at_executing(self, database, tmp_path):
        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda context, events: None)
        context = make_context(run_id=run_id, status=RunStatus.EXECUTING)
        driver = StepDriver(context)
        driver.add_action(1)
        driver.coreact_loop.step_index = 1
        budget = BudgetManager(context.budget_limits, context.budget_usage)
        await runtime._commit_coreact_step(context, driver, budget)

        context.resume_from_status = RunStatus.EXECUTING
        await runtime._transition(context, RunStatus.PAUSED, event_type="run.paused")
        await runtime.checkpoints.save(context, "paused")

        restored = await CheckpointManager(CheckpointRepository(database)).restore(run_id)
        assert restored is not None
        # 步级暂停的恢复目标就是 EXECUTING（循环从下一个动作续跑），
        # 而不是 legacy 的 REPORTING 跳转
        assert restored.resume_from_status is RunStatus.EXECUTING
        assert HarnessRuntime._coreact_continuation_pending(restored) is True


async def count_all_events(database, run_id: str) -> dict[str, int]:
    """分页取全量事件并按类型计数（不能用默认 limit=500 的窗口断言）。"""
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


class TestDedupAndBudget:
    async def test_duplicate_commit_beyond_500_events_publishes_no_duplicates(self, database, tmp_path):
        """>1000 条事件跨页后重复提交：task/tool 事件必须各恰好 1 条（空洞 0==0 不再可能）。"""
        from backend.app.schemas import RunEventCreate

        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda context, events: None)
        context = make_context(run_id=run_id)
        driver = StepDriver(context)
        driver.add_action(1)
        budget = BudgetManager(context.budget_limits, context.budget_usage)
        # 灌 1100 条无关事件：既越过默认 500 窗口，也覆盖 1000 翻页边界
        for index in range(1100):
            await EventRepository(database).append(
                RunEventCreate(run_id=run_id, event_type="coreact.progress", stage="executing", payload={"index": index}),
            )
        await runtime._commit_coreact_step(context, driver, budget)
        await runtime._commit_coreact_step(context, driver, budget)  # 重复提交
        counts = await count_all_events(database, run_id)
        assert counts.get("task.completed") == 1
        assert counts.get("tool.completed") == 1
        assert counts.get("coreact.progress") == 1100

    async def test_failed_tool_call_committed_twice_charged_once(self, database, tmp_path):
        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda context, events: None)
        context = make_context(run_id=run_id)
        driver = StepDriver(context)
        record = driver.add_action(1)
        record.tool_calls[0].status = "failed"
        record.tool_calls[0].result = None
        budget = BudgetManager(context.budget_limits, context.budget_usage)
        await runtime._commit_coreact_step(context, driver, budget)
        assert budget.usage.tool_calls == 1
        assert budget.usage.tavily_calls == 1  # web 来源计费一次
        await runtime._commit_coreact_step(context, driver, budget)
        assert budget.usage.tool_calls == 1  # failed 行重复提交不重复计费
        assert budget.usage.tavily_calls == 1

    async def test_evidence_rows_survive_duplicate_commit(self, database, tmp_path):
        from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult

        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda context, events: None)
        context = make_context(run_id=run_id)
        driver = StepDriver(context)
        record = driver.add_action(1)
        result = RetrievalResult(
            granularity="Chunk",
            evidence="检索增强生成是一种技术路线",
            metadata=RetrievalMetadata(source_id="https://example.com/a", source_type="webpage"),
            source="tavily_search",
            source_mode="web",
        )
        record.evidence.append(result)
        driver._evidence_results = [
            (record.task_id, record.tool_calls[0].tool_call_id, "tavily", result)
        ]

        def evidence_results():
            return driver._evidence_results

        driver.evidence_results = evidence_results
        budget = BudgetManager(context.budget_limits, context.budget_usage)
        await runtime._commit_coreact_step(context, driver, budget)
        await runtime._commit_coreact_step(context, driver, budget)
        stored = await EvidenceRepository(database).list_for_run(run_id)
        assert len(stored) == 1  # 幂等 upsert 不产生重复证据行


class TestPauseCancelArbitration:
    async def test_db_cancel_wins_over_pause(self, database, tmp_path):
        """同时请求 pause 与 cancel：仲裁结果必须是 CANCELLED。"""
        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda context, events: None)
        context = make_context(run_id=run_id, status=RunStatus.EXECUTING)
        driver = StepDriver(context)
        driver.add_action(1)
        driver.coreact_loop.step_index = 1
        budget = BudgetManager(context.budget_limits, context.budget_usage)
        await runtime._commit_coreact_step(context, driver, budget)

        await runtime.runs.update_status(run_id, status="pausing")
        await runtime.runs.request_cancel(run_id)

        # driver 步间探测到 pause（抛 CoreActPaused 的前置条件）
        probe = runtime._make_pause_probe(context)
        assert await probe() is True

        final = await runtime._resolve_coreact_pause(context)
        assert final is RunStatus.CANCELLED
        assert context.cancellation_requested is True
        run = await runtime.runs.get(run_id)
        assert run.status == "cancelled"
        assert bool(run.cancellation_requested) is True  # cancel 标志不得被清除

    async def test_pause_without_cancel_lands_paused_with_executing_resume(self, database, tmp_path):
        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda context, events: None)
        context = make_context(run_id=run_id, status=RunStatus.EXECUTING)
        # 从可暂停状态（EXECUTING，非 pausing）请求 pause；断言返回 True
        assert await runtime.runs.request_pause(run_id) is True
        before = await runtime.runs.get(run_id)
        assert bool(json.loads(before.config_snapshot_json or "{}").get("pause_requested")) is True  # 仲裁前标志为 true

        final = await runtime._resolve_coreact_pause(context)
        assert final is RunStatus.PAUSED
        assert context.resume_from_status is RunStatus.EXECUTING
        run = await runtime.runs.get(run_id)
        assert run.status == "paused"
        assert bool(run.cancellation_requested) is False
        assert bool(json.loads(run.config_snapshot_json or "{}").get("pause_requested")) is False  # true→false 已清除

    async def test_load_context_maps_coreact_interrupted_checkpoint(self, database, tmp_path):
        """真实 _load_context：coreact EXECUTING checkpoint 恢复到 EXECUTING；legacy 恢复到 REPORTING。"""
        run_id = await create_run(database)
        runtime = build_runtime(database, tmp_path, lambda context, events: None)
        context = make_context(run_id=run_id, status=RunStatus.EXECUTING)
        context.workflow_state = {
            "coreact": CoreActLoopState(step_index=2, decisions_used=2).model_dump(mode="json")
        }
        await runtime.checkpoints.save(context, "executing")
        await runtime.runs.update_status(run_id, status="interrupted")

        loaded = await runtime._load_context(run_id)
        assert loaded.resume_from_status is RunStatus.EXECUTING

        # legacy：同 Run 换成无 marker 的 EXECUTING checkpoint
        legacy_context = make_context(run_id=run_id, status=RunStatus.EXECUTING)
        legacy_context.workflow_state = {}
        await runtime.checkpoints.save(legacy_context, "executing")
        await runtime.runs.update_status(run_id, status="interrupted")
        loaded_legacy = await runtime._load_context(run_id)
        assert loaded_legacy.resume_from_status is RunStatus.REPORTING
