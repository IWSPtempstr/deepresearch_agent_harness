"""OpenTelemetry 轻量接入（P1-6）。

OTEL_ENABLED=true 且安装了 opentelemetry 时输出真实 trace（默认 console
exporter，OTEL_EXPORTER=otlp 留待部署侧接 Collector/Tempo）；否则全部为
no-op span，业务代码零感知。trace_id 可注入日志（TraceIdLogFilter）。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional


def tracing_enabled() -> bool:
    if os.getenv("OTEL_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        return False
    try:
        import opentelemetry  # noqa: F401
    except ImportError:
        return False
    return True


def setup_tracing(service_name: str = "deepresearch-agent") -> bool:
    """进程启动时调用一次；幂等。返回是否启用真实追踪。"""
    if not tracing_enabled():
        return False
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        return True  # 已初始化
    resource = Resource.create({"service.name": service_name})
    tracer_provider = TracerProvider(resource=resource)
    if os.getenv("OTEL_EXPORTER", "console").strip().lower() == "console":
        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tracer_provider)
    return True


class _NullSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def set_status(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *exc) -> None:
        pass


class NullTracer:
    def start_span(self, name: str, **kwargs) -> _NullSpan:
        return _NullSpan()

    @contextmanager
    def start_as_current_span(self, name: str, **kwargs) -> Iterator[_NullSpan]:
        yield _NullSpan()


_TRACERS: dict[str, Any] = {}


def get_tracer(name: str = "deepresearch"):
    if name not in _TRACERS:
        if tracing_enabled():
            from opentelemetry import trace

            _TRACERS[name] = trace.get_tracer(name)
        else:
            _TRACERS[name] = NullTracer()
    return _TRACERS[name]


def current_trace_id() -> Optional[str]:
    """当前 span 上下文的 trace_id（十六进制），无活动 span 时为 None。"""
    if not tracing_enabled():
        return None
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    if not context or not context.is_valid:
        return None
    return f"{context.trace_id:032x}"


class TraceIdLogFilter(logging.Filter):
    """给日志记录附加 trace_id 字段，配合 JSON formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id() or "-"
        return True
