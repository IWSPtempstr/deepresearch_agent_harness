# 由 agents/multi_agent/reporter/base_reporter.py 拆分（T-014），方法体与原位置逐行一致。
"""Evidence Card 路径：证据收集/缓存/分节写作（原 _generate_sections_from_cards 等 8 个方法）。"""

from typing import List, Dict, Any, Optional, Iterable, Tuple
import json
import logging
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.reporter.outline_builder import (
    OutlineBuilder,
    ReportOutline,
    SectionOutline,
)
from deepresearch_agent.agents.multi_agent.reporter.evidence_cards import (
    EvidenceCard,
    EvidenceCardPipeline,
)
from deepresearch_agent.agents.multi_agent.tools.evidence_tracker import EvidenceTracker
from deepresearch_agent.harness.report_safety import add_inline_citations, has_internal_material
_LOGGER = logging.getLogger(__name__)
from deepresearch_agent.agents.multi_agent.reporter.report_schemas import SectionContent


def _extract_tracker_from_state(state: PlanExecuteState) -> Optional[EvidenceTracker]:
    if state.execution_context is None:
        return None
    registry = state.execution_context.evidence_registry.get("tracker", {})
    tracker = registry.get("_instance")
    if isinstance(tracker, EvidenceTracker):
        return tracker
    tracker_state = registry.get("state")
    if isinstance(tracker_state, dict):
        return EvidenceTracker(tracker_state)  # type: ignore[arg-type]
    return None

