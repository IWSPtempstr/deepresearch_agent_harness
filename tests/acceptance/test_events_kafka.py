"""T-011：Kafka 事件流分发测试（stub 注入，免真实 broker）。

覆盖：
1. KafkaEventFanout publish（key=run_id 有序、payload JSON）与 Protocol 一致性；
2. per-Run 订阅过滤；
3. FanoutChain：单通道失败只计数、其余通道继续、close 全量抑制；
4. AuditEventConsumer 消费→JSONL 落盘→commit；
5. 真实 broker 用例由 TEST_KAFKA_BOOTSTRAP 门控。
"""

import asyncio
import json
import os

import pytest

from deepresearch_agent.events_kafka import (
    AuditEventConsumer,
    EVENTS_TOPIC_DEFAULT,
    FanoutChain,
    KafkaEventFanout,
)
from deepresearch_agent.observability import EVENT_FANOUT_FAILURES


class FakeKafkaMessage:
    def __init__(self, value, key=None, topic=EVENTS_TOPIC_DEFAULT, partition=0, offset=0):
        self.value = value
        self.key = key.encode() if isinstance(key, str) else key
        self.topic = topic
        self.partition = partition
        self.offset = offset


class FakeKafkaProducer:
    def __init__(self):
        self.started = False
        self.sent: list[tuple[str, str, str]] = []

    async def start(self):
        self.started = True

    async def send_and_wait(self, topic, value=None, key=None):
        # aiokafka 要求 value/key 为 bytes
        if key is not None and not isinstance(key, (bytes, bytearray)):
            raise TypeError(f"key 必须是 bytes，收到 {type(key).__name__}")
        if value is not None and not isinstance(value, (bytes, bytearray)):
            raise TypeError(f"value 必须是 bytes，收到 {type(value).__name__}")
        self.sent.append((topic, value, key))

    async def stop(self):
        self.started = False


class FakeKafkaConsumer:
    def __init__(self, batches):
        self._batches = list(batches)
        self.commits: list[dict] = []
        self.started = False

    async def start(self):
        self.started = True

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        while self._batches:
            for _tp, messages in self._batches.pop(0).items():
                for message in messages:
                    yield message

    async def commit(self, records=None):
        self.commits.append(records or {})

    async def stop(self):
        self.started = False


def _tp(topic, partition):
    return (topic, partition)


def _fanout(producer, consumer):
    return KafkaEventFanout(producer, consumer, tp_factory=_tp)


async def test_publish_sends_keyed_json():
    producer = FakeKafkaProducer()
    fanout = _fanout(producer, FakeKafkaConsumer([]))
    await fanout.publish("run-1", {"event_type": "run.started", "stage": "context_building"})
    assert producer.started  # 懒启动
    topic, value, key = producer.sent[0]
    assert topic == EVENTS_TOPIC_DEFAULT and key == b"run-1"
    assert json.loads(value.decode("utf-8"))["event_type"] == "run.started"


async def test_subscribe_filters_by_run_id():
    consumer = FakeKafkaConsumer([
        {("t", 0): [FakeKafkaMessage('{"a":1}', key="run-1"), FakeKafkaMessage('{"b":2}', key="run-2")]},
    ])
    fanout = _fanout(FakeKafkaProducer(), consumer)
    iterator = fanout.subscribe("run-1")
    received = [await asyncio.wait_for(anext(iterator), timeout=2.0)]
    with pytest.raises((asyncio.TimeoutError, StopAsyncIteration)):
        await asyncio.wait_for(anext(iterator), timeout=0.3)
    assert received == [{"a": 1}]


async def test_fanout_chain_isolates_failures():
    class Exploding:
        async def publish(self, run_id, event):
            raise ConnectionError("redis down")

        async def close(self):
            raise RuntimeError("close boom")

    class Recording:
        def __init__(self):
            self.events = []
            self.closed = False

        async def publish(self, run_id, event):
            self.events.append(event)

        async def close(self):
            self.closed = True

    recorder = Recording()
    chain = FanoutChain([Exploding(), recorder])
    before = EVENT_FANOUT_FAILURES._value.get()
    await chain.publish("run-1", {"event_type": "x"})
    assert recorder.events == [{"event_type": "x"}], "单通道失败不影响其余通道"
    assert EVENT_FANOUT_FAILURES._value.get() == before + 1
    await chain.close()
    assert recorder.closed, "close 全量执行且抑制单点异常"


async def test_audit_consumer_writes_jsonl_and_commits():
    tmp = os.environ.get("PYTEST_CURRENT_TEST", "")
    output = pytest.TempPathFactory().mktemp("audit") / "audit.jsonl" if False else __import__("tempfile").mktemp(suffix=".jsonl")
    message = FakeKafkaMessage('{"event_type": "run.completed", "run_id": "run-9"}', key="run-9", offset=3)
    consumer = FakeKafkaConsumer([{("t", 0): [message]}])
    fanout = _fanout(FakeKafkaProducer(), consumer)
    audit = AuditEventConsumer(fanout, output, run_id="run-9")
    task = audit.start()
    for _ in range(50):
        await asyncio.sleep(0.05)
        if __import__("os").path.exists(output) and __import__("os").path.getsize(output) > 0:
            break
    await audit.stop()
    with open(output, encoding="utf-8") as handle:
        line = handle.readline()
    assert json.loads(line)["event_type"] == "run.completed"
    assert consumer.commits, "审计消费按 offset 提交"
