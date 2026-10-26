"""租约心跳续约与抢占保护（P0-3a）。

覆盖两个跨进程安全的基石语义：
1. 长阶段执行期间租约被持续续约，其他 Runtime 无法中途抢走；
2. 租约一旦被抢占，本地 Runtime 立即停止写入且不产生任何终态转换。
"""

import asyncio

import pytest
import pytest_asyncio

from fake_driver import CompletedFakeDriver

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionMetadata, ExecutionRecord, ToolCall
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.harness import RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.harness.budgets import BudgetLimits
from deepresearch_agent.observability import RUN_TOTAL
from deepresearch_agent.harness.runtime import HarnessRuntime
from deepresearch_agent.persistence import ArtifactStore, Database
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository,
    SessionRepository,
)


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'lease.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


async def create_queued_run(database):
    session = await SessionRepository(database).create(SessionCreate(title="lease"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="lease takeover", client_message_id=f"client-{session.session_id}"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
            config_snapshot={"min_evidence": 1}, budget=BudgetLimits().model_dump(),
        ),
    )
    return run


class SlowFakeDriver:
    """FakeDriver 变体：execute 可配置阻塞时长，并记录是否在阻塞中被取消。"""

    def __init__(self, context, *, execute_sleep: float = 0.0):
        self.context = context
        restored = context.workflow_state
        self.executed = bool(restored.get("executed"))
        self.execute_sleep = execute_sleep
        self.cancelled_during_execute = False
        self.results = [
            RetrievalResult(
                result_id=item_id, granularity="Chunk", evidence=f"evidence-{index}",
                metadata=RetrievalMetadata(
                    source_id=f"doc-{index}", source_type="chunk",
                    title=f"source-{index}", content_hash=character * 64,
                ),
                source="local_search", source_mode="graphrag", score=0.9,
            )
            for index, (item_id, character) in enumerate((("raw-1", "a"), ("raw-2", "b")), start=1)
        ]
        self._report = restored.get("report")

    async def plan(self, failures=None):
        return None

    async def execute(self):
        if not self.executed:
            self.executed = True
            try:
                await asyncio.sleep(self.execute_sleep)
            except asyncio.CancelledError:
                self.cancelled_during_execute = True
                raise

    async def report(self):
        self._report = f"# 研究报告\n\n结论内容已有充分说明 [{self.results[0].result_id}]\n\n## 方法\n\n基于证据。\n\n## 局限\n\n当前报告仅直接引用一个来源。"
        return self._report

    async def repair_report(self, failures):
        return False

    def snapshot(self):
        return {"executed": self.executed, "report": self._report, "result_ids": [item.result_id for item in self.results]}

    def execution_records(self):
        call = ToolCall(tool_name="local_search", tool_call_id=f"call-{self.context.run_id}-0", source_mode="graphrag", args={"query": "safe"}, result={"ok": True})
        return [ExecutionRecord(task_id=f"task-{self.context.run_id}", session_id=self.context.session_id, worker_type="fake", tool_calls=[call], evidence=self.results, metadata=ExecutionMetadata(worker_type="fake", tool_calls_count=1, evidence_count=len(self.results)))]

    def evidence_results(self):
        return [(f"task-{self.context.run_id}", f"call-{self.context.run_id}-0", "local_search", item) for item in self.results]

    def report_consistency(self):
        return True

    def plan_record(self):
        tasks = [{"task_id": f"task-{self.context.run_id}", "task_type": "deep_research", "source_mode": "graphrag", "status": "pending", "description": "research-0"}]
        return ({"plan_id": f"plan-{self.context.run_id}", "status": "executing", "tasks": tasks}, tasks)


def build_runtime(database, tmp_path, factory, *, lease_seconds: int):
    return HarnessRuntime(
        run_repository=RunRepository(database), message_repository=MessageRepository(database),
        event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
        evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
        trajectory_repository=PlanTaskToolRepository(database), workflow_factory=factory,
        artifact_store=ArtifactStore(tmp_path / "artifacts"), artifact_repository=ArtifactRepository(database),
        lease_seconds=lease_seconds,
    )


async def test_lease_renewal_keeps_lease_alive_during_long_stage(database, tmp_path):
    """阶段耗时超过 TTL 时，心跳续约必须保住租约：竞争者在阶段中无法抢走。"""
    run = await create_queued_run(database)
    driver_holder = {}

    def factory(context, events=None):
        driver = SlowFakeDriver(context, execute_sleep=4.4)
        driver_holder["driver"] = driver
        return driver

    # TTL=3s、续约间隔 1s：若续约失效，3.6s 时租约必已过期；负载导致心跳
    # 循环被饿 2s 以上才会引入抖动（TTL 越大越稳，检测窗口保持有效）
    runtime = build_runtime(database, tmp_path, factory, lease_seconds=3)
    task = asyncio.create_task(runtime.execute_run(run.run_id))
    await asyncio.sleep(3.6)
    stolen = await RunRepository(database).acquire_lease(run.run_id, "rival-runtime", ttl_seconds=30)
    assert not stolen, "长阶段执行中租约被抢走：心跳续约未生效"
    context = await task
    assert context.status is RunStatus.COMPLETED
    assert driver_holder["driver"].executed is True