class CardSectionsMixin:
    def _collect_evidence(
        self,
        state: PlanExecuteState,
        execution_records: Iterable[ExecutionRecord],
    ) -> Dict[str, RetrievalResult]:
        """从执行记录与执行上下文中聚合标准化的RetrievalResult"""

        evidence_map: Dict[str, RetrievalResult] = {}

        tracker = _extract_tracker_from_state(state)
        if tracker is not None:
            for result in tracker.all_results():
                evidence_map[result.result_id] = result

        for record in execution_records:
            for item in record.evidence:
                try:
                    if isinstance(item, RetrievalResult):
                        result = item
                    elif isinstance(item, dict):
                        result = RetrievalResult.from_dict(item)
                    else:
                        continue
                    evidence_map[result.result_id] = result
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("忽略无法解析的证据: %s error=%s", item, exc)

        return evidence_map

    def _build_plan_summary(self, plan: PlanSpec) -> str:
        """
        根据PlanSpec生成用于提示纲要的任务摘要
        """
        lines = [f"计划ID: {plan.plan_id}, 版本: {plan.version}, 状态: {plan.status}"]
        for node in plan.task_graph.nodes:
            lines.append(
                f"- {node.task_id} | 类型:{node.task_type} | 状态:{node.status} | "
                f"优先级:{node.priority} | 描述:{node.description}"
            )
        return "\n".join(lines)

    def _build_evidence_summary(
        self,
        evidence_map: Dict[str, RetrievalResult],
    ) -> Tuple[str, List[str]]:
        """
        构建用于纲要生成的证据摘要，限制最大条数，防止Prompt过长
        """
        lines: List[str] = []
        limited_ids: List[str] = []
        for idx, result in enumerate(evidence_map.values()):
            if idx >= self.config.max_evidence_summary:
                lines.append("...（其余证据省略）")
                break
            snippet = ""
            if isinstance(result.evidence, str):
                snippet = result.evidence[:160].replace("\n", " ")
            elif isinstance(result.evidence, dict):
                snippet = str({k: result.evidence[k] for k in list(result.evidence.keys())[:4]})
            line = f"{result.result_id} | {result.granularity} | {result.source} | {snippet}"
            lines.append(line)
            limited_ids.append(result.result_id)
        summary_text = "\n".join(lines) if lines else "无结构化证据"
        return summary_text, limited_ids

    @staticmethod
    def _build_card_outline_summary(cards: List[EvidenceCard]) -> str:
        """Expose every card to outline planning with a deliberately small preview."""
        if not cards:
            return "无结构化证据"
        return "\n".join(
            f"{card.evidence_id} | 主题:{','.join(card.topics[:4])} | {card.summary[:160]}"
            for card in cards
        )

    def _load_evidence_card_cache(
        self,
        evidence_map: Dict[str, RetrievalResult],
    ) -> Dict[str, Dict[str, Any]]:
        if self._cache_manager is None:
            return {}
        cached: Dict[str, Dict[str, Any]] = {}
        for result in evidence_map.values():
            content_hash = result.metadata.content_hash
            if not content_hash:
                continue
            payload = self._cache_manager.get(
                f"evidence-card:v1:{content_hash}", skip_validation=True,
            )
            if isinstance(payload, dict):
                cached[str(content_hash)] = payload
        return cached

    def _save_evidence_card_cache(self, cards: List[EvidenceCard]) -> None:
        if self._cache_manager is None:
            return
        for card in cards:
            self._cache_manager.set(
                f"evidence-card:v1:{card.content_hash}",
                card.model_dump(mode="json"),
            )

    def _generate_sections_from_cards(
        self,
        *,
        report_id: str,
        outline: ReportOutline,
        cards: List[EvidenceCard],
        section_cache_index: Dict[str, Dict[str, Any]],
        evidence_fingerprint: Dict[str, str],
    ) -> Tuple[List[SectionContent], List[str], Dict[str, List[Dict[str, Any]]]]:
        """Write each section once from bounded card inputs while processing every card."""
        cards_by_id = {card.evidence_id: card for card in cards}
        section_contents: List[SectionContent] = []
        processed_ids: List[str] = []
        digest_state: Dict[str, List[Dict[str, Any]]] = {}

        for section in outline.sections:
            assigned_ids = [item for item in section.evidence_ids if item in cards_by_id]
            assigned_cards = [cards_by_id[item] for item in assigned_ids]
            cached_section = section_cache_index.get(section.section_id)
            if cached_section and self._can_reuse_section(
                section,
                cached_section,
                evidence_fingerprint,
            ):
                content = self._sanitize_section_text(section.title, cached_section["content"])
            elif not assigned_cards:
                content = "当前章节没有被路由到直接相关的 Evidence Card。"
            else:
                prompt_entries, digests = self._evidence_card_pipeline.section_inputs(assigned_cards)
                digest_state[section.section_id] = [item.model_dump(mode="json") for item in digests]
                prompt_map = {item.result_id: item for item in prompt_entries}
                prompt_section = section.model_copy(update={"evidence_ids": list(prompt_map)})
                prompt_outline = outline.model_copy(
                    update={
                        "sections": [
                            prompt_section if item.section_id == section.section_id else item
                            for item in outline.sections
                        ]
                    }
                )
                draft = self._section_writer.write_section(
                    outline=prompt_outline,
                    section=prompt_section,
                    evidence_map=prompt_map,
                    fallback_evidence_ids=list(prompt_map),
                )
                content = self._sanitize_section_text(section.title, draft.content)
                content = add_inline_citations(content, assigned_ids)
            section_contents.append(
                SectionContent(
                    section_id=section.section_id,
                    title=section.title,
                    content=content,
                    used_evidence_ids=assigned_ids,
                )
            )
            self._save_stage_checkpoint(
                report_id,
                f"section:{section.section_id}",
                {
                    "section_id": section.section_id,
                    "title": section.title,
                    "summary": section.summary,
                    "content": content,
                    "used_evidence_ids": assigned_ids,
                    "evidence_fingerprint": {
                        evidence_id: evidence_fingerprint.get(evidence_id)
                        for evidence_id in assigned_ids
                    },
                },
            )
            for evidence_id in assigned_ids:
                if evidence_id not in processed_ids:
                    processed_ids.append(evidence_id)

        return section_contents, processed_ids, digest_state

    def _collect_section_evidence(
        self,
        section: SectionOutline,
        evidence_map: Dict[str, RetrievalResult],
        fallback_evidence_ids: List[str],
    ) -> List[RetrievalResult]:
        if section.evidence_ids:
            candidate_ids = [eid for eid in section.evidence_ids if eid in evidence_map]
        elif fallback_evidence_ids:
            candidate_ids = [eid for eid in fallback_evidence_ids if eid in evidence_map]
        else:
            candidate_ids = list(evidence_map.keys())
        return [evidence_map[eid] for eid in candidate_ids]
