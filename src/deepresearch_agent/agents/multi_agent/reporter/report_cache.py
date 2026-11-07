# 由 agents/multi_agent/reporter/base_reporter.py 拆分（T-014），方法体与原位置逐行一致。
"""报告缓存与阶段检查点（原 _load_cached_payload 等 9 个方法）。"""

from typing import List, Dict, Any, Optional, Iterable, Tuple
import json
import logging
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.reporter.outline_builder import (
    OutlineBuilder,
    ReportOutline,
    SectionOutline,
)
from deepresearch_agent.agents.multi_agent.reporter.consistency_checker import (
    ConsistencyChecker,
    ConsistencyCheckResult,
)
_LOGGER = logging.getLogger(__name__)
from deepresearch_agent.agents.multi_agent.reporter.report_schemas import SectionContent, ReportResult


class ReportCacheMixin:
    def _load_cached_payload(self, report_id: str) -> Optional[Dict[str, Any]]:
        if self._cache_manager is None:
            return None
        cached = self._cache_manager.get(report_id, skip_validation=True)
        if isinstance(cached, dict):
            return cached
        return None

    def _load_stage_checkpoint(self, report_id: str, stage: str) -> Optional[Dict[str, Any]]:
        if self._cache_manager is None:
            return None
        cached = self._cache_manager.get(
            f"report-stage:v1:{report_id}:{stage}", skip_validation=True,
        )
        return cached if isinstance(cached, dict) else None

    def _save_stage_checkpoint(self, report_id: str, stage: str, payload: Dict[str, Any]) -> None:
        if self._cache_manager is not None:
            self._cache_manager.set(f"report-stage:v1:{report_id}:{stage}", payload)

    def _load_section_checkpoints(
        self,
        report_id: str,
        outline: ReportOutline,
    ) -> Dict[str, Dict[str, Any]]:
        output: Dict[str, Dict[str, Any]] = {}
        for section in outline.sections:
            payload = self._load_stage_checkpoint(report_id, f"section:{section.section_id}")
            if payload:
                output[section.section_id] = payload
        return output

    def _build_section_cache_index(
        self,
        cached_payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        if not cached_payload:
            return {}
        index: Dict[str, Dict[str, Any]] = {}
        for item in cached_payload.get("sections", []):
            if isinstance(item, dict) and item.get("section_id"):
                index[item["section_id"]] = item
        return index

    def _can_reuse_section(
        self,
        section: SectionOutline,
        cached_section: Dict[str, Any],
        evidence_fingerprint: Dict[str, str],
    ) -> bool:
        if cached_section.get("title") != section.title:
            return False
        if cached_section.get("summary") != section.summary:
            return False
        cached_fp: Dict[str, str] = cached_section.get("evidence_fingerprint", {})  # type: ignore[assignment]
        for evidence_id in cached_section.get("used_evidence_ids", []):
            if evidence_fingerprint.get(evidence_id) != cached_fp.get(evidence_id):
                return False
        return True

    def _save_report_cache(
        self,
        report_id: str,
        evidence_fingerprint: Dict[str, str],
        report_result: ReportResult,
        outline: ReportOutline,
        evidence_map: Dict[str, RetrievalResult],
    ) -> None:
        if self._cache_manager is None:
            return

        outline_lookup = {section.section_id: section for section in outline.sections}
        sections_payload: List[Dict[str, Any]] = []
        for section in report_result.sections:
            outline_section = outline_lookup.get(section.section_id)
            summary = outline_section.summary if outline_section else ""
            sections_payload.append(
                {
                    "section_id": section.section_id,
                    "title": section.title,
                    "summary": summary,
                    "content": section.content,
                    "used_evidence_ids": list(section.used_evidence_ids),
                    "evidence_fingerprint": {
                        eid: evidence_fingerprint.get(eid)
                        for eid in section.used_evidence_ids
                        if eid in evidence_fingerprint
                    },
                }
            )

        payload = {
            "outline": report_result.outline.model_dump(mode="json"),
            "sections": sections_payload,
            "final_report": report_result.final_report,
            "references": report_result.references,
            "consistency_check": (
                report_result.consistency_check.model_dump(mode="json")
                if report_result.consistency_check
                else None
            ),
            "evidence_fingerprint": evidence_fingerprint,
            "evidence_ids": list(evidence_map.keys()),
            "evidence_cards": report_result.evidence_cards,
            "evidence_routing": report_result.evidence_routing,
            "evidence_digests": report_result.evidence_digests,
            "evidence_card_coverage": report_result.evidence_card_coverage,
            "report_metrics": report_result.report_metrics,
        }

        try:
            self._cache_manager.set(report_id, payload)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("写入报告缓存失败: %s", exc)

    def _deserialize_report_result(self, payload: Dict[str, Any]) -> ReportResult:
        outline = ReportOutline(**payload.get("outline", {}))
        sections = [
            SectionContent(
                section_id=item.get("section_id", ""),
                title=item.get("title", ""),
                content=item.get("content", ""),
                used_evidence_ids=list(item.get("used_evidence_ids", [])),
            )
            for item in payload.get("sections", [])
            if isinstance(item, dict)
        ]
        consistency = None
        if payload.get("consistency_check"):
            consistency = ConsistencyCheckResult(**payload["consistency_check"])

        return ReportResult(
            outline=outline,
            sections=sections,
            final_report=payload.get("final_report", ""),
            references=payload.get("references"),
            consistency_check=consistency,
            evidence_cards=list(payload.get("evidence_cards", [])),
            evidence_routing=dict(payload.get("evidence_routing", {})),
            evidence_digests=dict(payload.get("evidence_digests", {})),
            evidence_card_coverage=dict(payload.get("evidence_card_coverage", {})),
            report_metrics=dict(payload.get("report_metrics", {})),
        )

    def _update_state_report_context(
        self,
        state: PlanExecuteState,
        report_result: ReportResult,
        report_id: str,
        cache_hit: bool,
    ) -> None:
        """
        将报告结果写回PlanExecuteState的report_context
        """
        context = state.report_context
        if context is None:
            return

        context.report_type = report_result.outline.report_type
        context.outline = report_result.outline.model_dump()
        context.section_drafts = {
            section.section_id: section.content for section in report_result.sections
        }
        context.citations = []
        if report_result.references:
            context.citations.append({"formatted": report_result.references})
        context.consistency_check_results = (
            report_result.consistency_check.model_dump()
            if report_result.consistency_check
            else None
        )
        context.report_id = report_id
        context.cache_hit = cache_hit
        context.evidence_cards = list(report_result.evidence_cards)
        context.evidence_routing = dict(report_result.evidence_routing)
        context.evidence_digests = dict(report_result.evidence_digests)
        context.evidence_card_coverage = dict(report_result.evidence_card_coverage)
        context.report_metrics = dict(report_result.report_metrics)
