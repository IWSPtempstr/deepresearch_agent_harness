"""可观测性（P1-6/7）：指标注册/观测、no-op 追踪、/metrics 端点。"""

import pytest
from fastapi.testclient import TestClient

from deepresearch_agent.observability import (
    LLM_RATE_LIMITED_TOTAL, RUN_TOTAL, TraceIdLogFilter, current_trace_id,
    get_tracer, observe_llm_call, setup_tracing, tracing_enabled,
)
from deepresearch_agent.llm_gateway.gateway import ProviderHealth, SlotLimiter
from backend.app.main import create_app


def test_llm_call_observation_updates_counters():
    before = observe_llm_call.__globals__["LLM_CALLS_TOTAL"]._metrics.get(("prov-a", "ok"))
    observe_llm_call(provider="prov-a", status="ok", latency_ms=250, usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    observe_llm_call(provider="prov-a", status="error", latency_ms=40, usage={})
    metrics = observe_llm_call.__globals__["LLM_CALLS_TOTAL"]._metrics
    assert metrics[("prov-a", "ok")]._value.get() == (before._value.get() if before else 0) + 1
    assert metrics[("prov-a", "error")]._value.get() == 1
    tokens = observe_llm_call.__globals__["LLM_TOKENS_TOTAL"]._metrics
    assert tokens[("prov-a", "input")]._value.get() == 10
    assert tokens[("prov-a", "output")]._value.get() == 5


def test_rate_limited_counter_increments_on_timeout():
    limiter = SlotLimiter(max_concurrency=1, queue_timeout_seconds=0.2)
    before = LLM_RATE_LIMITED_TOTAL._value.get()
    with limiter.slot("k"):
        with pytest.raises(Exception):
            with limiter.slot("k"):
                pass
    assert LLM_RATE_LIMITED_TOTAL._value.get() == before + 1


def test_null_tracer_and_log_filter_are_noop():
    assert tracing_enabled() is False
    assert setup_tracing("test") is False
    tracer = get_tracer("test")
    with tracer.start_as_current_span("op") as span:
        span.set_attribute("k", "v")
    assert current_trace_id() is None
    import logging

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    assert TraceIdLogFilter().filter(record) is True
    assert record.trace_id == "-"


def test_provider_health_cooldown_cycle():
    health = ProviderHealth(base_cooldown_seconds=0.05, max_cooldown_seconds=0.1)
    health.report_failure("p")
    assert health.in_cooldown("p")
    health.report_success("p")
    assert not health.in_cooldown("p")
    assert not health.in_cooldown("unknown")


def test_metrics_endpoint_exposes_dra_metrics():
    app = create_app(workflow_factory=lambda context, events=None: None, auto_resume=False)
    with TestClient(app) as client:
        response = client.get("/api/v1/metrics")
        assert response.status_code == 200
        body = response.text
        for marker in ("dra_runs_total", "dra_llm_latency_seconds_bucket", "dra_active_runs"):
            assert marker in body, f"/metrics 缺少 {marker}"


def test_run_metrics_labeled_on_completed_run(tmp_path):
    """跑一个完成的 Run，/metrics 上 dra_runs_total 出现 completed 维度。"""
    from tests.harness.fake_driver import CompletedFakeDriver

    app = create_app(workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events), auto_resume=False)
    with TestClient(app) as client:
        session = client.post("/api/v1/sessions", json={"title": "metrics"}).json()
        message = client.post(f"/api/v1/sessions/{session['session_id']}/messages", json={
            "client_message_id": f"m-{session['session_id']}", "content": "指标验证",
            "source_mode": "graphrag", "workflow_mode": "deep_research",
        }).json()
        run_id = message["run_id"]
        import time

        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            status = client.get(f"/api/v1/runs/{run_id}").json().get("status")
            if status in {"completed", "failed"}:
                break
            time.sleep(0.2)
        assert status == "completed"
        body = client.get("/api/v1/metrics").text
        assert 'dra_runs_total{status="completed"}' in body
