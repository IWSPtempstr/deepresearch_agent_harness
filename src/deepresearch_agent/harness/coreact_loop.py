# 由 harness/drivers_plan_execute.py 拆分（T-016），方法体与原位置逐行一致。
"""coreact_v1 步级循环：决策/批次执行/验证/定向重试/ContextState（原 _execute_coreact_loop 及其辅助）。"""

import asyncio
import json
from typing import Any
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec, TaskNode
from deepresearch_agent.agents.multi_agent.core.rubric import TaskRubric, build_task_rubric
from deepresearch_agent.harness.coreact_controller import CoreActController, DecisionContext
from deepresearch_agent.harness.coreact_state import (
    CoreActLoopState,
    CoreActPaused,
    PendingAction,
    loop_state_from_workflow_state,
)
from deepresearch_agent.harness.coreact_verifier import CoreActVerifier
from deepresearch_agent.config.settings import (
    COREACT_MAX_PARALLEL,
    EVIDENCE_CARD_MAX_TOKENS,
    REPORT_BATCH_BUDGET_RATIO,
    REPORT_BATCH_DIGEST_MAX_TOKENS,
    REPORT_MAX_SECTIONS,
    REPORT_RESERVED_TOKENS,
    REPORT_SECTION_EVIDENCE_BUDGET,
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

            # Durable pending action resumes as-is; do not ask the controller to replace it.
            pending = self.coreact_loop.pending_action
            if pending is not None and self.coreact_loop.phase in {"act", "verify"}:
                task = TaskNode.model_validate(pending.task)
                rubric = TaskRubric.model_validate(pending.rubric)
                attempt = pending.attempt
            else:
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
            # T-016 有界并行：controller 动作 + 确定性就绪补位（上限 COREACT_MAX_PARALLEL）。
            batch = [(task, build_task_rubric(
                task.task_id, description=task.description, source_mode=task.source_mode,
                min_evidence=min_evidence, completion_conditions=completion_conditions))]
            if max_parallel > 1:
                batch_tasks = self._admit_parallel_fillup(plan, eligible, primary=task, limit=max_parallel)
                for extra in batch_tasks:
                    batch.append((extra, build_task_rubric(
                        extra.task_id, description=extra.description, source_mode=extra.source_mode,
                        min_evidence=min_evidence, completion_conditions=completion_conditions)))
            query = self._coreact_query(task)
            if query and query in set(recent_queries):
                self.coreact_loop.no_progress_count += 1
                reason = f"拒绝重复查询: {query[:120]}"
                if reason not in self.coreact_loop.failure_reasons:
                    self.coreact_loop.failure_reasons.append(reason)
                if self.coreact_loop.no_progress_count >= 2:
                    self.coreact_loop.done = True
                    self.coreact_loop.phase = "done"
                    self.coreact_loop.stop_reason = "duplicate_query_limit"
                    break
                continue

            rubric = build_task_rubric(
                task.task_id,
                description=task.description,
                source_mode=task.source_mode,
                min_evidence=min_evidence,
                completion_conditions=completion_conditions,
            )
            evidence_before = len(self._coreact_ledger_ids())
            # T-016 intent 行：派发前持久化 plan/task（status=running）+ 游标快照。
            # 崩溃恢复时 DB 可见 in-flight 动作；工具调用级 intent 仍不做（如实记录）。
            if self.step_commit is not None and hasattr(self.step_commit, "__call__"):
                plan_for_intent = self.state.plan
                if isinstance(plan_for_intent, PlanSpec):
                    for batch_task, _ in batch:
                        plan_for_intent.update_task_status(batch_task.task_id, "running")
                await self._coreact_persist_intent(plan_for_intent)

            phase = "act"
            self.coreact_loop.phase = phase
            self.coreact_loop.pending_action = PendingAction(
                task=batch[0][0].model_dump(mode="json"), rubric=batch[0][1].model_dump(mode="json"),
                attempt=1,
            )
            batch_results: list[tuple[Any, Any, Any, Any, str | None]] = []  # (task, rubric, record, verdict, error)
            if len(batch) == 1:
                batch_task, batch_rubric = batch[0]
                _s, error, record = await self._execute_coreact_action(batch_task, loop)
                verdict = verifier.verify(
                    rubric=batch_rubric, task=batch_task,
                    records=[record] if record is not None else [],
                    ledger_ids=self._coreact_ledger_ids(), executed_queries=set(recent_queries),
                    allowed_tools=source_tools, semantic_allowed=False,
                )
                self.coreact_loop.phase = "verify"
                self._attach_coreact_verdict(record, batch_rubric, verdict, attempt=1, error=error)
                batch_results.append((batch_task, batch_rubric, record, verdict, error))
            else:
                # 并发执行（各动作独立 to_thread），逐动作独立 verify
                outcomes = await asyncio.gather(*[
                    self._execute_coreact_action(bt, loop) for bt, _ in batch
                ])
                self.coreact_loop.phase = "verify"
                for (bt, br), (_s, error_i, record_i) in zip(batch, outcomes):
                    v_i = verifier.verify(
                        rubric=br, task=bt,
                        records=[record_i] if record_i is not None else [],
                        ledger_ids=self._coreact_ledger_ids(), executed_queries=set(recent_queries),
                        allowed_tools=source_tools, semantic_allowed=False,
                    )
                    self._attach_coreact_verdict(record_i, br, v_i, attempt=1, error=error_i)
                    batch_results.append((bt, br, record_i, v_i, error_i))
                    if not v_i.passed:
                        self.coreact_loop.failure_reasons.append(
                            v_i.retry_reason or error_i or "步级 rubric 未通过")

            # 失败动作的串行定向重试（各至多一次，受全局 max_task_retries 约束）
            for idx, (bt, br, rec, v, err) in enumerate(batch_results):
                if v.passed or br.max_retries <= 0 or not self._coreact_retry_available():
                    continue
                if self.coreact_loop.decisions_used >= max_decisions:
                    break
                failure_reason = v.retry_reason or err or "步级 rubric 未通过"
                retry_candidate = bt.model_dump(mode="json")
                retry_candidate["status"] = "pending"
                retry_context = DecisionContext(
                    goal=self.context.resolved_query or self.context.original_query,
                    source_mode=self.context.source_mode.value,
                    eligible_tasks=[retry_candidate],
                    completed_count=len(self.state.execution_context.completed_task_ids) if self.state.execution_context else [],
                    evidence_count=len(self._coreact_ledger_ids()),
                    recent_queries=self._coreact_recent_queries(),
                    failed_rubric_reasons=[failure_reason],
                    remaining_tokens=max(0, self.context.budget_limits.max_llm_tokens - self.context.budget_usage.llm_tokens),
                    decisions_used=self.coreact_loop.decisions_used,
                    max_decisions=max_decisions,
                    allow_llm=max(0, self.context.budget_limits.max_llm_tokens - self.context.budget_usage.llm_tokens) > REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS + 1000,
                )
                retry_decision = await asyncio.to_thread(self._coreact_controller.decide, retry_context)
                self.coreact_loop.decisions_used += 1
                if retry_decision.action is None:
                    continue
                retry_task = bt.model_copy(deep=True)
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
                    continue
                self.context.budget_usage.task_retries += 1
                self.coreact_loop.phase = "act"
                self.coreact_loop.pending_action = PendingAction(
                    task=retry_task.model_dump(mode="json"), rubric=br.model_dump(mode="json"),
                    attempt=2, retry_reason=failure_reason,
                )
                _s2, error2, retry_record = await self._execute_coreact_action(retry_task, loop)
                retry_verdict = verifier.verify(
                    rubric=br, task=retry_task,
                    records=[retry_record] if retry_record is not None else [],
                    ledger_ids=self._coreact_ledger_ids(), executed_queries=set(recent_queries),
                    allowed_tools=source_tools, semantic_allowed=False,
                )
                self.coreact_loop.phase = "verify"
                self._attach_coreact_verdict(retry_record, br, retry_verdict, attempt=2, error=error2)
                batch_results[idx] = (bt, br, retry_record or rec, retry_verdict, error2)
                if not retry_verdict.passed and retry_verdict.retry_reason not in self.coreact_loop.failure_reasons:
                    self.coreact_loop.failure_reasons.append(retry_verdict.retry_reason or "定向重试仍未通过")

            for _bt, _br, _rec, _v, _e in batch_results:
                self._update_context_state(_v)

            self.coreact_loop.step_index += len(batch)
            evidence_after = len(self._coreact_ledger_ids())
            if evidence_after <= evidence_before:
                self.coreact_loop.no_progress_count += 1
            else:
                self.coreact_loop.no_progress_count = 0
            self._update_context_state(verdict)
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

    async def _coreact_persist_intent(self, plan: PlanSpec | None) -> None:
        """T-016 intent：仅持久化 plan/task 行（running）与循环游标 checkpoint。

        与 step_commit 的差异：不写 tool_call/evidence（动作尚未执行）、
        不做租约 fencing 之后的 checkpoint stage 变化（stage 仍为 executing）。
        """
        if self.step_commit is None or self._intent_persist is None:
            return
        await self._intent_persist(self)

    async def _execute_coreact_action(self, task: TaskNode, loop) -> tuple[bool, str | None, ExecutionRecord | None]:
        def progress_callback(kind, callback_task, record):
            if self.events is None:
                return
            future = asyncio.run_coroutine_threadsafe(
                self._publish_execution_progress(kind, callback_task, record), loop,
            )
            future.result(timeout=10)

        return await asyncio.to_thread(
            self.orchestrator.execute_action,
            self.state,
            task,
            progress_callback=progress_callback,
        )

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

    def _admit_parallel_fillup(self, plan: PlanSpec, eligible: list[dict], *, primary: TaskNode, limit: int) -> list[TaskNode]:
        """T-016 并行补位：从候选图中确定性选择独立就绪任务（不含主动作），上限 limit-1。"""
        admitted: list[TaskNode] = []
        if plan is None or len(plan.task_graph.nodes) >= self.context.budget_limits.max_plan_tasks:
            return admitted
        primary_query = self._coreact_query(primary)
        completed = list(self.state.execution_context.completed_task_ids) if self.state.execution_context else []
        for payload in eligible:
            if len(admitted) >= limit - 1 or len(plan.task_graph.nodes) >= self.context.budget_limits.max_plan_tasks:
                break
            try:
                node = TaskNode.model_validate(payload)
            except Exception:
                continue
            if node.task_id == primary.task_id or node.status != "pending":
                continue
            if node.source_mode != self.context.source_mode.value:
                continue
            node.depends_on = list(completed)
            plan.task_graph.nodes.append(node)
            admitted.append(node)
        return admitted
