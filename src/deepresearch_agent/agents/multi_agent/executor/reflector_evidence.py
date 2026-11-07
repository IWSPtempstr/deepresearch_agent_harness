# 由 agents/reflector.py 拆分（T-014），方法体与原位置逐行一致。
"""反思证据收集（原 _collect_evidence_from_records/_extract_evidence_text）。"""

from typing import Any, Dict, Optional, Tuple, List
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState


class ReflectionEvidenceMixin:
    def _collect_evidence_from_records(
        self,
        state: PlanExecuteState,
        *,
        exclude_tasks: Optional[set[str]] = None,
        primary_task_id: Optional[str] = None,
    ) -> List[str]:
        texts: List[str] = []
        for record in state.execution_records:
            if exclude_tasks and record.task_id in exclude_tasks:
                continue
            if primary_task_id and record.task_id != primary_task_id:
                # 允许聚合所有任务的证据，但保持与主任务相关的优先度
                pass
            for evidence in record.evidence:
                text = self._extract_evidence_text(evidence)
                if text:
                    texts.append(text)
            for call in record.tool_calls:
                result = call.result
                if isinstance(result, dict):
                    text = self._extract_answer_content(result)
                    if text:
                        texts.append(text)
        return texts

    def _extract_evidence_text(self, evidence: Any) -> Optional[str]:
        if evidence is None:
            return None
        if isinstance(evidence, str):
            return evidence
        if hasattr(evidence, "evidence"):
            inner = getattr(evidence, "evidence")
            if isinstance(inner, (str, bytes)):
                return inner if isinstance(inner, str) else inner.decode("utf-8", "ignore")
            if isinstance(inner, dict):
                return inner.get("text") or inner.get("content") or str(inner)
        if isinstance(evidence, dict):
            text = evidence.get("evidence") or evidence.get("text") or evidence.get("content")
            if isinstance(text, str):
                return text
            if isinstance(text, dict):
                return text.get("text") or text.get("content") or str(text)
        return str(evidence) if isinstance(evidence, (int, float)) else None
