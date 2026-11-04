"""T-003：依赖故障与队列重建——故障注入测试。

覆盖：
1. enqueue 失败可观测（QUEUE_ENQUEUE_FAILURES 计数，run 留在 queued）；
2. enqueue 有界重试后成功（RedisStreamRunQueue 层）；
3. worker 消费流断连自愈（重建生成器、退避后继续消费并完成 Run）；
4. DB 连接故障映射为 503 可重试（DATABASE_UNAVAILABLE）。
"""

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.exc import OperationalError

import deepresearch_agent.queueing as queueing_module
from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from backend.app.services.run_service import RunService
from deepresearch_agent.harness import RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.harness.budgets import BudgetLimits
from deepresearch_agent.observability import QUEUE_ENQUEUE_FAILURES
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository
from deepresearch_agent.queueing import (
    ENQUEUE_MAX_ATTEMPTS,
    InProcessRunQueue,
    QueueUnavailableError,
    RedisStreamRunQueue,
)

from fake_driver import CompletedFakeDriver


class FlakyQueue:
    """模拟真实实现的重试行为：enqueue 内部用 _enqueue_with_retry 包装底层操作。

    - fail_times < ENQUEUE_MAX_ATTEMPTS：重试后成功（对应 Redis/Kafka 层重试语义）
    - fail_times >= ENQUEUE_MAX_ATTEMPTS：重试耗尽抛 QueueUnavailableError
    """

    def __init__(self, fail_times: int, error: Exception | None = None):
        self.fail_times = fail_times
        self.error = error or ConnectionError("redis down")
        self.attempts = 0
        self.enqueued: list[str] = []

    async def _deliver(self) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.error

    async def enqueue(self, run_id: str) -> None:
        from deepresearch_agent.queueing import _enqueue_with_retry

        async def fast_sleep(delay, *_args, **_kwargs):
            return None


        async def _run():
            # 用瞬时 sleep 替换重试退避，加速测试且不改动实现
            global_sleep = queueing_module.asyncio.sleep
            queueing_module.asyncio.sleep = fast_sleep
            try:
                await _enqueue_with_retry(self._deliver)
            finally:
                queueing_module.asyncio.sleep = global_sleep

        await _run()
        self.enqueued.append(run_id)

    async def close(self) -> None:
        return None


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'resilience.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


async def create_queued_run(database):
    session = await SessionRepository(database).create(SessionCreate(title="resilience"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="resilience", client_message_id=f"client-{session.session_id}"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
            config_snapshot={"min_evidence": 1}, budget=BudgetLimits().model_dump(),
        ),
    )
    return run


async def _wait_for(condition, timeout: float = 5.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


async def test_enqueue_failure_is_observable_and_run_stays_queued(database, tmp_path):
    run = await create_queued_run(database)
    queue = FlakyQueue(fail_times=ENQUEUE_MAX_ATTEMPTS)  # 全部重试耗尽
    service = RunService(database, workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events), artifact_root=tmp_path / "artifacts", queue=queue)

    before = QUEUE_ENQUEUE_FAILURES._value.get()
    task = service.schedule(run.run_id)
    with pytest.raises(QueueUnavailableError):
        await asyncio.wait_for(task, timeout=10.0)  # 重试耗尽后任务以异常结束
    assert task.exception() is not None
    assert isinstance(task.exception(), QueueUnavailableError)

    after = QUEUE_ENQUEUE_FAILURES._value.get()
    assert after == before + 1, "enqueue 重试耗尽必须递增失败计数"
    assert queue.attempts == ENQUEUE_MAX_ATTEMPTS, "应有界重试至上限"
    persisted = await RunRepository(database).get(run.run_id)
    assert persisted.status == "queued", "信号未投递时 run 必须留在 queued（扫描兜底）"


async def test_enqueue_retry_then_success(database, tmp_path):
    run = await create_queued_run(database)
    queue = FlakyQueue(fail_times=2)  # 前 2 次失败，第 3 次成功
    service = RunService(database, workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events), artifact_root=tmp_path / "artifacts", queue=queue)

    task = service.schedule(run.run_id)
    await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
    assert task.exception() is None
    assert queue.enqueued == [run.run_id]


