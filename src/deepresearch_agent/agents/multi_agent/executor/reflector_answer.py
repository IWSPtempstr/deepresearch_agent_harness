# 由 agents/reflector.py 拆分（T-014），方法体与原位置逐行一致。
"""从状态/记录/中间产物解析答案（原 _resolve_query_answer 等 7 个方法）。"""

from typing import Any, Dict, Optional, Tuple, List
from deepresearch_agent.agents.multi_agent.core.execution_record import (
    ExecutionMetadata,
    ExecutionRecord,
    ReflectionResult,
    ToolCall,
)
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState


class ReflectionAnswerMixin:
    def _resolve_query_answer(
        self,
        state: PlanExecuteState,
        payload: Dict[str, Any],
        *,
        current_task_id: str,
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """
        确定用于校验的 query 与 answer。
        """
        query = str(payload.get("query") or state.plan_context.refined_query or state.plan_context.original_query or state.input or "")
        answer: Optional[str] = payload.get("answer")

        target_task_id = payload.get("target_task_id")
        if answer is None and target_task_id:
            answer = self._lookup_answer_from_state(state, target_task_id)

        if answer is None:
            answer, target_task_id = self._fallback_from_intermediate(
                state, target_task_id, current_task_id
            )

        if answer is None and state.execution_records:
            for record in reversed(state.execution_records):
                if record.worker_type == self.worker_type:
                    continue
                candidate = self._extract_answer_from_record(record)
                if candidate:
                    answer = candidate
                    if target_task_id is None:
                        target_task_id = record.task_id
                    break

        if answer is None and isinstance(state.response, str) and state.response.strip():
            answer = state.response.strip()

        return query, answer, target_task_id

    def _lookup_answer_from_state(
        self,
        state: PlanExecuteState,
        target_task_id: str,
    ) -> Optional[str]:
        """
        从ExecutionContext中查找目标任务的答案。
        """
        context = state.execution_context
        if context is None:
            return None

        intermediate = context.intermediate_results.get(target_task_id)
        if not intermediate:
            return None

        if isinstance(intermediate, dict):
            answer = intermediate.get("answer") or intermediate.get("research_result")
            if isinstance(answer, str):
                return answer.strip()
            if isinstance(answer, dict):
                candidate = self._extract_from_dict(answer)
                if candidate:
                    return candidate
                nested = answer.get("raw_result")
                if isinstance(nested, dict):
                    nested_candidate = self._extract_from_dict(nested)
                    if nested_candidate:
                        return nested_candidate
        return None

    def _extract_answer_from_record(
        self,
        record: ExecutionRecord,
    ) -> Optional[str]:
        """
        从已有的执行记录中提取可能的答案。
        """
        for call in record.tool_calls:
            result = call.result
            if isinstance(result, dict):
                candidate = self._extract_from_dict(result)
                if candidate:
                    return candidate
            elif isinstance(result, str) and result.strip():
                return result.strip()
        return None

    def _fallback_from_intermediate(
        self,
        state: PlanExecuteState,
        target_task_id: Optional[str],
        current_task_id: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        context = state.execution_context
        if context is None or not context.intermediate_results:
            return None, target_task_id

        if target_task_id:
            candidate = self._lookup_answer_from_state(state, target_task_id)
            if candidate:
                return candidate, target_task_id

        for task_id, payload in reversed(list(context.intermediate_results.items())):
            if task_id in {current_task_id, "__reflection_retry__"}:
                continue
            if not isinstance(payload, dict):
                continue
            candidate = payload.get("answer")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip(), task_id if target_task_id is None else target_task_id
            raw_result = payload.get("raw_result")
            if isinstance(raw_result, dict):
                extracted = self._extract_from_dict(raw_result)
                if extracted:
                    return extracted, task_id if target_task_id is None else target_task_id
        return None, target_task_id

    @staticmethod
    def _extract_from_dict(payload: Dict[str, Any]) -> Optional[str]:
        candidate_keys = (
            "answer",
            "final_answer",
            "final_report",
            "response",
            "summary",
            "output",
        )
        for key in candidate_keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        report_block = payload.get("report")
        if isinstance(report_block, dict):
            report_answer = report_block.get("final_report") or report_block.get("summary")
            if isinstance(report_answer, str) and report_answer.strip():
                return report_answer.strip()
        return None

    def _extract_answer_content(self, payload: Dict[str, Any]) -> Optional[str]:
        candidate_keys = (
            "final_report",
            "final_answer",
            "summary",
            "answer",
            "response",
            "content",
        )
        for key in candidate_keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        nested = payload.get("report")
        if isinstance(nested, dict):
            text = nested.get("final_report") or nested.get("summary")
            if isinstance(text, str) and text.strip():
                return text
        return None

    def _extract_possible_text(self, payload: Any) -> Optional[str]:
        if payload is None:
            return None
        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        if isinstance(payload, dict):
            direct = payload.get("answer") or payload.get("text") or payload.get("content")
            if isinstance(direct, str) and direct.strip():
                return direct.strip()
            return self._extract_answer_content(payload)
        if hasattr(payload, "final_report") and isinstance(payload.final_report, str):
            text = payload.final_report.strip()
            if text:
                return text
        return None
