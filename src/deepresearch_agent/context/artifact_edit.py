"""Build bounded context for editing one report section without rewriting others."""

from __future__ import annotations

import hashlib
import re

from deepresearch_agent.harness.errors import AppError
from dataclasses import dataclass


@dataclass(frozen=True)
class MarkdownSection:
    heading: str
    level: int
    content: str
    sha256: str


class ArtifactEditTargetAmbiguous(ValueError):
    """显式修订请求存在可读基线，但目标章节不唯一/未指定。

    调用方应转为澄清（列出可选章节），而不是静默退化成普通研究。
    """

    def __init__(self, message: str, *, questions: list[str]) -> None:
        super().__init__(message)
        self.questions = list(questions)


class ArtifactEditContextBuilder:
    EDIT_SIGNAL = re.compile(r"(?:修改|改写|重写|补充|完善|不满意|不要修改其他|保持其他|只改|section|revise|rewrite)", re.I)
    HEADING = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
    CITATION = re.compile(r"\[\^?(ev_[A-Za-z0-9_-]+)\]")
    CONTRACT_SCHEMA_VERSION = 1

    def __init__(self, runs, artifacts, store):
        self.runs, self.artifacts, self.store = runs, artifacts, store

    @classmethod
    def parse_sections(cls, report: str) -> list[MarkdownSection]:
        matches = list(cls.HEADING.finditer(report))
        sections: list[MarkdownSection] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(report)
            content = report[match.start():end].strip()
            sections.append(MarkdownSection(
                heading=match.group(2).strip(), level=len(match.group(1)), content=content,
                sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            ))
        return sections

    @staticmethod
    def _select(query: str, sections: list[MarkdownSection]) -> int | None:
        candidates = [index for index, section in enumerate(sections) if section.heading in query or any(part in query for part in re.split(r"[：:—\-、 ]", section.heading) if len(part) >= 3)]
        ordinal = re.search(r"第\s*(\d+)\s*(?:节|部分|章)", query)
        if ordinal:
            number = int(ordinal.group(1))
            numbered = [i for i, section in enumerate(sections) if re.match(rf"{number}(?:\.|、|\s)", section.heading)]
            candidates.extend(numbered)
        unique = sorted(set(candidates))
        return unique[0] if len(unique) == 1 else None

    @staticmethod
    def inherited_evidence_id(new_run_id: str, base_evidence_id: str, content_hash: str) -> str:
        """稳定映射：旧证据归入新 Run 时生成新 ID（同输入跨重放不变）。"""
        digest = hashlib.sha256(
            f"{new_run_id}:inherit:{base_evidence_id}:{content_hash}".encode("utf-8")
        ).hexdigest()[:24]
        return f"ev_{digest}"

    @classmethod
    def rewrite_citations(cls, text: str, mapping: dict[str, str] | None) -> str:
        """按映射改写引用（保留 ``[ev_x]`` / ``[^ev_x]`` 两种写法）。"""
        if not mapping:
            return text
        for old, new in mapping.items():
            if old and new and old != new:
                text = text.replace(f"[{old}]", f"[{new}]").replace(f"[^{old}]", f"[^{new}]")
        return text

    async def build(self, *, session_id: str, current_run_id: str, query: str) -> dict | None:
        if not self.EDIT_SIGNAL.search(query):
            return None
        prior_runs = [run for run in await self.runs.list_for_session(session_id, limit=50) if run.run_id != current_run_id and run.status == "completed"]
        readable_outlines: list[list[str]] = []
        for run in prior_runs:
            artifact = await self.artifacts.get_report(run.run_id)
            if artifact is None:
                continue
            try:
                # 完整性校验：摘要不一致的历史报告不可作为改写基线，跳过该候选
                report = self.store.read_verified(artifact.relative_path, artifact.sha256).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            except AppError:
                continue
            sections = self.parse_sections(report)
            headings = [section.heading for section in sections]
            readable_outlines.append(headings)
            # 重复标题使"目标章节/保留章节"集合不可判定：明确要求澄清，不猜第一个
            if len(headings) != len(set(headings)):
                raise ArtifactEditTargetAmbiguous(
                    "报告存在重复章节标题，无法确定修订目标",
                    questions=[f"报告存在重复章节标题，请明确要修改的唯一章节（当前标题：{'、'.join(headings[:8])}）。"],
                )
            selected = self._select(query, sections)
            if selected is None:
                continue
            target = sections[selected]
            preserved = [section for index, section in enumerate(sections) if index != selected]
            # 保留部分的旧引用：目标章节之外的所有内容（含首个标题前的导语）
            preserved_text = report.replace(target.content, "", 1)
            return {
                "schema_version": self.CONTRACT_SCHEMA_VERSION,
                "operation": "replace_section", "artifact_id": artifact.artifact_id,
                "relative_path": artifact.relative_path,
                "base_run_id": run.run_id, "base_sha256": artifact.sha256,
                "target_heading": target.heading, "target_content": target.content,
                "target_sha256": target.sha256,
                "outline": headings,
                "base_citations": sorted(set(self.CITATION.findall(preserved_text))),
                "previous_section_summary": "" if selected == 0 else sections[selected - 1].content[:500],
                "next_section_summary": "" if selected + 1 >= len(sections) else sections[selected + 1].content[:500],
                "preserve_sections": [{"heading": section.heading, "sha256": section.sha256} for section in preserved],
                "instruction": "只输出目标章节的替换内容；其他章节由程序原样保留，禁止改写。",
            }
        if readable_outlines:
            options = "、".join(readable_outlines[0][:8])
            raise ArtifactEditTargetAmbiguous(
                "修订请求缺少唯一目标章节",
                questions=[f"请明确要修改的章节（可选：{options}）。"],
            )
        return None

    @classmethod
    def apply_replacement(cls, base_report: str, contract: dict, generated: str, *, citation_mapping: dict[str, str] | None = None) -> str:
        """Deterministically splice one generated section into the base report.

        引用映射只作用于**基线的保留内容**（含导语）；新生成的目标章节保持原样，
        若模型复用了旧证据 ID 则无法通过新 Run 的 citation_integrity（显式失败，
        不静默改写）。
        """
        base_sections = cls.parse_sections(base_report)
        headings = [section.heading for section in base_sections]
        if len(headings) != len(set(headings)):
            raise ValueError("Artifact edit requires unique section headings")
        targets = [section for section in base_sections if section.heading == contract["target_heading"]]
        if len(targets) != 1:
            raise ValueError("Artifact edit target is not unique in the base report")
        target = targets[0]
        by_heading = {section.heading: section for section in base_sections}
        actual_preserved = [section for section in base_sections if section.heading != target.heading]
        contract_preserved = contract.get("preserve_sections") or []
        if not isinstance(contract_preserved, list):
            raise ValueError("Artifact edit preserve_sections 形状非法")
        contract_headings = [str(item.get("heading", "")) for item in contract_preserved if isinstance(item, dict)]
        if (
            len(contract_preserved) != len(actual_preserved)
            or sorted(contract_headings) != sorted(section.heading for section in actual_preserved)
        ):
            raise ValueError("Artifact edit preserved section set does not match the base report")
        for entry in contract_preserved:
            actual = by_heading.get(str(entry.get("heading")))
            if actual is None or actual.sha256 != entry.get("sha256"):
                raise ValueError(f"Artifact edit modified preserved section: {entry.get('heading')}")

        generated_sections = cls.parse_sections(generated)
        replacement = next((section.content for section in generated_sections if section.heading == target.heading), None)
        if replacement is None:
            marker = "#" * target.level
            replacement = f"{marker} {target.heading}\n\n{generated.strip()}"

        if citation_mapping:
            # 先映射基线（含目标章节）；目标章节随后被新内容整体替换，因此
            # 新内容中的旧 ID 不会被误改写。
            working = cls.rewrite_citations(base_report, citation_mapping)
            working_sections = cls.parse_sections(working)
            working_target = next(
                section for section in working_sections if section.heading == target.heading
            )
            merged = working.replace(working_target.content, replacement.strip(), 1)
            merged_sections = {section.heading: section for section in cls.parse_sections(merged)}
            for entry in contract_preserved:
                base_section = by_heading[str(entry["heading"])]
                expected = hashlib.sha256(
                    cls.rewrite_citations(base_section.content, citation_mapping).encode("utf-8")
                ).hexdigest()
                declared = entry.get("mapped_sha256")
                if declared is not None and declared != expected:
                    raise ValueError(f"Artifact edit mapped hash mismatch: {entry['heading']}")
                if merged_sections.get(str(entry["heading"])) is None or merged_sections[str(entry["heading"])].sha256 != expected:
                    raise ValueError(f"Artifact edit modified preserved section: {entry['heading']}")
            return merged

        merged = base_report.replace(target.content, replacement.strip(), 1)
        merged_hashes = {section.heading: section.sha256 for section in cls.parse_sections(merged)}
        for entry in contract_preserved:
            if merged_hashes.get(str(entry["heading"])) != entry.get("sha256"):
                raise ValueError(f"Artifact edit modified preserved section: {entry.get('heading')}")
        return merged
