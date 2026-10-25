"""学术科研情报 provider（P2-11）：OpenAlex 主源 + arXiv 副源，链式兜底。

- OpenAlex：免 key，polite pool 仅需 mailto 参数（OPENALEX_MAILTO）
- arXiv：免 key 的 Atom API；主源为空或失败时兜底，顺带演示同模式多 provider 组合
- 全部通过注入 httpx.AsyncClient(transport=...) 可离线测试
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.retrieval.base import SearchDepth, SearchFilters, ToolCallContext

OPENALEX_BASE = "https://api.openalex.org"
ARXIV_BASE = "https://export.arxiv.org/api/query"


def _inverted_abstract_to_text(inverted: Optional[dict[str, list[int]]]) -> str:
    """OpenAlex 的 abstract_inverted_index 还原为可读摘要（Evidence 用）。"""
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        positions.extend((index, word) for index in indexes)
    return " ".join(word for _, word in sorted(positions))


class OpenAlexProvider:
    mode = SourceMode.SCHOLAR
    provider_name = "openalex"
    strategies = {"scholar_search"}

    def __init__(self, *, mailto: str = "", client: Optional[httpx.AsyncClient] = None, base_url: str = OPENALEX_BASE):
        self.mailto = mailto
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        search_depth: SearchDepth,
        filters: SearchFilters,
        call_context: ToolCallContext,
    ) -> list[RetrievalResult]:
        params: dict[str, Any] = {
            "search": query,
            "per-page": max(1, min(int(top_k), 50)),
            "select": "id,doi,title,display_name,publication_date,primary_location,relevance_score,abstract_inverted_index,authorships",
        }
        if self.mailto:
            params["mailto"] = self.mailto
        if filters.start_date:
            params["filter"] = f"from_publication_date:{filters.start_date}"
        client = await self._get_client()
        try:
            response = await client.get(f"{self.base_url}/works", params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError(ErrorCode.RETRIEVAL_FAILED, f"OpenAlex 检索失败: {exc}", retryable=True) from exc
        works = (response.json() or {}).get("results", [])
        results: list[RetrievalResult] = []
        for index, work in enumerate(works[:top_k]):
            abstract = _inverted_abstract_to_text(work.get("abstract_inverted_index"))
            if not abstract:
                continue
            doi_url = work.get("doi") or ""
            primary = work.get("primary_location") or {}
            source_info = primary.get("source") or {}
            published = work.get("publication_date")
            authors = [a.get("author", {}).get("display_name", "") for a in (work.get("authorships") or [])[:3]]
            summary_parts = [f"论文《{work.get('display_name') or work.get('title') or 'Untitled'}》"]
            if authors:
                summary_parts.append(f"作者：{'、'.join(name for name in authors if name)}")
            if published:
                summary_parts.append(f"发表于 {published}")
            if source_info.get("display_name"):
                summary_parts.append(f"期刊/会议：{source_info['display_name']}")
            summary_parts.append(abstract[:1200])
            results.append(RetrievalResult(
                result_id=str(work.get("id") or f"openalex-{index}"),
                granularity="Chunk",
                evidence="\n".join(summary_parts),
                metadata=RetrievalMetadata(
                    source_id=str(work.get("id") or doi_url or f"openalex-{index}"),
                    source_type="webpage",
                    title=work.get("display_name") or work.get("title"),
                    url=doi_url or (primary.get("landing_page_url") or None),
                    domain="openalex.org",
                    published_at=(
                        datetime.fromisoformat(published).replace(tzinfo=timezone.utc)
                        if published else None
                    ),
                    confidence=min(0.95, 0.5 + float(work.get("relevance_score") or 0.0) / 1000.0),
                ),
                source=self.provider_name,
                source_mode=self.mode.value,
                score=max(0.1, min(1.0, float(work.get("relevance_score") or 0.0) / 100.0)) or 0.5,
            ))
        return results


ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


class ArxivProvider:
    mode = SourceMode.SCHOLAR
    provider_name = "arxiv"
    strategies = {"scholar_search"}

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None, base_url: str = ARXIV_BASE):
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        search_depth: SearchDepth,
        filters: SearchFilters,
        call_context: ToolCallContext,
    ) -> list[RetrievalResult]:
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max(1, min(int(top_k), 30)),
            "sortBy": "relevance",
        }
        client = await self._get_client()
        try:
            response = await client.get(self.base_url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError(ErrorCode.RETRIEVAL_FAILED, f"arXiv 检索失败: {exc}", retryable=True) from exc
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise AppError(ErrorCode.RETRIEVAL_FAILED, "arXiv 返回非 Atom XML", retryable=True) from exc

        results: list[RetrievalResult] = []
        for entry in root.findall("atom:entry", ARXIV_NS)[:top_k]:
            title = " ".join((entry.findtext("atom:title", "", ARXIV_NS) or "").split())
            abstract = " ".join((entry.findtext("atom:summary", "", ARXIV_NS) or "").split())
            entry_id = entry.findtext("atom:id", "", ARXIV_NS) or ""
            arxiv_id = entry_id.rsplit("/abs/", 1)[-1]
            published_text = entry.findtext("atom:published", "", ARXIV_NS)
            authors = [a.findtext("atom:name", "", ARXIV_NS) for a in entry.findall("atom:author", ARXIV_NS)][:3]
            if not abstract:
                continue
            summary_parts = [f"论文《{title}》"]
            if authors:
                summary_parts.append(f"作者：{'、'.join(name for name in authors if name)}")
            if published_text:
                summary_parts.append(f"发表于 {published_text[:10]}")
            summary_parts.append(abstract[:1200])
            results.append(RetrievalResult(
                result_id=f"arxiv:{arxiv_id}",
                granularity="Chunk",
                evidence="\n".join(summary_parts),
                metadata=RetrievalMetadata(
                    source_id=arxiv_id,
                    source_type="webpage",
                    title=title,
                    url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None,
                    domain="arxiv.org",
                    published_at=(
                        datetime.fromisoformat(published_text).astimezone(timezone.utc)
                        if published_text else None
                    ),
                    confidence=0.7,
                ),
                source=self.provider_name,
                source_mode=self.mode.value,
                score=0.6,
            ))
        return results


_DOI_HINT = re.compile(r"doi|10\.\d{4}", re.IGNORECASE)


class ChainedScholarProvider:
    """scholar 模式的对外门面：OpenAlex 优先，空结果或失败时回落 arXiv。"""

    mode = SourceMode.SCHOLAR
    provider_name = "scholar"
    strategies = {"scholar_search"}

    def __init__(self, providers: Optional[list[Any]] = None):
        self.providers = providers

    async def _resolve(self) -> list[Any]:
        if self.providers is None:
            from deepresearch_agent.config.settings import OPENALEX_MAILTO

            self.providers = [OpenAlexProvider(mailto=OPENALEX_MAILTO), ArxivProvider()]
        return self.providers

    async def aclose(self) -> None:
        for provider in (self.providers or []):
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        search_depth: SearchDepth,
        filters: SearchFilters,
        call_context: ToolCallContext,
    ) -> list[RetrievalResult]:
        last_error: Optional[Exception] = None
        for provider in await self._resolve():
            try:
                results = await provider.search(
                    query, top_k=top_k, search_depth=search_depth, filters=filters, call_context=call_context,
                )
            except AppError as exc:
                last_error = exc
                continue
            if results:
                return results
        if last_error is not None:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                "学术检索源全部不可用（OpenAlex/arXiv）",
                details={"reason": str(last_error)},
            )
        return []
