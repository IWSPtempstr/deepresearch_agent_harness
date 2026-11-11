# 由 agents/reflector.py 拆分（T-014），方法体与原位置逐行一致。
"""反思任务执行主体（原 execute_task，167 行整方法搬移）。"""

from typing import Any, Dict, Optional, List
import time
from deepresearch_agent.agents.multi_agent.core.execution_record import (
    ExecutionMetadata,
    ExecutionRecord,
    ReflectionResult,
    ToolCall,
)
from deepresearch_agent.agents.multi_agent.core.plan_spec import (
    PlanExecutionSignal,
    TaskNode,
)
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.executor.base_executor import (
    TaskExecutionResult,
)
from deepresearch_agent.search.tool.validation_tool import AnswerValidationTool


class ReflectionExecuteMixin:
    def execute_task(
        self,
        task: TaskNode,
        state: PlanExecuteState,
        signal: PlanExecutionSignal,
    ) -> TaskExecutionResult:
        """
        对指定任务结果进行反思与验证。
        """
        payload = self.build_default_inputs(task)
        query, answer, target_task_id = self._resolve_query_answer(
            state, payload, current_task_id=task.task_id
        )
        reference_keywords = self._build_reference_keywords(
            state,
            target_task_id,
            query,
            reflection_task_id=task.task_id,
        )

        evaluation_text = self._resolve_evaluation_text(
            answer,
            state,
            target_task_id,
        )
        if not evaluation_text:
            evaluation_text = query

        start_time = time.perf_counter()
        validation_payload: Optional[Dict[str, Any]] = None
        validation_passed = False
        error: Optional[str] = None
        suggestions: List[str] = []

        if not query:
            error = "缺少用于验证的查询内容"
        elif answer is None:
            error = "未找到可验证的答案"
        else:
            try:
                validation_tool = self._validation_tool
                if validation_tool is None:
                    validation_tool = self._validation_tools.setdefault(
                        state.source_mode,
                        AnswerValidationTool(enable_graph=state.source_mode == "graphrag"),
                    )
                validation_payload = validation_tool.validate(
                    query,
                    evaluation_text,
                    reference_keywords=reference_keywords,
                )
                validation_result = validation_payload.get("validation", {})
                validation_passed = bool(validation_result.get("passed", False))
                for key, passed in validation_result.items():
                    if key == "passed":
                        continue
                    if not passed:
                        suggestions.append(f"验证项 {key} 未通过")
            except Exception as exc:  # noqa: BLE001
                error = f"答案验证失败: {exc}"

        if (
            not validation_passed
            and error is None
            and isinstance(evaluation_text, str)
            and evaluation_text.strip()
        ):
            suggestions.extend(
                self._derive_keyword_suggestions(
                    query,
                    evaluation_text,
                    reference_keywords=reference_keywords,
                )
            )

        if suggestions:
            # 去重保持顺序
            suggestions = list(dict.fromkeys(suggestions))

        latency = time.perf_counter() - start_time

        tool_calls = []
        tool_status = "failed" if error else "success"
        if validation_payload is not None:
            tool_calls.append(
                ToolCall(
                    tool_name="answer_validator",
                    args={
                        "query": query,
                        "answer": evaluation_text,
                        "reference_keywords": reference_keywords or {},
                    },
                    result=validation_payload,
                    status=tool_status,
                    latency_ms=round(latency * 1000, 3),
                    error=(
                        error
                        if error
                        else (None if validation_passed else "验证未通过")
                    ),
                )
            )

        metadata = ExecutionMetadata(
            worker_type=self.worker_type,
            latency_seconds=latency,
            tool_calls_count=len(tool_calls),
            evidence_count=0,
            environment={
                "execution_mode": signal.execution_mode,
                "target_task_id": target_task_id,
                "validation_passed": validation_passed,
                "reference_keywords": reference_keywords or {},
                "suggestions": suggestions,
                "evaluation_preview": evaluation_text[:160] if evaluation_text else "",
            },
        )

        if validation_passed:
            reflection_reason = "验证通过，未发现明显问题"
        elif error:
            reflection_reason = error
        else:
            reflection_reason = "验证未通过，建议回滚或追加检索"
        if suggestions and not validation_passed:
            # 使用中文提示串联建议列表，方便在日志中观察反思结论
            reflection_reason = f"{reflection_reason}；建议：{'；'.join(suggestions[:3])}"

        reflection = ReflectionResult(
            success=validation_passed,
            confidence=0.85 if validation_passed else 0.4,
            suggestions=suggestions if not validation_passed else [],
            needs_retry=not validation_passed,
            reasoning=reflection_reason,
        )

        record = ExecutionRecord(
            task_id=task.task_id,
            session_id=state.session_id,
            worker_type=self.worker_type,
            inputs={
                "payload": payload,
                "query": query,
                "answer": answer,
                "evaluation_text": evaluation_text,
            },
            tool_calls=tool_calls,
            reflection=reflection,
            metadata=metadata,
        )

        self._update_state(
            state,
            task,
            record,
            success=error is None,
            error=error,
            target_task_id=target_task_id,
            needs_retry=not validation_passed,
        )

        return TaskExecutionResult(
            record=record,
            success=error is None,
            error=error,
        )
