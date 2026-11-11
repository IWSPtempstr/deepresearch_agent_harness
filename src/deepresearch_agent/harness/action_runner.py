"""T-403 统一隔离 ActionRunner：预派发准入 → 副本隔离执行 → 稳定合并 → 有限重试。

替代 coreact 循环内的手写 gather/重试（design §5）：

- 准入：依赖就绪、工具白名单（SourcePolicy 派生）、批内/历史 query 去重、
  **预派发预算预约**（in-flight 动作的 tool 额度先扣减，不能等执行结束后才
  发现超发）；
- 执行：每个动作在共享状态的深拷贝上运行，绝不让 worker 直接改共享账本；
- 合并：协调者按 action sequence 顺序稳定合并 delta——完成顺序不改变最终
  状态（以来源字段维持每动作 provenance，dict 合并只增不改他任务局部结果）；
- 重试：每个动作至多一次定向修正（correction_fn 注入），所有 attempt 的记录
  与成本保留，活动结果指向最新有效尝试（删除失败历史是调用方禁止行为）。

线程安全说明：隔离的正确性来自"每动作独立深拷贝 + 合并只在协调者线程发生"，
不依赖 GIL 下 list.append 的原子性（本模块不含共享可变集合的并发写）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskNode
from deepresearch_agent.agents.multi_agent.core.rubric import TaskRubric
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState

logger = logging.getLogger(__name__)

VerifyFn = Callable[[Any, list[ExecutionRecord], set[str]], Any]
CorrectionFn = Callable[[Any, Any, str | None], Awaitable[Any | None]]
StopCheck = Callable[[], bool]
ProgressCallback = Callable[[str, TaskNode, ExecutionRecord | None], None]


@dataclass
class BatchAction:
    """批内动作：冻结接口（spec，身份/重试记账）+ 执行载荷（task）+ 验收契约（rubric）。"""

    spec: Any  # ActionSpec（避免硬依赖；由 T-402 模型提供）
    task: TaskNode
    rubric: TaskRubric

    @property
    def action_id(self) -> str:
        return str(self.spec.action_id)

    @property
    def attempt(self) -> int:
        return int(self.spec.attempt)


@dataclass
class Rejection:
    """准入拒绝记录（可观测；不静默丢弃）。"""

    action_id: str
    reason_code: str  # dependency_not_ready | tool_not_allowed | duplicate_query | budget_exhausted
    detail: str = ""


@dataclass
class AdmissionReport:
    admitted: list[BatchAction] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)


@dataclass
class AttemptOutcome:
    """单次尝试的执行结果（隔离状态随行，供协调者合并后即弃）。"""

    action: BatchAction
    success: bool
    error: str | None
    record: ExecutionRecord | None
    isolated_state: PlanExecuteState | None = None
    cancelled: bool = False
    verdict: Any = None


@dataclass
class ActionOutcome:
    """一个动作（含其全部尝试）的最终结果；active = 最新有效尝试。"""

    action: BatchAction
    attempts: list[AttemptOutcome] = field(default_factory=list)
    verdict: Any = None
    active: AttemptOutcome | None = None


class DispatchBudget:
    """预派发预算预约：tool 额度在派发前预扣，完成后释放（实际计费仍由提交层做）。"""

    def __init__(self, *, used_fn: Callable[[], int], limit: int):
        self._used_fn = used_fn
        self._limit = max(0, int(limit))
        self._reserved = 0

    def can_dispatch(self, n: int) -> bool:
        return self._used_fn() + self._reserved + max(0, n) <= self._limit

    def reserve(self, n: int) -> None:
        self._reserved += max(0, n)

    def release(self, n: int) -> None:
        self._reserved = max(0, self._reserved - max(0, n))

    @property
    def reserved(self) -> int:
        return self._reserved


def merge_isolated_state(
    *,
    shared_state: PlanExecuteState,
    isolated_state: PlanExecuteState,
    task_id: str,
    records: list[ExecutionRecord],
) -> None:
    """把一个动作的隔离 delta 合并回共享状态（只在协调者线程调用）。

    语义与 executor/worker_coordinator 的协调者合并一致（T-403 提取为公开函数）：
    记录按 record_id 去重追加；上下文字典只增不改他任务局部结果；tool 历史/错误
    按 (task_id, tool_name, timestamp) 身份去重；计划节点状态以隔离副本为准。
    """
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
        # evidence_registry：evidence tracker 子树按键合并而非整体覆盖——
        # 并发批次里先合并动作的 tracker 登记（by_key/by_id）不能被后合并者
        # 冲掉（design §5.3：dict.update 不得覆盖其他任务局部结果）。
        for registry_key, local_value in local_context.evidence_registry.items():
            shared_value = shared_context.evidence_registry.get(registry_key)
            if registry_key == "tracker" and isinstance(shared_value, dict) and isinstance(local_value, dict):
                for tracker_key, tracker_value in local_value.items():
                    if isinstance(tracker_value, dict) and isinstance(shared_value.get(tracker_key), dict):
                        shared_value[tracker_key].update(tracker_value)
                    else:
                        shared_value[tracker_key] = tracker_value
            else:
                shared_context.evidence_registry[registry_key] = local_value
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


class ActionRunner:
    """统一动作执行器：准入 / 隔离执行 / 稳定合并 / 单次定向修正。

    execute_action: (state, task, progress_callback=None) -> (success, error, record)；
    生产路径传 orchestrator.execute_action（真实执行器栈），测试可传任何等价回调。
    """

    def __init__(
        self,
        *,
        execute_action: Callable[..., tuple[bool, str | None, ExecutionRecord | None]],
        max_parallel: int,
        max_concurrency: int,
        allowed_tools: Sequence[str],
        budget: DispatchBudget | None = None,
    ):
        self._execute_action = execute_action
        self.max_parallel = max(1, int(max_parallel))
        self.max_concurrency = max(1, int(max_concurrency))
        self.allowed_tools = frozenset(allowed_tools)
        self.budget = budget

    # ---------- 预派发准入 ----------

    def admit(
        self,
        actions: Sequence[BatchAction],
        *,
        completed_task_ids: Sequence[str],
        executed_queries: Sequence[str],
    ) -> AdmissionReport:
        """依赖就绪 / 工具白名单 / query 去重 / 预算预约。

        拒绝不抛异常（调用方按 no-progress 记账）；通过准入的整批动作先预约
        tool 额度——预算不足的动作**零派发**。
        """
        report = AdmissionReport()
        completed = set(completed_task_ids)
        seen_queries = {str(query).strip() for query in executed_queries if str(query).strip()}
        admitted_count = 0
        for action in actions:
            unresolved = [dep for dep in action.task.depends_on or [] if dep not in completed]
            if unresolved:
                report.rejections.append(Rejection(action.action_id, "dependency_not_ready", f"未完成依赖: {unresolved}"))
                continue
            if action.task.task_type not in self.allowed_tools:
                report.rejections.append(
                    Rejection(action.action_id, "tool_not_allowed", f"工具 {action.task.task_type!r} 超出来源白名单")
                )
                continue
            query = str(action.spec.query or "").strip()
            if query and query in seen_queries:
                report.rejections.append(Rejection(action.action_id, "duplicate_query", query[:120]))
                continue
            # 批内已准入的数量必须计入预约判断（否则额度边界上的批次会超发）
            if self.budget is not None and not self.budget.can_dispatch(admitted_count + 1):
                report.rejections.append(Rejection(action.action_id, "budget_exhausted", "tool 额度不足（预派发预约拒绝）"))
                continue
            if query:
                seen_queries.add(query)
            admitted_count += 1
            report.admitted.append(action)
        if self.budget is not None and admitted_count:
            self.budget.reserve(admitted_count)
        return report

    def release_reserved(self, count: int) -> None:
        if self.budget is not None:
            self.budget.release(count)

    # ---------- 隔离执行 ----------

    async def execute_isolated(
        self,
        action: BatchAction,
        *,
        shared_state: PlanExecuteState,
        progress_callback: ProgressCallback | None = None,
        stop_check: StopCheck | None = None,
    ) -> AttemptOutcome:
        """在共享状态的深拷贝上执行单个动作；共享状态不被触碰。

        progress_callback 在 worker 线程内触发（与既有 driver 事件发布约定一致：
        回调内部用 run_coroutine_threadsafe 回投事件循环，不能在事件循环线程
        阻塞等待）。
        """
        if stop_check is not None and stop_check():
            return AttemptOutcome(
                action=action, success=False,
                error="cancelled_before_dispatch", record=None, cancelled=True,
            )
        isolated_state = shared_state.model_copy(deep=True)
        loop = asyncio.get_running_loop()

        def _run() -> tuple[bool, str | None, ExecutionRecord | None]:
            if progress_callback is not None:
                progress_callback("task.started", action.task, None)
            try:
                success, error, record = self._execute_action(isolated_state, action.task)
            except Exception as exc:  # noqa: BLE001 —— 执行器异常转为失败记录语义
                logger.exception("动作执行异常 action_id=%s error=%s", action.action_id, exc)
                return False, str(exc), None
            if progress_callback is not None and record is not None:
                # 与旧行为一致：仅在拿到记录时发布 completed（record=None 时
                # driver 的发布器会把它当 task.started 重复发布）
                progress_callback("task.completed", action.task, record)
            return success, error, record

        success, error, record = await loop.run_in_executor(None, _run)
        return AttemptOutcome(
            action=action, success=bool(success), error=error, record=record,
            isolated_state=isolated_state,
        )

    async def _execute_overlapping(
        self,
        actions: Sequence[BatchAction],
        *,
        shared_state: PlanExecuteState,
        progress_callback: ProgressCallback | None = None,
        stop_check: StopCheck | None = None,
    ) -> list[AttemptOutcome]:
        """并发执行（每动作独立线程 + 独立深拷贝）；并发上限取
        max_parallel 与 max_concurrency 的交集。"""
        limit = max(1, min(self.max_parallel, self.max_concurrency, len(actions)))
        semaphore = asyncio.Semaphore(limit)

        async def _guarded(action: BatchAction) -> AttemptOutcome:
            async with semaphore:
                return await self.execute_isolated(
                    action, shared_state=shared_state,
                    progress_callback=progress_callback, stop_check=stop_check,
                )

        return list(await asyncio.gather(*[_guarded(action) for action in actions]))

    # ---------- 批次：执行 → 稳定合并 → 验证 → 单次定向修正 ----------

    async def run_and_merge(
        self,
        actions: Sequence[BatchAction],
        *,
        shared_state: PlanExecuteState,
        verify_fn: VerifyFn,
        correction_fn: CorrectionFn | None = None,
        executed_queries: Sequence[str] = (),
        progress_callback: ProgressCallback | None = None,
        stop_check: StopCheck | None = None,
    ) -> list[ActionOutcome]:
        """执行整批并按 sequence 顺序合并/验证；失败动作至多一次定向修正。

        返回与输入同序的 ActionOutcome 列表；所有尝试（含修正）的记录都已
        合并进共享状态（record_id 去重），失败历史不删除。
        """
        if not actions:
            return []
        try:
            executed = await self._execute_overlapping(
                actions, shared_state=shared_state,
                progress_callback=progress_callback, stop_check=stop_check,
            )
        finally:
            # 预约必须在任何路径上释放（异常终止批次也不例外）；
            # 实际计费由提交层承担，与预约无关。
            self.release_reserved(len(actions))

        by_id = {outcome.action.action_id: outcome for outcome in executed}
        # 稳定合并：按调用方给定的 sequence（即 actions 顺序），与完成顺序无关
        outcomes: list[ActionOutcome] = []
        executed_query_set = {str(query).strip() for query in executed_queries if str(query).strip()}
        for action in actions:
            first = by_id[action.action_id]
            if first.cancelled:
                outcomes.append(ActionOutcome(action=action, attempts=[first], verdict=None, active=first))
                continue
            if first.record is not None and first.isolated_state is not None:
                merge_isolated_state(
                    shared_state=shared_state, isolated_state=first.isolated_state,
                    task_id=action.task.task_id, records=[first.record],
                )
                first.isolated_state = None  # delta 已合并：副本即弃，不让 K 份状态滞留
            # verify_fn 允许返回 awaitable（异步验证器）；在事件循环内解析
            first.verdict = verify_fn(action, [first.record] if first.record is not None else [], executed_query_set)
            if asyncio.isfuture(first.verdict) or asyncio.iscoroutine(first.verdict):
                first.verdict = await first.verdict
            outcomes.append(ActionOutcome(action=action, attempts=[first], verdict=first.verdict, active=first))

        # 单次定向修正：仅对验证未通过的动作；修正结果同样走隔离执行 + 稳定合并
        if correction_fn is not None:
            for outcome in outcomes:
                if outcome.verdict is not None and getattr(outcome.verdict, "passed", True):
                    continue
                if outcome.active is not None and outcome.active.cancelled:
                    continue
                retry_action = await correction_fn(outcome.action, outcome.verdict, outcome.active.error if outcome.active else None)
                if retry_action is None:
                    continue
                retry_outcome = await self.execute_isolated(
                    retry_action, shared_state=shared_state,
                    progress_callback=progress_callback, stop_check=stop_check,
                )
                if not retry_outcome.cancelled and retry_outcome.record is not None and retry_outcome.isolated_state is not None:
                    merge_isolated_state(
                        shared_state=shared_state, isolated_state=retry_outcome.isolated_state,
                        task_id=retry_action.task.task_id, records=[retry_outcome.record],
                    )
                    retry_outcome.isolated_state = None
                retry_outcome.verdict = verify_fn(
                    retry_action,
                    [retry_outcome.record] if retry_outcome.record is not None else [],
                    executed_query_set,
                )
                if asyncio.isfuture(retry_outcome.verdict) or asyncio.iscoroutine(retry_outcome.verdict):
                    retry_outcome.verdict = await retry_outcome.verdict
                outcome.attempts.append(retry_outcome)
                outcome.verdict = retry_outcome.verdict
                outcome.active = retry_outcome
        return outcomes
