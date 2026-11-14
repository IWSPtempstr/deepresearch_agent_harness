"""S1：web 聚合通道——同一 `source_mode='web'` 下路由多个检索 API。

设计（docs/specs/coreact-consolidation/source-aggregation-and-multisource.md）：

- `source_mode` 是冻结**通道**，`provider` 是通道内的具体 API；
- `web_search` → Tavily；`scholar_search` → OpenAlex/arXiv（ChainedScholarProvider）；
- 子 provider 返回的证据统一改写为 `source_mode='web'`，保留 API 归属
  （metadata.extra.provider / provider_label_for），使 `source_match`、证据台账
  与预算计数仍能区分 Tavily 与学术 API；
- 未配置的 API 不静默换通道：返回 `SOURCE_UNAVAILABLE`。
"""

from __future__ import annotations

from typing import Any

from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.retrieval.base import (
    RetrievalProvider,
    SearchDepth,
    SearchFilters,
    ToolCallContext,
)

SCHOLAR_TOOL = "scholar_search"
TAVILY_TOOLS = frozenset({"web_search", "tavily_search"})


class AggregateWebProvider:
    """把 web 通道内的多个 API 组合成单一 RetrievalProvider。"""

    mode = SourceMode.WEB
    provider_name = "web"

    def __init__(self, *, tavily: RetrievalProvider | None, scholar: RetrievalProvider):
        self._tavily = tavily
        self._scholar = scholar

    @staticmethod
    def provider_label_for(tool_name: str) -> str:
        """纯函数：任务类型 → 证据 provider 归属（线程安全，供 executor 使用）。"""
        return "scholar" if str(tool_name) == SCHOLAR_TOOL else "tavily"

    def _select(self, tool_name: str) -> RetrievalProvider:
        if str(tool_name) == SCHOLAR_TOOL:
            return self._scholar
        if str(tool_name) in TAVILY_TOOLS:
            if self._tavily is None:
                raise AppError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    "web 通道的 Tavily API 未配置（TAVILY_API_KEY 缺失）",
                    details={"provider": "tavily", "tool": str(tool_name)},
                )
            return self._tavily
        raise AppError(
            ErrorCode.SOURCE_POLICY_VIOLATION,
            f"web 聚合通道不支持工具 {tool_name!r}",
            details={"tool": str(tool_name)},
        )

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        search_depth: SearchDepth,
        filters: SearchFilters,
        call_context: ToolCallContext,
    ) -> list[Any]:
        tool_name = str(filters.strategy or "web_search")
        provider = self._select(tool_name)
        results = await provider.search(
            query, top_k=top_k, search_depth=search_depth, filters=filters,
            call_context=call_context,
        )
        label = self.provider_label_for(tool_name)
        for result in results:
            # 通道统一为 web；具体 API 留在 provider/metadata，不伪造单一来源。
            result.source_mode = SourceMode.WEB.value
            result.metadata.extra["provider"] = label
            result.metadata.extra["channel"] = SourceMode.WEB.value
        return results

    async def aclose(self) -> None:
        for provider in (self._tavily, self._scholar):
            if provider is None:
                continue
            inner = getattr(provider, "_instance", None) or provider
            close = getattr(inner, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:  # noqa: BLE001
                continue


__all__ = ["AggregateWebProvider", "SCHOLAR_TOOL", "TAVILY_TOOLS"]
