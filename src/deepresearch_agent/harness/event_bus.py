"""Durable-first event publication; phase 4 can subscribe for SSE."""

from __future__ import annotations

import inspect
import json
from collections import defaultdict
from typing import Any, Awaitable, Callable, DefaultDict

from backend.app.schemas import RunEventCreate
from deepresearch_agent.persistence.repositories import EventRepository

Subscriber = Callable[[Any], Awaitable[None] | None]


class EventBus:
    def __init__(self, repository: EventRepository, fanout=None):
        self.repository = repository
        # 跨实例扇出（P0-4）：worker/API 侧发布后推 Redis，任意 API 实例可订阅；
        # None 时仅进程内通知（单机兼容模式）
        self.fanout = fanout
        self._subscribers: DefaultDict[str, list[Subscriber]] = defaultdict(list)

    def subscribe(self, run_id: str, callback: Subscriber) -> Callable[[], None]:
        self._subscribers[run_id].append(callback)
        return lambda: self._subscribers[run_id].remove(callback)

    async def publish(self, run_id: str, event_type: str, *, stage: str | None = None, payload: dict[str, Any] | None = None):
        event = await self.repository.append(RunEventCreate(run_id=run_id, event_type=event_type, stage=stage, payload=payload or {}))
        if self.fanout is not None:
            # 评审 H2：扇出是 best-effort——durable 落库已完成，Redis 故障绝不能
            # 传播进状态机（否则 Run 被误标 FAILED）。SSE 消费方有 DB 重放兜底。
            try:
                await self.fanout.publish(run_id, {
                    "event_id": event.event_id,
                    "run_id": event.run_id,
                    "event_type": event.event_type,
                    "stage": event.stage,
                    "created_at": event.created_at,
                    "payload": json.loads(event.payload_json or "{}"),
                })
            except Exception:
                try:
                    from deepresearch_agent.observability import EVENT_FANOUT_FAILURES

                    EVENT_FANOUT_FAILURES.inc()
                except Exception:
                    pass
        for callback in tuple(self._subscribers.get(run_id, ())):
            outcome = callback(event)
            if inspect.isawaitable(outcome):
                await outcome
        return event

