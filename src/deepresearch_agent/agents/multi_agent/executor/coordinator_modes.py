# 由 agents/worker_coordinator.py 拆分（T-014），方法体与原位置逐行一致。
"""执行模式解析与串行/并行调度（原 _resolve_execution_mode 等 3 个方法）。"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Dict, List, Optional, Tuple
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
_LOGGER = logging.getLogger(__name__)


class CoordinatorModesMixin:
    def _resolve_execution_mode(self, requested_mode: str) -> str:
        requested = (requested_mode or "sequential").lower()
        if requested not in {"sequential", "parallel"}:
            if requested == "adaptive":
                _LOGGER.warning("Planner 请求 adaptive 模式，当前仅支持串行或并行，将使用 %s", self.execution_mode)
            else:
                _LOGGER.warning(
                    "Planner 请求的执行模式 %s 无效，将使用 %s",
                    requested_mode,
                    self.execution_mode,
                )
            requested = "sequential"

        effective = self.execution_mode
        if effective != requested:
            _LOGGER.info(
                "WorkerCoordinator 使用 %s 模式执行计划（覆盖 planner 请求 %s）",
                effective,
                requested_mode,
            )
        else:
            _LOGGER.debug("WorkerCoordinator 以 %s 模式执行计划", effective)
        return effective

    def _execute_sequential(
        self,
        state: PlanExecuteState,
        signal: PlanExecutionSignal,
        task_map: Dict[str, TaskNode],
        *,
        stop_predicate: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[str, TaskNode, Optional[ExecutionRecord]], None]] = None,
    ) -> List[ExecutionRecord]:
        results: List[ExecutionRecord] = []
        sequence = signal.execution_sequence or list(task_map.keys())
        for task_id in sequence:
            if stop_predicate is not None and stop_predicate():
                _LOGGER.warning(
                    "执行阶段已到报告/验证预留边界，停止扩展后续任务: next_task=%s",
                    task_id,
                )
                break
            task = task_map.get(task_id)
            if task is None:
                _LOGGER.warning("计划信号中包含未知任务: %s", task_id)
                continue
            if progress_callback is not None:
                progress_callback("task.started", task, None)
            start_index = len(results)
            self._execute_single_task(
                state=state,
                signal=signal,
                task=task,
                task_map=task_map,
                results=results,
                skip_dependency_check=False,
            )
            if progress_callback is not None:
                for record in results[start_index:]:
                    progress_callback("task.completed", task, record)
        return results

    def _execute_parallel(
        self,
        state: PlanExecuteState,
        signal: PlanExecutionSignal,
        task_map: Dict[str, TaskNode],
        *,
        progress_callback: Optional[Callable[[str, TaskNode, Optional[ExecutionRecord]], None]] = None,
    ) -> List[ExecutionRecord]:
        results: List[ExecutionRecord] = []
        sequence = signal.execution_sequence or list(task_map.keys())
        missing = [task_id for task_id in sequence if task_id not in task_map]
        for task_id in missing:
            _LOGGER.warning("计划信号中包含未知任务: %s", task_id)

        pending: List[str] = [task_id for task_id in sequence if task_id in task_map]
        if not pending:
            return results

        max_workers = min(self.max_parallel_workers, max(1, len(pending)))
        inflight: Dict[object, str] = {}
        task_status: Dict[str, str] = {task_id: "pending" for task_id in pending}
        sequence_index = {task_id: index for index, task_id in enumerate(sequence)}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while pending or inflight:
                scheduled_this_round = False

                for task_id in list(pending):
                    if len(inflight) >= max_workers:
                        break

                    status = task_status.get(task_id)
                    if status != "pending":
                        pending.remove(task_id)
                        continue

                    task = task_map[task_id]
                    dependency_ok, dependency_error, failure_reason = self._check_dependencies(task, state)
                    if dependency_ok:
                        if progress_callback is not None:
                            progress_callback("task.started", task, None)
                        future = executor.submit(
                            self._execute_isolated_task,
                            shared_state=state,
                            signal=signal,
                            task=task,
                            task_map=task_map,
                        )
                        inflight[future] = task_id
                        task_status[task_id] = "running"
                        pending.remove(task_id)
                        scheduled_this_round = True
                        continue

                    if failure_reason in {"dependency_failed", "dependency_missing"}:
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
                        if progress_callback is not None:
                            progress_callback("task.completed", task, failure_record)
                        task_status[task_id] = "failed"
                        pending.remove(task_id)
                        scheduled_this_round = True
                    # dependency 未完成，等待下一轮

                if inflight:
                    done, _ = wait(inflight.keys(), return_when=FIRST_COMPLETED)
                    completed_batch = []
                    for future in done:
                        task_id = inflight.pop(future)
                        task = task_map[task_id]
                        try:
                            success, _reason, isolated_state, isolated_records = future.result()
                            completed_batch.append((task_id, success, isolated_state, isolated_records))
                        except Exception as exc:  # noqa: BLE001
                            _LOGGER.exception("任务执行异常: task_id=%s error=%s", task_id, exc)
                            failure_record = self._create_failure_record(
                                state,
                                task,
                                f"任务执行异常: {exc}",
                                failure_reason="execution_exception",
                            )
                            results.append(failure_record)
                            if progress_callback is not None:
                                progress_callback("task.completed", task, failure_record)
                            success = False
                            task_status[task_id] = "failed"
                        scheduled_this_round = True

                    # Workers never mutate the shared state. The coordinator merges a
                    # completed batch in plan order, making checkpoints deterministic.
                    for task_id, success, isolated_state, isolated_records in sorted(
                        completed_batch, key=lambda item: sequence_index[item[0]]
                    ):
                        self._merge_isolated_state(
                            shared_state=state,
                            isolated_state=isolated_state,
                            task_id=task_id,
                            records=isolated_records,
                        )
                        results.extend(isolated_records)
                        task_status[task_id] = "completed" if success else "failed"
                        if progress_callback is not None:
                            for record in isolated_records:
                                progress_callback("task.completed", task_map[task_id], record)
                    continue

                if not scheduled_this_round:
                    if pending:
                        for task_id in list(pending):
                            task = task_map[task_id]
                            failure_record = self._create_failure_record(
                                state,
                                task,
                                "任务依赖未解析或存在循环依赖",
                                failure_reason="dependency_unresolved",
                            )
                            results.append(failure_record)
                            if progress_callback is not None:
                                progress_callback("task.completed", task, failure_record)
                            task_status[task_id] = "failed"
                        pending.clear()
                    break

        return results
