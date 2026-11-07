# 由 agents/multi_agent/reporter/base_reporter.py 拆分（T-014），方法体与原位置逐行一致。
"""MapReduce 分节路径（原 _generate_sections_mapreduce 等 5 个方法）。"""

from typing import List, Dict, Any, Optional, Iterable, Tuple
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
from deepresearch_agent.agents.multi_agent.reporter.outline_builder import (
    OutlineBuilder,
    ReportOutline,
    SectionOutline,
)
from deepresearch_agent.agents.multi_agent.reporter.mapreduce import (
    EvidenceMapper,
    EvidenceSummary,
    SectionReducer,
    ReduceStrategy,
    ReportAssembler,
)
_LOGGER = logging.getLogger(__name__)
from deepresearch_agent.agents.multi_agent.reporter.report_schemas import SectionContent


class MapReduceSectionsMixin:
    def _should_use_mapreduce(
        self,
        evidence_map: Dict[str, RetrievalResult],
    ) -> bool:
        """
        判断是否启用Map-Reduce模式。
        """
        if not self.config.enable_mapreduce:
            return False
        return len(evidence_map) >= self.config.mapreduce_evidence_threshold

    def _generate_sections_mapreduce(
        self,
        outline: ReportOutline,
        evidence_map: Dict[str, RetrievalResult],
        section_cache_index: Dict[str, Dict[str, Any]],
        evidence_fingerprint: Dict[str, str],
        fallback_evidence_ids: List[str],
    ) -> Tuple[List[SectionContent], List[str]]:
        self._ensure_mapreduce_components()
        assert self._evidence_mapper is not None
        assert self._section_reducer is not None

        section_contents: List[SectionContent] = []
        all_used_ids: List[str] = []

        for section in outline.sections:
            cached_section = section_cache_index.get(section.section_id)
            if cached_section and self._can_reuse_section(
                section,
                cached_section,
                evidence_fingerprint,
            ):
                sanitized_content = self._sanitize_section_text(
                    section.title,
                    cached_section["content"],
                )
                sanitized_content, normalized_ids = self._normalize_section_references(
                    sanitized_content,
                    cached_section["used_evidence_ids"],
                    evidence_map,
                )
                section_contents.append(
                    SectionContent(
                        section_id=section.section_id,
                        title=section.title,
                        content=sanitized_content,
                        used_evidence_ids=normalized_ids,
                    )
                )
                all_used_ids.extend(normalized_ids)
                continue

            evidence_entries = self._collect_section_evidence(
                section,
                evidence_map,
                fallback_evidence_ids,
            )
            if not evidence_entries:
                section_contents.append(
                    SectionContent(
                        section_id=section.section_id,
                        title=section.title,
                        content="",
                        used_evidence_ids=[],
                    )
                )
                continue
            evidence_batches = self._evidence_mapper.split_batches(evidence_entries)
            evidence_summaries = self._map_evidence_batches(evidence_batches, section)

            section_text = self._section_reducer.reduce(
                evidence_summaries,
                section,
                max_tokens=self.config.max_tokens_per_reduce,
            )

            used_ids: List[str] = []
            for summary in evidence_summaries:
                for evidence_id in summary.evidence_ids:
                    if evidence_id not in used_ids:
                        used_ids.append(evidence_id)

            sanitized_content = self._sanitize_section_text(section.title, section_text.strip())
            sanitized_content, normalized_ids = self._normalize_section_references(
                sanitized_content,
                used_ids,
                evidence_map,
            )

            section_contents.append(
                SectionContent(
                    section_id=section.section_id,
                    title=section.title,
                    content=sanitized_content,
                    used_evidence_ids=normalized_ids,
                )
            )
            all_used_ids.extend(normalized_ids)

        return section_contents, all_used_ids

    def _map_evidence_batches(
        self,
        evidence_batches: List[List[RetrievalResult]],
        section: SectionOutline,
    ) -> List[EvidenceSummary]:
        assert self._evidence_mapper is not None

        if not evidence_batches:
            return []

        if (
            self.config.enable_parallel_map
            and len(evidence_batches) > 1
        ):
            max_workers = min(len(evidence_batches), 4)
            try:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {
                        executor.submit(
                            self._evidence_mapper.map_evidence_batch,
                            batch,
                            section,
                            batch_index=index,
                        ): index
                        for index, batch in enumerate(evidence_batches)
                    }
                    results: List[Optional[EvidenceSummary]] = [None] * len(evidence_batches)
                    for future in as_completed(future_map):
                        idx = future_map[future]
                        try:
                            results[idx] = future.result()
                        except Exception as exc:  # noqa: BLE001
                            _LOGGER.warning("并行证据映射失败，索引%d: %s", idx, exc)
                            results[idx] = None

                fallback_indices = [idx for idx, res in enumerate(results) if res is None]
                for idx in fallback_indices:
                    results[idx] = self._evidence_mapper.map_evidence_batch(
                        evidence_batches[idx],
                        section,
                        batch_index=idx,
                    )
                return [res for res in results if res is not None]
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("并行Map阶段异常，改为串行执行: %s", exc)

        return [
            self._evidence_mapper.map_evidence_batch(
                batch,
                section,
                batch_index=index,
            )
            for index, batch in enumerate(evidence_batches)
        ]

    def _assemble_report_mapreduce(
        self,
        outline: ReportOutline,
        section_contents: List[SectionContent],
        *,
        query: str,
        evidence_count: int,
    ) -> str:
        self._ensure_mapreduce_components()
        assert self._report_assembler is not None
        section_payload = {
            section.section_id: section.content
            for section in section_contents
        }
        return self._report_assembler.assemble(
            outline,
            section_payload,
            global_context={
                "query": query,
                "evidence_count": evidence_count,
            },
        )

    def _ensure_mapreduce_components(self) -> None:
        if self._evidence_mapper is None:
            self._evidence_mapper = EvidenceMapper(
                max_evidence_per_batch=self.config.section_writer.max_evidence_per_call,
            )
        if self._section_reducer is None:
            self._section_reducer = SectionReducer(
                strategy=self.config.reduce_strategy,
            )
        if self._report_assembler is None:
            self._report_assembler = ReportAssembler()
