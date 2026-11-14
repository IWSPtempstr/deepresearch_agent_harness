from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from deepresearch_agent.harness.contracts import ContractCheckData, SourceMode
from deepresearch_agent.harness.report_safety import has_internal_material

_CITATION = re.compile(r"\[\^?(ev_[A-Za-z0-9_-]+)\]")


class DeterministicVerifiers:
    version = "2.0"

    # 已知 provider → 通道（S4：provider/通道一致性；未知标签跳过一致性检查，
    # 兼容存量以工具名标注 provider 的行为）。注意双时代语义：``scholar_search``
    # 在 S1 前是 legacy scholar 通道的工具、S1 后是 web 聚合通道的 API，其
    # provider 标签两种形态（工具名/API 名）都不在本映射内——一致性由集合成员
    # 与 executor 的通道一致性防御兜底（S3）。
    PROVIDER_CHANNEL: ClassVar[dict[str, SourceMode]] = {
        "tavily": SourceMode.WEB, "scholar": SourceMode.WEB,
        "web": SourceMode.WEB, "tavily_search": SourceMode.WEB, "web_search": SourceMode.WEB,
        "graphrag": SourceMode.GRAPHRAG,
        "local_search": SourceMode.GRAPHRAG, "global_search": SourceMode.GRAPHRAG,
        "hybrid_search": SourceMode.GRAPHRAG, "naive_search": SourceMode.GRAPHRAG,
        "chain_exploration": SourceMode.GRAPHRAG,
        "openalex": SourceMode.SCHOLAR, "arxiv": SourceMode.SCHOLAR,
    }

    def __init__(self, *, run_id, source_mode: SourceMode, report: str, evidence: list[Any],
                 allowed_sources: list[SourceMode] | list[str] | None = None):
        self.run_id = run_id
        self.source_mode = SourceMode(source_mode)
        # S4：冻结来源集合（缺省 = 单来源，保持旧语义）。
        self.allowed_sources = {
            SourceMode(item).value for item in (allowed_sources or [source_mode])
        }
        self.report = report or ""
        self.evidence = evidence
        self._by_id = {str(self._get(item, "evidence_id", "")): item for item in evidence}

    @staticmethod
    def _get(item: Any, key: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    @classmethod
    def _is_inherited(cls, item: Any) -> bool:
        raw = cls._get(item, "metadata_json")
        if raw is None:
            raw = cls._get(item, "metadata")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except (TypeError, ValueError):
                return False
        return isinstance(raw, dict) and raw.get("inherited") is True

    def _check(self, kind: str, passed: bool, *, expected: Any, observed: Any, explanation: str, required: bool = True, threshold: float | None = None) -> ContractCheckData:
        return ContractCheckData(
            check_id=f"contract_{self.run_id}_{kind}", run_id=self.run_id, kind=kind,
            required=required, threshold=threshold, verifier="deterministic", verifier_version=self.version,
            passed=passed, observed={"expected": expected, "actual": observed}, explanation=explanation,
        )

    def source_match(self) -> ContractCheckData:
        raw_modes = [self._get(item, "source_mode", "") for item in self.evidence]
        modes = [mode.value if hasattr(mode, "value") else str(mode) for mode in raw_modes]
        outside = sorted({mode for mode in modes if mode not in self.allowed_sources})
        # S4：provider/通道一致性——已知 provider 标签必须与其证据通道匹配，
        # 防止把 Tavily 结果伪造到 graphrag 通道（未知标签跳过，兼容存量）。
        provider_mismatch = sorted({
            f"{self._get(item, 'provider', '')}->{mode}"
            for item, mode in zip(self.evidence, modes)
            if self.PROVIDER_CHANNEL.get(str(self._get(item, "provider", ""))) is not None
            and self.PROVIDER_CHANNEL[str(self._get(item, "provider", ""))].value != mode
        })
        passed = not outside and not provider_mismatch
        return self._check(
            "source_match", passed, expected=sorted(self.allowed_sources),
            observed={"modes": modes, "outside_allowed": outside,
                      "provider_mismatch": provider_mismatch},
            explanation="全部证据通道 ∈ Run 冻结集合，且已知 provider 与通道一致",
        )

    def min_evidence(self, minimum: int) -> ContractCheckData:
        count = len(self.evidence)
        inherited = sum(1 for item in self.evidence if self._is_inherited(item))
        # 继承证据属于新 Run 的权威台账（用于保留章节的既有引用），但单独披露
        # produced 数量，避免把"本 Run 新检索"与"合法继承"混为一谈。
        return self._check(
            "min_evidence", count >= minimum, expected=minimum,
            observed={"count": count, "inherited": inherited, "produced": count - inherited},
            threshold=float(minimum), explanation="有效 Evidence Ledger 记录数量（继承/新产生分别计数）",
        )

    def citation_integrity(self) -> ContractCheckData:
        citations = _CITATION.findall(self.report)
        missing = sorted({citation for citation in citations if citation not in self._by_id})
        claim_lines = self._claim_lines()
        uncited = [line[:160] for line in claim_lines if not _CITATION.search(line)]
        passed = bool(citations) and not missing and not uncited
        return self._check("citation_integrity", passed, expected="每条实质性主张就近引用当前 Run 证据", observed={"citations": citations, "missing": missing, "uncited_claims": uncited}, explanation="解析稳定 evidence_id 并检查主张附近引用")

    def required_section(self, required_sections: list[str]) -> ContractCheckData:
        headings = [match.group(1).strip() for match in re.finditer(r"(?m)^#{1,4}\s+(.+?)\s*$", self.report)]
        missing = [name for name in required_sections if not any(name.lower() in heading.lower() for heading in headings)]
        passed = bool(headings) and not missing and not has_internal_material(self.report)
        return self._check("required_section", passed, expected=required_sections or "至少一个非空 Markdown 标题", observed={"headings": headings, "missing": missing}, explanation="报告必需章节存在且非空")

    def claim_support(self) -> ContractCheckData:
        claims = self._claim_lines()
        supported = [line for line in claims if any(citation in self._by_id for citation in _CITATION.findall(line))]
        passed = bool(claims) and len(supported) == len(claims)
        return self._check("claim_support", passed, expected="每条实质性主张都有当前 Run 证据", observed={"claims": len(claims), "supported": len(supported)}, explanation="以逐段就近引用作为确定性支持下限")

    def report_consistency(self, consistency_passed: bool | None) -> ContractCheckData:
        passed = consistency_passed is True and bool(self.report.strip()) and not has_internal_material(self.report)
        return self._check("report_consistency", passed, expected=True, observed=consistency_passed, explanation="现有 ConsistencyChecker 未报告失败；未配置 LLM 检查时保守依赖确定性检查")

    def source_diversity(self) -> ContractCheckData:
        sources = set()
        produced_sources = set()
        channels = set()
        cited = set(_CITATION.findall(self.report))
        for item in self.evidence:
            if str(self._get(item, "evidence_id", "")) not in cited:
                continue
            mode = self._get(item, "source_mode", "")
            item_mode = mode.value if hasattr(mode, "value") else str(mode)
            channels.add(item_mode)
            source_id = str(self._get(item, "source_id", ""))
            metadata = self._get(item, "metadata_json", "{}")
            if item_mode == SourceMode.WEB.value:
                try:
                    payload = metadata if isinstance(metadata, dict) else json.loads(metadata or "{}")
                except (TypeError, ValueError):
                    payload = {}
                source_key = str(payload.get("domain") or source_id)
            else:
                source_key = source_id
            sources.add(source_key)
            if not self._is_inherited(item):
                produced_sources.add(source_key)
        sources.discard("")
        produced_sources.discard("")
        limitation = bool(re.search(r"局限|限制|single[- ]source|limited", self.report, re.I))
        passed = len(sources) >= 2 or (bool(sources) and limitation)
        return self._check(
            "source_diversity", passed,
            expected="至少两个独立来源，或明确披露单一来源局限",
            observed={
                "sources": sorted(sources),
                "produced_sources": sorted(produced_sources),
                "channels": sorted(channels),
                "inherited_source_count": len(sources - produced_sources),
                "limitation_disclosed": limitation,
            },
            explanation="来源不足不得伪造多样性（继承来源与新来源分别披露）",
        )

    def evidence_card_coverage(self, coverage: dict[str, Any]) -> ContractCheckData:
        ledger_ids = set(coverage.get("ledger_ids") or [])
        card_ids = set(coverage.get("card_ids") or [])
        routed_ids = set(coverage.get("routed_ids") or [])
        processed_ids = set(coverage.get("processed_ids") or [])
        annex_ids = set(coverage.get("annex_ids") or [])
        missing = {
            "cards": sorted(ledger_ids - card_ids),
            "routed": sorted(ledger_ids - routed_ids),
            "processed": sorted(ledger_ids - processed_ids),
            "annex": sorted(ledger_ids - annex_ids),
        }
        exact_sets = (
            ledger_ids == card_ids == routed_ids == processed_ids == annex_ids
        )
        reported_card_count = int(coverage.get("card_count", len(card_ids)) or 0)
        passed = (
            bool(ledger_ids)
            and exact_sets
            and reported_card_count == len(ledger_ids)
            and all(not values for values in missing.values())
        )
        return self._check(
            "evidence_card_coverage",
            passed,
            expected="Ledger/Card/路由/处理/结构化证据索引的 Evidence ID 集合完全一致",
            observed={
                "ledger_count": len(ledger_ids),
                "card_count": reported_card_count,
                "unique_card_count": len(card_ids),
                "routed_count": len(routed_ids),
                "processed_count": len(processed_ids),
                "annex_count": len(annex_ids),
                "missing_by_stage": missing,
                "inherited_count": len(set(coverage.get("inherited_ids") or [])),
            },
            explanation="全量 Evidence Card 保存在结构化证据台账中；正文只需引用支撑结论的证据",
        )

    def rubric_coverage(self, rubrics: list[dict]) -> ContractCheckData:
        """步级 rubric 主张词必须被"报告已引用的账本证据"覆盖。

        与 claim_support 的区别：claim_support 只要求主张行有就近引用；
        rubric_coverage 进一步要求引用到的证据内容确实覆盖 rubric 关键词，
        防止"引用了不相干证据"的表面合规。
        """
        cited_ids = set(_CITATION.findall(self.report))
        cited_terms_blob = ""
        cited_count = 0
        for item in self.evidence:
            if str(self._get(item, "evidence_id", "")) not in cited_ids:
                continue
            cited_count += 1
            cited_terms_blob += (str(self._get(item, "summary", "")) + " " + str(self._get(item, "title", ""))).lower()
        unmet: list[dict[str, Any]] = []
        total_terms = 0
        seen_pairs: set[tuple[str, str]] = set()
        for rubric in rubrics:
            rubric_id = str(rubric.get("rubric_id", ""))
            for criterion in rubric.get("criteria", []) or []:
                if criterion.get("kind") != "claim_support" or not criterion.get("required", True):
                    continue
                for term in criterion.get("required_terms", []) or []:
                    key = (rubric_id, term.lower())
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    total_terms += 1
                    if not cited_terms_blob or term.lower() not in cited_terms_blob:
                        unmet.append({"rubric_id": rubric_id, "term": term})
        # 无可判词的 rubric 属合法形态（与步级 verifier 的"无判词跳过"一致）：
        # 空转通过，交由 claim_support/步级验证兜底，避免永久不可修复失败。
        passed = not unmet
        return self._check(
            "rubric_coverage",
            passed,
            expected="每个 rubric 关键主张词都被报告引用的账本证据覆盖",
            observed={"rubric_count": len(rubrics), "required_terms": total_terms, "unmet": unmet[:10], "cited_evidence": cited_count},
            explanation="拒绝引用不相干证据的表面合规；rubric 来自执行记录，不接受 reporter 自报",
        )

    def _claim_lines(self) -> list[str]:
        claims: list[str] = []
        excluded_section = False
        for raw in self.report.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                excluded_section = bool(re.search(r"局限|限制|方法|证据引用|参考来源", line, re.I))
                continue
            if excluded_section or len(line) < 12 or line.startswith(("```", "|")):
                continue
            if re.search(r"局限|证据引用|参考来源|基于证据[。.]?$", line, re.I):
                continue
            claims.append(line)
        return claims
