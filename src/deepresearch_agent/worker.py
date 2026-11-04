"""独立 Run 执行 worker 进程（P0-2）。

用法：REDIS_URL=redis://... 或 KAFKA_BOOTSTRAP_SERVERS=kafka:9092 \
python -m deepresearch_agent.worker

关停语义（对应 P0-3b 的接管设计）：
- SIGTERM/SIGINT 只停止拉取新 Run，给在跑 Run 一个宽限窗口自然结束；
- 宽限期内未结束的 Run 不做取消也不写终态，进程直接退出——等价于一次
  崩溃，由租约过期扫描 + 其他 worker 接管完成（避免「优雅关停被误标
  CANCELLED、无法接管」的语义冲突）。
- Kafka 传输层（T-010）：唤醒信号为至少一次投递；ack 在 Run 执行完成后
  提交 offset，崩溃/重投由租约互斥去重，DB 扫描兜底语义不变。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket

from backend.app.services import RunService
from deepresearch_agent.config import settings
from deepresearch_agent.events_fanout import RedisEventFanout
from deepresearch_agent.persistence import Database
from deepresearch_agent.queueing import RedisStreamRunQueue, STREAM_KEY, create_run_queue

SHUTDOWN_GRACE_SECONDS = 30.0

logger = logging.getLogger("deepresearch.worker")


async def _scan_loop(service: RunService, interval: float) -> None:
    """P0-3b：周期扫描过期租约并触发接管投递；顺带刷新队列深度与等待指标。"""
    from deepresearch_agent.observability import QUEUE_DEPTH, WAITING_RUNS

    while True:
        await asyncio.sleep(interval)
        try:
            await service.recover_expired()
        except Exception:
            # 单轮扫描失败（如 DB 抖动）不终止循环；下一轮重试
            continue
        try:
            # T-003：WAITING_RUNS = queued 状态行数（复用扫描轮次，不增加查询频率）
            waiting = await service.runs.count_by_status("queued")
            WAITING_RUNS.set(int(waiting))
        except Exception:
            pass
        try:
            # 仅 Redis Streams 提供积压深度（流只承载唤醒信号，该值仅供观测）；
            # Kafka/进程内队列无对应语义，跳过该指标
            if isinstance(service.queue, RedisStreamRunQueue):
                length = await service.queue._client.xlen(STREAM_KEY)
                QUEUE_DEPTH.set(int(length))
        except Exception:
            continue


async def execute_safely(service: RunService, run_id: str) -> None:
    """评审遗留修复：execute_run 已把失败状态落库，这里补日志与指标现场。

    异常必须吞掉：单个 Run 的失败不能拖垮 worker 的消费循环。
    """
    import logging

    from deepresearch_agent.observability import WORKER_RUN_FAILURES

    logger = logging.getLogger("deepresearch.worker")
    try:
        await service.execute_now(run_id)
    except Exception:
        logger.exception("Run %s 执行异常（状态已落库）", run_id)
        try:
            WORKER_RUN_FAILURES.inc()
        except Exception:
            pass


async def _consume(worker_name: str | None = None) -> None:
    if not settings.REDIS_URL and not settings.KAFKA_BOOTSTRAP_SERVERS:
        raise SystemExit("worker 进程需要设置 REDIS_URL 或 KAFKA_BOOTSTRAP_SERVERS（平台模式）")
    database = Database(settings.APP_DATABASE_URL)
    await database.create_schema()
    from deepresearch_agent.llm_gateway import start_metering, stop_metering
    from deepresearch_agent.observability import setup_tracing

    setup_tracing("deepresearch-worker")
    start_metering(database)
    queue = create_run_queue(
        settings.REDIS_URL,
        kafka_bootstrap=settings.KAFKA_BOOTSTRAP_SERVERS,
        kafka_topic=settings.KAFKA_TOPIC,
        kafka_group=settings.KAFKA_CONSUMER_GROUP,
        consumer_name=worker_name or os.environ.get("WORKER_NAME") or f"worker-{socket.gethostname()}",
    )
    # 事件实时扇出仍走 Redis（Kafka 事件 topic 属 T-011，未实现前 SSE 依赖
    # DB 重放 + 心跳兜底，Kafka-only 模式不提供 live 推送）
    event_fanout = RedisEventFanout.from_url(settings.REDIS_URL) if settings.REDIS_URL else None
    service = RunService(database, event_fanout=event_fanout)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def run_one(run_id: str, ack=None) -> None:
        try:
            await execute_safely(service, run_id)
        finally:
            if ack is not None:
                try:
                    await ack()
                except Exception:
                    # commit 失败 → offset 未提交 → 重投；租约互斥覆盖重复执行
                    logger.exception("提交 Kafka offset 失败（信号将重投，租约去重）")

    try:
        semaphore = asyncio.Semaphore(max(1, settings.WORKER_CONCURRENCY))
        inflight: set[asyncio.Task] = set()
        scanner = asyncio.create_task(_scan_loop(service, settings.RECOVERY_SCAN_SECONDS), name="lease-scanner")
        # KafkaRunQueue 提供 consume_with_ack（执行后提交 offset 的至少一次语义）；
        # 其余实现沿用无 ack 的 consume()
        consume_with_ack = getattr(queue, "consume_with_ack", None)

        def _open_stream():
            # T-003：断连自愈——每次（重）连都重建消费生成器；
            # Kafka/Redis/进程内实现统一适用
            return consume_with_ack() if consume_with_ack is not None else queue.consume()

        generator = _open_stream()
        reconnect_backoff = 1.0
        # T-003 关键修复：anext 用独立 task 承载，wait_for 超时只放弃"这一次等待"，
        # 不取消生成器——否则挂起中的消费循环（如 Redis xreadgroup block）会被
        # 1s 超时反复杀死，交付值丢失、生成器死亡。下次循环继续收同一个 anext。
        next_item: asyncio.Task | None = None
        while not stop.is_set():
            if next_item is None:
                next_item = asyncio.ensure_future(anext(generator))
            try:
                delivered = await asyncio.wait_for(asyncio.shield(next_item), timeout=1.0)
            except asyncio.CancelledError:
                next_item.cancel()
                raise
            except (asyncio.TimeoutError, TimeoutError):
                # 区分两种超时（py3.11+ 同类型）：
                # - 1s 等待超时且任务仍挂起 → 正常空转，anext 保持
                # - 任务自身抛 TimeoutError（如 socket_timeout）→ done 任务
                #   每轮立即重抛造成零 yield 忙旋、饿死 stop 信号 → 走断连自愈
                if not next_item.done():
                    continue
                delivered = None  # 落入下方断连自愈
            except Exception:
                delivered = None  # 连接类/其他异常：落入下方断连自愈
            if delivered is None:
                # T-003：Redis/Kafka 断连不再杀死 worker——记日志 + 计数 +
                # 指数退避（1s 起、30s 封顶，stop 信号可打断）后重建生成器继续。
                # 恢复语义不变：未 ack 的信号重投/丢失信号由租约扫描兜底。
                from deepresearch_agent.observability import WORKER_CONSUME_RECONNECTS

                next_item = None
                logger.exception("队列消费流断连，%.1fs 后重建生成器重试", reconnect_backoff)
                with contextlib.suppress(Exception):
                    await generator.aclose()
                try:
                    WORKER_CONSUME_RECONNECTS.inc()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=reconnect_backoff)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                if stop.is_set():
                    break
                reconnect_backoff = min(reconnect_backoff * 2, 30.0)
                generator = _open_stream()
                continue
            # 正常消费成功一次即重置退避
            reconnect_backoff = 1.0
            run_id, ack = delivered if isinstance(delivered, tuple) else (delivered, None)
            next_item = None
            await semaphore.acquire()
            task = asyncio.create_task(run_one(run_id, ack), name=f"run:{run_id}")

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
        # 取消悬挂中的 anext 任务，再显式关闭消费生成器：
        # 结束 Kafka/Redis 客户端侧的拉取循环，然后关队列资源
        if next_item is not None and not next_item.done():
            next_item.cancel()
            await asyncio.gather(next_item, return_exceptions=True)
        with contextlib.suppress(Exception):
            await generator.aclose()
        await service.shutdown()
        await stop_metering()
        await queue.close()
        await database.close()


def main() -> None:
    asyncio.run(_consume())


if __name__ == "__main__":
    main()
