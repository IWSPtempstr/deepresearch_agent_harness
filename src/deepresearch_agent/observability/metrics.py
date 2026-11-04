"""Prometheus 指标定义（P1-7）。

所有指标以 dra_ 前缀注册在默认 REGISTRY，由 API 进程的 /metrics 端点暴露；
worker 进程的指标经各自 /metrics（uvicorn 不在 worker 里，暂以 push 方式留待
P2 接入 Pushgateway，单机演示以 API 进程指标为主）。
"""

from prometheus_client import Counter, Gauge, Histogram

# ---- Run 生命周期 ----------------------------------------------------------
RUN_TOTAL = Counter("dra_runs_total", "按终态计数的 Run 总量", ["status"])
RUN_DURATION_SECONDS = Histogram(
    "dra_run_duration_seconds", "Run 端到端耗时（秒）",
    buckets=(5, 15, 30, 60, 120, 300, 600, 1200, 1800, float("inf")),
)
ACTIVE_RUNS = Gauge("dra_active_runs", "当前执行中的 Run 数（进程内视图）")
WAITING_RUNS = Gauge("dra_waiting_runs", "queued 状态 Run 数（worker 扫描轮次统计 DB 行数）")
QUEUE_DEPTH = Gauge("dra_queue_depth", "执行队列积压深度")

# ---- LLM 网关 --------------------------------------------------------------
LLM_CALLS_TOTAL = Counter("dra_llm_calls_total", "LLM 网关出站调用数", ["provider", "status"])
LLM_LATENCY_SECONDS = Histogram(
    "dra_llm_latency_seconds", "LLM 网关单次调用时延（秒）", ["provider"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
LLM_TOKENS_TOTAL = Counter("dra_llm_tokens_total", "token 消耗", ["provider", "direction"])
LLM_RATE_LIMITED_TOTAL = Counter("dra_llm_rate_limited_total", "限流排队超时次数")
EVENT_FANOUT_FAILURES = Counter("dra_event_fanout_failures_total", "事件扇出失败次数（Redis 不可用等，durable 落库不受影响）")
WORKER_RUN_FAILURES = Counter("dra_worker_run_failures_total", "worker 执行入口异常次数（Run 状态已落库，此为排障现场计数）")
QUEUE_ENQUEUE_FAILURES = Counter(
    "dra_queue_enqueue_failures_total",
    "唤醒信号入队失败次数（重试耗尽后；run 留在 queued，由租约扫描重建投递）",
)
WORKER_CONSUME_RECONNECTS = Counter(
    "dra_worker_consume_reconnects_total",
    "worker 消费流断连后重建生成器的次数（Redis/Kafka 断连自愈）",
)


def observe_llm_call(*, provider: str, status: str, latency_ms: int, usage: dict | None) -> None:
    LLM_CALLS_TOTAL.labels(provider=provider, status=status).inc()
    LLM_LATENCY_SECONDS.labels(provider=provider).observe(max(0.0, latency_ms) / 1000.0)
    usage = usage or {}
    if int(usage.get("input_tokens") or 0):
        LLM_TOKENS_TOTAL.labels(provider=provider, direction="input").inc(int(usage["input_tokens"]))
    if int(usage.get("output_tokens") or 0):
        LLM_TOKENS_TOTAL.labels(provider=provider, direction="output").inc(int(usage["output_tokens"]))
