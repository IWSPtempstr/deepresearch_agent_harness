"""T-011：可选 Kafka 事件流分发。

定位：**事件分发通道扩展**，不改变 durable-first 语义——事件先落 run_events
（事实源），扇出失败只计数、永不传播进状态机（harness/event_bus.py 既有约束）。

组件：
- KafkaEventFanout：实现既有 EventFanout Protocol（publish/subscribe/close）。
  publish 按 key=run_id 有序写 `dra-events` topic（幂等 producer，与 T-010 同款）；
  subscribe 供独立 consumer group 消费（SSE 网关组 / 审计组可各自独立 offset）。
- FanoutChain：多通道组合——按序 publish，单通道失败只计数（EVENT_FANOUT_FAILURES）
  并继续其余通道；演示「同一条事件流、多个独立消费者」的 Kafka 核心价值。
- AuditEventConsumer：审计 consumer group 的最小骨架（group_id=dra-audit），
  把事件写入 JSONL 文件——演示跨系统消费场景。

订阅语义说明：Kafka consumer group 的 subscribe 是**进程级**的（与 Redis 的
per-run pub/sub 不同）——SSE 的 per-Run 实时订阅仍以 Redis 为主通道；Kafka
面向「全量事件流的独立下游」（审计/评测/数仓），这正是引入它的触发条件。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Callable

from deepresearch_agent.observability import EVENT_FANOUT_FAILURES

logger = logging.getLogger(__name__)

EVENTS_TOPIC_DEFAULT = "dra-events"
AUDIT_GROUP_DEFAULT = "dra-audit"


class KafkaEventFanout:
    """全量事件流 Kafka 通道（与 RedisEventFanout 同 Protocol）。"""

    def __init__(
        self,
        producer: Any,
        consumer: Any,
        *,
        topic: str = EVENTS_TOPIC_DEFAULT,
        group_id: str = AUDIT_GROUP_DEFAULT,
        tp_factory: Callable[[str, int], Any] | None = None,
    ):
        self._producer = producer
        self._consumer = consumer
        self.topic = topic
        self.group_id = group_id
        self._tp_factory = tp_factory or self._default_tp_factory
        self._started = False

    @classmethod
    def from_bootstrap(
        cls,
        bootstrap_servers: str,
        *,
        topic: str = EVENTS_TOPIC_DEFAULT,
        group_id: str = AUDIT_GROUP_DEFAULT,
    ) -> "KafkaEventFanout":
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        producer = AIOKafkaProducer(bootstrap_servers=bootstrap_servers, acks="all", enable_idempotence=True)
        consumer = AIOKafkaConsumer(
            bootstrap_servers=bootstrap_servers, group_id=group_id,
            client_id=f"{group_id}-{id(cls)}", enable_auto_commit=False,
        )
        return cls(producer, consumer, topic=topic, group_id=group_id)

    @staticmethod
    def _default_tp_factory(topic: str, partition: int) -> Any:
        from aiokafka import TopicPartition

        return TopicPartition(topic, partition)

    async def _ensure_started(self) -> None:
        if self._started:
            return
        await self._producer.start()
        await self._consumer.start()
        self._started = True

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        await self._ensure_started()
        payload = json.dumps(event, ensure_ascii=False)
        await self._producer.send_and_wait(self.topic, value=payload, key=run_id)

    async def subscribe(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        """per-Run 订阅在 Kafka 语义下退化为「从当前位点跟随整个 topic，
        过滤 run_id」。SSE 实时通道请优先用 RedisEventFanout；此实现面向
        独立消费组（审计/评测）。"""
        await self._ensure_started()
        async for message in self._consumer:
            if message.key and message.key.decode("utf-8", "replace") != run_id:
                continue
            yield json.loads(message.value)

    async def close(self) -> None:
        if not self._started:
            return
        await self._producer.stop()
        await self._consumer.stop()
        self._started = False


class FanoutChain:
    """组合多通道扇出：durable-first 约束下的尽力而为——单通道失败只计数。"""

    def __init__(self, channels: list[Any]):
        self._channels = channels

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        for channel in self._channels:
            try:
                await channel.publish(run_id, event)
            except Exception:
                try:
                    EVENT_FANOUT_FAILURES.inc()
                except Exception:
                    pass
                logger.exception("事件扇出通道失败（%s），不影响其余通道", type(channel).__name__)

    async def close(self) -> None:
        for channel in self._channels:
            close = getattr(channel, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                continue


class AuditEventConsumer:
    """审计消费组骨架：跟随全量事件 topic，按 run_id 过滤写入 JSONL。"""

    def __init__(self, fanout: KafkaEventFanout, output_path: str | None = None, *, run_id: str | None = None):
        self._fanout = fanout
        self._output_path = output_path
        self._run_id = run_id
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        await self._fanout._ensure_started()
        async for message in self._fanout._consumer:
            event = json.loads(message.value)
            if self._run_id is not None and message.key and message.key.decode("utf-8", "replace") != self._run_id:
                continue
            if self._output_path:
                with open(self._output_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            await self._fanout._consumer.commit()

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self._run(), name="audit-event-consumer")
        return self._task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
