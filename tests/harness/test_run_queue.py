"""Run 队列抽象与 API/worker 执行解耦（P0-2）。

覆盖：
1. InProcessRunQueue 进程内往返；
2. RunService 队列模式：schedule 只投递唤醒信号、不创建本地执行任务，
   execute_now（worker 侧入口）完成整个 Run；
3. RedisStreamRunQueue（由 TEST_REDIS_URL 门控）：enqueue→consume→ACK 往返
   与 consumer group 幂等创建；
4. KafkaRunQueue（T-010）：工厂选择、enqueue 分区键、至少一次消费语义
   （执行后提交 offset），stub 注入免真实 broker；真实 broker 用例由
   TEST_KAFKA_BOOTSTRAP 门控。
"""

import asyncio
import os

import pytest
import pytest_asyncio

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from backend.app.services.run_service import RunService
from deepresearch_agent.harness import RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.harness.budgets import BudgetLimits
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository
from deepresearch_agent.queueing import (
    InProcessRunQueue,
    KafkaRunQueue,
    RedisStreamRunQueue,
    create_run_queue,
)

from fake_driver import CompletedFakeDriver

REDIS_URL = os.environ.get("TEST_REDIS_URL")
KAFKA_BOOTSTRAP = os.environ.get("TEST_KAFKA_BOOTSTRAP")


class RecordingQueue:
    def __init__(self):
        self.enqueued: list[str] = []

    async def enqueue(self, run_id: str) -> None:
        self.enqueued.append(run_id)

    async def close(self) -> None:
        return None


class FakeKafkaMessage:
    def __init__(self, value: str, topic: str = "dra-runs-queue", partition: int = 0, offset: int = 0):
        # 真实 aiokafka 的 value 是 bytes（消费侧必须解码）
        self.value = value.encode("utf-8") if isinstance(value, str) else value
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
        # aiokafka 要求 value/key 为 bytes；str 会让真实 partitioner 崩溃
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
        self.stopped = False

    async def start(self):
        self.started = True

    async def getmany(self, *, timeout_ms=0, max_records=None):
        return self._batches.pop(0) if self._batches else {}

    async def commit(self, records):
        self.commits.append(dict(records))

    async def stop(self):
        self.stopped = True


def _fake_tp(topic, partition):
    return (topic, partition)


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'queue.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


async def create_queued_run(database):
    session = await SessionRepository(database).create(SessionCreate(title="queue"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="queue mode", client_message_id=f"client-{session.session_id}"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
            config_snapshot={"min_evidence": 1}, budget=BudgetLimits().model_dump(),
        ),
    )
    return run


async def test_in_process_queue_roundtrip():
    queue = InProcessRunQueue()
    await queue.enqueue("run-1")
    generator = queue.consume()
    assert await asyncio.wait_for(anext(generator), timeout=2.0) == "run-1"
    await queue.close()


async def test_queue_mode_schedule_only_signals_and_worker_completes(database, tmp_path):
    run = await create_queued_run(database)
    queue = RecordingQueue()
    service = RunService(database, workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events), artifact_root=tmp_path / "artifacts", queue=queue)
    # API 侧：schedule 只投递信号，不产生本地执行任务
    task = service.schedule(run.run_id)
    await asyncio.wait_for(task, timeout=5.0)
    assert queue.enqueued == [run.run_id]
    assert not service._tasks, "队列模式不应创建本地执行任务"
    # worker 侧：execute_now 消费信号后完成 Run
    context = await service.execute_now(run.run_id)
    assert context.status is RunStatus.COMPLETED
    persisted = await RunRepository(database).get(run.run_id)
    assert persisted.status == "completed"


async def test_local_mode_without_queue_keeps_inprocess_scheduling(database, tmp_path):
    run = await create_queued_run(database)
    service = RunService(database, workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events), artifact_root=tmp_path / "artifacts")
    task = service.schedule(run.run_id)
    await asyncio.wait_for(task, timeout=10.0)
    assert service.queue is None
    persisted = await RunRepository(database).get(run.run_id)
    assert persisted.status == "completed"


