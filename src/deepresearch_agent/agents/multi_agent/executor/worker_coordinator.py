"""
执行调度器

根据 PlanExecutionSignal 调度不同类型的 Worker 执行任务，支持串行与并行模式。
"""

# 由 agents/worker_coordinator.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, register_executor, execute_plan, _prepare_tasks, _select_executor。
# MRO：Mixin 在前、ABC 基类殿后（abc 抽象检查沿 MRO getattr，基类在前会忽略 Mixin 中的具体实现）。
from typing import Callable, Dict, List, Optional
import logging
from deepresearch_agent.agents.multi_agent.core.execution_record import (
    ExecutionRecord,
)
from deepresearch_agent.agents.multi_agent.core.plan_spec import (
    PlanExecutionSignal,
    TaskNode,
)
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.executor.base_executor import BaseExecutor
from deepresearch_agent.agents.multi_agent.executor.research_executor import ResearchExecutor
from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor
from deepresearch_agent.agents.multi_agent.executor.reflector import ReflectionExecutor
from deepresearch_agent.config.settings import (
    MULTI_AGENT_WORKER_EXECUTION_MODE,
    MULTI_AGENT_WORKER_MAX_CONCURRENCY,
)
from deepresearch_agent.retrieval.base import RetrievalProvider
from deepresearch_agent.retrieval.router import RetrievalRouter
_LOGGER = logging.getLogger(__name__)

from deepresearch_agent.agents.multi_agent.executor.coordinator_modes import CoordinatorModesMixin
from deepresearch_agent.agents.multi_agent.executor.coordinator_tasks import CoordinatorTasksMixin

class WorkerCoordinator(CoordinatorModesMixin, CoordinatorTasksMixin):
    """
    Worker协调器

    负责解析计划信号、选择合适的执行器并串行或并行地执行任务。
    默认模式可通过环境变量或构造参数进行配置。
    """

    def __init__(
        self,
        executors: Optional[List[BaseExecutor]] = None,
        *,
        execution_mode: Optional[str] = None,
        max_parallel_workers: Optional[int] = None,
        retrieval_provider: Optional[RetrievalProvider] = None,
        retrieval_router: Optional[RetrievalRouter] = None,
    ) -> None:
        if executors is None:
            executors = [
                RetrievalExecutor(provider=retrieval_provider, router=retrieval_router),
                ResearchExecutor(),
                ReflectionExecutor(),
            ]
        self.executors = executors
        configured_mode = (
            execution_mode.strip().lower()
            if isinstance(execution_mode, str)
            else MULTI_AGENT_WORKER_EXECUTION_MODE
        )
        if configured_mode not in {"sequential", "parallel"}:
            raise ValueError(
                f"WorkerCoordinator execution_mode 必须为 sequential 或 parallel，当前为 {configured_mode}"
            )
        workers = max_parallel_workers or MULTI_AGENT_WORKER_MAX_CONCURRENCY
        if workers < 1:
            raise ValueError("max_parallel_workers 必须大于等于 1")
        self.execution_mode = configured_mode
        self.max_parallel_workers = workers

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

    def execute_plan(
        self,
        state: PlanExecuteState,
        signal: PlanExecutionSignal,
        *,
        stop_predicate: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[str, TaskNode, Optional[ExecutionRecord]], None]] = None,
    ) -> List[ExecutionRecord]:
        """根据计划信号执行所有任务，返回执行记录列表。"""
        task_map = self._prepare_tasks(signal)

        if state.plan is not None:
            state.plan.status = "executing"

        effective_mode = self._resolve_execution_mode(signal.execution_mode)
        if effective_mode == "parallel":
            results = self._execute_parallel(
                state, signal, task_map, progress_callback=progress_callback,
            )
        else:
            results = self._execute_sequential(
                state, signal, task_map, stop_predicate=stop_predicate,
                progress_callback=progress_callback,
            )

        if state.plan is not None:
            node_status = [node.status for node in state.plan.task_graph.nodes]
            if node_status and all(status == "completed" for status in node_status):
                state.plan.status = "completed"
            elif any(status == "failed" for status in node_status):
                state.plan.status = "failed"

        sequence = signal.execution_sequence or list(task_map.keys())
        ordered_ids = {task_id: index for index, task_id in enumerate(sequence)}
        results.sort(key=lambda record: (ordered_ids.get(record.task_id, len(ordered_ids)), record.created_at, record.record_id))
        result_ids = {record.record_id for record in results}
        prior_records = [record for record in state.execution_records if record.record_id not in result_ids]
        state.execution_records = prior_records + results
        if state.execution_context is not None:
            state.execution_context.completed_task_ids.sort(key=lambda task_id: ordered_ids.get(task_id, len(ordered_ids)))
        return results

    def _prepare_tasks(self, signal: PlanExecutionSignal) -> Dict[str, TaskNode]:
        """将信号中的任务恢复为TaskNode对象"""
        task_map: Dict[str, TaskNode] = {}
        for task_payload in signal.tasks:
            try:
                task = TaskNode(**task_payload)
                task_map[task.task_id] = task
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("任务解析失败: payload=%s error=%s", task_payload, exc)
        return task_map

    def _select_executor(self, task_type: str) -> Optional[BaseExecutor]:
        for executor in self.executors:
            if executor.can_handle(task_type):
                return executor
        return None
