# 由 agents/multi_agent/reporter/base_reporter.py 拆分（T-014），方法体与原位置逐行一致。
"""附录/清洗/引用格式化与证据指纹（原 _append_evidence_annex 等 8 个方法）。"""

from typing import List, Dict, Any, Optional, Iterable, Tuple
import hashlib
import json
import re
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult


class ReportFormattingMixin:
    def _format_evidence_for_check(self, evidence_entries: Iterable[RetrievalResult]) -> str:
        """
        将证据转换为一致性检查所需的文本格式
        """
        lines = []
        for item in evidence_entries:
            snippet = ""
            if isinstance(item.evidence, str):
                snippet = item.evidence.replace("\n", " ")[:200]
            elif isinstance(item.evidence, dict):
                snippet = str(item.evidence)
            lines.append(
                f"{item.result_id} | {item.granularity} | {item.source} | "
                f"{snippet}"
            )
        return "\n".join(lines)

    def _append_evidence_annex(
        self,
        report: str,
        evidence_map: Dict[str, RetrievalResult],
        used_evidence_ids: List[str],
        *,
        snippet_length: int = 200,
    ) -> str:
        """
        在报告末尾追加证据附录，列出使用到的证据ID与原文片段
        """
        if not evidence_map or not used_evidence_ids:
            return report

        unique_ids: List[str] = []
        for eid in used_evidence_ids:
            if eid in evidence_map and eid not in unique_ids:
                unique_ids.append(eid)

        if not unique_ids:
            return report

        annex_entries: List[Dict[str, Any]] = []
        for eid in unique_ids:
            result = evidence_map.get(eid)
            if result is None:
                continue
            snippet: str
            if isinstance(result.evidence, str):
                snippet = result.evidence.replace("\n", " ").strip()
            elif isinstance(result.evidence, dict):
                snippet = json.dumps(result.evidence, ensure_ascii=False)
            else:
                snippet = str(result.evidence)
            snippet = snippet[:snippet_length]

            entry: Dict[str, Any] = {
                "id": eid,
                "source": result.source,
                "source_mode": result.source_mode,
                "source_label": "[Web]" if result.source_mode == "web" else "[私有库]",
                "source_id": getattr(result.metadata, "source_id", ""),
                "url": getattr(result.metadata, "url", None),
                "domain": getattr(result.metadata, "domain", None),
                "granularity": result.granularity,
                "snippet": snippet,
            }
            confidence = getattr(result.metadata, "confidence", None)
            if confidence is not None:
                entry["confidence"] = round(confidence, 3)
            annex_entries.append(entry)

        if not annex_entries:
            return report

        annex_json = json.dumps(annex_entries, ensure_ascii=False, indent=2)
        annex_block = (
            "\n\n## 证据附录\n"
            "```json\n"
            f"{annex_json}\n"
            "```\n"
        )
        return report.rstrip() + annex_block

    @staticmethod
    def _normalize_heading_text(text: str) -> str:
        normalized = re.sub(r"[#\s]+", "", (text or "")).replace("：", ":").replace("，", ",")
        return normalized.strip().lower()

    def _sanitize_section_text(self, title: str, content: str) -> str:
        if not content:
            return ""
        normalized_title = self._normalize_heading_text(title)
        cleaned_lines: List[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                heading_text = re.sub(r"^#+\s*", "", stripped)
                if self._normalize_heading_text(heading_text) == normalized_title:
                    continue
            cleaned_lines.append(line)
        while cleaned_lines and not cleaned_lines[0].strip():
            cleaned_lines.pop(0)
        return "\n".join(cleaned_lines).strip()

    def _normalize_section_references(
        self,
        content: str,
        candidate_ids: Iterable[str],
        evidence_map: Dict[str, RetrievalResult],
    ) -> tuple[str, List[str]]:
        valid_ids: List[str] = []

        def replacer(match: re.Match[str]) -> str:
            evidence_id = match.group(1).strip()
            if evidence_id in evidence_map:
                if evidence_id not in valid_ids:
                    valid_ids.append(evidence_id)
                return match.group(0)
            return ""

        sanitized = re.sub(r"\[证据ID[:：]\s*([A-Za-z0-9\-]+)\]", replacer, content or "")

        candidate_order = [
            eid for eid in candidate_ids if eid in evidence_map and eid not in valid_ids
        ]
        ordered_ids = valid_ids + candidate_order
        return sanitized.strip(), ordered_ids

    def _format_references(
        self,
        evidence_map: Dict[str, RetrievalResult],
        used_evidence_ids: List[str],
    ) -> Optional[str]:
        """
        调用引用格式化器生成引用列表
        """
        if not evidence_map or not used_evidence_ids:
            return None
        unique_ids = []
        for eid in used_evidence_ids:
            if eid in evidence_map and eid not in unique_ids:
                unique_ids.append(eid)
        results = [evidence_map[eid] for eid in unique_ids if eid in evidence_map]
        if not results:
            return None
        return self._citation_formatter.format_references(results, self.config.citation_style)

    def _resolve_report_id(self, plan: PlanSpec, report_type: str) -> str:
        """生成用于缓存的report_id"""
        return f"{plan.plan_id}:{plan.version}:{report_type}"

    def _build_evidence_fingerprint(
        self,
        evidence_map: Dict[str, RetrievalResult],
    ) -> Dict[str, str]:
        fingerprint: Dict[str, str] = {}
        for rid, result in evidence_map.items():
            payload = {
                "granularity": result.granularity,
                "source": result.source,
                "score": result.score,
                "metadata": result.metadata.model_dump(mode="json"),
            }
            fingerprint[rid] = hashlib.sha1(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
        return fingerprint
