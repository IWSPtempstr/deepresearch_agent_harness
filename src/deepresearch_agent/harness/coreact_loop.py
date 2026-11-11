# T-015/T-016 引入步级循环；T-403 重写执行段（统一隔离 ActionRunner）。
"""coreact_v1 步级循环：决策/批次执行/验证/定向重试/ContextState。

T-403 起：批次执行、隔离、稳定合并、预派发准入与定向修正统一交给
harness/action_runner.ActionRunner（真实执行器栈经回调注入）；本循环保留
Controller 决策、游标/pending 语义、intent 持久化与 ContextState 记账。
已知继承差异：current_task_id 不再由 coreact 路径写入共享状态（隔离副本
语义；coreact 当前无读者）；tool_call_history 去重键继承 staged 实现。
"""

import asyncio
from typing import Any
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec, TaskNode
from deepresearch_agent.agents.multi_agent.core.rubric import TaskRubric, build_task_rubric
from deepresearch_agent.harness.action_models import ActionSpec, action_spec_from_task_node
from deepresearch_agent.harness.action_runner import ActionRunner, BatchAction, DispatchBudget
from deepresearch_agent.harness.coreact_controller import DecisionContext
from deepresearch_agent.harness.coreact_state import (
    CoreActLoopState,
    CoreActPaused,
    PendingAction,
)
from deepresearch_agent.harness.coreact_verifier import CoreActVerifier, StepVerdict
from deepresearch_agent.config.settings import (
    COREACT_MAX_PARALLEL,
    REPORT_RESERVED_TOKENS,
    VERIFICATION_RESERVED_TOKENS,
)