@pytest.mark.skipif(not REDIS_URL, reason="set TEST_REDIS_URL to verify the Redis Streams queue")
async def test_redis_stream_queue_roundtrip():
    queue = RedisStreamRunQueue.from_url(REDIS_URL, consumer_name="pytest-consumer")
    await queue.enqueue("run-redis-1")
    generator = queue.consume()
    assert await asyncio.wait_for(anext(generator), timeout=10.0) == "run-redis-1"
    # 组创建幂等：二次 consume 不抛 BUSYGROUP
    await queue.ensure_group()
    await queue.close()


# ---------- T-010：Kafka 可选传输层（stub 注入，无需真实 broker） ----------


def test_factory_selects_kafka_over_redis_then_inprocess(monkeypatch):
    # 分支选择测试：stub 掉 Kafka 构造器，不要求本地安装 aiokafka
    sentinel = object()
    monkeypatch.setattr(
        KafkaRunQueue, "from_bootstrap", classmethod(lambda cls, *args, **kwargs: sentinel),
    )
    kafka = create_run_queue("redis://fallback", kafka_bootstrap="kafka:9092")
    redis = create_run_queue("redis://x")
    in_process = create_run_queue("")
    assert kafka is sentinel
    assert isinstance(redis, RedisStreamRunQueue)
    assert isinstance(in_process, InProcessRunQueue)


async def test_kafka_enqueue_keys_by_run_id_and_starts_lazily():
    producer = FakeKafkaProducer()
    consumer = FakeKafkaConsumer([])
    queue = KafkaRunQueue(producer, consumer, tp_factory=_fake_tp)
    assert not producer.started  # 懒启动：构造不触达 broker
    await queue.enqueue("run-k-1")
    assert producer.started
    assert producer.sent == [("dra-runs-queue", b"run-k-1", b"run-k-1")]


async def test_kafka_consume_with_ack_commits_only_after_ack():
    message = FakeKafkaMessage("run-k-2", partition=0, offset=4)
    consumer = FakeKafkaConsumer([{("dra-runs-queue", 0): [message]}])
    queue = KafkaRunQueue(FakeKafkaProducer(), consumer, tp_factory=_fake_tp, offset_factory=lambda o: o)
    generator = queue.consume_with_ack()
    run_id, ack = await asyncio.wait_for(anext(generator), timeout=2.0)
    assert run_id == "run-k-2"
    # 至少一次语义：执行完成（调用 ack）前不提交 offset
    assert consumer.commits == []
    await ack()
    assert consumer.commits == [{("dra-runs-queue", 0): 5}]


async def test_kafka_ack_out_of_order_waits_for_watermark():
    """乱序完成时按分区水位线单调提交：ack(5) 先完成不越位,ack(4) 补齐后一次推进。"""
    m4 = FakeKafkaMessage("run-a", partition=0, offset=4)
    m5 = FakeKafkaMessage("run-b", partition=0, offset=5)
    consumer = FakeKafkaConsumer([{("dra-runs-queue", 0): [m4, m5]}])
    queue = KafkaRunQueue(FakeKafkaProducer(), consumer, tp_factory=_fake_tp, offset_factory=lambda o: o)
    generator = queue.consume_with_ack()
    run_a, ack4 = await asyncio.wait_for(anext(generator), timeout=2.0)
    run_b, ack5 = await asyncio.wait_for(anext(generator), timeout=2.0)
    assert (run_a, run_b) == ("run-a", "run-b")
    # 交付顺序为 [m4, m5],此处刻意先完成高位 ack5,验证不越位提交
    await ack5()
    assert consumer.commits == [], "水位线未到 4,不得提交 5"
    await ack4()
    assert consumer.commits == [{("dra-runs-queue", 0): 6}]


async def test_kafka_rebalance_ignores_stale_ack_and_rebases_after_redelivery():
    """rebalance 竞态：撤销后旧任务的 ack 即使在新会话重建水位线后才调用，
    也必须被代次检查忽略；重投消息的新 ack 正常提交。"""
    message = FakeKafkaMessage("run-c", partition=0, offset=4)
    consumer = FakeKafkaConsumer([{("dra-runs-queue", 0): [message]}])
    queue = KafkaRunQueue(FakeKafkaProducer(), consumer, tp_factory=_fake_tp, offset_factory=lambda o: o)
    generator = queue.consume_with_ack()
    run_id, stale_ack = await asyncio.wait_for(anext(generator), timeout=2.0)
    assert run_id == "run-c"
    # 模拟 rebalance：分区撤销（代次 +1，水位线丢弃），随后同消息重投
    await queue._rebalance_listener.on_partitions_revoked([message])
    redelivered = FakeKafkaMessage("run-c", partition=0, offset=4)
    consumer._batches.append({("dra-runs-queue", 0): [redelivered]})
    run_id2, fresh_ack = await asyncio.wait_for(anext(generator), timeout=2.0)
    assert run_id2 == "run-c"
    # 旧代次 ack 在新会话水位线重建之后才调用——不得越权提交
    await stale_ack()
    assert consumer.commits == [], "过期代次的 ack 不得推进新会话水位线"
    await fresh_ack()
    assert consumer.commits == [{("dra-runs-queue", 0): 5}]


