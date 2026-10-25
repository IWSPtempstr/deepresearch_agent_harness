"""Run 队列抽象与 API/worker 执行解耦（P0-2）。

覆盖：
1. InProcessRunQueue 进程内往返；
2. RunService 队列模式：schedule 只投递唤醒信号、不创建本地执行任务，
   execute_now（worker 侧入口）完成整个 Run；
3. RedisStreamRunQueue（由 TEST_REDIS_URL 门控）：enqueue→consume→ACK 往返
   与 consumer group 幂等创建。
"""

import asyncio
import os

import pytest
import pytest_asyncio

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from backend.app.services.run_service import RunService
from deepresearch_agent.harness import RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.harness.budgets import BudgetLimits
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository
from deepresearch_agent.queueing import InProcessRunQueue, RedisStreamRunQueue

from fake_driver import CompletedFakeDriver

REDIS_URL = os.environ.get("TEST_REDIS_URL")


class RecordingQueue:
    def __init__(self):
        self.enqueued: list[str] = []

    async def enqueue(self, run_id: str) -> None:
        self.enqueued.append(run_id)

    async def close(self) -> None:
        return None


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'queue.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


async def create_queued_run(database):
    session = await SessionRepository(database).create(SessionCreate(title="queue"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="queue mode", client_message_id=f"client-{session.session_id}"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
            config_snapshot={"min_evidence": 1}, budget=BudgetLimits().model_dump(),
        ),
    )
    return run


async def test_in_process_queue_roundtrip():
    queue = InProcessRunQueue()
    await queue.enqueue("run-1")
    generator = queue.consume()
    assert await asyncio.wait_for(anext(generator), timeout=2.0) == "run-1"
    await queue.close()


async def test_queue_mode_schedule_only_signals_and_worker_completes(database, tmp_path):
    run = await create_queued_run(database)
    queue = RecordingQueue()
    service = RunService(database, workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events), artifact_root=tmp_path / "artifacts", queue=queue)
    # API 侧：schedule 只投递信号，不产生本地执行任务
    task = service.schedule(run.run_id)
    await asyncio.wait_for(task, timeout=5.0)
    assert queue.enqueued == [run.run_id]
    assert not service._tasks, "队列模式不应创建本地执行任务"
    # worker 侧：execute_now 消费信号后完成 Run
    context = await service.execute_now(run.run_id)
    assert context.status is RunStatus.COMPLETED
    persisted = await RunRepository(database).get(run.run_id)
    assert persisted.status == "completed"


async def test_local_mode_without_queue_keeps_inprocess_scheduling(database, tmp_path):
    run = await create_queued_run(database)
    service = RunService(database, workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events), artifact_root=tmp_path / "artifacts")
    task = service.schedule(run.run_id)
    await asyncio.wait_for(task, timeout=10.0)
    assert service.queue is None
    persisted = await RunRepository(database).get(run.run_id)
    assert persisted.status == "completed"


@pytest.mark.skipif(not REDIS_URL, reason="set TEST_REDIS_URL to verify the Redis Streams queue")
async def test_redis_stream_queue_roundtrip():
    queue = RedisStreamRunQueue.from_url(REDIS_URL, consumer_name="pytest-consumer")
    await queue.enqueue("run-redis-1")
    generator = queue.consume()
    assert await asyncio.wait_for(anext(generator), timeout=10.0) == "run-redis-1"
    # 组创建幂等：二次 consume 不抛 BUSYGROUP
    await queue.ensure_group()
    await queue.close()
