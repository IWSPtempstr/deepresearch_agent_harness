"""Run 执行队列抽象（P0-2）：进程内 asyncio 队列 与 Redis Streams 两种实现。

设计决策——**流只是唤醒信号，数据库才是事实源**：
- 消息读出即 ACK（at-most-once 投递）。Run 的排队/中断/恢复状态全部落在
  runs 表，崩溃恢复依赖启动恢复 + 租约过期扫描（P0-3b），而非流的 pending
  重投。这样避免「流重投 + 租约接管」两条恢复路径互相打架导致双执行。
- 消费语义为并发受限的拉取：worker 以 WORKER_CONCURRENCY 控制同时在跑的
  Run 数（对应原 API 进程内多 task 并发的行为）。
"""

from __future__ import annotations

import asyncio
import socket
from typing import AsyncIterator, Optional, Protocol

STREAM_KEY = "dra:runs:queue"
CONSUMER_GROUP = "dra-workers"


class RunQueue(Protocol):
    async def enqueue(self, run_id: str) -> None: ...

    def consume(self) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...


class InProcessRunQueue:
    """单机兼容模式：与旧版 asyncio.create_task 行为等价的本地队列。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def enqueue(self, run_id: str) -> None:
        self._queue.put_nowait(run_id)

    async def consume(self) -> AsyncIterator[str]:
        while True:
            yield await self._queue.get()

    async def close(self) -> None:
        return None


class RedisStreamRunQueue:
    """平台模式：Redis Streams + consumer group 的唤醒信号队列。"""

    def __init__(self, client, *, consumer_name: Optional[str] = None):
        self._client = client
        self.consumer_name = consumer_name or f"worker-{socket.gethostname()}"

    @classmethod
    def from_url(cls, url: str, *, consumer_name: Optional[str] = None) -> "RedisStreamRunQueue":
        import redis.asyncio as redis

        return cls(redis.from_url(url, decode_responses=True), consumer_name=consumer_name)

    async def ensure_group(self) -> None:
        try:
            await self._client.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP：组已存在
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, run_id: str) -> None:
        await self._client.xadd(STREAM_KEY, {"run_id": run_id})

    async def consume(self) -> AsyncIterator[str]:
        await self.ensure_group()
        while True:
            response = await self._client.xreadgroup(
                CONSUMER_GROUP, self.consumer_name, {STREAM_KEY: ">"}, count=1, block=5000,
            )
            if not response:
                continue
            for _stream, entries in response:
                for entry_id, fields in entries:
                    # 读出即 ACK：恢复路径以 runs 表状态 + 租约扫描为准
                    await self._client.xack(STREAM_KEY, CONSUMER_GROUP, entry_id)
                    run_id = fields.get("run_id")
                    if run_id:
                        yield run_id

    async def close(self) -> None:
        await self._client.aclose()


def create_run_queue(redis_url: str) -> RunQueue:
    """按配置选择队列实现；REDIS_URL 为空时保持单机进程内模式。"""
    if redis_url:
        return RedisStreamRunQueue.from_url(redis_url)
    return InProcessRunQueue()
