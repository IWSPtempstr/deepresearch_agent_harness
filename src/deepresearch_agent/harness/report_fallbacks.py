# 由 harness/workflow.py 拆分（T-014），与原位置逐行一致。
"""证据兜底报告（原 workflow 模块级函数，仅被两个 driver 使用）。"""
from __future__ import annotations


import re
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
from .report_safety import add_inline_citations, citation_evidence_ids, has_internal_material, rank_evidence, sanitize_report


def _evidence_only_report(
    results: list[RetrievalResult],
    *,
    title: str = "修复后的研究报告",
    required_sections: list[str] | None = None,
) -> tuple[str, list[RetrievalResult]]:
    """Build a conservative report that cannot introduce unsupported claims."""
    selected: list[RetrievalResult] = []
    seen_ids: set[str] = set()
    for item in rank_evidence(results):
        if not item.result_id or item.result_id in seen_ids:
            continue
        seen_ids.add(item.result_id)
        selected.append(item)
        if len(selected) == 6:
            break

    lines: list[str] = []
    for index, item in enumerate(selected, 1):
        summary = re.sub(r"\[(?:ev_)?[A-Za-z0-9_-]+\]", "", str(item.evidence or ""))
        summary = re.sub(r"\s+", " ", summary).strip()
        if len(summary) > 520:
            summary = summary[:517].rstrip() + "…"
        if summary:
            lines.append(f"{index}. {summary} [{item.result_id}]")

    if not lines:
        return f"# {title}\n\n## 证据支持的要点\n\n当前没有可用于修复报告的有效证据。", []

    sections = [f"# {title}", "## 证据支持的要点", "\n".join(lines)]
    existing = {"证据支持的要点", "局限"}
    first_id = selected[0].result_id
    for name in required_sections or []:
        clean_name = str(name).strip()
        if clean_name and clean_name not in existing:
            sections.extend([f"## {clean_name}", f"本节仅保留上方证据能够直接支持的内容。 [{first_id}]"])
            existing.add(clean_name)
    sections.extend([
        "## 局限",
        "本报告由一致性门禁触发保守修复，仅保留当前 Run 证据能够直接支持的陈述；被判定为不一致或无法就近引用的原报告内容已移除。",
    ])
    return "\n\n".join(sections), selected


