# 由 harness/drivers_plan_execute.py 拆分（T-016），方法体与原位置逐行一致。
"""报告生成与修复（原 report/_requires_reserved_budget_fallback/_build_*/_clean_fallback_claim/_uses_compact_report/repair_report）。"""

import asyncio
import re
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
from deepresearch_agent.agents.multi_agent.reporter.base_reporter import ReportResult, SectionContent
from deepresearch_agent.agents.multi_agent.reporter.consistency_checker import ConsistencyCheckResult
from deepresearch_agent.agents.multi_agent.reporter.evidence_cards import EvidenceCardPipeline
from deepresearch_agent.agents.multi_agent.reporter.outline_builder import ReportOutline, SectionOutline
from deepresearch_agent.config.settings import (
    EVIDENCE_CARD_MAX_TOKENS,
    REPORT_BATCH_BUDGET_RATIO,
    REPORT_BATCH_DIGEST_MAX_TOKENS,
    REPORT_MAX_SECTIONS,
    REPORT_RESERVED_TOKENS,
    REPORT_SECTION_EVIDENCE_BUDGET,
    VERIFICATION_RESERVED_TOKENS,
)
from .report_safety import add_inline_citations, citation_evidence_ids, has_internal_material, rank_evidence, sanitize_report
from deepresearch_agent.harness.report_fallbacks import _evidence_only_report


