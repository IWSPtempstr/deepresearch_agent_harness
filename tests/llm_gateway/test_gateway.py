"""LLM 网关（P1-5）：failover / 健康度冷却 / 限流排队 / 计量落库。

全部使用 fake 传输层客户端与 _run_chain/_agenerate_chain 链控入口，不出网。
"""

import time
import types

import pytest
import pytest_asyncio
from sqlalchemy import select

from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.llm_gateway.gateway import (
    FailoverChatOpenAI, LlmRateLimited, ProviderHealth, SlotLimiter, _usage_from_result,
)
from deepresearch_agent.llm_gateway.metering import LlmCallRecorder
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.models import LlmCallModel


class FakeChatResult:
    def __init__(self, total_tokens=30):
        message = types.SimpleNamespace(
            usage_metadata={"input_tokens": 20, "output_tokens": total_tokens - 20, "total_tokens": total_tokens},
            response_metadata={},
        )
        self.llm_output = None
        self.generations = [types.SimpleNamespace(message=message)]


class FakeClient:
    """只实现传输层 _generate/_agenerate 的假客户端。"""

    def __init__(self, *, fail_times=0, error=RuntimeError("provider down")):
        self.fail_times = fail_times
        self.calls = 0
        self.error = error

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return FakeChatResult()


def build_model(primary: FakeClient, fallback: FakeClient, *, limiter=None, recorder=None, health=None) -> tuple[FailoverChatOpenAI, FakeClient, FakeClient]:
    model = FailoverChatOpenAI(
        api_key="sk-test", model="test-model", base_url="http://primary.local/v1",
        fallbacks=[("fallback.local", fallback)],
        limiter=limiter, health=health or ProviderHealth(base_cooldown_seconds=60.0), recorder=recorder,
    )
    return model, primary, fallback


def run_sync(model, primary, fallback):
    """走 _run_chain（同步链控：invoke_primary 对应主 provider 传输层）。"""
    return model._run_chain(
        lambda: primary._generate([]),
        lambda client: client._generate([]),
    )


def test_primary_failure_falls_back_to_secondary():
    model, primary, fallback = build_model(FakeClient(fail_times=99), FakeClient())
    result = run_sync(model, primary, fallback)
    assert result.generations[0].message.usage_metadata["total_tokens"] == 30
    assert fallback.calls == 1, "主 provider 失败后必须切到备用"
    assert model.gateway_health.in_cooldown("primary.local")


def test_all_providers_failed_raises_typed_error():
    model, primary, fallback = build_model(FakeClient(fail_times=99), FakeClient(fail_times=99))
    with pytest.raises(AppError) as excinfo:
        run_sync(model, primary, fallback)
    assert excinfo.value.code == ErrorCode.LLM_ALL_PROVIDERS_FAILED


def test_cooldown_skips_unhealthy_primary():
    model, _, _ = build_model(FakeClient(), FakeClient())
    model.gateway_health.report_failure("primary.local")
    chain = model._provider_chain()
    assert [label for label, _ in chain] == ["fallback.local"], "冷却中的主 provider 应被跳过"


def test_metering_records_ok_and_error_paths():
    recorder = LlmCallRecorder()
    model, primary, fallback = build_model(FakeClient(fail_times=1), FakeClient(), recorder=recorder)
    run_sync(model, primary, fallback)
    # 主 provider 一次失败计量 + 备用一次成功计量
    assert recorder._queue.qsize() == 2
    items = [recorder._queue.get_nowait() for _ in range(2)]
    assert items[0]["provider"] == "primary.local" and items[0]["status"] == "error"
    assert items[1]["provider"] == "fallback.local" and items[1]["status"] == "ok" and items[1]["total_tokens"] == 30


def test_slot_limiter_queues_then_times_out():
    limiter = SlotLimiter(max_concurrency=1, queue_timeout_seconds=0.3)
    with limiter.slot("t1"):
        started = time.monotonic()
        with pytest.raises(LlmRateLimited) as excinfo:
            with limiter.slot("t1"):
                pass
        assert excinfo.value.code == ErrorCode.LLM_RATE_LIMITED
        assert time.monotonic() - started >= 0.25, "必须先排队等满超时窗口再抛 429 语义错误"


def test_slot_limiter_releases_slot():
    limiter = SlotLimiter(max_concurrency=1, queue_timeout_seconds=1.0)
    with limiter.slot("t1"):
        pass
    with limiter.slot("t1"):
        pass  # 槽已释放，立即获取


