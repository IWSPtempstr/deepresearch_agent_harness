# 由 agents/worker_coordinator.py 拆分（T-014），方法体与原位置逐行一致。
"""单任务/隔离执行、状态合并、失败记录与重试（原 _execute_single_task 等 6 个方法）。"""

from typing import Dict, List, Optional, Tuple
import logging
from deepresearch_agent.agents.multi_agent.core.execution_record import (
    ExecutionMetadata,
    ExecutionRecord,
)
from deepresearch_agent.agents.multi_agent.core.plan_spec import (
    PlanExecutionSignal,
    TaskNode,
)
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.executor.base_executor import (
    BaseExecutor,
    TaskExecutionResult,
)
from deepresearch_agent.config.settings import (
    MULTI_AGENT_REFLECTION_ALLOW_RETRY,
    MULTI_AGENT_REFLECTION_MAX_RETRIES,
)
_LOGGER = logging.getLogger(__name__)


class CoordinatorTasksMixin:
    def _execute_isolated_task(
        self,
        *,
        shared_state: PlanExecuteState,
        signal: PlanExecutionSignal,
        task: TaskNode,
        task_map: Dict[str, TaskNode],
    ) -> Tuple[bool, Optional[str], PlanExecuteState, List[ExecutionRecord]]:
        """Execute one worker against a deep state snapshot, never shared state."""
        isolated_state = shared_state.model_copy(deep=True)
        isolated_results: List[ExecutionRecord] = []
        success, reason = self._execute_single_task(
            state=isolated_state,
            signal=signal,
            task=task.model_copy(deep=True),
            task_map={key: value.model_copy(deep=True) for key, value in task_map.items()},
            results=isolated_results,
            skip_dependency_check=True,
        )
        return success, reason, isolated_state, isolated_results

    def _merge_isolated_state(
        self,
        *,
        shared_state: PlanExecuteState,
        isolated_state: PlanExecuteState,
        task_id: str,
        records: List[ExecutionRecord],
    ) -> None:
        """Apply a worker's local delta on the coordinator thread."""
        existing_record_ids = {record.record_id for record in shared_state.execution_records}
        for record in records:
            if record.record_id not in existing_record_ids:
                shared_state.execution_records.append(record)
                existing_record_ids.add(record.record_id)

        shared_context = shared_state.execution_context
        local_context = isolated_state.execution_context
        if shared_context is not None and local_context is not None:
            shared_context.retrieval_cache.update(local_context.retrieval_cache)
            shared_context.intermediate_results.update(local_context.intermediate_results)
            shared_context.evidence_registry.update(local_context.evidence_registry)
            shared_context.reflection_retry_counts.update(local_context.reflection_retry_counts)

            for completed_id in local_context.completed_task_ids:
                if completed_id not in shared_context.completed_task_ids:
                    shared_context.completed_task_ids.append(completed_id)

            existing_calls = {
                (str(item.get("task_id")), str(item.get("tool_name")), str(item.get("timestamp")))
                for item in shared_context.tool_call_history
            }
            for item in local_context.tool_call_history:
                identity = (str(item.get("task_id")), str(item.get("tool_name")), str(item.get("timestamp")))
                if identity not in existing_calls:
                    shared_context.tool_call_history.append(item)
                    existing_calls.add(identity)

            existing_errors = {
                (str(item.get("task_id")), str(item.get("reason")), str(item.get("error")))
                for item in shared_context.errors
            }
            for item in local_context.errors:
                identity = (str(item.get("task_id")), str(item.get("reason")), str(item.get("error")))
                if identity not in existing_errors:
                    shared_context.errors.append(item)
                    existing_errors.add(identity)

            shared_context.updated_at = local_context.updated_at

        if shared_state.plan is not None and isolated_state.plan is not None:
            local_node = next(
                (node for node in isolated_state.plan.task_graph.nodes if node.task_id == task_id),
                None,
            )
            if local_node is not None:
                shared_state.plan.update_task_status(task_id, local_node.status)

    def _execute_single_task(
        self,
        *,
        state: PlanExecuteState,
        signal: PlanExecutionSignal,
        task: TaskNode,
        task_map: Dict[str, TaskNode],
        results: List[ExecutionRecord],
        skip_dependency_check: bool,
    ) -> Tuple[bool, Optional[str]]:
        if not skip_dependency_check:
            dependency_ok, dependency_error, failure_reason = self._check_dependencies(task, state)
            if not dependency_ok:
                _LOGGER.error(
                    "任务依赖未满足，跳过执行: task_id=%s reason=%s",
                    task.task_id,
                    dependency_error,
                )
                failure_record = self._create_failure_record(
                    state,
                    task,
                    dependency_error or "依赖未满足",
                    failure_reason=failure_reason,
                )
                results.append(failure_record)
                return False, failure_reason

        executor = self._select_executor(task.task_type)
        if executor is None:
            _LOGGER.error("未找到匹配的执行器: task_id=%s type=%s", task.task_id, task.task_type)
            failure_record = self._create_failure_record(
                state,
                task,
                f"未找到任务类型 {task.task_type} 对应的执行器",
                failure_reason="executor_not_found",
            )
            results.append(failure_record)
            return False, "executor_not_found"

        try:
            if state.plan is not None:
                state.plan.update_task_status(task.task_id, "running")

            exec_result = executor.execute_task(task, state, signal)
            results.append(exec_result.record)

            if (
                task.task_type == "reflection"
                and MULTI_AGENT_REFLECTION_ALLOW_RETRY
            ):
                retry_result = self._handle_reflection_retry(
                    task=task,
                    initial_result=exec_result,
                    state=state,
                    signal=signal,
                    task_map=task_map,
                    executor=executor,
                    results=results,
                )
                if retry_result is not None:
                    exec_result = retry_result

            if exec_result.success:
                return True, None
            return False, exec_result.error or "execution_failed"

        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("任务执行异常: task_id=%s error=%s", task.task_id, exc)
            failure_record = self._create_failure_record(
                state,
                task,
                f"任务执行异常: {exc}",
                failure_reason="execution_exception",
            )
            results.append(failure_record)
            return False, "execution_exception"

    def _create_failure_record(
        self,
        state: PlanExecuteState,
        task: TaskNode,
        error: str,
        *,
        failure_reason: str = "unknown",
    ) -> ExecutionRecord:
        """
        当无可用执行器时创建失败记录，并更新状态。
        """
        metadata = ExecutionMetadata(
            worker_type="worker_coordinator",
            latency_seconds=0.0,
            tool_calls_count=0,
            evidence_count=0,
            environment={"reason": failure_reason},
        )

        record = ExecutionRecord(
            task_id=task.task_id,
            session_id=state.session_id,
            worker_type="worker_coordinator",
            inputs={
                "task": task.model_dump(),
            },
            tool_calls=[],
            evidence=[],
            metadata=metadata,
        )

        if state.execution_context is not None:
            state.execution_context.errors.append(
                {
                    "task_id": task.task_id,
                    "error": error,
                    "worker_type": "worker_coordinator",
                    "reason": failure_reason,
                }
            )

        state.execution_records.append(record)

        if state.plan is not None:
            state.plan.update_task_status(task.task_id, "failed")

        return record

    def _check_dependencies(
        self,
        task: TaskNode,
        state: PlanExecuteState,
    ) -> tuple[bool, Optional[str], str]:
        """
        检查任务依赖是否满足。

        返回 (是否可执行, 错误信息, 失败原因标签)。
        """
        if not task.depends_on:
            return True, None, "none"

        plan = state.plan
        status_map: Dict[str, str] = {}
        if plan is not None:
            status_map = {node.task_id: node.status for node in plan.task_graph.nodes}

        exec_context = state.execution_context
        completed_ids = set(exec_context.completed_task_ids if exec_context else [])

        failed_dependencies = []
        pending_dependencies = []
        missing_dependencies = []

        for dep_id in task.depends_on:
            status = status_map.get(dep_id)
            if status == "failed":
                failed_dependencies.append(dep_id)
            elif status == "completed" or dep_id in completed_ids:
                continue
            elif status is None:
                missing_dependencies.append(dep_id)
            else:
                pending_dependencies.append(dep_id)

        if failed_dependencies:
            return (
                False,
                f"依赖任务失败: {', '.join(failed_dependencies)}",
                "dependency_failed",
            )

        if missing_dependencies:
            return (
                False,
                f"依赖任务缺失: {', '.join(missing_dependencies)}",
                "dependency_missing",
            )

        if pending_dependencies:
            return (
                False,
                f"依赖任务未完成: {', '.join(pending_dependencies)}",
                "dependency_unfinished",
            )

        if not all(dep in completed_ids for dep in task.depends_on):
            remaining = [dep for dep in task.depends_on if dep not in completed_ids]
            return (
                False,
                f"依赖任务尚未标记完成: {', '.join(remaining)}",
                "dependency_unfinished",
            )

        return True, None, "ready"

    def _handle_reflection_retry(
        self,
        *,
        task: TaskNode,
        initial_result: TaskExecutionResult,
        state: PlanExecuteState,
        signal: PlanExecutionSignal,
        task_map: Dict[str, TaskNode],
        executor: BaseExecutor,
        results: List[ExecutionRecord],
    ) -> Optional[TaskExecutionResult]:
        """
        处理反思任务的自动重试逻辑，必要时重新执行目标任务并再次运行反思。
        """
        reflection = getattr(initial_result.record, "reflection", None)  # type: ignore[attr-defined]
        if reflection is None or not reflection.needs_retry:
            return None

        exec_context = state.execution_context
        if exec_context is None:
            return None

        target_task_id = initial_result.record.metadata.environment.get("target_task_id")
        if not target_task_id:
            _LOGGER.warning(
                "反思任务缺失 target_task_id，无法执行重试: task_id=%s",
                task.task_id,
            )
            return None

        final_result: Optional[TaskExecutionResult] = None
        retry_counts = exec_context.reflection_retry_counts

        while (
            reflection is not None
            and reflection.needs_retry
            and retry_counts.get(target_task_id, 0)
            < MULTI_AGENT_REFLECTION_MAX_RETRIES
        ):
            attempt_index = retry_counts.get(target_task_id, 0) + 1
            retry_counts[target_task_id] = attempt_index

            target_task = task_map.get(target_task_id)
            if target_task is None:
                _LOGGER.warning(
                    "无法找到反思重试的目标任务: target_task_id=%s",
                    target_task_id,
                )
                break

            target_executor = self._select_executor(target_task.task_type)
            if target_executor is None:
                _LOGGER.warning(
                    "缺少处理目标任务的执行器，终止反思重试: task_type=%s",
                    target_task.task_type,
                )
                break

            _LOGGER.info(
                "触发反思重试: target_task_id=%s attempt=%s/%s",
                target_task_id,
                attempt_index,
                MULTI_AGENT_REFLECTION_MAX_RETRIES,
            )
            if state.plan is not None:
                state.plan.update_task_status(target_task_id, "running")
            retry_result = target_executor.execute_task(target_task, state, signal)
            results.append(retry_result.record)

            if not retry_result.success:
                _LOGGER.warning(
                    "目标任务重试失败，终止后续反思: target_task_id=%s",
                    target_task_id,
                )
                break

            if state.plan is not None:
                state.plan.update_task_status(task.task_id, "running")
            updated_result = executor.execute_task(task, state, signal)
            results.append(updated_result.record)
            final_result = updated_result
            reflection = getattr(updated_result.record, "reflection", None)

        if (
            reflection is not None
            and reflection.needs_retry
            and retry_counts.get(target_task_id, 0)
            >= MULTI_AGENT_REFLECTION_MAX_RETRIES
        ):
            _LOGGER.info(
                "反思重试已达上限，仍未通过验证: target_task_id=%s",
                target_task_id,
            )

        if final_result is None:
            return None
        return final_result