async def test_kafka_ack_rechecks_generation_under_commit_lock():
    """TOCTOU：ack 通过锁外快路径检查后在 _commit_lock 上等待，
    等锁期间发生 rebalance，拿到锁后锁内代次校验必须拦下这次提交。"""
    message = FakeKafkaMessage("run-d", partition=0, offset=4)
    consumer = FakeKafkaConsumer([{("dra-runs-queue", 0): [message]}])
    queue = KafkaRunQueue(FakeKafkaProducer(), consumer, tp_factory=_fake_tp, offset_factory=lambda o: o)
    generator = queue.consume_with_ack()
    run_id, ack = await asyncio.wait_for(anext(generator), timeout=2.0)
    assert run_id == "run-d"
    # 占住提交锁，让 ack 启动并阻塞在锁上
    await queue._commit_lock.acquire()
    ack_task = asyncio.create_task(ack())
    await asyncio.sleep(0)  # 让 ack 运行至等锁位置
    assert not ack_task.done()
    # ack 等锁期间发生 rebalance：代次递增、水位线清空
    await queue._rebalance_listener.on_partitions_revoked([message])
    queue._commit_lock.release()
    await asyncio.wait_for(ack_task, timeout=2.0)
    assert consumer.commits == [], "等锁期间 rebalance 后，锁内校验必须忽略本次 ack"


async def test_kafka_consume_plain_rejected():
    queue = KafkaRunQueue(FakeKafkaProducer(), FakeKafkaConsumer([]), tp_factory=_fake_tp)
    with pytest.raises(NotImplementedError):
        queue.consume()


async def test_kafka_close_without_start_is_noop():
    producer = FakeKafkaProducer()
    consumer = FakeKafkaConsumer([])
    queue = KafkaRunQueue(producer, consumer, tp_factory=_fake_tp)
    await queue.close()
    assert not producer.started and not consumer.started and not consumer.stopped


async def test_kafka_close_stops_producer_then_consumer():
    producer = FakeKafkaProducer()
    consumer = FakeKafkaConsumer([])
    queue = KafkaRunQueue(producer, consumer, tp_factory=_fake_tp)
    await queue.enqueue("run-k-3")
    await queue.close()
    assert not producer.started and consumer.stopped


@pytest.mark.skipif(not KAFKA_BOOTSTRAP, reason="set TEST_KAFKA_BOOTSTRAP to verify the Kafka queue")
async def test_kafka_queue_roundtrip():
    queue = KafkaRunQueue.from_bootstrap(KAFKA_BOOTSTRAP, client_id="pytest-consumer")
    await queue.enqueue("run-kafka-real")
    generator = queue.consume_with_ack()
    run_id, ack = await asyncio.wait_for(anext(generator), timeout=30.0)
    assert run_id == "run-kafka-real"
    await ack()
    await queue.close()


def test_kafka_consumer_subscribes_topic():
    """回归防线：aiokafka consumer 必须带 topic 订阅，否则 getmany 永远无数据
    （真实 broker 才会暴露；stub 测试无法覆盖）。"""
    import inspect

    from deepresearch_agent import queueing as queueing_mod

    source = inspect.getsource(queueing_mod.KafkaRunQueue.from_bootstrap)
    assert "AIOKafkaConsumer(" in source
    consumer_call = source[source.index("AIOKafkaConsumer("):]
    # 第一个位置参数应为 topic（订阅），而非仅 keyword 参数
    first_arg = consumer_call.split("(", 1)[1].split(",")[0].split(")")[0].strip()
    assert first_arg == "topic", f"consumer 首个位置参数应为 topic（订阅），当前为 {first_arg!r}"
