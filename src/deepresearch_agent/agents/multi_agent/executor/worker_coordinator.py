"""
执行调度器（T-408：仅保留 coreact 的单动作执行入口）。

整批执行（execute_plan/串行/并行调度/隔离合并/反思重试）已由统一
ActionRunner（harness/action_runner.py）承接并删除重复实现——
Controller + ActionRunner 是唯一的执行路径。
"""

# 由 agents/worker_coordinator.py 拆分（T-014）；T-408 起仅保留 execute_action。
from typing import Callable, List, Optional
import logging
from deepresearch_agent.agents.multi_agent.core.execution_record import (
    ExecutionRecord,
)
from deepresearch_agent.agents.multi_agent.core.plan_spec import (
    TaskNode,
)
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.executor.base_executor import BaseExecutor
from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor
from deepresearch_agent.retrieval.base import RetrievalProvider
from deepresearch_agent.retrieval.router import RetrievalRouter
_LOGGER = logging.getLogger(__name__)


class WorkerCoordinator:
    """coreact 单动作执行器宿主：选择执行器并执行一个任务动作。"""

    def __init__(
        self,
        executors: Optional[List[BaseExecutor]] = None,
        *,
        max_parallel_workers: Optional[int] = None,
        retrieval_provider: Optional[RetrievalProvider] = None,
        retrieval_router: Optional[RetrievalRouter] = None,
    ) -> None:
        if executors is None:
            executors = [
                RetrievalExecutor(provider=retrieval_provider, router=retrieval_router),
            ]
        self.executors = executors
        # 兼容保留：并发上限由 Harness/ActionRunner 权威约束（预算/准入），
        # 此处仅保存供审计读取。
        self.max_parallel_workers = max_parallel_workers or 1

    def register_executor(self, executor: BaseExecutor) -> None:
        """注册额外的执行器"""
        self.executors.append(executor)

    def execute_action(
        self,
        state: PlanExecuteState,
        task: TaskNode,
        *,
        progress_callback: Optional[Callable[[str, TaskNode, Optional[ExecutionRecord]], None]] = None,
    ) -> tuple[bool, Optional[str], Optional[ExecutionRecord]]:
        """Execute exactly one task action; the caller owns the controller loop."""
        if state.plan is None:
            return False, "missing_plan", None
        task_map = {
            node.task_id: node
            for node in state.plan.task_graph.nodes
        }
        task_map[task.task_id] = task
        signal = state.plan.to_execution_signal()
        signal.execution_mode = "sequential"
        signal.tasks = [task.model_dump()]
        signal.execution_sequence = [task.task_id]
        if progress_callback is not None:
            progress_callback("task.started", task, None)
        records: List[ExecutionRecord] = []
        success, reason = self._execute_single_task(
            state=state,
            signal=signal,
            task=task,
            task_map=task_map,
            results=records,
            skip_dependency_check=False,
        )
        record = records[-1] if records else None
        if progress_callback is not None and record is not None:
            progress_callback("task.completed", task, record)
        return success, reason, record

    def _execute_single_task(
        self,
        *,
        state: PlanExecuteState,
        signal,
        task: TaskNode,
        task_map: dict,
        results: List[ExecutionRecord],
        skip_dependency_check: bool,
    ) -> tuple[bool, Optional[str]]:
        """单任务执行（依赖校验 + 失败记录；reflection 重试已随旧引擎删除）。"""
        if not skip_dependency_check and task.depends_on:
            plan = state.plan
            status_map = {node.task_id: node.status for node in plan.task_graph.nodes} if plan is not None else {}
            exec_context = state.execution_context
            completed_ids = set(exec_context.completed_task_ids if exec_context else [])
            unfinished = [
                dep for dep in task.depends_on
                if status_map.get(dep) == "failed" or (dep not in completed_ids and status_map.get(dep) != "completed")
            ]
            if unfinished:
                _LOGGER.error("任务依赖未满足，跳过执行: task_id=%s reason=%s", task.task_id, unfinished)
                failure_record = self._create_failure_record(
                    state, task, f"依赖未满足: {', '.join(unfinished)}",
                    failure_reason="dependency_unfinished",
                )
                results.append(failure_record)
                return False, "dependency_unfinished"

        executor = self._select_executor(task.task_type)
        if executor is None:
            _LOGGER.error("未找到匹配的执行器: task_id=%s type=%s", task.task_id, task.task_type)
            failure_record = self._create_failure_record(
                state, task, f"未找到任务类型 {task.task_type} 对应的执行器",
                failure_reason="executor_not_found",
            )
            results.append(failure_record)
            return False, "executor_not_found"

        try:
            if state.plan is not None:
                state.plan.update_task_status(task.task_id, "running")
            exec_result = executor.execute_task(task, state, signal)
            results.append(exec_result.record)
            if exec_result.success:
                return True, None
            return False, exec_result.error or "execution_failed"
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("任务执行异常: task_id=%s error=%s", task.task_id, exc)
            failure_record = self._create_failure_record(
                state, task, f"任务执行异常: {exc}",
                failure_reason="execution_exception",
            )
            results.append(failure_record)
            return False, "execution_exception"

    def _select_executor(self, task_type: str) -> Optional[BaseExecutor]:
        for executor in self.executors:
            if executor.can_handle(task_type):
                return executor
        return None

    @staticmethod
    def _create_failure_record(
        state: PlanExecuteState,
        task: TaskNode,
        error: str,
        *,
        failure_reason: str = "unknown",
    ) -> ExecutionRecord:
        """当无可用执行器或依赖未满足时创建失败记录，并更新状态。"""
        from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionMetadata

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
            inputs={"task": task.model_dump()},
            tool_calls=[],
            evidence=[],
            metadata=metadata,
        )
        if state.execution_context is not None:
            state.execution_context.errors.append(
                {"task_id": task.task_id, "error": error, "worker_type": "worker_coordinator", "reason": failure_reason}
            )
        state.execution_records.append(record)
        if state.plan is not None:
            state.plan.update_task_status(task.task_id, "failed")
        return record
