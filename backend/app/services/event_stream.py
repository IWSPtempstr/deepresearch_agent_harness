"""Durable replay plus live notification for SSE clients.

P0-4：配置 EventFanout（Redis pub/sub）时，实时事件来自跨实例订阅而非进程内
EventBus——任何 API 实例都能看到任意 Run 的事件流；断线重放始终以数据库为准。
"""

import asyncio
import json
from collections.abc import AsyncIterator

from deepresearch_agent.harness.event_bus import EventBus
from deepresearch_agent.persistence.repositories import EventRepository, RunRepository


TERMINAL_STATUSES = {"completed", "failed", "budget_exhausted", "cancelled", "paused"}


class EventStreamService:
    def __init__(self, events: EventRepository, runs: RunRepository, event_bus: EventBus, fanout=None, *, heartbeat_seconds: float = 20.0):
        self.events = events
        self.runs = runs
        self.event_bus = event_bus
        self.fanout = fanout
        self.heartbeat_seconds = heartbeat_seconds

    @staticmethod
    def serialize(event) -> dict[str, str]:
        payload = json.loads(event.payload_json or "{}")
        payload.update({"event_id": event.event_id, "run_id": event.run_id, "event_type": event.event_type, "stage": event.stage, "created_at": event.created_at})
        return {"id": str(event.event_id), "event": event.event_type, "data": json.dumps(payload, ensure_ascii=False)}

    @staticmethod
    def serialize_fanout(event: dict) -> dict[str, str]:
        payload = dict(event.get("payload") or {})
        payload.update({key: event.get(key) for key in ("event_id", "run_id", "event_type", "stage", "created_at")})
        return {"id": str(event.get("event_id")), "event": str(event.get("event_type")), "data": json.dumps(payload, ensure_ascii=False)}

    async def _replay(self, run_id: str, cursor: int, *, page_size: int = 500) -> AsyncIterator:
        """按游标翻页直到数据库无更多事件（list_after 单次上限 1000）。

        BC-03：终态 Run 可能积累 >1000 事件；只读一页会让断线重连/首次加载
        静默丢事件。翻页以 event_id 严格递增，天然去重。
        """
        while True:
            batch = await self.events.list_after(
                run_id, after_event_id=cursor, limit=page_size,
            )
            if not batch:
                return
            for event in batch:
                cursor = event.event_id
                yield event
            if len(batch) < page_size:
                return

    async def stream(self, run_id: str, *, after_event_id: int = 0) -> AsyncIterator[dict[str, str]]:
        cursor = max(0, after_event_id)
        async for event in self._replay(run_id, cursor):
            cursor = event.event_id
            yield self.serialize(event)
        run = await self.runs.get(run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return
        if self.fanout is not None:
            async for item in self._stream_via_fanout(run_id, cursor):
                yield item
            return
        async for item in self._stream_in_process(run_id, cursor):
            yield item

    async def _stream_via_fanout(self, run_id: str, cursor: int) -> AsyncIterator[dict[str, str]]:
        # await subscribe 完成 Redis SUBSCRIBE 后再做一次 DB 补读，
        # 覆盖「首次重放结束 → 订阅生效」之间落库的事件；重复由 cursor 去重
        live = await self.fanout.subscribe(run_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        async def pump() -> None:
            try:
                async for event in live:
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass
            except asyncio.CancelledError:
                raise

        pump_task = asyncio.create_task(pump())
        try:
            async for event in self._replay(run_id, cursor):
                cursor = event.event_id
                yield self.serialize(event)
            # 终态状态可能先于终态事件落库（_transition 先写状态、再 PUBLISH）。
            # 心跳看到终态但本轮补读为空时，再等一个心跳周期，避免在两次提交
            # 之间关闭流而丢掉终态事件。
            terminal_empty = 0
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=self.heartbeat_seconds)
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": json.dumps({"run_id": run_id, "after_event_id": cursor})}
                    # 评审遗留修复：Redis 扇出是 at-most-once，终态事件可能丢失——
                    # 心跳周期顺带查库，终态则做最后一次 DB 补读后结束，流不再悬挂。
                    # BC-03：补读必须翻页（终态 Run 可能积压 >1000 条事件）。
                    drained = 0
                    async for event in self._replay(run_id, cursor):
                        drained += 1
                        cursor = event.event_id
                        yield self.serialize(event)
                    refreshed = await self.runs.get(run_id)
                    if refreshed is None:
                        return
                    if refreshed.status in TERMINAL_STATUSES:
                        if drained:
                            return
                        terminal_empty += 1
                        if terminal_empty >= 2:
                            return
                        continue
                    terminal_empty = 0
                    continue
                if int(event.get("event_id") or 0) <= cursor:
                    continue
                cursor = int(event["event_id"])
                yield self.serialize_fanout(event)
                refreshed = await self.runs.get(run_id)
                if refreshed is None or refreshed.status in TERMINAL_STATUSES:
                    return
        finally:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)

    async def _stream_in_process(self, run_id: str, cursor: int) -> AsyncIterator[dict[str, str]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)

        def on_event(event):
            if event.event_id > cursor:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

        unsubscribe = self.event_bus.subscribe(run_id, on_event)
        try:
            async for event in self._replay(run_id, cursor):
                cursor = event.event_id
                yield self.serialize(event)
            terminal_empty = 0
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=self.heartbeat_seconds)
                    if event.event_id <= cursor:
                        continue
                    cursor = event.event_id
                    yield self.serialize(event)
                    refreshed = await self.runs.get(run_id)
                    if refreshed is None or refreshed.status in TERMINAL_STATUSES:
                        return
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": json.dumps({"run_id": run_id, "after_event_id": cursor})}
                    # BC-03：无 fanout 时实时通知只在本进程内有效（重启/丢通知
                    # 会静默漏事件）。心跳周期用数据库补齐并正常结束终态流。
                    drained = 0
                    async for event in self._replay(run_id, cursor):
                        drained += 1
                        cursor = event.event_id
                        yield self.serialize(event)
                    refreshed = await self.runs.get(run_id)
                    if refreshed is None:
                        return
                    if refreshed.status in TERMINAL_STATUSES:
                        if drained:
                            return
                        # 状态先于终态事件落库：再等一个心跳周期，避免提前关闭
                        terminal_empty += 1
                        if terminal_empty >= 2:
                            return
                        continue
                    terminal_empty = 0
        finally:
            unsubscribe()
