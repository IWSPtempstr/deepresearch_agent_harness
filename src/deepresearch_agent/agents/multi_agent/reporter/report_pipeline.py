# 由 agents/multi_agent/reporter/base_reporter.py 拆分（T-014），方法体与原位置逐行一致。
"""报告编排主流程（原 generate_report/recheck_consistency/_generate_sections_traditional/_assemble_report）。"""

from typing import List, Dict, Any, Optional, Iterable, Tuple
import json
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
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
from deepresearch_agent.harness.report_safety import add_inline_citations, has_internal_material
from deepresearch_agent.agents.multi_agent.reporter.report_schemas import SectionContent, ReportResult


class ReportPipelineMixin:
    def generate_report(
        self,
        state: PlanExecuteState,
        plan: Optional[PlanSpec] = None,
        execution_records: Optional[List[ExecutionRecord]] = None,
        report_type: Optional[str] = None,
    ) -> ReportResult:
        """生成报告主流程，支持report_id级缓存与分段复用"""

        plan = plan or state.plan
        if plan is None:
            raise ValueError("生成报告需要PlanSpec")

        execution_records = execution_records or state.execution_records

        resolved_report_type = report_type or (
            state.report_context.report_type if state.report_context else None
        )
        if not resolved_report_type:
            resolved_report_type = self.config.default_report_type

        report_id = self._resolve_report_id(plan, resolved_report_type)
        evidence_map = self._collect_evidence(state, execution_records)
        evidence_fingerprint = self._build_evidence_fingerprint(evidence_map)

        cached_payload = self._load_cached_payload(report_id)
        if (
            cached_payload
            and cached_payload.get("evidence_fingerprint") == evidence_fingerprint
            and (cached_payload.get("evidence_card_coverage") or {}).get("passed") is True
        ):
            report_result = self._deserialize_report_result(cached_payload)
            self._update_state_report_context(
                state,
                report_result,
                report_id,
                cache_hit=True,
            )
            state.response = report_result.final_report
            state.update_timestamp()
            return report_result

        cached_cards = self._load_evidence_card_cache(evidence_map)
        evidence_cards, card_cache_hits = self._evidence_card_pipeline.build_cards(
            evidence_map.values(), cached_by_hash=cached_cards,
        )
        self._save_evidence_card_cache(evidence_cards)

        self._save_stage_checkpoint(
            report_id, "evidence_cards",
            {"evidence_fingerprint": evidence_fingerprint, "card_count": len(evidence_cards)},
        )
        routing_checkpoint = self._load_stage_checkpoint(report_id, "routing")
        outline_model_calls = 0
        if (
            routing_checkpoint
            and routing_checkpoint.get("evidence_fingerprint") == evidence_fingerprint
            and routing_checkpoint.get("outline")
        ):
            outline = ReportOutline.model_validate(routing_checkpoint["outline"])
        else:
            plan_summary = self._build_plan_summary(plan)
            evidence_summary = self._build_card_outline_summary(evidence_cards)
            outline = self._outline_builder.build_outline(
                query=plan.problem_statement.original_query,
                plan_summary=plan_summary,
                evidence_summary=evidence_summary,
                evidence_count=len(evidence_map),
                report_type=resolved_report_type,
            )
            outline_model_calls = 1
        outline.sections = outline.sections[: self.config.max_report_sections]
        routing = self._evidence_card_pipeline.route_cards(evidence_cards, outline)
        self._save_stage_checkpoint(
            report_id, "routing",
            {
                "evidence_fingerprint": evidence_fingerprint,
                "outline": outline.model_dump(mode="json"),
                "routing": routing,
            },
        )

        expected_report_calls = outline_model_calls + sum(
            1 for section in outline.sections if routing.get(section.section_id)
        )
        if expected_report_calls > self.config.max_report_model_calls:
            raise RuntimeError(
                f"报告预计模型调用 {expected_report_calls} 次，超过上限 {self.config.max_report_model_calls}"
            )

        section_cache_index = self._build_section_cache_index(cached_payload)
        section_cache_index.update(self._load_section_checkpoints(report_id, outline))
        section_contents, used_evidence_ids, evidence_digests = self._generate_sections_from_cards(
            report_id=report_id,
            outline=outline,
            cards=evidence_cards,
            section_cache_index=section_cache_index,
            evidence_fingerprint=evidence_fingerprint,
        )
        final_report = self._assemble_report(outline, section_contents)
        # The Evidence Ledger is a durable audit surface, not part of the reading
        # flow. Keep its exact coverage metadata, but do not dump every card into
        # the user-facing Markdown report.
        _, annex_ids = self._evidence_card_pipeline.annex(evidence_cards)
        coverage = self._evidence_card_pipeline.coverage(
            evidence_cards, routing, used_evidence_ids, annex_ids,
        )
        consistency_result = ConsistencyCheckResult(
            is_consistent=bool(final_report.strip() and not has_internal_material(final_report)),
            issues=[] if coverage["passed"] else [{"kind": "evidence_card_coverage", "missing_ids": coverage["missing_ids"]}],
            raw_response="deterministic full Evidence Card coverage check",
        )

        references = self._format_references(evidence_map, used_evidence_ids)
        report_model_calls = expected_report_calls
        report_metrics = {
            "evidence_count": len(evidence_map),
            "evidence_card_count": len(evidence_cards),
            "evidence_card_cache_hits": card_cache_hits,
            "section_count": len(outline.sections),
            "model_calls": report_model_calls,
            "max_model_calls": self.config.max_report_model_calls,
            "section_evidence_budget": self.config.section_evidence_budget,
            "within_model_call_budget": report_model_calls <= self.config.max_report_model_calls,
        }
        report_result = ReportResult(
            outline=outline,
            sections=section_contents,
            final_report=final_report,
            references=references,
            consistency_check=consistency_result,
            evidence_cards=[item.model_dump(mode="json") for item in evidence_cards],
            evidence_routing=routing,
            evidence_digests=evidence_digests,
            evidence_card_coverage=coverage,
            report_metrics=report_metrics,
        )

        self._save_report_cache(
            report_id,
            evidence_fingerprint,
            report_result,
            outline,
            evidence_map,
        )
        self._save_stage_checkpoint(
            report_id,
            "final_assembly",
            {
                "evidence_fingerprint": evidence_fingerprint,
                "coverage": coverage,
                "report_metrics": report_metrics,
            },
        )

        self._update_state_report_context(
            state,
            report_result,
            report_id,
            cache_hit=False,
        )
        state.response = final_report
        state.update_timestamp()
        return report_result

    def recheck_consistency(
        self,
        report_content: str,
        evidence_entries: Iterable[RetrievalResult],
    ) -> ConsistencyCheckResult:
        """Run the same consistency gate against a repaired report."""
        evidence_text = self._format_evidence_for_check(evidence_entries)
        return self._consistency_checker.check(report_content, evidence_text)

    def _generate_sections_traditional(
        self,
        outline: ReportOutline,
        evidence_map: Dict[str, RetrievalResult],
        section_cache_index: Dict[str, Dict[str, Any]],
        evidence_fingerprint: Dict[str, str],
        fallback_evidence_ids: List[str],
    ) -> Tuple[List[SectionContent], List[str]]:
        section_contents: List[SectionContent] = []
        used_evidence_ids: List[str] = []

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
                used_evidence_ids.extend(normalized_ids)
                continue

            draft = self._section_writer.write_section(
                outline=outline,
                section=section,
                evidence_map=evidence_map,
                fallback_evidence_ids=fallback_evidence_ids,
            )
            sanitized_content = self._sanitize_section_text(section.title, draft.content)
            sanitized_content, normalized_ids = self._normalize_section_references(
                sanitized_content,
                draft.used_evidence_ids,
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
            used_evidence_ids.extend(normalized_ids)

        return section_contents, used_evidence_ids

    def _assemble_report(self, outline: ReportOutline, sections: List[SectionContent]) -> str:
        """
        将标题、摘要、章节内容组装成最终Markdown
        """
        parts: List[str] = [f"# {outline.title}"]
        if outline.report_type == "long_document" and outline.abstract:
            parts.append("## 摘要")
            parts.append(outline.abstract.strip())

        for section in sections:
            parts.append(f"## {section.title}")
            parts.append(section.content.strip())

        return "\n\n".join(parts)