class ReportPipelineStagedMixin:
    async def report(self) -> str:
        if self._repair_pending and self.report_result is not None:
            self._repair_pending = False
        elif self._uses_compact_report():
            self.report_result = self._build_compact_report()
        elif self._requires_reserved_budget_fallback():
            self.report_result = self._build_reserved_budget_report()
        else:
            report_type = str(self.context.config_snapshot.get("report_type") or "long_document")
            self.report_result = await asyncio.to_thread(
                self.orchestrator.report,
                self.state,
                report_type=report_type,
            )
        self.report_result.final_report = sanitize_report(self.report_result.final_report)
        evidence_ids = citation_evidence_ids([result for _, _, _, result in self.evidence_results()])
        self.report_result.final_report = add_inline_citations(self.report_result.final_report, evidence_ids)
        # T-405 报告安全降级：语义核验未通过/不可用的动作必须在报告局限中显式
        # 标注（unknown/fail 不得写成"已核验"；关键词检查仅为代理，不是事实认证）。
        semantic_gaps = self.coreact_semantic_gaps() if hasattr(self, "coreact_semantic_gaps") else []
        if semantic_gaps:
            statuses = "、".join(sorted({gap["status"] for gap in semantic_gaps}))
            note = (
                f"\n\n## 局限（语义核验）\n\n部分动作（{len(semantic_gaps)} 个）的语义核验状态为 {statuses}，"
                "其结论仅由确定性检查与所引用证据支撑，未经语义级核验，请谨慎采信。"
            )
            if "## 局限（语义核验）" not in self.report_result.final_report:
                self.report_result.final_report += note
        self.state.response = self.report_result.final_report
        return self.report_result.final_report

    def _requires_reserved_budget_fallback(self) -> bool:
        remaining = (
            self.context.budget_limits.max_llm_tokens
            - self.context.budget_usage.llm_tokens
        )
        return remaining < REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS

    def _build_reserved_budget_report(self) -> ReportResult:
        """Finish without another model call while keeping the report readable."""
        evidence_by_id: dict[str, RetrievalResult] = {}
        for _, _, _, result in self.evidence_results():
            evidence_by_id[result.result_id] = result
        evidence = list(evidence_by_id.values())
        pipeline = EvidenceCardPipeline(
            card_max_tokens=EVIDENCE_CARD_MAX_TOKENS,
            section_token_budget=REPORT_SECTION_EVIDENCE_BUDGET,
            batch_budget_ratio=REPORT_BATCH_BUDGET_RATIO,
            digest_max_tokens=REPORT_BATCH_DIGEST_MAX_TOKENS,
        )
        cards, _ = pipeline.build_cards(evidence)
        plan = self.state.plan if isinstance(self.state.plan, PlanSpec) else (self.planner_result.plan_spec if self.planner_result else None)
        nodes = [node for node in plan.task_graph.nodes if node.status != "skipped"][:REPORT_MAX_SECTIONS] if plan else []
        sections = [
            SectionOutline(
                section_id=f"reserved_{index}",
                title=str(node.description or f"研究发现 {index}")[:80],
                summary=str(node.description or "证据支持的研究发现")[:240],
            )
            for index, node in enumerate(nodes, 1)
        ]
        if not sections:
            sections = [SectionOutline(
                section_id="reserved_findings",
                title="证据支持的研究发现",
                summary="按全量 Evidence Card 呈现研究发现",
            )]
        outline = ReportOutline(
            report_type="long_document",
            title="研究报告（预算保护模式）",
            abstract="执行阶段已接近预留边界，报告以确定性 Evidence Card 形式完整交付。",
            sections=sections,
        )
        routing = pipeline.route_cards(cards, outline)
        section_contents: list[SectionContent] = []
        processed_ids: list[str] = []
        cards_by_id = {card.evidence_id: card for card in cards}
        for section in outline.sections:
            assigned = routing.get(section.section_id, [])
            lines: list[str] = []
            seen_claims: set[str] = set()
            for evidence_id in assigned:
                card = cards_by_id[evidence_id]
                claim = card.claims[0] if card.claims else card.summary
                claim = self._clean_fallback_claim(claim)
                fingerprint = re.sub(r"\W+", "", claim).lower()[:160]
                if not claim or fingerprint in seen_claims:
                    continue
                seen_claims.add(fingerprint)
                lines.append(f"- {claim} [{evidence_id}]")
                if card.conflict_findings:
                    conflict = self._clean_fallback_claim(card.conflict_findings[0])
                    if conflict:
                        lines.append(f"  - 冲突或不一致：{conflict} [{evidence_id}]")
                if card.limitations:
                    limitation = self._clean_fallback_claim(card.limitations[0])
                    if limitation:
                        lines.append(f"  - 局限：{limitation} [{evidence_id}]")
                if len(seen_claims) >= 4:
                    break
            content = "\n".join(lines) or "当前证据不足以形成可靠结论。"
            if len(assigned) > len(seen_claims):
                content += f"\n\n本节另有 {len(assigned) - len(seen_claims)} 条证据保存在证据台账中，可在报告的“证据”视图核查。"
            section_contents.append(SectionContent(
                section_id=section.section_id,
                title=section.title,
                content=content,
                used_evidence_ids=assigned,
            ))
            processed_ids.extend(assigned)
        report_parts = [f"# {outline.title}", outline.abstract or ""]
        for section, content in zip(outline.sections, section_contents):
            report_parts.extend([f"## {section.title}", content.content])
        # Full coverage lives in structured metadata and the Evidence Ledger.
        _, annex_ids = pipeline.annex(cards)
        final_report = "\n\n".join(part for part in report_parts if part).strip()
        coverage = pipeline.coverage(cards, routing, processed_ids, annex_ids)
        return ReportResult(
            outline=outline,
            sections=section_contents,
            final_report=final_report,
            consistency_check=ConsistencyCheckResult(
                is_consistent=bool(final_report.strip() and not has_internal_material(final_report)),
                raw_response="deterministic reserved-budget Evidence Card report",
            ),
            evidence_cards=[card.model_dump(mode="json") for card in cards],
            evidence_routing=routing,
            evidence_card_coverage=coverage,
            report_metrics={
                "evidence_count": len(evidence),
                "evidence_card_count": len(cards),
                "model_calls": 0,
                "reserved_budget_fallback": True,
                "remaining_tokens_at_entry": max(
                    0,
                    self.context.budget_limits.max_llm_tokens
                    - self.context.budget_usage.llm_tokens,
                ),
            },
        )

    @staticmethod
    def _clean_fallback_claim(value: str, *, limit: int = 360) -> str:
        """Remove common webpage chrome and Markdown that corrupt report layout."""
        text = re.sub(r"[`#>*_|]+", " ", str(value or ""))
        text = re.sub(
            r"(?i)skip to content|navigation menu|sign in|cookie settings|loading(?:\s+loading)+|"
            r"documentation index|console\s+log\s+in",
            " ",
            text,
        )
        text = re.sub(r"\s+", " ", text).strip(" -—:：")
        if len(text) > limit:
            text = text[:limit].rstrip() + "…"
        return text

    def _uses_compact_report(self) -> bool:
        plan = self.state.plan if isinstance(self.state.plan, PlanSpec) else (self.planner_result.plan_spec if self.planner_result else None)
        if plan is None:
            return False
        if self.orchestration == "coreact_v1":
            # 增量动作数代表轨迹长度而非查询复杂度；compact 依据真实动作数 + 查询形态
            action_count = self.coreact_loop.step_index if self.coreact_loop is not None else 0
            if action_count > 1:
                return False
        elif len(plan.task_graph.nodes) != 1:
            return False
        query = (self.context.resolved_query or self.context.original_query).lower()
        markers = ("概括", "总结", "要点", "列出", "是什么", "简述", "summarize", "list ", "what is")
        return len(query) <= 160 and any(marker in query for marker in markers)

    def _build_compact_report(self) -> ReportResult:
        ranked = rank_evidence([result for _, _, _, result in self.evidence_results()])
        unique: list[RetrievalResult] = []
        seen: set[str] = set()
        for item in ranked:
            if item.result_id not in seen:
                seen.add(item.result_id)
                unique.append(item)
        query = (self.context.resolved_query or self.context.original_query).lower()
        candidates = unique
        if "药物" in query or "用药" in query:
            drug_terms = ("药物", "用药", "阿司匹林", "抗血小板", "抗凝", "溶栓", "尼莫地平")
            directly_on_topic = [
                item for item in unique
                if any(term in f"{item.evidence} {item.metadata.source_id}" for term in drug_terms)
            ]
            if directly_on_topic:
                candidates = directly_on_topic
        selected: list[RetrievalResult] = []
        fingerprints: list[set[str]] = []
        for item in candidates:
            compact = re.sub(r"\s+", "", str(item.evidence or ""))
            grams = {compact[index:index + 2] for index in range(max(0, len(compact) - 1))}
            duplicate = any(
                grams and prior and len(grams & prior) / min(len(grams), len(prior)) >= 0.6
                for prior in fingerprints
            )
            if duplicate:
                continue
            selected.append(item)
            fingerprints.append(grams)
            if len(selected) == 3:
                break
        lines: list[str] = []
        for index, item in enumerate(selected, 1):
            summary = re.sub(r"\s+", " ", str(item.evidence or "")).strip()
            if len(summary) > 420:
                summary = summary[:417].rstrip() + "…"
            lines.append(f"{index}. {summary} [{item.result_id}]")
        if not lines:
            lines.append("当前私有库没有检索到可直接支持该问题的证据。")
        body = "\n".join(lines)
        count_note = f"本次仅检索到 {len(selected)} 条不重复且直接相关的证据；" if len(selected) < 3 else ""
        limitation = f"{count_note}当前回答严格限于本次检索到的库内证据，未被证据直接覆盖的治疗细节不作推断。"
        final_report = f"# 研究报告\n\n## 证据支持的要点\n\n{body}\n\n## 局限\n\n{limitation}"
        outline = ReportOutline(
            report_type="short_answer",
            title="研究报告",
            sections=[SectionOutline(section_id="evidence_points", title="证据支持的要点", summary="基于库内证据的紧凑回答", evidence_ids=[item.result_id for item in selected], estimated_words=300)],
            total_estimated_words=300,
        )
        section = SectionContent(
            section_id="evidence_points", title="证据支持的要点", content=body,
            used_evidence_ids=[item.result_id for item in selected],
        )
        self.state.response = final_report
        return ReportResult(
            outline=outline, sections=[section], final_report=final_report,
            consistency_check=ConsistencyCheckResult(is_consistent=True, raw_response="deterministic evidence-only report"),
        )

    async def repair_report(self, failures: list[str]) -> bool:
        if self.report_result is None:
            return False
        supported = {"citation_integrity", "required_section", "report_consistency", "source_diversity", "evidence_card_coverage", "rubric_coverage"}
        if not set(failures).issubset(supported):
            return False
        report = self.report_result.final_report
        if set(failures) & {"citation_integrity", "report_consistency", "rubric_coverage"}:
            # rubric_coverage 失败意味着引用的证据未覆盖主张词：确定性重建
            # 重新引用账本证据（尽力覆盖），能否通过仍取决于证据内容本身；
            # 重建不成功时由 runtime 的 max_task_retries 上限诚实失败。
            all_evidence = [result for _, _, _, result in self.evidence_results()]
            report, selected = _evidence_only_report(
                all_evidence,
                required_sections=list(self.context.config_snapshot.get("required_sections", [])),
            )
            if not selected:
                return False
            try:
                consistency = await asyncio.to_thread(
                    self.orchestrator.recheck_report_consistency,
                    report,
                    selected,
                )
            except Exception as exc:  # a failed checker must not be treated as a pass
                consistency = ConsistencyCheckResult(
                    is_consistent=False,
                    issues=[{"kind": type(exc).__name__, "message": str(exc)[:240]}],
                    raw_response=None,
                )
            self.report_result.consistency_check = consistency
        if "required_section" in failures and not report.lstrip().startswith("#"):
            report = "# 研究报告\n\n" + report
        if "source_diversity" in failures and "局限" not in report:
            report += "\n\n## 局限\n\n当前证据来源数量有限，结论应结合更多独立来源复核。"
        if "evidence_card_coverage" in failures and self.report_result.evidence_cards:
            from deepresearch_agent.agents.multi_agent.reporter.evidence_cards import EvidenceCardPipeline, EvidenceCard
            cards = [EvidenceCard.model_validate(item) for item in self.report_result.evidence_cards]
            processed_ids = [
                evidence_id
                for section in self.report_result.sections
                for evidence_id in section.used_evidence_ids
            ]
            index_ids = [card.evidence_id for card in cards]
            coverage = EvidenceCardPipeline.coverage(
                cards, self.report_result.evidence_routing, processed_ids, index_ids,
            )
            self.report_result.evidence_card_coverage = coverage
            if not coverage.get("passed"):
                return False
        self.report_result.final_report = report
        self.state.response = report
        self._repair_pending = True
        return True
