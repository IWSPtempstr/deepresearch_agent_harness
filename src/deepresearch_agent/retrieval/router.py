"""Strict run-level provider selection without cross-source fallback."""

from __future__ import annotations

import threading
from typing import Callable, Mapping

from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.retrieval.base import RetrievalProvider, ToolCallContext


class RetrievalRouter:
    def __init__(self, providers: Mapping[SourceMode | str, RetrievalProvider]):
        self._providers = {SourceMode(key): provider for key, provider in providers.items()}
        for mode, provider in self._providers.items():
            if provider.mode != mode:
                raise ValueError(f"Provider {provider.provider_name} 的 mode 与路由键不一致")

    def for_mode(self, source_mode: SourceMode | str) -> RetrievalProvider:
        try:
            mode = SourceMode(source_mode)
        except ValueError as exc:
            raise AppError(ErrorCode.INVALID_SOURCE_MODE, f"不支持的信息源: {source_mode}") from exc
        provider = self._providers.get(mode)
        if provider is None:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                f"信息源 {mode.value} 未配置",
                details={"source_mode": mode.value},
            )
        return provider

    async def aclose(self) -> None:
        """T-006：关停时释放各 provider 持有的资源（如 scholar httpx 客户端）。"""
        for provider in self._providers.values():
            inner = getattr(provider, "_instance", None) or provider
            close = getattr(inner, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                continue

    def for_modes(self, sources) -> RetrievalProvider:
        """S3：按 Run 冻结的来源集合返回组合 provider。

        单来源 Run 原样返回该通道 provider（legacy 语义不变）；多来源返回
        `CompositeRetrievalProvider`，按工具所属通道派发（不跨通道 fallback）。
        集合中任一通道未配置 → SOURCE_UNAVAILABLE（fail fast，不产出注定
        失败的 Run）。
        """
        from deepresearch_agent.harness.policies import SourcePolicy

        requested: list[SourceMode] = []
        for item in sources:
            try:
                mode = SourceMode(item)
            except ValueError as exc:
                raise AppError(
                    ErrorCode.INVALID_SOURCE_MODE, f"不支持的信息源: {item!r}",
                ) from exc
            if mode not in requested:
                requested.append(mode)
        if not requested:
            raise AppError(ErrorCode.SOURCE_UNAVAILABLE, "来源集合为空")
        missing = [mode for mode in requested if mode not in self._providers]
        if missing:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                f"冻结来源中包含未配置的通道: {[mode.value for mode in missing]}",
                details={"missing_sources": [mode.value for mode in missing]},
            )
        if len(requested) == 1:
            return self._providers[requested[0]]
        primary = requested[0]
        return CompositeRetrievalProvider(
            members={mode: self._providers[mode] for mode in requested},
            primary=primary,
            tool_source={tool: mode for tool, mode in SourcePolicy.TOOL_SOURCE.items()},
        )


