# T-015/T-016 引入步级循环；T-403 重写执行段（统一隔离 ActionRunner）。
"""coreact_v1 步级循环：决策/批次执行/验证/定向重试/ContextState。

T-403 起：批次执行、隔离、稳定合并、预派发准入与定向修正统一交给
harness/action_runner.ActionRunner（真实执行器栈经回调注入）；本循环保留
Controller 决策、游标/pending 语义、intent 持久化与 ContextState 记账。
已知继承差异：current_task_id 不再由 coreact 路径写入共享状态（隔离副本
语义；coreact 当前无读者）；tool_call_history 去重键继承 staged 实现。
"""

import asyncio
import json
import logging
from typing import Any
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec, TaskNode
from deepresearch_agent.agents.multi_agent.core.rubric import TaskRubric
from deepresearch_agent.harness.action_models import (
    ActionSpec,
    RubricInjectionRecord,
    action_spec_from_task_node,
    pre_action_blocks,
    text_sha256,
)
from deepresearch_agent.harness.action_runner import ActionRunner, BatchAction, DispatchBudget
from deepresearch_agent.harness.coreact_controller import DecisionContext
from deepresearch_agent.harness.coreact_support import build_semantic_judge
from deepresearch_agent.harness.pre_action_advisor import PreActionAdvisor, task_rubric_from_action_rubric
from deepresearch_agent.harness.rubric_builder import RubricBuilder, RubricBuilderConfig
from deepresearch_agent.harness.coreact_state import (
    CoreActLoopState,
    CoreActPaused,
    PendingAction,
)
from deepresearch_agent.harness.coreact_verifier import CoreActVerifier, StepVerdict
from deepresearch_agent.config.settings import (
    COREACT_MAX_PARALLEL,
    COREACT_PRE_ACTION_MODE,
    COREACT_RUBRIC_GENERATOR,
    COREACT_SEMANTIC_ENABLED,
    REPORT_RESERVED_TOKENS,
    VERIFICATION_RESERVED_TOKENS,
)

logger = logging.getLogger(__name__)

