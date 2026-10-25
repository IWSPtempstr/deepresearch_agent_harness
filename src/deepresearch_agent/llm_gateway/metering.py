"""llm_calls 计量：内存缓冲 + 后台批量落库，绝不阻塞模型调用主路径。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import insert

from deepresearch_agent.persistence.models import LlmCallModel


class LlmCallRecorder:
    """进程内计量缓冲；start() 后由后台任务每秒批量写入 llm_calls。"""

    def __init__(self, max_buffer: int = 10000):
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_buffer)
        self._flush_task: Optional[asyncio.Task] = None
        self._database = None

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def record(self, *, provider: str, model: str, run_id: Optional[str], usage: dict[str, int], latency_ms: int, status: str, error_code: Optional[str], tenant_id: str = "default") -> None:
        item = {
            "tenant_id": tenant_id, "run_id": run_id, "provider": provider, "model": model,
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "latency_ms": int(latency_ms or 0), "status": status, "error_code": error_code,
            "created_at": self._now_iso(),
        }
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # 丢弃最旧的一条再入队：计量完整性让位于模型调用主路径
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except Exception:
                pass

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.flush()
            except Exception:
                # DB 抖动时留到下一轮；队列有界，极端情况按溢出策略丢旧
                continue

    async def flush(self) -> int:
        if self._database is None or self._queue.empty():
            return 0
        batch: list[dict[str, Any]] = []
        while len(batch) < 200 and not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if not batch:
            return 0
        async with self._database.transaction() as session:
            await session.execute(insert(LlmCallModel).values(batch))
        return len(batch)

    def start(self, database) -> None:
        if self._flush_task is not None:
            return
        self._database = database
        self._flush_task = asyncio.create_task(self._flush_loop(), name="llm-calls-recorder")

    async def stop(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            await asyncio.gather(self._flush_task, return_exceptions=True)
            self._flush_task = None
        try:
            await self.flush()
        except Exception:
            pass


_recorder: Optional[LlmCallRecorder] = None


def get_recorder() -> Optional[LlmCallRecorder]:
    """未 start 时返回 None，调用方据此跳过计量（本地未接线则零开销）。"""
    return _recorder


def start_metering(database) -> LlmCallRecorder:
    """在 API/worker 进程的 lifespan 中调用；幂等。"""
    global _recorder
    if _recorder is None:
        _recorder = LlmCallRecorder()
    _recorder.start(database)
    return _recorder


async def stop_metering() -> None:
    global _recorder
    if _recorder is not None:
        await _recorder.stop()
        _recorder = None