async def test_redis_enqueue_wraps_bounded_retry(monkeypatch):
    """RedisStreamRunQueue.enqueue 底层 xadd 持续失败 → 有界重试后抛 QueueUnavailableError。"""

    class ExplodingClient:
        def __init__(self):
            self.calls = 0

        async def xadd(self, *args, **kwargs):
            self.calls += 1
            raise ConnectionError("xadd down")

    # 跳过真实退避等待（queueing 模块的 asyncio.sleep）
    sleeps: list[float] = []

    async def fake_sleep(delay, *_args, **_kwargs):
        sleeps.append(delay)

    import deepresearch_agent.queueing as queueing

    monkeypatch.setattr(queueing.asyncio, "sleep", fake_sleep)
    queue = RedisStreamRunQueue(ExplodingClient())
    with pytest.raises(QueueUnavailableError):
        await queue.enqueue("run-x")
    assert queue._client.calls == ENQUEUE_MAX_ATTEMPTS
    assert len(sleeps) == ENQUEUE_MAX_ATTEMPTS - 1
    assert sleeps == [0.2, 0.4], "退避应按指数递增（0.2→0.4s）"


async def test_worker_consume_reconnects_after_outage(database, tmp_path, monkeypatch):
    """消费流第一轮抛连接错误 → worker 退避重建生成器 → 第二轮消费并完成 Run。

    断连退避 1s（真实时序）；工厂注入 fake queue 避免连真实 Redis。
    """
    run = await create_queued_run(database)

    class ReconnectingQueue:
        """第一轮 consume 立即抛连接错误；重建后的第二轮交付 run_id 后挂起。"""

        def __init__(self, run_id: str):
            self.run_id = run_id
            self.generation = 0
            self.opened = 0

        def consume(self):
            self.opened += 1
            self.generation += 1
            return self._gen(self.generation)

        async def _gen(self, generation):
            if generation == 1:
                raise ConnectionError("xreadgroup down")
            yield self.run_id
            await asyncio.Event().wait()  # 交付一次后挂起，模拟正常空闲消费

        async def close(self) -> None:
            return None

    queue = ReconnectingQueue(run.run_id)
    import deepresearch_agent.worker as worker_mod
    from deepresearch_agent.config import settings as worker_settings

    # _consume 要求平台模式环境变量；并用工厂注入 fake queue,
    # 避免连真实 Redis（settings 指向的 redis://... 会被 create_run_queue 实例化）
    monkeypatch.setattr(worker_mod.settings, "REDIS_URL", "redis://pytest-placeholder")
    monkeypatch.setattr(worker_settings, "REDIS_URL", "redis://pytest-placeholder")
    # 隔离 _consume 自建的 Database/create_schema/metering——
    # 不指向真实 ./data/app.db（评审发现：默认路径会被执行 DDL/建 FTS）
    monkeypatch.setattr(worker_settings, "APP_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'worker.db').as_posix()}")
    monkeypatch.setattr(worker_mod, "create_run_queue", lambda *args, **kwargs: queue)
    # 扫描间隔压到 1s 加速 WAITING_RUNS 轮询
    monkeypatch.setattr(worker_settings, "RECOVERY_SCAN_SECONDS", 1.0)

    service = RunService(database, workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events), artifact_root=tmp_path / "artifacts")
    service.queue = queue  # 强制队列模式（schedule 只投递，消费由 _consume 模拟）
    # _consume 内部自建 RunService（用 settings 的默认 DB）；重定向到测试 service,
    # 保证 execute 落在 tmp 数据库的 run 上
    monkeypatch.setattr(worker_mod, "RunService", lambda *args, **kwargs: service)

    consume_task = asyncio.create_task(worker_mod._consume("pytest-worker"))
    try:
        # 断连后必须重建生成器（opened>=2）而不是退出进程
        await _wait_for(lambda: queue.opened >= 2, timeout=10.0)
        # 第二轮生成器交付 run_id → execute_safely 完成整个 Run
        async with asyncio.timeout(20):
            while True:
                persisted = await RunRepository(database).get(run.run_id)
                if persisted.status == "completed":
                    break
                await asyncio.sleep(0.05)
    finally:
        consume_task.cancel()
        await asyncio.gather(consume_task, return_exceptions=True)


async def test_db_outage_maps_to_503():
    from backend.app.main import _map_unexpected_status

    exc = OperationalError("stmt", {}, ConnectionError("db down"))
    status, payload = _map_unexpected_status(exc)
    assert status == 503
    assert payload["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert payload["error"]["retryable"] is True

    status2, payload2 = _map_unexpected_status(ValueError("boom"))
    assert status2 == 500
    assert payload2["error"]["code"] == "INTERNAL_ERROR"