class CompositeRetrievalProvider:
    """多来源 Run 的组合 provider：按工具所属通道派发，不跨通道 fallback。

    - `mode` = 主通道（供 legacy 读取）；`sources` 暴露冻结集合；
    - `search` 以 `filters.strategy`（= 任务类型/工具名）反查通道；
      未配置该工具的通道 → SOURCE_UNAVAILABLE；
    - 结果不做 source_mode 改写：各通道 provider 本就返回其真实通道。
    """

    def __init__(
        self,
        *,
        members: Mapping[SourceMode, RetrievalProvider],
        primary: SourceMode,
        tool_source: Mapping[str, SourceMode],
    ):
        self.members = dict(members)
        self.mode = primary
        self.sources = tuple(members.keys())
        self.provider_name = "composite"
        self._tool_source = dict(tool_source)

    def channel_for(self, tool_name: str) -> SourceMode:
        mode = self._tool_source.get(str(tool_name))
        if mode is None or mode not in self.members:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                f"工具 {tool_name!r} 不属于本次 Run 冻结的来源集合",
                details={"tool": str(tool_name), "sources": sorted(m.value for m in self.members)},
            )
        return mode

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        search_depth,
        filters,
        call_context,
    ):
        tool_name = str(getattr(filters, "strategy", "") or "")
        channel = self.channel_for(tool_name)
        provider = self.members[channel]
        scoped_context = (
            call_context
            if getattr(call_context, "source_mode", None) == channel
            else ToolCallContext(
                run_id=call_context.run_id, source_mode=channel,
                task_id=call_context.task_id, tool_call_id=call_context.tool_call_id,
            )
        )
        return await provider.search(
            query, top_k=top_k, search_depth=search_depth,
            filters=filters, call_context=scoped_context,
        )

    async def aclose(self) -> None:
        for provider in self.members.values():
            inner = getattr(provider, "_instance", None) or provider
            close = getattr(inner, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                continue


class _LazyProvider:
    """Delay provider imports and external-service initialization until selected."""

    def __init__(
        self,
        *,
        mode: SourceMode,
        provider_name: str,
        factory: Callable[[], RetrievalProvider],
    ) -> None:
        self.mode = mode
        self.provider_name = provider_name
        self._factory = factory
        self._instance: RetrievalProvider | None = None
        # 并行动作可能在不同线程同时触发首次初始化；双检锁保证只构造一次。
        self._lock = threading.Lock()

    def _get(self) -> RetrievalProvider:
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = self._factory()
        return self._instance

    async def search(self, *args, **kwargs):
        return await self._get().search(*args, **kwargs)


__all__ = ["RetrievalRouter", "CompositeRetrievalProvider"]


def create_default_router() -> RetrievalRouter:
    """Build lazy GraphRAG and configured Tavily providers for real entry points."""
    from deepresearch_agent.config.settings import (
        ARTIFACT_ROOT,
        TAVILY_API_KEY,
        TAVILY_CACHE_TTL_SECONDS,
        TAVILY_MAX_RESULTS,
        TAVILY_SEARCH_DEPTH,
        TAVILY_TIMEOUT_SECONDS,
    )
    def build_graphrag() -> RetrievalProvider:
        from deepresearch_agent.retrieval.graphrag_provider import GraphRAGProvider
        return GraphRAGProvider()

    providers: dict[SourceMode, RetrievalProvider] = {
        SourceMode.GRAPHRAG: _LazyProvider(
            mode=SourceMode.GRAPHRAG,
            provider_name="graphrag",
            factory=build_graphrag,
        )
    }
    def build_tavily() -> RetrievalProvider:
        from deepresearch_agent.persistence.artifact_store import ArtifactStore
        from deepresearch_agent.retrieval.tavily_provider import TavilyProvider
        return TavilyProvider(
            api_key=TAVILY_API_KEY,
            artifact_store=ArtifactStore(ARTIFACT_ROOT),
            default_depth=TAVILY_SEARCH_DEPTH,  # type: ignore[arg-type]
            default_max_results=TAVILY_MAX_RESULTS,
            timeout_seconds=TAVILY_TIMEOUT_SECONDS,
            cache_ttl_seconds=TAVILY_CACHE_TTL_SECONDS,
        )

    # P2-11：学术科研情报（OpenAlex 主源 + arXiv 兜底），免 key 常开
    def build_scholar() -> RetrievalProvider:
        from deepresearch_agent.retrieval.scholar_provider import ChainedScholarProvider
        return ChainedScholarProvider()

    # S1：web 是聚合通道——Tavily 与学术 API 同属 source_mode='web'。
    # Tavily 未配置时仍保留 scholar_search；web_search 会在派发时报
    # SOURCE_UNAVAILABLE，不静默换通道。
    def build_web() -> RetrievalProvider:
        from deepresearch_agent.retrieval.web_aggregate import AggregateWebProvider

        return AggregateWebProvider(
            tavily=build_tavily() if TAVILY_API_KEY else None,
            scholar=build_scholar(),
        )

    providers[SourceMode.WEB] = _LazyProvider(
        mode=SourceMode.WEB,
        provider_name="web",
        factory=build_web,
    )
    # 只读兼容：历史 scholar Run 恢复时仍使用独立通道 provider；新请求在
    # API 边界归一化为 web（不再创建 scholar Run）。
    providers[SourceMode.SCHOLAR] = _LazyProvider(
        mode=SourceMode.SCHOLAR,
        provider_name="scholar",
        factory=build_scholar,
    )
    return RetrievalRouter(providers)


__all__.append("create_default_router")