def test_limiter_applies_to_gateway_calls():
    limiter = SlotLimiter(max_concurrency=1, queue_timeout_seconds=0.5)
    model, primary, fallback = build_model(FakeClient(), FakeClient(), limiter=limiter)
    with limiter.slot("default"):  # 槽被占满
        with pytest.raises(LlmRateLimited):
            run_sync(model, primary, fallback)


async def test_async_chain_fails_over():
    model, primary, fallback = build_model(FakeClient(fail_times=99), FakeClient())
    result = await model._agenerate_chain([], None, None)
    assert result.generations[0].message.usage_metadata["total_tokens"] == 30
    assert fallback.calls == 1


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'meter.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


async def test_metering_flush_persists_llm_calls(database):
    recorder = LlmCallRecorder()
    recorder.start(database)
    try:
        recorder.record(
            provider="primary.local", model="test-model", run_id="run-1",
            usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            latency_ms=120, status="ok", error_code=None,
        )
        recorder.record(
            provider="primary.local", model="test-model", run_id=None,
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            latency_ms=45, status="error", error_code="ConnectionError",
        )
        flushed = await recorder.flush()
        assert flushed == 2
        async with database.sessions() as session:
            rows = list((await session.execute(select(LlmCallModel).order_by(LlmCallModel.call_id))).scalars())
        assert len(rows) == 2
        ok_row, error_row = rows
        assert ok_row.total_tokens == 30 and ok_row.run_id == "run-1" and ok_row.status == "ok"
        assert error_row.error_code == "ConnectionError" and error_row.status == "error"
    finally:
        await recorder.stop()


def test_metering_carries_tenant_dimension():
    """评审 M4：llm_calls 必须记录当前租户。"""
    from backend.app.tenancy import set_current_tenant

    recorder = LlmCallRecorder()
    model, primary, fallback = build_model(FakeClient(), FakeClient(), recorder=recorder)
    token = set_current_tenant("tenant-xyz")
    try:
        run_sync(model, primary, fallback)
    finally:
        import backend.app.tenancy as tenancy

        tenancy._current_tenant.reset(token)
    items = [recorder._queue.get_nowait() for _ in range(recorder._queue.qsize())]
    assert items and all(item["tenant_id"] == "tenant-xyz" for item in items), items


async def test_astream_chain_fails_over_and_meters():
    """评审遗留修复：流式路径与 _agenerate 同权——主失败切备用、健康度冷却、计量入队。"""
    import types as _types

    recorder = LlmCallRecorder()
    model, primary, fallback = build_model(FakeClient(fail_times=99), FakeClient(), recorder=recorder)

    async def primary_stream(messages, stop=None, run_manager=None):
        raise RuntimeError("primary down")
        yield  # pragma: no cover

    async def fallback_stream(client, messages, stop=None, run_manager=None):
        yield _types.SimpleNamespace(usage_metadata=None)
        yield _types.SimpleNamespace(usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12})

    chunks = [chunk async for chunk in model._astream_chain([], None, None, primary_stream, fallback_stream)]
    assert len(chunks) == 2
    assert model.gateway_health.in_cooldown("primary.local")
    items = [recorder._queue.get_nowait() for _ in range(recorder._queue.qsize())]
    assert any(item["provider"] == "primary.local" and item["status"] == "error" for item in items)
    assert any(item["provider"] == "fallback.local" and item["status"] == "ok" and item["total_tokens"] == 12 for item in items)


async def test_astream_never_fails_over_mid_stream():
    """评审 F2：已产出 chunk 后失败必须上抛——备用流从头重放会重复拼接输出。"""
    import types as _types

    model, primary, fallback = build_model(FakeClient(), FakeClient())

    async def primary_stream(messages, stop=None, run_manager=None):
        yield _types.SimpleNamespace(usage_metadata=None)
        raise RuntimeError("mid-stream drop")

    async def fallback_stream(client, messages, stop=None, run_manager=None):
        yield _types.SimpleNamespace(usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    chunks = []
    with pytest.raises(RuntimeError, match="mid-stream drop"):
        async for chunk in model._astream_chain([], None, None, primary_stream, fallback_stream):
            chunks.append(chunk)
    assert len(chunks) == 1, "失败前的 chunk 保留，但不得出现备用重放"
    assert model.gateway_health.in_cooldown("primary.local")
    assert fallback.calls == 0, "中途失败禁止切换备用"


def test_usage_extraction_variants():
    assert _usage_from_result(None) == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    usage = _usage_from_result(FakeChatResult(total_tokens=50))
    assert usage == {"input_tokens": 20, "output_tokens": 30, "total_tokens": 50}
