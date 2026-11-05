"""Run 执行队列抽象（P0-2）：进程内 asyncio 队列、Redis Streams 与 Kafka 三种实现。

设计决策——**流/主题只是唤醒信号，数据库才是事实源**：
- Redis Streams 读出即 ACK（at-most-once 投递）。Run 的排队/中断/恢复状态全部落在
  runs 表，崩溃恢复依赖启动恢复 + 租约过期扫描（P0-3b），而非流的 pending
  重投。这样避免「流重投 + 租约接管」两条恢复路径互相打架导致双执行。
- Kafka（T-010 可选传输层）采用**至少一次 + 执行后提交 offset** 的消费语义：
  consumer group rebalance/崩溃时未提交信号会重投，重复投递由租约条件 UPDATE
  （runs 表）天然去重；DB 扫描兜底语义保持不变。
- 消费语义为并发受限的拉取：worker 以 WORKER_CONCURRENCY 控制同时在跑的
  Run 数（对应原 API 进程内多 task 并发的行为）。
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, AsyncIterator, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

STREAM_KEY = "dra:runs:queue"
CONSUMER_GROUP = "dra-workers"
KAFKA_TOPIC_DEFAULT = "dra-runs-queue"
KAFKA_GROUP_DEFAULT = "dra-workers"

# 入队有界重试（T-003）：重试间指数退避；全部耗尽后抛 QueueUnavailableError，
# 调用方观测记录，run 留在 queued 由租约扫描重建投递
ENQUEUE_MAX_ATTEMPTS = 3
ENQUEUE_BACKOFF_BASE_SECONDS = 0.2


class QueueUnavailableError(RuntimeError):
    """队列后端不可用（重试耗尽）：信号未投递，恢复路径依赖 DB 扫描兜底。"""


async def _enqueue_with_retry(operation: Callable[[], Any], *, cancel_event: Optional[asyncio.Event] = None) -> None:
    """执行一次入队操作，带指数退避有界重试；全部失败抛 QueueUnavailableError。"""
    last_error: BaseException | None = None
    for attempt in range(1, ENQUEUE_MAX_ATTEMPTS + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise QueueUnavailableError("入队取消（cancel_event 已置位）") from last_error
        try:
            await operation()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= ENQUEUE_MAX_ATTEMPTS:
                break
            await asyncio.sleep(ENQUEUE_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    raise QueueUnavailableError(f"入队失败（重试 {ENQUEUE_MAX_ATTEMPTS} 次）") from last_error


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
        await _enqueue_with_retry(lambda: self._client.xadd(STREAM_KEY, {"run_id": run_id}))

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


class _KafkaRebalanceListener:
    """rebalance 时清理本地水位线：未提交 ack 随分区撤销丢弃，新分配分区从
    上次已提交位点继续。重复投递由租约互斥覆盖（见 KafkaRunQueue 类注释）。"""

    def __init__(self, queue: "KafkaRunQueue"):
        self._queue = queue

    async def on_partitions_revoked(self, partitions) -> None:
        # 代次先递增：撤销瞬间起，所有旧代次 ack 失效
        self._queue._assignment_generation += 1
        for p in partitions:
            tp = self._queue._tp_factory(p.topic, p.partition)
            self._queue._acked_offsets.pop(tp, None)
            self._queue._next_commit.pop(tp, None)
        logger.info("Kafka 分区撤销，代次=%s，pending 水位线已丢弃", self._queue._assignment_generation)

    async def on_partitions_assigned(self, partitions) -> None:
        for p in partitions:
            tp = self._queue._tp_factory(p.topic, p.partition)
            self._queue._acked_offsets.pop(tp, None)
            self._queue._next_commit.pop(tp, None)


class KafkaRunQueue:
    """可选 Kafka 传输层（T-010）：唤醒信号经 Kafka topic 在 API 与 worker 间投递。

    语义要点（与 Redis Streams 的差异，均为有意设计）：
    - **至少一次**：执行完成后才提交 offset（enable_auto_commit=False）。consumer
      崩溃 / rebalance 时未提交消息会重投；重复投递由租约条件 UPDATE（唯一互斥量）
      去重，不产生双执行副作用。
    - **分区键 = run_id**：同一 Run 的信号有序落在同一分区；跨 Run 天然并行。
    - 幂等 producer（enable_idempotence=True, acks="all"）：broker 端去重重试。
    - 仅支持 consume_with_ack()：无 ack 的 consume() 会因永不提交 offset 导致
      重启后全量重放，因此显式禁用。
    """

    def __init__(
        self,
        producer: Any,
        consumer: Any,
        *,
        topic: str = KAFKA_TOPIC_DEFAULT,
        tp_factory: Optional[Callable[[str, int], Any]] = None,
        offset_factory: Optional[Callable[[int], Any]] = None,
    ):
        # producer/consumer 允许注入 stub（单元测试无需真实 broker）
        self._producer = producer
        self._consumer = consumer
        self.topic = topic
        self._tp_factory = tp_factory or self._default_tp_factory
        self._offset_factory = offset_factory or self._default_offset_factory
        self._started = False
        self._commit_lock = asyncio.Lock()
        self._next_commit: dict[Any, int] = {}
        self._acked_offsets: dict[Any, set[int]] = {}
        # 分区分配代次：每次 revoke 递增；旧代次的 ack 一律忽略，
        # 防止 revoke 前完成的任务在新分配会话重建水位线后越权提交
        self._assignment_generation = 0
        self._rebalance_listener = _KafkaRebalanceListener(self)

    @classmethod
    def from_bootstrap(
        cls,
        bootstrap_servers: str,
        *,
        topic: str = KAFKA_TOPIC_DEFAULT,
        group_id: str = KAFKA_GROUP_DEFAULT,
        client_id: Optional[str] = None,
    ) -> "KafkaRunQueue":
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        producer = AIOKafkaProducer(
            bootstrap_servers=bootstrap_servers, acks="all", enable_idempotence=True,
        )
        # 必须带 topic：aiokafka 的 getmany 只返回已订阅 topic 的消息
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=bootstrap_servers,
            group_id=group_id,
            client_id=client_id,
            enable_auto_commit=False,
            max_poll_records=100,
        )
        return cls(producer, consumer, topic=topic)

    @staticmethod
    def _default_tp_factory(topic: str, partition: int) -> Any:
        from aiokafka import TopicPartition

        return TopicPartition(topic, partition)

    @staticmethod
    def _default_offset_factory(offset: int) -> Any:
        from aiokafka.structs import OffsetAndMetadata

        return OffsetAndMetadata(offset, "")

    async def _ensure_started(self) -> None:
        if self._started:
            return
        self._consumer.consumer_rebalance_listener = self._rebalance_listener
        await self._producer.start()
        await self._consumer.start()
        self._started = True

    async def enqueue(self, run_id: str) -> None:
        # start 纳入重试：broker 不可用时的首连失败与后续 send 同等对待（T-003）
        await _enqueue_with_retry(self._ensure_started)
        # key=run_id：同 Run 信号保序；str 由 aiokafka 按 utf-8 编码
        # aiokafka 的 key 必须是 bytes（partitioner 内部按 key 计算分区）
        await _enqueue_with_retry(
            lambda: self._producer.send_and_wait(
                self.topic, value=run_id.encode("utf-8"), key=run_id.encode("utf-8"),
            ),
        )

    def consume(self) -> AsyncIterator[str]:
        raise NotImplementedError(
            "KafkaRunQueue 仅支持 consume_with_ack()（执行后提交 offset 的至少一次语义）；"
            "无 ack 的消费会在重启后全量重放"
        )

    def consume_with_ack(self) -> AsyncIterator[tuple[str, Callable[[], Any]]]:
        """产出 (run_id, ack)。ack 提交该消息的 offset（+1），应在 Run 执行完成后调用。

        ack 按 offset 单调提交：先完成的低位 offset 会等待同分区未 ack 的更低位
        消息（水位线），避免越位提交跳过未完成消息。rebalance/分区撤销时未提交
        的 offset 随分区撤销被丢弃（ack 绑定分配代次，过期即忽略），消费者重新分配后从上次
        提交位点继续——重复投递由租约互斥覆盖。
        """
        return self._consume_with_ack()

    async def _consume_with_ack(self) -> AsyncIterator[tuple[str, Callable[[], Any]]]:
        await self._ensure_started()
        while True:
            # 500ms < worker 侧 1s wait_for 超时，保证空转时也能检查 stop 信号
            batch = await self._consumer.getmany(timeout_ms=500)
            for _tp, messages in batch.items():
                for message in messages:
                    # value 是 bytes（aiokafka 要求），解码为 str
                    raw = message.value
                    run_id = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                    tp = self._tp_factory(message.topic, message.partition)
                    generation = self._assignment_generation
                    # 水位线基点 = 本会话（本次分配）首个交付 offset；
                    # rebalance 清理后由下一次交付重新建立
                    if tp not in self._next_commit:
                        self._next_commit[tp] = message.offset

                    def _make_ack(tp=tp, offset=message.offset, generation=generation):
                        async def ack() -> None:
                            if generation != self._assignment_generation:
                                return  # 快路径：已过期直接忽略
                            # 权威检查在锁内：等锁期间发生 rebalance 时，
                            # 拿到锁后再次校验代次，防止 TOCTOU 越权提交
                            await self._register_ack(tp, offset, generation)

                        return ack

                    if run_id:
                        yield run_id, _make_ack()

    async def _register_ack(self, tp: Any, offset: int, generation: int) -> None:
        """按分区记录 ack 并推进水位线，单调提交（offset+1）。

        代次校验必须在 _commit_lock 内进行：等锁期间发生的 rebalance 会
        递增代次并清空水位线，锁外检查存在 TOCTOU 竞态。
        """
        async with self._commit_lock:
            if generation != self._assignment_generation:
                return
            pending = self._acked_offsets.setdefault(tp, set())
            pending.add(offset)
            committed = self._next_commit.get(tp, 0)
            # 水位线推进：连续段 [committed, next] 内全部已 ack 才提交到 next+1
            nxt = committed
            while nxt in pending:
                pending.discard(nxt)
                nxt += 1
            if nxt > committed:
                self._next_commit[tp] = nxt
                records = {tp: self._offset_factory(nxt)}
                await self._consumer.commit(records)

    async def close(self) -> None:
        if not self._started:
            return
        # 先停 producer：flush 在途 send；再停 consumer
        await self._producer.stop()
        await self._consumer.stop()
        self._started = False
        self._acked_offsets.clear()
        self._next_commit.clear()


def create_run_queue(
    redis_url: str = "",
    *,
    kafka_bootstrap: str = "",
    kafka_topic: str = KAFKA_TOPIC_DEFAULT,
    kafka_group: str = KAFKA_GROUP_DEFAULT,
    consumer_name: Optional[str] = None,
) -> RunQueue:
    """按配置选择队列实现：Kafka 优先，其次 Redis Streams，否则进程内模式。"""
    if kafka_bootstrap:
        return KafkaRunQueue.from_bootstrap(
            kafka_bootstrap, topic=kafka_topic, group_id=kafka_group, client_id=consumer_name,
        )
    if redis_url:
        return RedisStreamRunQueue.from_url(redis_url, consumer_name=consumer_name)
    return InProcessRunQueue()
