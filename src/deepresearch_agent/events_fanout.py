"""Run 事件的跨实例扇出（P0-4）：Redis pub/sub，channel 按 run_id。

事件仍然是 durable-first（先落 run_events 表再扇出，见 harness/event_bus.py），
Redis 只承担「把已落库事件实时推给任意 API 实例」的通知职责；断线重放
始终以数据库为准，因此 Redis 抖动只损失实时性、不损失事件。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Protocol


def channel_for_run(run_id: str) -> str:
    return f"dra:events:{run_id}"


class EventFanout(Protocol):
    async def publish(self, run_id: str, event: dict[str, Any]) -> None: ...

    async def subscribe(self, run_id: str) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None: ...


class RedisEventFanout:
    def __init__(self, client):
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> "RedisEventFanout":
        import redis.asyncio as redis

        return cls(redis.from_url(url, decode_responses=True))

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        await self._client.publish(channel_for_run(run_id), json.dumps(event, ensure_ascii=False))

    async def subscribe(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        """await 即完成 SUBSCRIBE 订阅，随后的 DB 重放不会漏事件。"""
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(channel_for_run(run_id))

        async def _iter() -> AsyncIterator[dict[str, Any]]:
            try:
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    yield json.loads(message["data"])
            finally:
                await pubsub.aclose()

        return _iter()

    async def close(self) -> None:
        await self._client.aclose()
