"""SSE 跨实例扇出（P0-4）。

覆盖：
1. EventBus 携带 fanout 时：事件先落库、后扇出（durable-first 顺序不回归）；
2. EventStreamService 走 fanout 路径：先 DB 重放、后跨实例实时事件、
   cursor 去重、终态后结束；
3. RedisEventFanout 真实往返（TEST_REDIS_URL 门控）。
"""

import asyncio
import json
import os

import pytest
import pytest_asyncio

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from backend.app.services.event_stream import EventStreamService
from deepresearch_agent.events_fanout import RedisEventFanout
from deepresearch_agent.harness import SourceMode, WorkflowMode
from deepresearch_agent.harness.budgets import BudgetLimits
from deepresearch_agent.harness.event_bus import EventBus
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import EventRepository, RunRepository, SessionRepository

REDIS_URL = os.environ.get("TEST_REDIS_URL")


class FakeFanout:
    """进程内假扇出：publish 广播给所有订阅者；deliver 供测试注入跨实例事件。"""

    def __init__(self):
        self.published: list[dict] = []
        self._subscribers: list[asyncio.Queue] = []
        self._ready = asyncio.Event()

    async def publish(self, run_id: str, event: dict) -> None:
        self.published.append(event)
        await self.deliver(event)

    async def deliver(self, event: dict) -> None:
        for queue in tuple(self._subscribers):
            queue.put_nowait(event)

    async def wait_subscribed(self) -> None:
        await self._ready.wait()

    async def subscribe(self, run_id: str):
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        self._ready.set()

        async def _iter():
            while True:
                yield await queue.get()

        return _iter()

    async def close(self) -> None:
        return None


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'sse.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


async def _create_active_run(database):
    session = await SessionRepository(database).create(SessionCreate(title="sse"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="sse", client_message_id=f"client-{session.session_id}"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
            config_snapshot={"min_evidence": 1}, budget=BudgetLimits().model_dump(),
        ),
    )
    return run


async def test_event_bus_publishes_durable_first_then_fanout(database):
    fanout = FakeFanout()
    bus = EventBus(EventRepository(database), fanout=fanout)
    run = await _create_active_run(database)
    event = await bus.publish(run.run_id, "run.started", stage="context_building", payload={"status": "context_building"})
    persisted = await EventRepository(database).list_after(run.run_id)
    assert len(persisted) >= 2, "事件必须先落库（durable-first 不回归）"
    assert fanout.published, "落库后必须扇出"
    fanned = fanout.published[-1]
    assert fanned["event_id"] == event.event_id
    assert fanned["event_type"] == "run.started"
    assert fanned["payload"]["schema_version"] == persisted[-1].schema_version or isinstance(fanned["payload"].get("schema_version"), int)


class ExplodingFanout(FakeFanout):
    async def publish(self, run_id: str, event: dict) -> None:
        raise RuntimeError("redis down")


async def test_event_bus_survives_fanout_failure(database):
    """评审 H2：Redis 故障不得传播进状态机——事件已 durable 落库，扇出失败被吞并计数。"""
    from deepresearch_agent.observability import EVENT_FANOUT_FAILURES

    run = await _create_active_run(database)
    bus = EventBus(EventRepository(database), fanout=ExplodingFanout())
    notified: list = []
    bus.subscribe(run.run_id, lambda event: notified.append(event))
    before = EVENT_FANOUT_FAILURES._value.get()
    event = await bus.publish(run.run_id, "run.started", stage="context_building", payload={"status": "context_building"})
    assert event.event_id > 0, "事件必须已落库"
    assert notified, "扇出失败不影响进程内订阅者通知"
    assert EVENT_FANOUT_FAILURES._value.get() == before + 1, "扇出失败必须计数"


async def test_stream_replays_then_serves_cross_instance_events(database):
    fanout = FakeFanout()
    run = await _create_active_run(database)
    service = EventStreamService(EventRepository(database), RunRepository(database), EventBus(EventRepository(database)), fanout=fanout)
    stream = service.stream(run.run_id, after_event_id=0)
    first = await anext(stream)
    assert first["event"] == "run.queued", "先做 DB 断线重放"

    async def deliver_cross_instance_event(event: dict) -> None:
        # 生成器挂起在上一条 yield 时订阅尚未建立，等订阅就绪后再注入
        await asyncio.wait_for(fanout.wait_subscribed(), timeout=3.0)
        await asyncio.sleep(0.05)
        await fanout.deliver(event)

    deliverer = asyncio.create_task(deliver_cross_instance_event({"event_id": 99, "run_id": run.run_id, "event_type": "run.started", "stage": "context_building", "created_at": "2026-10-25T00:00:00Z", "payload": {"status": "context_building"}}))
    live = await asyncio.wait_for(anext(stream), timeout=3.0)
    await deliverer
    assert live["event"] == "run.started" and live["id"] == "99"
    payload = json.loads(live["data"])
    assert payload["event_id"] == 99 and payload["status"] == "context_building"

    # 终态事件送达后，流在该事件后的状态检查处结束
    await fanout.deliver({"event_id": 100, "run_id": run.run_id, "event_type": "run.failed", "stage": "failed", "created_at": "2026-10-25T00:00:01Z", "payload": {}})
    final = await asyncio.wait_for(anext(stream), timeout=3.0)
    assert final["event"] == "run.failed"
    await RunRepository(database).update_status(run.run_id, status="failed", error_code="X")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=3.0)


@pytest.mark.skipif(not REDIS_URL, reason="set TEST_REDIS_URL to verify the Redis pub/sub fanout")
async def test_redis_event_fanout_roundtrip():
    fanout = RedisEventFanout.from_url(REDIS_URL)
    live = await fanout.subscribe("run-fanout-test")
    pump_queue: asyncio.Queue = asyncio.Queue(maxsize=10)

    async def pump():
        async for event in live:
            pump_queue.put_nowait(event)

    task = asyncio.create_task(pump())
    try:
        await asyncio.sleep(0.2)  # 订阅生效
        await fanout.publish("run-fanout-test", {"event_id": 7, "event_type": "run.queued", "payload": {"k": "中文"}})
        event = await asyncio.wait_for(pump_queue.get(), timeout=5.0)
        assert event["event_id"] == 7 and event["payload"]["k"] == "中文"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fanout.close()
