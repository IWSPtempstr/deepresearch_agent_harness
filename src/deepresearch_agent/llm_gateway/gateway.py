"""failover / 限流排队 / 计量的传输层实现。"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Optional
from urllib.parse import urlparse

from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.models.get_models import PrefixAwareChatOpenAI
from deepresearch_agent.models.prefix_cache import get_current_run


class ProviderHealth:
    """连续失败进入指数退避冷却；成功即复位。进程内视图，各 worker 独立判断。"""

    def __init__(self, *, base_cooldown_seconds: float = 30.0, max_cooldown_seconds: float = 300.0):
        self.base_cooldown_seconds = base_cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def in_cooldown(self, label: str) -> bool:
        with self._lock:
            until = self._cooldown_until.get(label, 0.0)
            return time.monotonic() < until

    def report_failure(self, label: str) -> None:
        with self._lock:
            failures = self._failures.get(label, 0) + 1
            self._failures[label] = failures
            cooldown = min(self.max_cooldown_seconds, self.base_cooldown_seconds * (2 ** (failures - 1)))
            self._cooldown_until[label] = time.monotonic() + cooldown

    def report_success(self, label: str) -> None:
        with self._lock:
            self._failures.pop(label, None)
            self._cooldown_until.pop(label, None)


class LlmRateLimited(AppError):
    """排队超时的限流语义（对上呈现 429 语义，retryable）。"""

    def __init__(self, message: str, *, waited_seconds: float):
        super().__init__(ErrorCode.LLM_RATE_LIMITED, message, retryable=True, details={"waited_seconds": waited_seconds})


class SlotLimiter:
    """按租户键的并发槽限流：获取排队、超时抛 LlmRateLimited。

    P1 为进程内实现（单 worker 验收场景足够）；P2 多租户接入后可替换为
    Redis 分布式信号量，接口不变。
    """

    def __init__(self, *, max_concurrency: int = 8, queue_timeout_seconds: float = 30.0):
        self.max_concurrency = max(1, max_concurrency)
        self.queue_timeout_seconds = queue_timeout_seconds
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)

    @contextmanager
    def slot(self, key: str = "default"):
        started = time.monotonic()
        with self._condition:
            while self._counts.get(key, 0) >= self.max_concurrency:
                remaining = self.queue_timeout_seconds - (time.monotonic() - started)
                if remaining <= 0 or not self._condition.wait(timeout=max(0.05, remaining)):
                    if self._counts.get(key, 0) >= self.max_concurrency:
                        raise LlmRateLimited(
                            f"LLM 网关排队超时（{key} 并发上限 {self.max_concurrency}）",
                            waited_seconds=time.monotonic() - started,
                        )
            self._counts[key] = self._counts.get(key, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                remaining = self._counts.get(key, 1) - 1
                if remaining <= 0:
                    self._counts.pop(key, None)
                else:
                    self._counts[key] = remaining
                self._condition.notify_all()


_default_limiter: Optional[SlotLimiter] = None


def default_limiter() -> SlotLimiter:
    global _default_limiter
    if _default_limiter is None:
        from deepresearch_agent.config import settings

        _default_limiter = SlotLimiter(
            max_concurrency=settings.LLM_MAX_CONCURRENT_CALLS,
            queue_timeout_seconds=settings.LLM_QUEUE_TIMEOUT_SECONDS,
        )
    return _default_limiter


def _provider_host(base_url: Optional[str]) -> str:
    if not base_url:
        return "primary"
    host = urlparse(str(base_url)).hostname or str(base_url)
    return host


def _usage_from_result(result: Any) -> dict[str, int]:
    """从 ChatResult 各代 langchain 版本可能的位置提取 token 用量。"""
    usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if result is None:
        return usage
    candidates = []
    llm_output = getattr(result, "llm_output", None)
    if isinstance(llm_output, dict):
        candidates.append(llm_output.get("token_usage") or llm_output.get("usage") or llm_output)
    generations = getattr(result, "generations", None) or []
    if generations:
        message = getattr(generations[0], "message", None)
        if message is not None:
            usage_metadata = getattr(message, "usage_metadata", None)
            if isinstance(usage_metadata, dict):
                candidates.append(usage_metadata)
            response_metadata = getattr(message, "response_metadata", None)
            if isinstance(response_metadata, dict):
                token_usage = response_metadata.get("token_usage") or response_metadata.get("usage")
                if isinstance(token_usage, dict):
                    candidates.append(token_usage)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        values = {
            "input_tokens": candidate.get("input_tokens") or candidate.get("prompt_tokens") or 0,
            "output_tokens": candidate.get("output_tokens") or candidate.get("completion_tokens") or 0,
            "total_tokens": candidate.get("total_tokens") or 0,
        }
        if any(int(v) for v in values.values()):
            if not values["total_tokens"]:
                values["total_tokens"] = int(values["input_tokens"]) + int(values["output_tokens"])
            return {key: int(value) for key, value in values.items()}
    return usage


class FailoverChatOpenAI(PrefixAwareChatOpenAI):
    """在传输层做 provider 链式切换：主失败 → 按健康度退避 → 备用。

    bind_tools / with_structured_output 等派生 Runnable 最终都会回到
    _generate/_agenerate，因此全部路径共享同一套 failover、限流与计量。
    """

    def __init__(
        self,
        *,
        fallbacks: Optional[list[tuple[str, Any]]] = None,
        limiter: Optional[SlotLimiter] = None,
        health: Optional[ProviderHealth] = None,
        recorder: Any = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        # ChatOpenAI 是 pydantic 模型，拒绝未声明字段；网关内部控制件用
        # object.__setattr__ 存入实例字典，不参与序列化
        object.__setattr__(self, "gateway_fallbacks", list(fallbacks or []))
        object.__setattr__(self, "gateway_limiter", limiter)
        object.__setattr__(self, "gateway_health", health or ProviderHealth())
        object.__setattr__(self, "gateway_recorder", recorder)
        object.__setattr__(self, "provider_label", _provider_host(kwargs.get("base_url")))

    # ---- 内部工具 -------------------------------------------------

    def _provider_chain(self) -> list[tuple[str, Any]]:
        chain = [(self.provider_label, self)]
        chain.extend((label, client) for label, client in self.gateway_fallbacks if not self.gateway_health.in_cooldown(label))
        if self.gateway_health.in_cooldown(self.provider_label) and len(chain) > 1:
            # 主 provider 冷却中且有备用可用：跳过主，直接走备用
            chain = chain[1:]
        return chain

    def _meter(self, label: str, result: Any, latency_ms: int, error_code: Optional[str]) -> None:
        if self.gateway_recorder is None:
            return
        usage = _usage_from_result(result)
        try:
            self.gateway_recorder.record(
                provider=label,
                model=str(self.model_name or ""),
                run_id=get_current_run(),
                usage=usage,
                latency_ms=latency_ms,
                status="error" if error_code else "ok",
                error_code=error_code,
            )
        except Exception:
            # 计量失败绝不影响模型调用主路径
            pass

    def _attempt(self, label: str, client: Any, invoke_primary, invoke_client):
        started = time.monotonic()
        try:
            result = invoke_primary() if client is self else invoke_client(client)
        except Exception as exc:
            self.gateway_health.report_failure(label)
            self._meter(label, None, int((time.monotonic() - started) * 1000), type(exc).__name__)
            return None, exc
        self.gateway_health.report_success(label)
        self._meter(label, result, int((time.monotonic() - started) * 1000), None)
        return result, None

    def _run_chain(self, invoke_primary, invoke_client):
        """同步调用链：限流排队 → 按健康度逐 provider 尝试。"""
        limiter = self.gateway_limiter
        if limiter is None:
            return self._run_chain_inner(invoke_primary, invoke_client)
        with limiter.slot("default"):
            return self._run_chain_inner(invoke_primary, invoke_client)

    def _run_chain_inner(self, invoke_primary, invoke_client):
        last_error: Optional[Exception] = None
        for label, client in self._provider_chain():
            result, error = self._attempt(label, client, invoke_primary, invoke_client)
            if error is None:
                return result
            last_error = error
        raise AppError(
            ErrorCode.LLM_ALL_PROVIDERS_FAILED,
            f"LLM 网关所有 provider 均不可用（{len(self.gateway_fallbacks) + 1} 个）: {last_error}",
        ) from last_error

    # ---- 传输层覆写（所有派生 Runnable 的汇聚点） ------------------

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._run_chain(
            lambda: super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs),
            lambda client: client._generate(messages, stop=stop, run_manager=run_manager, **kwargs),
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        import asyncio

        limiter = self.gateway_limiter
        if limiter is None:
            return await self._agenerate_chain(messages, stop, run_manager, **kwargs)
        # 同步限流器（threading.Condition）通过 to_thread 复用到异步路径；
        # 槽位上下文管理器必须先构造再跨线程 enter/exit，保证释放的是同一个槽
        slot = limiter.slot("default")
        await asyncio.to_thread(slot.__enter__)
        try:
            return await self._agenerate_chain(messages, stop, run_manager, **kwargs)
        finally:
            await asyncio.to_thread(slot.__exit__, None, None, None)

    async def _agenerate_chain(self, messages, stop, run_manager, **kwargs):
        last_error: Optional[Exception] = None
        for label, client in self._provider_chain():
            started = time.monotonic()
            try:
                if client is self:
                    result = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
                else:
                    result = await client._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
            except Exception as exc:
                self.gateway_health.report_failure(label)
                self._meter(label, None, int((time.monotonic() - started) * 1000), type(exc).__name__)
                last_error = exc
                continue
            self.gateway_health.report_success(label)
            self._meter(label, result, int((time.monotonic() - started) * 1000), None)
            return result
        raise AppError(
            ErrorCode.LLM_ALL_PROVIDERS_FAILED,
            f"LLM 网关所有 provider 均不可用（{len(self.gateway_fallbacks) + 1} 个）: {last_error}",
        ) from last_error


def build_failover_llm(primary_config: dict[str, Any], fallback_configs: list[dict[str, Any]], *, limiter=None, health=None, recorder=None) -> FailoverChatOpenAI:
    """按主/备配置构造网关模型；fallback 客户端只走传输层，无需网关再包一层。"""
    fallbacks: list[tuple[str, Any]] = []
    for index, config in enumerate(fallback_configs):
        label = _provider_host(config.get("base_url")) or f"fallback-{index}"
        fallbacks.append((label, PrefixAwareChatOpenAI(**config)))
    return FailoverChatOpenAI(fallbacks=fallbacks, limiter=limiter, health=health, recorder=recorder, **primary_config)