async def test_lease_loss_stops_local_runtime_without_terminal_transition(database, tmp_path):
    """续约失败（被抢占）时：本地任务被取消、Run 不进入终态，交给接管者驱动。"""
    run = await create_queued_run(database)
    driver_holder = {}

    def factory(context, events=None):
        driver = SlowFakeDriver(context, execute_sleep=30.0)
        driver_holder["driver"] = driver
        return driver

    runs_repository = RunRepository(database)
    original_acquire = runs_repository.acquire_lease
    acquire_calls = {"count": 0}

    async def acquire_then_report_stolen(run_id, owner, *, ttl_seconds=60):
        acquire_calls["count"] += 1
        if acquire_calls["count"] > 1:
            # 首次为 execute_run 的初始获取；此后都是心跳续约，模拟已被抢占
            return False
        return await original_acquire(run_id, owner, ttl_seconds=ttl_seconds)

    runs_repository.acquire_lease = acquire_then_report_stolen
    runtime = build_runtime(database, tmp_path, factory, lease_seconds=2)
    runtime.runs = runs_repository
    context = await runtime.execute_run(run.run_id)

    assert acquire_calls["count"] >= 2, "心跳循环未尝试续约"
    assert driver_holder["driver"].cancelled_during_execute is True, "被抢占后本地阶段任务未被取消"
    assert context.status is RunStatus.EXECUTING, "被抢占的 Runtime 不应推进状态机"
    persisted = await RunRepository(database).get(run.run_id)
    assert persisted.status == "executing", "Run 应停留在非终态等待接管者"
    assert persisted.error_code is None and persisted.error_message is None

class SlowContextBuilder:
    """可配置阻塞的 context_builder；记录构建是否完成/被取消。"""

    def __init__(self, sleep_seconds: float):
        self.sleep_seconds = sleep_seconds
        self.built = False
        self.cancelled = False

    async def build(self, context):
        try:
            await asyncio.sleep(self.sleep_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.built = True
        return context


async def test_context_build_renews_lease_during_long_build(database, tmp_path):
    """评审遗留修复：context 构建超过 TTL 时租约必须保住（构建期心跳续约）。"""
    run = await create_queued_run(database)
    builder = SlowContextBuilder(4.4)

    def factory(context, events=None):
        return CompletedFakeDriver(context, events=events)

    runtime = build_runtime(database, tmp_path, factory, lease_seconds=3)
    runtime.context_builder = builder
    task = asyncio.create_task(runtime.execute_run(run.run_id))
    await asyncio.sleep(3.6)
    stolen = await RunRepository(database).acquire_lease(run.run_id, "rival-runtime", ttl_seconds=30)
    assert not stolen, "context 构建超过 TTL 时租约被抢走：构建期心跳续约未生效"
    context = await task
    assert context.status is RunStatus.COMPLETED
    assert builder.built is True


async def test_lease_loss_during_context_build_cancels_build_and_skips_run_counter(database, tmp_path):
    """评审遗留修复：构建期被抢占 → 构建任务被取消、Run 停留非终态、非终态不计入 dra_runs_total。"""
    run = await create_queued_run(database)
    builder = SlowContextBuilder(30.0)

    def factory(context, events=None):
        return CompletedFakeDriver(context, events=events)

    runs_repository = RunRepository(database)
    original_acquire = runs_repository.acquire_lease
    acquire_calls = {"count": 0}

    async def acquire_then_report_stolen(run_id, owner, *, ttl_seconds=60):
        acquire_calls["count"] += 1
        if acquire_calls["count"] > 1:
            return False
        return await original_acquire(run_id, owner, ttl_seconds=ttl_seconds)

    runs_repository.acquire_lease = acquire_then_report_stolen
    runtime = build_runtime(database, tmp_path, factory, lease_seconds=2)
    runtime.context_builder = builder
    runtime.runs = runs_repository

    before = RUN_TOTAL.labels(status="context_building")._value.get()
    context = await runtime.execute_run(run.run_id)
    assert acquire_calls["count"] >= 2, "构建期未尝试续约"
    assert builder.cancelled is True, "被抢占后构建任务未被取消"
    assert context.status is RunStatus.CONTEXT_BUILDING
    persisted = await RunRepository(database).get(run.run_id)
    assert persisted.status == "context_building", "Run 应停留在非终态等待接管者"
    assert RUN_TOTAL.labels(status="context_building")._value.get() == before, "非终态不得计入 dra_runs_total"
