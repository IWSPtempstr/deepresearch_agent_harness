"""独立 Run 执行 worker 进程（P0-2）。

用法：REDIS_URL=redis://... python -m deepresearch_agent.worker

关停语义（对应 P0-3b 的接管设计）：
- SIGTERM/SIGINT 只停止拉取新 Run，给在跑 Run 一个宽限窗口自然结束；
- 宽限期内未结束的 Run 不做取消也不写终态，进程直接退出——等价于一次
  崩溃，由租约过期扫描 + 其他 worker 接管完成（避免「优雅关停被误标
  CANCELLED、无法接管」的语义冲突）。
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket

from backend.app.services import RunService
from deepresearch_agent.config import settings
from deepresearch_agent.persistence import Database
from deepresearch_agent.queueing import RedisStreamRunQueue

SHUTDOWN_GRACE_SECONDS = 30.0


async def _scan_loop(service: RunService, interval: float) -> None:
    """P0-3b：周期扫描过期租约并触发接管投递。"""
    while True:
        await asyncio.sleep(interval)
        try:
            await service.recover_expired()
        except Exception:
            # 单轮扫描失败（如 DB 抖动）不终止循环；下一轮重试
            continue


async def _consume(worker_name: str | None = None) -> None:
    if not settings.REDIS_URL:
        raise SystemExit("worker 进程需要设置 REDIS_URL（平台模式）")
    database = Database(settings.APP_DATABASE_URL)
    await database.create_schema()
    queue = RedisStreamRunQueue.from_url(
        settings.REDIS_URL,
        consumer_name=worker_name or os.environ.get("WORKER_NAME") or f"worker-{socket.gethostname()}",
    )
    service = RunService(database)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def run_one(run_id: str) -> None:
        try:
            await service.execute_now(run_id)
        except Exception:
            # execute_run 内部已把失败落库；此处吞掉避免单个 Run 拖垮消费循环
            return

    try:
    semaphore = asyncio.Semaphore(max(1, settings.WORKER_CONCURRENCY))
    inflight: set[asyncio.Task] = set()
    scanner = asyncio.create_task(_scan_loop(service, settings.RECOVERY_SCAN_SECONDS), name="lease-scanner")
    generator = queue.consume()
        while not stop.is_set():
            try:
                run_id = await asyncio.wait_for(anext(generator), timeout=1.0)
            except (asyncio.TimeoutError, TimeoutError):
                continue
            await semaphore.acquire()
            task = asyncio.create_task(run_one(run_id), name=f"run:{run_id}")

            def _release(_task, _sem=semaphore):
                _sem.release()

            task.add_done_callback(_release)
            inflight.add(task)
            task.add_done_callback(inflight.discard)
        # 停止拉取后给在跑 Run 宽限窗口；超时直接退出交给租约接管
        if inflight:
            await asyncio.wait(set(inflight), timeout=SHUTDOWN_GRACE_SECONDS)
    finally:
        scanner.cancel()
        await asyncio.gather(scanner, return_exceptions=True)
        await service.shutdown()
        await queue.close()
        await database.close()


def main() -> None:
    asyncio.run(_consume())


if __name__ == "__main__":
    main()