class CoreActLoopMixin:
    async def _execute_coreact_loop(self) -> None:
        """Run the new F2 step controller; Harness still owns final verification."""
        if self.planner_result is None or self.planner_result.plan_spec is None:
            raise RuntimeError("coreact_v1 缺少初始 PlanSpec")
        if self.state.plan is None:
            self.state.plan = self.planner_result.plan_spec
        if self.coreact_loop is None:
            self.coreact_loop = CoreActLoopState()
        if self.coreact_loop.done:
            return

        loop = asyncio.get_running_loop()
        max_decisions = max(2, int(self.context.budget_limits.max_plan_tasks) * 2)
        min_evidence = max(1, int(self.context.config_snapshot.get("min_evidence", 1)))
        acceptance = self.planner_result.plan_spec.acceptance_criteria
        completion_conditions = list(acceptance.completion_conditions or [])
        verifier = CoreActVerifier()
        max_parallel = max(1, int(COREACT_MAX_PARALLEL or 1))
        source_tools = {
            "graphrag": {"local_search", "global_search", "hybrid_search", "naive_search", "chain_exploration"},
            "web": {"web_search"},
            "scholar": {"scholar_search"},
        }.get(self.context.source_mode.value, set())

        # T-403：统一隔离执行器（预派发预算预约 + 副本隔离 + 稳定合并 + 单次修正）
        self._action_runner = ActionRunner(
            execute_action=self._execute_coreact_action_sync,
            max_parallel=max_parallel,
            max_concurrency=self.context.budget_limits.max_concurrency,
            allowed_tools=source_tools,
            budget=DispatchBudget(
                used_fn=lambda: self.context.budget_usage.tool_calls,
                limit=self.context.budget_limits.max_tool_calls,
            ),
        )

        def verify_action(action: BatchAction, records: list[ExecutionRecord], executed_queries: set[str]) -> StepVerdict:
            return verifier.verify(
                rubric=action.rubric, task=action.task,
                records=records,
                ledger_ids=self._coreact_ledger_ids(),
                executed_queries=executed_queries,
                allowed_tools=source_tools, semantic_allowed=False,
            )

        for _ in range(max_decisions + 1):
            if self.context.cancellation_requested:
                raise RuntimeError("coreact 在动作边界检测到取消")
            if self.lease_probe is not None and not await self.lease_probe():
                # T-016 fencing：租约已易主，静默让位（接管者驱动后续状态）
                from deepresearch_agent.harness.errors import RunLeaseLost

                raise RunLeaseLost("coreact 动作边界检测到租约易主")
            if self.pause_requested is not None and await self.pause_requested():
                # Pause is honoured at an action boundary: the committed actions
                # stay durable and the takeover/resume continues the loop.
                self.coreact_loop.done = False
                self.coreact_loop.phase = "decide"
                raise CoreActPaused("coreact 在动作边界暂停")
            remaining = max(
                0,
                self.context.budget_limits.max_llm_tokens - self.context.budget_usage.llm_tokens,
            )
            if remaining <= REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS + 1000:
                self.coreact_loop.done = True
                self.coreact_loop.phase = "done"
                self.coreact_loop.stop_reason = "reserved_budget_reached"
                break

            plan = self.state.plan
            if not isinstance(plan, PlanSpec):
                plan = PlanSpec.model_validate(plan)
                self.state.plan = plan
            completed = list(self.state.execution_context.completed_task_ids) if self.state.execution_context else []
            recent_queries = self._coreact_recent_queries()
            evidence_count = len(self._coreact_ledger_ids())
            eligible = [
                task.model_dump(mode="json")
                for task in plan.get_ready_tasks(completed)
                if task.source_mode == self.context.source_mode.value
            ]

            # Durable pending action resumes as-is: redo only that action,
            # without a fresh controller decision or admission (conservative
            # serial replay; the intent row already marked it running).
            pending = self.coreact_loop.pending_action
            if pending is not None and self.coreact_loop.phase in {"act", "verify"}:
                resumed_task = TaskNode.model_validate(pending.task)
                resumed_rubric = TaskRubric.model_validate(pending.rubric)
                batch = [(resumed_task, resumed_rubric)]
                attempt = pending.attempt
                # 恢复路径不走 admit/查重/重试（attempt=2 已是重试）。
                resumed_only = True
            else:
                resumed_only = False
                if self.coreact_loop.decisions_used >= max_decisions:
                    self.coreact_loop.done = True
                    self.coreact_loop.phase = "done"
                    self.coreact_loop.stop_reason = "max_decisions_reached"
                    break
                context = DecisionContext(
                    goal=self.context.resolved_query or self.context.original_query,
                    source_mode=self.context.source_mode.value,
                    eligible_tasks=eligible,
                    completed_count=len(completed),
                    evidence_count=evidence_count,
                    recent_queries=recent_queries,
                    failed_rubric_reasons=self.coreact_loop.failure_reasons[-6:],
                    context_state=self.coreact_loop.context_state.model_dump(),
                    remaining_tokens=remaining,
                    decisions_used=self.coreact_loop.decisions_used,
                    max_decisions=max_decisions,
                    allow_llm=remaining > REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS + 1000,
                )
                decision = await asyncio.to_thread(self._coreact_controller.decide, context)
                self.coreact_loop.decisions_used += 1
                if decision.ready_to_report or decision.action is None:
                    self.coreact_loop.done = True
                    self.coreact_loop.phase = "done"
                    self.coreact_loop.stop_reason = decision.reason or decision.source
                    break

                task = self._admit_coreact_action(decision.action, eligible)
                if task is None:
                    self.coreact_loop.no_progress_count += 1
                    if self.coreact_loop.no_progress_count >= 2:
                        self.coreact_loop.done = True
                        self.coreact_loop.phase = "done"
                        self.coreact_loop.stop_reason = "invalid_action_limit"
                        break
                    continue
                query = self._coreact_query(task)
                # T-016 有界并行：controller 动作 + 确定性就绪补位（含批内/历史查重、
                # 并发上限受 budget_limits.max_concurrency 约束）。
                batch = [(task, build_task_rubric(
                    task.task_id, description=task.description, source_mode=task.source_mode,
                    min_evidence=min_evidence, completion_conditions=completion_conditions))]
                attempt = 1
                if max_parallel > 1:
                    seen_queries = set(recent_queries)
                    seen_queries.add(query or "")
                    fill_limit = min(max_parallel - 1, self.context.budget_limits.max_concurrency - 1)
                    for extra in self._parallel_fillup(plan, completed, seen_queries, limit=fill_limit):
                        batch.append((extra, build_task_rubric(
                            extra.task_id, description=extra.description, source_mode=extra.source_mode,
                            min_evidence=min_evidence, completion_conditions=completion_conditions)))

            # T-403：预派发准入（依赖/白名单/查重/预算预约）。恢复重放跳过 admit。
            batch_actions = [
                BatchAction(
                    spec=action_spec_from_task_node(bt, rubric_id=br.rubric_id, attempt=attempt),
                    task=bt, rubric=br,
                )
                for bt, br in batch
            ]
            if resumed_only:
                admitted = batch_actions
            else:
                admission = self._action_runner.admit(
                    batch_actions, completed_task_ids=completed, executed_queries=recent_queries,
                )
                admitted = admission.admitted
                if not admitted:
                    # 主动作被拒（含重复查询）：与原实现同样按无进展记账
                    self.coreact_loop.no_progress_count += 1
                    for rejection in admission.rejections:
                        reason = f"{rejection.reason_code}: {rejection.detail}"
                        if reason not in self.coreact_loop.failure_reasons:
                            self.coreact_loop.failure_reasons.append(reason)
                    if self.coreact_loop.no_progress_count >= 2:
                        self.coreact_loop.done = True
                        self.coreact_loop.phase = "done"
                        self.coreact_loop.stop_reason = "admission_rejected"
                        break
                    continue
                # 部分拒绝也要可观测（不改变派发纪律：拒绝动作零派发）
                for rejection in admission.rejections:
                    reason = f"{rejection.reason_code}: {rejection.detail}"
                    if reason not in self.coreact_loop.failure_reasons:
                        self.coreact_loop.failure_reasons.append(reason)

            evidence_before = len(self._coreact_ledger_ids())

            # 先设置游标（pending_action 含完整主动作+rubric），再持久化 intent——
            # 崩溃恢复时 checkpoint 同时携带 running 任务行与可重放的游标。
            first = admitted[0]
            self.coreact_loop.phase = "act"
            self.coreact_loop.pending_action = PendingAction(
                task=first.task.model_dump(mode="json"), rubric=first.rubric.model_dump(mode="json"),
                attempt=first.attempt,
            )
            if self.step_commit is not None and not resumed_only:
                plan_for_intent = self.state.plan
                if isinstance(plan_for_intent, PlanSpec):
                    for action in admitted:
                        plan_for_intent.update_task_status(action.task.task_id, "running")
                await self._coreact_persist_intent(plan_for_intent)

            outcomes = await self._action_runner.run_and_merge(
                admitted,
                shared_state=self.state,
                verify_fn=verify_action,
                correction_fn=None if resumed_only else await self._coreact_correction(max_decisions),
                executed_queries=recent_queries,
                progress_callback=self._coreact_progress_callback(loop),
            )

            self.coreact_loop.step_index += len(outcomes)
            for outcome in outcomes:
                for attempt_outcome in outcome.attempts:
                    if attempt_outcome.record is not None:
                        self._attach_coreact_verdict(
                            attempt_outcome.record, outcome.action.rubric, attempt_outcome.verdict,
                            attempt=attempt_outcome.action.attempt, error=attempt_outcome.error,
                        )
                if outcome.verdict is not None:
                    self._update_context_state(outcome.verdict)
                    if not outcome.verdict.passed:
                        reason = (
                            outcome.verdict.retry_reason
                            or (outcome.active.error if outcome.active else None)
                            or "步级 rubric 未通过"
                        )
                        if reason not in self.coreact_loop.failure_reasons:
                            self.coreact_loop.failure_reasons.append(reason)
            evidence_after = len(self._coreact_ledger_ids())
            if evidence_after <= evidence_before:
                self.coreact_loop.no_progress_count += 1
            else:
                self.coreact_loop.no_progress_count = 0
            self.coreact_loop.pending_action = None
            self.coreact_loop.phase = "decide"

            if self.step_commit is not None:
                await self.step_commit(self)

            if self.coreact_loop.no_progress_count >= 2:
                self.coreact_loop.done = True
                self.coreact_loop.phase = "done"
                self.coreact_loop.stop_reason = "no_evidence_progress"
                break
            if self.context.budget_usage.llm_tokens >= self.context.budget_limits.max_llm_tokens:
                self.coreact_loop.done = True
                self.coreact_loop.phase = "done"
                self.coreact_loop.stop_reason = "token_budget_exhausted"
                break
        else:
            self.coreact_loop.done = True
            self.coreact_loop.phase = "done"
            self.coreact_loop.stop_reason = "max_decisions_reached"

        if isinstance(self.state.plan, PlanSpec):
            if self.coreact_loop.done:
                # 候选图只是 controller 的建议池；未选中项显式标 skipped，
                # 不能让完成的 Run 留下看似待执行的任务。
                for node in self.state.plan.task_graph.nodes:
                    if node.status == "pending":
                        node.status = "skipped"
            self.state.plan.status = "completed" if self.coreact_loop.stop_reason else "executing"
        # Final snapshot is still persisted by the normal runtime stage boundary.

    def _coreact_progress_callback(self, loop):
        """把 worker 线程内的进度事件回投到事件循环（与 staged 路径同源）。"""
        if self.events is None:
            return None

        def progress_callback(kind, task, record):
            future = asyncio.run_coroutine_threadsafe(
                self._publish_execution_progress(kind, task, record), loop,
            )
            future.result(timeout=10)

        return progress_callback

    async def _coreact_correction(self, max_decisions: int):
        """定向修正回调（ActionRunner 每个失败动作至多调用一次）。

        修正内容由 Controller 决定；重试预算记账（task_retries）在此发生。
        返回 None 表示放弃修正。
        """

        async def correct(action: BatchAction, verdict, error) -> BatchAction | None:
            br = action.rubric
            if br.max_retries <= 0 or not self._coreact_retry_available():
                return None
            if self.coreact_loop.decisions_used >= max_decisions:
                return None
            failure_reason = (verdict.retry_reason if verdict is not None else None) or error or "步级 rubric 未通过"
            retry_candidate = action.task.model_dump(mode="json")
            retry_candidate["status"] = "pending"
            remaining = max(0, self.context.budget_limits.max_llm_tokens - self.context.budget_usage.llm_tokens)
            retry_context = DecisionContext(
                goal=self.context.resolved_query or self.context.original_query,
                source_mode=self.context.source_mode.value,
                eligible_tasks=[retry_candidate],
                completed_count=len(self.state.execution_context.completed_task_ids) if self.state.execution_context else 0,
                evidence_count=len(self._coreact_ledger_ids()),
                recent_queries=self._coreact_recent_queries(),
                failed_rubric_reasons=[failure_reason],
                remaining_tokens=remaining,
                decisions_used=self.coreact_loop.decisions_used,
                max_decisions=max_decisions,
                allow_llm=remaining > REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS + 1000,
            )
            retry_decision = await asyncio.to_thread(self._coreact_controller.decide, retry_context)
            self.coreact_loop.decisions_used += 1
            if retry_decision.action is None:
                return None
            retry_task = action.task.model_copy(deep=True)
            retry_task.description = retry_decision.action.description
            retry_task.parameters = dict(retry_decision.action.parameters)
            retry_task.status = "pending"
            # 执行后刷新：recent 必须包含刚失败的查询，原样重复直接拒绝
            recent_after_batch = self._coreact_recent_queries()
            retry_query = self._coreact_query(retry_task)
            if retry_query and retry_query in set(recent_after_batch):
                reason = f"定向重试仍重复查询: {retry_query[:120]}"
                if reason not in self.coreact_loop.failure_reasons:
                    self.coreact_loop.failure_reasons.append(reason)
                return None
            self.context.budget_usage.task_retries += 1
            self.coreact_loop.phase = "act"
            self.coreact_loop.pending_action = PendingAction(
                task=retry_task.model_dump(mode="json"), rubric=br.model_dump(mode="json"),
                attempt=action.attempt + 1, retry_reason=failure_reason,
            )
            spec: ActionSpec = action.spec.next_attempt(query=retry_query or None)
            return BatchAction(spec=spec, task=retry_task, rubric=br)

        return correct

    async def _coreact_persist_intent(self, plan: PlanSpec | None) -> None:
        """T-016 intent：仅持久化 plan/task 行（running）与循环游标 checkpoint。

        与 step_commit 的差异：不写 tool_call/evidence（动作尚未执行）、
        不做租约 fencing 之后的 checkpoint stage 变化（stage 仍为 executing）。
        """
        if self.step_commit is None or self._intent_persist is None:
            return
        await self._intent_persist(self)

    def _execute_coreact_action_sync(self, state, task: TaskNode) -> tuple[bool, str | None, ExecutionRecord | None]:
        """真实执行器回调：WorkerCoordinator.execute_action（在 runner 的 worker 线程调用）。"""
        return self.orchestrator.execute_action(state, task)

    def _admit_coreact_action(self, action: TaskNode, eligible: list[dict[str, Any]]) -> TaskNode | None:
        plan = self.state.plan
        if not isinstance(plan, PlanSpec):
            return None
        existing = next((node for node in plan.task_graph.nodes if node.task_id == action.task_id), None)
        if existing is not None:
            if existing.status != "pending" or existing.source_mode != action.source_mode or existing.task_type != action.task_type:
                return None
            # Candidate identity/source/type are frozen by the planner, but the
            # controller may refine query/description for a targeted retry.
            existing.description = action.description
            existing.parameters = dict(action.parameters)
            return existing
        if len(plan.task_graph.nodes) >= self.context.budget_limits.max_plan_tasks:
            return None
        if action.source_mode != self.context.source_mode.value:
            return None
        # 新动作采用 step_index 顺序执行；不继承模型生成的依赖关系。
        action.depends_on = list(self.state.execution_context.completed_task_ids) if self.state.execution_context else []
        plan.task_graph.nodes.append(action)
        return action

    def _parallel_fillup(self, plan: PlanSpec, completed: list[str], seen_queries: set, *, limit: int) -> list[TaskNode]:
        """T-016 并行补位：直接复用 plan 中已有的 pending 就绪节点（不重复 append），
        排除与历史/批内重复的查询；数量受 limit 约束（调用方已按 max_concurrency 收敛）。"""
        admitted: list[TaskNode] = []
        if plan is None or limit <= 0:
            return admitted
        for node in plan.get_ready_tasks(completed):
            if len(admitted) >= limit:
                break
            if node.source_mode != self.context.source_mode.value or node.status != "pending":
                continue
            node_query = self._coreact_query(node)
            if node_query and node_query in seen_queries:
                continue
            seen_queries.add(node_query or "")
            admitted.append(node)
        return admitted