# RG-03：建议器/修订决策调用的预算余量（在报告/验证预留之外再留出的估算成本）
ADVISOR_BUDGET_MARGIN_TOKENS = 1_500


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

        # T-405：可选语义判定器（设置门控 + 预算门控；默认关闭 → not_run）。
        # 已知口径：判定器/控制器的 LLM 消耗与既有 controller decide 一致，
        # 不进入 llm_tokens 计费（预算门按工作流上报的用量判断，为咨询性门）。
        # RG-02/03：rubric 指导生成器（deterministic 默认；model 需显式授权）+
        # 动作前建议门（off 默认）。guidance 在 Controller 提案前构建并注入。
        guidance_builder = RubricBuilder(
            llm=self._coreact_controller.llm if COREACT_RUBRIC_GENERATOR == "model" else None,
            model_id=getattr(getattr(self._coreact_controller, "llm", None), "model_name", None),
            config=RubricBuilderConfig(generator=COREACT_RUBRIC_GENERATOR),
        )
        pre_action_advisor = PreActionAdvisor(
            llm=self._coreact_controller.llm,
            model_id=getattr(getattr(self._coreact_controller, "llm", None), "model_name", None),
        )
        pre_action_mode = COREACT_PRE_ACTION_MODE

        semantic_fn = None
        semantic_allowed = False
        if COREACT_SEMANTIC_ENABLED and getattr(self._coreact_controller, "llm", None) is not None:
            semantic_fn = build_semantic_judge(self._coreact_controller.llm)
            semantic_allowed = True

        def semantic_budget_gate() -> bool:
            remaining = max(0, self.context.budget_limits.max_llm_tokens - self.context.budget_usage.llm_tokens)
            return remaining > REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS + 1000

        def verify_action(action: BatchAction, records: list[ExecutionRecord], executed_queries: set[str]) -> StepVerdict:
            return verifier.verify(
                rubric=action.rubric, task=action.task,
                records=records,
                ledger_ids=self._coreact_ledger_ids(),
                executed_queries=executed_queries,
                allowed_tools=source_tools,
                semantic_allowed=semantic_allowed,
                semantic_fn=semantic_fn,
                semantic_budget_gate=semantic_budget_gate,
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

            # Durable pending actions resume as-is: replay the whole uncommitted
            # in-flight batch (T-404：pending_batch + 单动作 pending_action 兼容)
            # without a fresh controller decision or admission.
            guidance = None  # RG-02/03：本步指导 rubric（fresh 决策分支构建）
            pre_verdicts: dict[str, Any] = {}  # RG-03：动作前判定（恢复重放亦还原）
            inflight = self.coreact_loop.inflight_items() if self.coreact_loop.phase in {"act", "verify"} else []
            if inflight:
                # 防御：pending_batch 中已提交的项是游标不一致（主机制是 commit 时
                # 收缩游标）——跳过防重放。pending_action 是修正重试游标，与已提交
                # 动作共享 action_id，必须始终重放（attempt 序号区分尝试）。
                committed = set(self.coreact_loop.committed_action_ids)
                replayable = [
                    item for item in (self.coreact_loop.pending_batch or inflight)
                    if item.action_id not in committed
                ]
                retry_item = self.coreact_loop.pending_action
                if (
                    retry_item is not None
                    and retry_item not in replayable
                    and self.coreact_loop.phase in {"act", "verify"}
                    and (
                        not self.coreact_loop.pending_batch
                        # 重试游标（attempt>1）始终重放；attempt=1 且批次非空的
                        # 场景只可能是批量首项（已在 replayable 或被 committed 过滤
                        # ——后者属游标不一致的损坏快照，不重放已提交动作）。
                        or retry_item.attempt > 1
                    )
                ):
                    replayable.append(retry_item)
                skipped = len(inflight) - len(replayable)
                if skipped:
                    logger.warning("coreact 恢复：跳过已提交动作 %d 个（防重放）", skipped)
                if not replayable:
                    # 游标实际为空：重新决策
                    self.coreact_loop.pending_batch = []
                    self.coreact_loop.pending_action = None
                    self.coreact_loop.phase = "decide"
                    continue
                batch = []
                for item in replayable:
                    restored_guidance = None
                    if getattr(item, "guidance", None):
                        try:
                            from deepresearch_agent.harness.action_models import ActionRubric

                            restored_guidance = ActionRubric.model_validate(item.guidance)
                        except Exception:  # noqa: BLE001 —— 畸形旧字段不阻塞重放
                            restored_guidance = None
                    if getattr(item, "pre_verification", None):
                        try:
                            from deepresearch_agent.harness.action_models import PreActionVerification

                            pre_verdicts[item.action_id] = PreActionVerification.model_validate(
                                item.pre_verification
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    batch.append((
                        TaskNode.model_validate(item.task),
                        TaskRubric.model_validate(item.rubric),
                        item.attempt,
                        restored_guidance,
                    ))
                # 恢复路径不走 admit/查重/重试（attempt=2 已是重试）。
                resumed_only = True
            else:
                resumed_only = False
                if self.coreact_loop.decisions_used >= max_decisions:
                    self.coreact_loop.done = True
                    self.coreact_loop.phase = "done"
                    self.coreact_loop.stop_reason = "max_decisions_reached"
                    break
                # RG-02/03：提案前构建本步 rubric 指导（轨迹/阶段条件化；
                # deterministic 默认，model 需显式授权）——注入 Controller 提案
                # 上下文，并在提案后绑定到具体动作（content_hash 不变）。
                unanswered = [
                    str(question)
                    for observation in self.coreact_loop.observations
                    for question in (observation.get("unanswered_questions") or [])
                ]
                guidance_result = await asyncio.to_thread(
                    guidance_builder.build,
                    run_id=self.context.run_id,
                    step_index=self.coreact_loop.step_index,
                    goal=self.context.resolved_query or self.context.original_query,
                    source_mode=self.context.source_mode.value,
                    allowed_tools=sorted(source_tools),
                    observations=self.coreact_loop.observations,
                    unanswered=unanswered,
                    completion_conditions=completion_conditions,
                    min_evidence=min_evidence,
                    remaining_tokens=remaining,
                    max_llm_tokens=self.context.budget_limits.max_llm_tokens,
                    tool_calls_used=self.context.budget_usage.tool_calls,
                    max_tool_calls=self.context.budget_limits.max_tool_calls,
                    evidence_count=evidence_count,
                    semantic_enabled=semantic_allowed,
                    pre_action=pre_action_mode,
                )
                guidance = guidance_result.rubric
                context = DecisionContext(
                    goal=self.context.resolved_query or self.context.original_query,
                    source_mode=self.context.source_mode.value,
                    eligible_tasks=eligible,
                    completed_count=len(completed),
                    evidence_count=evidence_count,
                    recent_queries=recent_queries,
                    failed_rubric_reasons=self.coreact_loop.failure_reasons[-6:],
                    context_state=self.coreact_loop.context_state.model_dump(),
                    observations=self.coreact_loop.observations,
                    assessments=self.coreact_loop.assessments,
                    rubric_guidance=[criterion.model_dump(mode="json") for criterion in guidance.criteria],
                    rubric_phase=guidance.phase or "",
                    rubric_hash=guidance.content_hash,
                    remaining_tokens=remaining,
                    decisions_used=self.coreact_loop.decisions_used,
                    max_decisions=max_decisions,
                    allow_llm=remaining > REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS + 1000,
                )
                decision = await asyncio.to_thread(self._coreact_controller.decide, context)
                self.coreact_loop.decisions_used += 1
                # RG-03 注入证据：真实发出的 Controller messages（有模型参与时）
                if getattr(self._coreact_controller, "last_messages_hash", None):
                    self._coreact_append_injection(RubricInjectionRecord(
                        injection_id=(
                            f"inj_controller_s{self.coreact_loop.step_index}_"
                            f"{len(self.coreact_loop.rubric_injections)}"
                        ),
                        rubric_id=guidance.rubric_id,
                        rubric_hash=guidance.content_hash,
                        targets=["controller"],
                        messages_hash=self._coreact_controller.last_messages_hash,
                        step_index=self.coreact_loop.step_index,
                    ))
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
                # T-405：语义判定器可用时（设置门控 + LLM 存在），动作 rubric 携带
                # 可选语义检查（unknown 不判罚；fail 追加；required 由 rubric kind
                # 配置，主路径默认 optional）。
                batch = [(task, task_rubric_from_action_rubric(
                    guidance.bind(task_id=task.task_id), task_id=task.task_id), 1, guidance)]
                if max_parallel > 1:
                    seen_queries = set(recent_queries)
                    seen_queries.add(query or "")
                    fill_limit = min(max_parallel - 1, self.context.budget_limits.max_concurrency - 1)
                    for extra in self._parallel_fillup(plan, completed, seen_queries, limit=fill_limit):
                        batch.append((extra, task_rubric_from_action_rubric(
                            guidance.bind(task_id=extra.task_id), task_id=extra.task_id), 1, guidance))

            # T-403：预派发准入（依赖/白名单/查重/预算预约）。恢复重放跳过 admit。
            batch_actions = [
                BatchAction(
                    spec=action_spec_from_task_node(bt, rubric_id=br.rubric_id, attempt=bt_attempt),
                    task=bt, rubric=br, guidance=bg,
                )
                for bt, br, bt_attempt, bg in batch
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

            # RG-03：动作前建议门（同一 rubric_id/hash；与确定性准入分离）。
            # 语义（对齐 §4.3，整批口径）：
            # - required：fail/unknown 均不得派发；
            # - advisory：fail 允许**一次**提案修订（同 guidance）——修订被接受时
            #   整批替换为该单一动��（fillup 兄弟动作一并作废，成本记录在案）；
            #   修订未变/被拒/不存在 → 整批不派发，交 Controller 下一轮替代决策；
            # - advisory unknown：显式降级继续（degraded，不是验证通过）。
            # 预算对账：admit() 的预留按"最终 admitted 数"精确释放，无泄漏。
            if not resumed_only and pre_action_mode != "off" and guidance is not None:
                admitted, pre_verdicts = await self._coreact_pre_action_gate(
                    admitted=admitted,
                    guidance=guidance,
                    pre_action_mode=pre_action_mode,
                    pre_action_advisor=pre_action_advisor,
                    source_tools=source_tools,
                    executed_queries=recent_queries,
                    completed=completed,
                    max_decisions=max_decisions,
                    remaining=remaining,
                )
                if not admitted:
                    self.coreact_loop.no_progress_count += 1
                    if self.coreact_loop.no_progress_count >= 2:
                        self.coreact_loop.done = True
                        self.coreact_loop.phase = "done"
                        self.coreact_loop.stop_reason = "pre_action_blocked"
                        break
                    continue

            evidence_before = len(self._coreact_ledger_ids())

            # T-404：派发前把**全部批内动作**写入可持久化游标（intent）——崩溃恢复
            # 不丢次要动作。pending_action 保留为批首动作（旧快照兼容读取）。
            self.coreact_loop.phase = "act"
            self.coreact_loop.pending_batch = [
                PendingAction(
                    task=action.task.model_dump(mode="json"), rubric=action.rubric.model_dump(mode="json"),
                    attempt=action.attempt,
                    # RG-03：冻结指导与动作前判定随 intent 持久化（崩溃恢复不丢）
                    guidance=(
                        action.guidance.model_dump(mode="json")
                        if action.guidance is not None else None
                    ),
                    pre_verification=(
                        pre_verdicts[action.action_id].model_dump(mode="json")
                        if action.action_id in pre_verdicts else None
                    ),
                )
                for action in admitted
            ]
            self.coreact_loop.pending_action = self.coreact_loop.pending_batch[0]
            if self.step_commit is not None and not resumed_only:
                plan_for_intent = self.state.plan
                if isinstance(plan_for_intent, PlanSpec):
                    for action in admitted:
                        plan_for_intent.update_task_status(action.task.task_id, "running")
                await self._coreact_persist_intent(plan_for_intent)

            async def commit_action(action: BatchAction) -> None:
                """T-404 逐动作提交：已合并结果落库 + 游标收缩（幂等）。

                使用 action_commit 钩子（只持久化）；step 事件保持在批次边界。
                """
                if action.action_id not in self.coreact_loop.committed_action_ids:
                    self.coreact_loop.committed_action_ids.append(action.action_id)
                self.coreact_loop.pending_batch = [
                    item for item in self.coreact_loop.pending_batch
                    if item.action_id != action.action_id
                ]
                if (
                    self.coreact_loop.pending_action is not None
                    and self.coreact_loop.pending_action.action_id == action.action_id
                ):
                    self.coreact_loop.pending_action = None
                # 仅用 action_commit（持久化、无 step 事件）；step 事件保持在批次
                # 边界（step_commit 由批次收尾调用）。缺省（离线工具）只动内存游标。
                commit_hook = getattr(self, "action_commit", None)
                if commit_hook is not None:
                    await commit_hook(self)

            def record_action(action: BatchAction, attempt_outcome) -> None:
                """T-404/T-405：验证元数据在提交前写入记录（崩溃恢复后 committed
                记录仍携带 rubric/verdict，最终 rubric_coverage 门不被削弱）。
                RG-03/04：guidance 身份与动作前建议随记录持久化（前后判定分离）。"""
                if attempt_outcome.record is not None:
                    self._attach_coreact_verdict(
                        attempt_outcome.record, action.rubric, attempt_outcome.verdict,
                        attempt=attempt_outcome.action.attempt, error=attempt_outcome.error,
                        rubric_hash=(
                            action.guidance.content_hash
                            if action.guidance is not None else ""
                        ),
                    )
                    if action.guidance is not None:
                        payload = attempt_outcome.record.metadata.environment.setdefault("coreact", {})
                        payload["guidance"] = {
                            "rubric_id": action.guidance.rubric_id,
                            "content_hash": action.guidance.content_hash,
                            "phase": action.guidance.phase,
                            "phase_rule_version": action.guidance.phase_rule_version,
                        }
                        pre = pre_verdicts.get(action.action_id)
                        if pre is not None:
                            payload["pre_action_verification"] = pre.model_dump(mode="json")
                        # worker 注入证据：实际下发到隔离上下文的 guidance 指纹
                        payload["worker_injection"] = {
                            "targets": ["worker"],
                            "messages_hash": text_sha256(json.dumps(
                                action.guidance.model_dump(mode="json"), sort_keys=True,
                                ensure_ascii=False,
                            )),
                        }

            outcomes = await self._action_runner.run_and_merge(
                admitted,
                shared_state=self.state,
                verify_fn=verify_action,
                correction_fn=(
                None if resumed_only
                else await self._coreact_correction(max_decisions, guidance, pre_verdicts)
            ),
                commit_fn=commit_action,
                record_fn=record_action,
                executed_queries=recent_queries,
                progress_callback=self._coreact_progress_callback(loop),
            )

            self.coreact_loop.step_index += len(outcomes)
            for outcome in outcomes:
                # RG-03 worker 注入证据：实际合并记录携带的 guidance 指纹（有 LLM
                # 或无 LLM 的执行器一致——注入的是上下文载荷本身）
                action_guidance = outcome.action.guidance
                if (
                    action_guidance is not None
                    and outcome.active is not None
                    and outcome.active.record is not None
                ):
                    g = outcome.active.record.metadata.environment.get("guidance")
                    if isinstance(g, dict) and g.get("content_hash") == action_guidance.content_hash:
                        self._coreact_append_injection(RubricInjectionRecord(
                            injection_id=(
                                f"inj_worker_s{self.coreact_loop.step_index}_"
                                f"{len(self.coreact_loop.rubric_injections)}"
                            ),
                            rubric_id=action_guidance.rubric_id,
                            rubric_hash=action_guidance.content_hash,
                            action_id=outcome.action.action_id,
                            targets=["worker"],
                            messages_hash=text_sha256(json.dumps(
                                g, sort_keys=True, ensure_ascii=False,
                            )),
                            step_index=self.coreact_loop.step_index,
                        ))
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
                # T-405：有界观察摘要（账本过滤后）+ claim 级评估迁移
                active_records = (
                    [outcome.active.record] if outcome.active is not None and outcome.active.record is not None else []
                )
                observation = self._coreact_build_observation(outcome.action, outcome.verdict, active_records)
                self._coreact_append_observation(observation)
                if outcome.verdict is not None:
                    self._coreact_record_assessments(outcome.action, outcome.verdict)
            evidence_after = len(self._coreact_ledger_ids())
            if evidence_after <= evidence_before:
                self.coreact_loop.no_progress_count += 1
            else:
                self.coreact_loop.no_progress_count = 0
            # 批次收尾：清空全部 in-flight 游标（T-404 pending_batch + 兼容字段）
            self.coreact_loop.pending_batch = []
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

    def _coreact_record_pre_history(self, action_id: str, verdict: Any, *, reason: str) -> None:
        """失败/被丢弃的动作前判定入历史（含 usage 成本；有界 16，供审计）。"""
        payload = {
            "action_id": action_id,
            "reason": reason,
            "verification": verdict.model_dump(mode="json"),
        }
        history = [*self.coreact_loop.pre_action_history, payload]
        self.coreact_loop.pre_action_history = history[-16:]

    async def _coreact_pre_action_gate(
        self,
        *,
        admitted: list[BatchAction],
        guidance: Any,
        pre_action_mode: str,
        pre_action_advisor: PreActionAdvisor,
        source_tools: set[str],
        executed_queries: list[str],
        completed: list[str],
        max_decisions: int,
        remaining: int,
    ) -> tuple[list[BatchAction], dict[str, Any]]:
        """动作前建议门（整批口径；返回最终批次与逐动作判定）。

        - 整批无问题 → 原样返回（advisory unknown 记 degraded）；
        - 有问题 → 一次提案修订（同 rubric_id/hash）：修订被确定性准入接受则
          整批替换为单一动作；未变/被拒/无提案 → 整批不派发；
        - 预算对账：本方法返回前，未派发动作的 tool 预约已精确释放，
          返回批次的预约数 == len(returns)。
        """
        pre_verdicts: dict[str, Any] = {}
        reserved = len(admitted)
        reserves = REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS
        budget = {"remaining": max(0, int(remaining))}

        async def advise_all(actions: list[BatchAction]) -> None:
            for action in actions:
                # 每个动作前重算许可（前一次评估的消耗必须先扣减）
                allow_llm = budget["remaining"] > reserves + ADVISOR_BUDGET_MARGIN_TOKENS
                verdict = await asyncio.to_thread(
                    pre_action_advisor.advise,
                    guidance=action.guidance, task=action.task,
                    allowed_tools=sorted(source_tools),
                    executed_queries=executed_queries,
                    allow_llm=allow_llm,
                )
                pre_verdicts[action.action_id] = verdict
                spent = max(0, int(verdict.usage.get("estimated_tokens", 0) or 0))
                budget["remaining"] = max(0, budget["remaining"] - spent)
                if spent:
                    # §4.6：guidance 消耗计入共享预算（估算口径，后续门/报告预留可见）
                    self.context.budget_usage.llm_tokens += spent

        def problem_of(action: BatchAction) -> bool:
            verdict = pre_verdicts.get(action.action_id)
            if verdict is None:
                return False
            return verdict.status == "fail" or pre_action_blocks(pre_action_mode, verdict.status)

        def drop_all(reason_prefix: str) -> tuple[list[BatchAction], dict[str, Any]]:
            for action in admitted:
                verdict = pre_verdicts.get(action.action_id)
                if verdict is None:
                    continue
                self._coreact_record_pre_history(action.action_id, verdict, reason=reason_prefix)
                reason = f"{reason_prefix}: {self._bounded_pre_reason(verdict)}"
                if reason not in self.coreact_loop.failure_reasons:
                    self.coreact_loop.failure_reasons.append(reason)
            self._action_runner.release_reserved(reserved)
            return [], {}

        await advise_all(admitted)
        problems = [action for action in admitted if problem_of(action)]
        if not problems:
            for action in admitted:
                verdict = pre_verdicts[action.action_id]
                if verdict.status == "unknown" and pre_action_mode == "advisory":
                    verdict.degraded = True
                    verdict.degraded_reason = "advisory_unknown_continued"
            return admitted, pre_verdicts

        # 一次提案修订（冻结 guidance；不重试已执行工具）
        problem = problems[0]
        failed = pre_verdicts[problem.action_id]
        failure_reason = self._bounded_pre_reason(failed)
        if self.coreact_loop.decisions_used >= max_decisions:
            return drop_all("pre_action_required_no_revision")
        original_query = self._coreact_query(problem.task)
        revision_context = DecisionContext(
            goal=self.context.resolved_query or self.context.original_query,
            source_mode=self.context.source_mode.value,
            eligible_tasks=[problem.task.model_dump(mode="json")],
            completed_count=len(completed),
            evidence_count=len(self._coreact_ledger_ids()),
            recent_queries=list(executed_queries),
            failed_rubric_reasons=[failure_reason],
            context_state=self.coreact_loop.context_state.model_dump(),
            observations=self.coreact_loop.observations,
            assessments=self.coreact_loop.assessments,
            rubric_guidance=[criterion.model_dump(mode="json") for criterion in guidance.criteria],
            rubric_phase=guidance.phase or "",
            rubric_hash=guidance.content_hash,
            remaining_tokens=budget["remaining"],
            decisions_used=self.coreact_loop.decisions_used,
            max_decisions=max_decisions,
            allow_llm=budget["remaining"] > reserves + ADVISOR_BUDGET_MARGIN_TOKENS,
        )
        revision_decision = await asyncio.to_thread(self._coreact_controller.decide, revision_context)
        self.coreact_loop.decisions_used += 1
        if getattr(self._coreact_controller, "last_messages_hash", None):
            self._coreact_append_injection(RubricInjectionRecord(
                injection_id=(
                    f"inj_controller_s{self.coreact_loop.step_index}_"
                    f"{len(self.coreact_loop.rubric_injections)}"
                ),
                rubric_id=guidance.rubric_id,
                rubric_hash=guidance.content_hash,
                targets=["controller"],
                messages_hash=self._coreact_controller.last_messages_hash,
                step_index=self.coreact_loop.step_index,
            ))

        accepted: BatchAction | None = None
        if revision_decision.action is not None:
            candidate = revision_decision.action
            candidate_query = self._coreact_query(candidate)
            if candidate_query and candidate_query != original_query:
                # 先做**无副作用**的确定性准入（预算/白名单/查重），通过后才写入
                # 计划图——被拒的提案不得改动既有节点或留下幽灵节点。
                # 修订即作废原批次：先释放原批次全部预留，再为替换动作做准入
                # （否则 max_tool_calls 边界上替换动作会被自身旧预留误拒）。
                self._action_runner.release_reserved(reserved)
                reserved = 0
                candidate_action = BatchAction(
                    spec=action_spec_from_task_node(
                        candidate, rubric_id=guidance.rubric_id, attempt=1,
                    ),
                    task=candidate,
                    rubric=task_rubric_from_action_rubric(
                        guidance.bind(task_id=candidate.task_id), task_id=candidate.task_id,
                    ),
                    guidance=guidance,
                )
                admissions = self._action_runner.admit(
                    [candidate_action], completed_task_ids=completed,
                    executed_queries=executed_queries,
                )
                if admissions.admitted:
                    bound = self._admit_coreact_action(candidate, [problem.task.model_dump(mode="json")])
                    if bound is not None:
                        accepted = BatchAction(
                            spec=action_spec_from_task_node(
                                bound, rubric_id=guidance.rubric_id, attempt=1,
                            ),
                            task=bound,
                            rubric=task_rubric_from_action_rubric(
                                guidance.bind(task_id=bound.task_id), task_id=bound.task_id,
                            ),
                            guidance=guidance,
                        )
                        reserved = 1  # 替换动作的预约
                    else:
                        self._action_runner.release_reserved(1)
                else:
                    for rejection in admissions.rejections:
                        reason = f"revision_rejected: {rejection.reason_code}: {rejection.detail}"
                        if reason not in self.coreact_loop.failure_reasons:
                            self.coreact_loop.failure_reasons.append(reason)

        if accepted is None:
            return drop_all("pre_action_unrevised")

        # 修订接受：原批次的全部判定（含成本）先归档，再替换为单一动作
        for original in admitted:
            verdict = pre_verdicts.get(original.action_id)
            if verdict is not None:
                self._coreact_record_pre_history(
                    original.action_id, verdict, reason="batch_replaced_by_revision",
                )
        # 修订动作重新过同一建议门（仅此一个动作）
        admitted = [accepted]
        pre_verdicts = {}
        await advise_all(admitted)
        if problem_of(accepted):
            verdict = pre_verdicts[accepted.action_id]
            self._coreact_record_pre_history(accepted.action_id, verdict, reason="revision_still_failing")
            reason = "pre_action_revision_failed: " + self._bounded_pre_reason(verdict)
            if reason not in self.coreact_loop.failure_reasons:
                self.coreact_loop.failure_reasons.append(reason)
            self._action_runner.release_reserved(1)
            return [], {}
        verdict = pre_verdicts[accepted.action_id]
        verdict.proposal_revision_used = True
        if verdict.status == "unknown" and pre_action_mode == "advisory":
            verdict.degraded = True
            verdict.degraded_reason = "advisory_unknown_continued"
        return [accepted], pre_verdicts

    async def _coreact_correction(
        self, max_decisions: int, guidance: Any | None = None,
        pre_verdicts: dict[str, Any] | None = None,
    ):
        """定向修正回调（ActionRunner 每个失败动作至多调用一次）。

        修正内容由 Controller 决定；重试预算记账（task_retries）在此发生。
        返回 None 表示放弃修正。
        """

        pre_verdicts = pre_verdicts or {}

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
                context_state=self.coreact_loop.context_state.model_dump(),
                observations=self.coreact_loop.observations,
                assessments=self.coreact_loop.assessments,
                rubric_guidance=[criterion.model_dump(mode="json") for criterion in (guidance.criteria if guidance else [])],
                rubric_phase=(guidance.phase or "") if guidance else "",
                rubric_hash=(guidance.content_hash if guidance else ""),
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
                guidance=(
                    action.guidance.model_dump(mode="json")
                    if getattr(action, "guidance", None) is not None else None
                ),
                pre_verification=(
                    pre_verdicts.get(action.action_id).model_dump(mode="json")
                    if action.action_id in pre_verdicts else None
                ),
            )
            # T-404：修正游标即时持久化——崩溃于重试执行中时恢复能重放该重试，
            # 而不是静默丢弃候选动作（外部调用仍不承诺恰好一次）。
            await self._coreact_persist_intent(self.state.plan)
            spec: ActionSpec = action.spec.next_attempt(query=retry_query or None)
            return BatchAction(spec=spec, task=retry_task, rubric=br, guidance=action.guidance)

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

    async def _coreact_rollback_plan_nodes(self, keep_ids: set[str]) -> None:
        """撤销本轮修订尝试追加的候选节点（被确定性硬门拒绝的提案不留幽灵任务）。"""
        plan = self.state.plan
        if not isinstance(plan, PlanSpec):
            return
        plan.task_graph.nodes = [
            node for node in plan.task_graph.nodes
            if node.task_id in keep_ids or node.status != "pending"
        ]

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
