"""
反思执行器

基于 AnswerValidationTool 对既有任务输出进行质量校验，并产出反思结果。
"""

# 由 agents/reflector.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, can_handle, _update_state。
# MRO：Mixin 在前、ABC 基类殿后（abc 抽象检查沿 MRO getattr，基类在前会忽略 Mixin 中的具体实现）。
from typing import Any, Dict, Optional, Tuple, List
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
    BaseExecutor,
    ExecutorConfig,
    TaskExecutionResult,
)
from deepresearch_agent.search.tool.validation_tool import AnswerValidationTool

from deepresearch_agent.agents.multi_agent.executor.reflector_execute import ReflectionExecuteMixin
from deepresearch_agent.agents.multi_agent.executor.reflector_answer import ReflectionAnswerMixin
from deepresearch_agent.agents.multi_agent.executor.reflector_keywords import ReflectionKeywordsMixin
from deepresearch_agent.agents.multi_agent.executor.reflector_evidence import ReflectionEvidenceMixin

class ReflectionExecutor(ReflectionExecuteMixin, ReflectionAnswerMixin, ReflectionKeywordsMixin, ReflectionEvidenceMixin, BaseExecutor):
    """
    反思任务执行器
    """

    worker_type: str = "reflection_executor"

    def __init__(
        self,
        config: Optional[ExecutorConfig] = None,
        *,
        validation_tool: Optional[AnswerValidationTool] = None,
    ) -> None:
        super().__init__(config)
        self._validation_tool = validation_tool
        self._validation_tools: Dict[str, AnswerValidationTool] = {}

    def can_handle(self, task_type: str) -> bool:
        return task_type == "reflection"

    def _update_state(
        self,
        state: PlanExecuteState,
        task: TaskNode,
        record: ExecutionRecord,
        success: bool,
        error: Optional[str],
        target_task_id: Optional[str],
        needs_retry: bool,
    ) -> None:
        """
        将执行结果写回状态，维护任务完成情况。
        """
        state.execution_records.append(record)

        exec_context = state.execution_context
        if exec_context is not None:
            action = "retry" if needs_retry else "confirm"
            exec_context.current_task_id = task.task_id
            exec_context.tool_call_history.append(
                {
                    "task_id": task.task_id,
                    "tool_name": "answer_validator",
                    "status": "success" if success else "failed",
                    "latency_ms": record.metadata.latency_seconds * 1000,
                }
            )
            exec_context.intermediate_results[task.task_id] = {
                "reflection": record.reflection.model_dump(),
                "target_task_id": target_task_id,
                "action": action,
            }

            if success:
                if task.task_id not in exec_context.completed_task_ids:
                    exec_context.completed_task_ids.append(task.task_id)
            if needs_retry:
                reason = error or "validation_failed"
                if target_task_id and target_task_id in exec_context.completed_task_ids:
                    exec_context.completed_task_ids.remove(target_task_id)
                retry_bucket = exec_context.intermediate_results.setdefault(
                    "__reflection_retry__", []
                )
                if isinstance(retry_bucket, list):
                    retry_bucket.append(
                        {
                            "target_task_id": target_task_id,
                            "reason": reason,
                            "reflection_task_id": task.task_id,
                        }
                    )
                exec_context.errors.append(
                    {
                        "task_id": task.task_id,
                        "error": reason,
                        "worker_type": self.worker_type,
                        "target_task_id": target_task_id,
                    }
                )
            elif not success:
                if target_task_id and target_task_id in exec_context.completed_task_ids:
                    exec_context.completed_task_ids.remove(target_task_id)
                exec_context.errors.append(
                    {
                        "task_id": task.task_id,
                        "error": error or "unknown",
                        "worker_type": self.worker_type,
                        "target_task_id": target_task_id,
                    }
                )

        if state.plan is not None:
            state.plan.update_task_status(task.task_id, "completed" if success else "failed")
            if needs_retry and target_task_id:
                state.plan.update_task_status(target_task_id, "pending")
            elif not success and target_task_id:
                state.plan.update_task_status(target_task_id, "pending")
