# 由 harness/runtime.py 拆分（T-014），方法体与原位置逐行一致。
"""Run 执行主循环与心跳（原 execute_run/_execute_run_inner/_run_with_heartbeat；状态机主循环整块搬移）。"""
from __future__ import annotations


import asyncio
import inspect
import time
import uuid
from typing import Any, Callable
from deepresearch_agent.harness.budgets import BudgetExceeded, BudgetLimits, BudgetManager
from deepresearch_agent.harness.contracts import ContractEvaluator, RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.harness.errors import AppError, ErrorCode, NeedsUserInput, RunCancelled, RunLeaseLost
from deepresearch_agent.harness.recovery import classify_exception, classify_verification_failures
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.config.settings import REPORT_RESERVED_TOKENS, VERIFICATION_RESERVED_TOKENS
from deepresearch_agent.models.prefix_cache import set_current_run
from deepresearch_agent.harness.report_safety import sanitize_report


class RuntimeExecutionMixin:
    async def execute_run(self, run_id: str) -> RunContext:
        """观测外壳：span + 指标；真正的执行语义在 _execute_run_inner。"""
        from deepresearch_agent.observability import ACTIVE_RUNS, RUN_DURATION_SECONDS, RUN_TOTAL, get_tracer

        tracer = get_tracer("deepresearch.harness")
        started_at = time.monotonic()
        ACTIVE_RUNS.inc()
        with tracer.start_as_current_span("harness.run", attributes={"run_id": run_id}) as run_span:
            try:
                context = await self._execute_run_inner(run_id)
                run_span.set_attribute("run.status", context.status.value)
                # 评审遗留修复：仅终态计数——让位/接管中的非终态不产生伪维度
                if self.state_machine.is_terminal(context.status):
                    RUN_TOTAL.labels(status=context.status.value).inc()
                return context
            except BaseException as exc:
                try:
                    run_span.record_exception(exc)
                except Exception:
                    pass
                raise
            finally:
                ACTIVE_RUNS.dec()
                RUN_DURATION_SECONDS.observe(max(0.0, time.monotonic() - started_at))

    async def _execute_run_inner(self, run_id: str) -> RunContext:
        owner = f"harness-{uuid.uuid4().hex}"
        if not await self.runs.acquire_lease(run_id, owner, ttl_seconds=self.lease_seconds):
            # 其他实例持有未过期租约：对调用方是可重试的暂时性冲突
            raise AppError(ErrorCode.LEASE_UNAVAILABLE, f"Run {run_id} 已由其他 Runtime 执行", retryable=True)
        context: RunContext | None = None
        recovery_target: RunStatus | None = None
        set_current_run(run_id)
        try:
            context = await self._load_context(run_id)
            if context.status in {RunStatus.INTERRUPTED, RunStatus.PAUSED}:
                resume_target = context.resume_from_status or RunStatus.QUEUED
                context.resume_from_status = None
                await self._transition(context, resume_target, event_type="run.resumed")
                recovery_target = resume_target
            if context.status in {RunStatus.QUEUED, RunStatus.CONTEXT_BUILDING}:
                if context.status is RunStatus.QUEUED:
                    await self._transition(context, RunStatus.CONTEXT_BUILDING, event_type="run.started")
                if self.context_builder is not None:
                    # 评审遗留修复：context 构建同样受租约保护——构建被拉长超过
                    # TTL 时续约，被抢占即让位（否则接管者会与本地双写上下文快照）
                    build_budget = BudgetManager(context.budget_limits, context.budget_usage)
                    context = await self._run_with_heartbeat(
                        context, build_budget, self.context_builder.build(context), owner=owner,
                    )
                    selected_skill = context.context_snapshot.get("selected_skill") or {}
                    await self.runs.update_model_snapshot(run_id, {
                        **context.model_snapshot,
                        "skill": None if not selected_skill else {
                            "name": selected_skill.get("name"), "version": selected_skill.get("version"),
                            "status": (selected_skill.get("selection") or {}).get("status", "active"),
                        },
                        "context_policy": {
                            "recent_turns": len(context.context_snapshot.get("recent_messages", [])),
                            "memory_snapshot_version": context.context_snapshot.get("memory_snapshot_version", 0),
                            "curated_memory_ids": [item.get("memory_id") for item in context.context_snapshot.get("curated_memory", [])],
                            "historical_recall_session_ids": context.context_snapshot.get("used_session_ids", []),
                            "stable_snapshot_id": context.context_snapshot.get("stable_snapshot_id"),
                            "input_tokens": context.context_snapshot.get("total_input_tokens", 0),
                        },
                    })
                else:
                    context.resolved_query = context.resolved_query or context.original_query
            driver = self.workflow_factory(context, self.events)
            if inspect.isawaitable(driver):
                driver = await driver
            if context.status is RunStatus.CONTEXT_BUILDING:
                context.workflow_state = driver.snapshot()
                await self.checkpoints.save(context, "context_building")
                await self.events.publish(run_id, "context.completed", stage="context_building", payload={
                    "query_resolved": True,
                    "used_message_ids": context.used_message_ids,
                    "curated_memory_count": len(context.context_snapshot.get("curated_memory", [])),
                    "memory_snapshot_version": context.context_snapshot.get("memory_snapshot_version", 0),
                    "historical_recall_count": len(context.context_snapshot.get("historical_recall", [])),
                    "historical_recall_searched": bool(context.context_snapshot.get("historical_recall_searched")),
                    "context_tokens": context.context_snapshot.get("total_input_tokens", 0),
                    "artifact_edit": bool(context.context_snapshot.get("artifact_edit")),
                })
                await self._transition(context, RunStatus.PLANNING)
            budget = BudgetManager(context.budget_limits, context.budget_usage)
            # A checkpoint and its relational side effects are normally written
            # in order, but a process can stop between those commits. Replaying
            # these idempotent persistence steps reconstructs missing rows from
            # the driver snapshot without repeating an external tool/LLM call.
            if recovery_target in {RunStatus.EXECUTING, RunStatus.REPORTING, RunStatus.VERIFYING}:
                await self._persist_plan(context, driver)
            if recovery_target in {RunStatus.REPORTING, RunStatus.VERIFYING}:
                await self._persist_execution(context, driver, budget)
                context.budget_usage = budget.usage
            if recovery_target is RunStatus.VERIFYING:
                await self._save_report_artifact(context)

            while not self.state_machine.is_terminal(context.status):
                if await self._pause_at_safe_boundary(context):
                    return context
                await self._assert_not_cancelled(context)
                budget.assert_available()

                if context.status in {RunStatus.PLANNING, RunStatus.REPLANNING}:
                    failures = list(context.workflow_state.get("verification_failures", []))
                    await self._run_with_heartbeat(context, budget, driver.plan(failures or None), owner=owner)
                    context.plan_version += 1
                    context.workflow_state = driver.snapshot()
                    plan_extra: dict[str, Any] = {}
                    plan_record = driver.plan_record()
                    if plan_record is not None:
                        _, plan_tasks = plan_record
                        budget.observe_plan_tasks(len(plan_tasks))
                        context.budget_usage = budget.usage
                        plan_extra = {
                            "task_count": len(plan_tasks),
                            "tasks": [
                                {"task_id": str(t.get("task_id", "")), "task_type": str(t.get("task_type", "")), "description": str(t.get("description", ""))[:120]}
                                for t in plan_tasks
                            ],
                        }
                    await self._persist_plan(context, driver)
                    await self.checkpoints.save(context, context.status.value)
                    await self.events.publish(run_id, "plan.revised" if failures else "plan.created", stage=context.status.value, payload={"plan_version": context.plan_version, **plan_extra})
                    await self._transition(context, RunStatus.EXECUTING)

                elif context.status in {RunStatus.EXECUTING, RunStatus.RETRYING}:
                    await self._assert_not_cancelled(context)
                    try:
                        await self._run_with_heartbeat(context, budget, driver.execute(), owner=owner)
                    except Exception as exc:
                        decision = classify_exception(exc)
                        if decision.action == "retry_same":
                            budget.consume_retry()
                            context.budget_usage = budget.usage
                            if context.status is not RunStatus.RETRYING:
                                await self._transition(context, RunStatus.RETRYING, event_type="run.retrying", error_code=decision.reason, error_message=str(exc))
                            await self._transition(context, RunStatus.EXECUTING, event_type="run.retry_resumed")
                            continue
                        raise
                    await self._assert_not_cancelled(context)
                    await self._persist_execution(context, driver, budget)
                    context.workflow_state = driver.snapshot()
                    context.budget_usage = budget.usage
                    persisted_evidence = await self.evidence_repository.list_for_run(run_id)
                    minimum_evidence = int(context.config_snapshot.get("min_evidence", 1))
                    if len(persisted_evidence) < minimum_evidence:
                        evidence_count = len(persisted_evidence)
                        coverage_reason = "no_source_evidence" if evidence_count == 0 else "insufficient_source_evidence"
                        failures = ["min_evidence", coverage_reason]
                        context.workflow_state["verification_failures"] = failures
                        await self.events.publish(
                            run_id,
                            "source.coverage_checked",
                            stage="executing",
                            payload={
                                "evidence_count": evidence_count,
                                "minimum_evidence": minimum_evidence,
                                "source_mode": context.source_mode.value,
                                "passed": False,
                            },
                        )
                        # One focused replan is useful for a poorly phrased first
                        # retrieval (and recovers known short-query cases). Repeating
                        # another full plan after two zero-evidence executions only
                        # burns time/tokens and cannot produce a supported report.
                        if budget.usage.replans == 0 and budget.limits.max_replans > 0:
                            budget.consume_replan()
                            context.budget_usage = budget.usage
                            await self._transition(
                                context,
                                RunStatus.REPLANNING,
                                event_type="plan.replanning",
                                payload={
                                    "recovery_action": "replan",
                                    "recovery_reason": coverage_reason,
                                    "attempt": 1,
                                    "max_attempts": 1,
                                    "failures": failures,
                                },
                            )
                            await self.checkpoints.save(context, "replanning")
                            continue

                        source_hint = (
                            "请导入与该主题相关的私域资料，或改用联网搜索。"
                            if context.source_mode is SourceMode.GRAPHRAG
                            else "请调整问题或检查联网检索配置。"
                        )
                        error_code = "NO_SOURCE_EVIDENCE" if evidence_count == 0 else "INSUFFICIENT_SOURCE_EVIDENCE"
                        evidence_summary = (
                            "未检索到可支持该问题的证据"
                            if evidence_count == 0
                            else f"仅检索到 {evidence_count} 条有效证据，低于本报告所需的 {minimum_evidence} 条"
                        )
                        await self._transition(
                            context,
                            RunStatus.FAILED,
                            event_type="run.failed",
                            error_code=error_code,
                            error_message=f"连续两轮检索后{evidence_summary}；{source_hint}",
                            payload={
                                "recovery_action": "fail",
                                "recovery_reason": coverage_reason,
                                "attempts_exhausted": True,
                                "failures": failures,
                            },
                        )
                        await self.checkpoints.save(context, "failed")
                        continue
                    await self.checkpoints.save(context, "executing")
                    remaining_tokens = budget.remaining_tokens()
                    await self.events.publish(
                        run_id,
                        "budget.stage_reserved",
                        stage="executing",
                        payload={
                            "remaining_tokens": remaining_tokens,
                            "report_reserved_tokens": REPORT_RESERVED_TOKENS,
                            "verification_reserved_tokens": VERIFICATION_RESERVED_TOKENS,
                            "reserved_budget_fallback": not budget.has_token_reserve(
                                REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS
                            ),
                        },
                    )
                    await self._transition(context, RunStatus.REPORTING)

                elif context.status is RunStatus.REPORTING:
                    await self._assert_not_cancelled(context)
                    generated_report = await self._run_with_heartbeat(context, budget, driver.report(), owner=owner)
                    context.report = sanitize_report(self._apply_artifact_edit(context, generated_report))
                    await self._assert_not_cancelled(context)
                    context.workflow_state = driver.snapshot()
                    await self._save_report_artifact(context)
                    await self.checkpoints.save(context, "reporting")
                    report_metrics = (
                        driver.report_metrics() if hasattr(driver, "report_metrics") else {}
                    )
                    await self.events.publish(
                        run_id,
                        "report.completed",
                        stage="reporting",
                        payload={"characters": len(context.report or ""), **report_metrics},
                    )
                    await self._transition(context, RunStatus.VERIFYING)

                elif context.status is RunStatus.VERIFYING:
                    evidence = await self.evidence_repository.list_for_run(run_id)
                    verdict = await self.contracts.evaluate(
                        run_id=run_id, source_mode=context.source_mode, report=context.report or "",
                        evidence=evidence, min_evidence=int(context.config_snapshot.get("min_evidence", 1)),
                        required_sections=list(context.config_snapshot.get("required_sections", [])),
                        consistency_passed=driver.report_consistency(),
                        evidence_card_coverage=(
                            driver.evidence_card_coverage()
                            if hasattr(driver, "evidence_card_coverage")
                            else None
                        ),
                    )
                    context.workflow_state = driver.snapshot()
                    context.workflow_state["verification_failures"] = verdict.failures
                    await self.checkpoints.save(context, "verifying")
                    decision = classify_verification_failures(verdict.failures) if not verdict.passed else None
                    recovery_action = decision.action if verdict.recoverable and decision is not None else "complete" if verdict.passed else "fail"
                    attempt = None
                    max_attempts = None
                    attempts_exhausted = False
                    if recovery_action == "repair_report":
                        max_attempts = budget.limits.max_task_retries
                        attempts_exhausted = budget.usage.task_retries >= max_attempts
                        attempt = budget.usage.task_retries if attempts_exhausted else budget.usage.task_retries + 1
                    elif recovery_action == "replan":
                        max_attempts = budget.limits.max_replans
                        attempts_exhausted = budget.usage.replans >= max_attempts
                        attempt = budget.usage.replans if attempts_exhausted else budget.usage.replans + 1
                    await self.events.publish(
                        run_id, "verification.completed", stage="verifying",
                        payload={
                            "passed": verdict.passed,
                            "failures": verdict.failures,
                            "recovery_action": recovery_action,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "attempts_exhausted": attempts_exhausted,
                        },
                    )
                    if verdict.passed:
                        self.state_machine.validate(context.status, RunStatus.COMPLETED, contract_passed=True)
                        context.status = RunStatus.COMPLETED
                        context.budget_usage = budget.usage
                        self._attach_prefix_usage(context)
                        await self.runs.complete_verified(run_id, assistant_content=context.report or "", usage=budget.snapshot())
                        await self.checkpoints.save(context, "completed")
                        await self.events.publish(run_id, "run.completed", stage="completed", payload={"verified": True})
                        if self.memory_extractor is not None:
                            try:
                                candidates = await self.memory_extractor.extract_from_completed_run(run_id)
                                for candidate in candidates:
                                    await self.events.publish(run_id, "memory.candidate_created", stage="completed", payload={
                                        "memory_id": candidate.memory_id,
                                        "target": candidate.target or candidate.scope,
                                        "kind": candidate.kind,
                                        "confidence": candidate.confidence,
                                        "created_by": candidate.created_by,
                                    })
                            except Exception as exc:
                                await self.events.publish(run_id, "memory.extraction_failed", stage="completed", payload={"error": type(exc).__name__})
                        if self.skill_distiller is not None:
                            try:
                                candidate = await self.skill_distiller.distill(run_id)
                                if candidate is not None:
                                    await self.events.publish(run_id, "skill.candidate_created", stage="completed", payload={"candidate_id": candidate.candidate_id, "name": candidate.name, "version": candidate.proposed_version})
                            except Exception as exc:
                                await self.events.publish(run_id, "skill.distillation_failed", stage="completed", payload={"error": type(exc).__name__})
                        break

                    assert decision is not None
                    if (
                        verdict.recoverable
                        and decision.action == "repair_report"
                        and budget.usage.task_retries >= budget.limits.max_task_retries
                    ):
                        await self._transition(
                            context, RunStatus.FAILED, event_type="run.failed",
                            error_message=f"报告自动修复 {budget.usage.task_retries} 次后仍未通过: {', '.join(verdict.failures)}",
                            payload={"recovery_action": "repair_report", "attempts_exhausted": True, "failures": verdict.failures},
                        )
                    elif (
                        verdict.recoverable
                        and decision.action == "replan"
                        and budget.usage.replans >= budget.limits.max_replans
                    ):
                        await self._transition(
                            context, RunStatus.FAILED, event_type="run.failed",
                            error_message=f"重新规划 {budget.usage.replans} 次后仍未通过: {', '.join(verdict.failures)}",
                            payload={"recovery_action": "replan", "attempts_exhausted": True, "failures": verdict.failures},
                        )
                    elif verdict.recoverable and decision.action == "repair_report" and await driver.repair_report(verdict.failures):
                        budget.consume_retry()
                        context.budget_usage = budget.usage
                        context.workflow_state = driver.snapshot()
                        await self._transition(
                            context, RunStatus.REPORTING, event_type="run.retrying",
                            payload={
                                "recovery_action": "repair_report", "attempt": budget.usage.task_retries,
                                "max_attempts": budget.limits.max_task_retries, "failures": verdict.failures,
                            },
                        )
                        await self.checkpoints.save(context, "reporting")
                    elif verdict.recoverable and decision.action == "replan":
                        budget.consume_replan()
                        context.budget_usage = budget.usage
                        await self._transition(
                            context, RunStatus.REPLANNING, event_type="plan.replanning",
                            payload={
                                "recovery_action": "replan", "attempt": budget.usage.replans,
                                "max_attempts": budget.limits.max_replans, "failures": verdict.failures,
                            },
                        )
                    else:
                        await self._transition(context, RunStatus.FAILED, event_type="run.failed", error_message=f"Completion Contract 未通过: {', '.join(verdict.failures)}")

            return context
        except (RunCancelled, asyncio.CancelledError):
            if context is None:
                # 加载阶段即被取消：无状态机可驱动，原样传播（finally 释放租约）
                raise
            if context.status is not RunStatus.CANCELLING:
                await self._transition(context, RunStatus.CANCELLING, event_type="run.cancelling")
            await self._transition(context, RunStatus.CANCELLED, event_type="run.cancelled")
            await self.checkpoints.save(context, "cancelled")
            return context
        except BudgetExceeded as exc:
            if context is None:
                raise
            await self._transition(context, RunStatus.BUDGET_EXHAUSTED, event_type="run.budget_exhausted", error_code="BUDGET_EXHAUSTED", error_message=str(exc))
            await self.checkpoints.save(context, "budget_exhausted")
            return context
        except RunLeaseLost:
            # 租约被抢占：Run 的状态机由接管者驱动，本地不做任何状态转换或
            # checkpoint 写入，否则会与接管者双写互相覆盖
            if context is None:
                raise
            return context
        except Exception as exc:
            if context is None:
                # 加载阶段失败（Run 不存在/Checkpoint 损坏等）尚无状态机可驱动：
                # 直接落库失败并上抛原始异常——不再经 _load_context 二次抛出
                # 掩盖第一现场，也不用 assert 的 AssertionError 取而代之
                try:
                    await self.runs.update_status(run_id, status="failed", error_code=type(exc).__name__, error_message=str(exc))
                except Exception:
                    # 落库尽力而为：失败本就可能源自持久层故障，落库的二次异常
                    # 不得取代第一现场上抛
                    pass
                raise
            if not self.state_machine.is_terminal(context.status):
                if isinstance(exc, NeedsUserInput):
                    await self._transition(context, RunStatus.NEEDS_USER_INPUT, event_type="run.needs_user_input", error_message=str(exc))
                else:
                    await self._transition(context, RunStatus.FAILED, event_type="run.failed", error_code=type(exc).__name__, error_message=str(exc))
                await self.checkpoints.save(context, context.status.value)
            return context
        finally:
            set_current_run(None)
            await self.runs.release_lease(run_id, owner)

    async def _run_with_heartbeat(self, context: RunContext, budget: BudgetManager, operation, *, owner: str | None = None):
        """Run one long stage while persisting usage and enforcing cancel/budget limits."""
        from deepresearch_agent.observability import get_tracer

        tracer = get_tracer("deepresearch.harness")
        task = asyncio.create_task(operation)
        last_renewal = time.monotonic()
        try:
            with tracer.start_as_current_span(
                "harness.stage", attributes={"run_id": context.run_id, "stage": context.status.value},
            ) as stage_span:
                while not task.done():
                    await asyncio.wait({task}, timeout=1.0)
                    self._attach_prefix_usage(context)
                    context.budget_usage = budget.usage
                    budget.assert_available()
                    await self.runs.update_usage(context.run_id, budget.snapshot())
                    if not task.done():
                        if owner is not None and time.monotonic() - last_renewal >= self.lease_renew_seconds:
                            # 以同 owner 条件续约；失败即租约已被其他 Runtime 抢占，
                            # 本地必须立刻让位，避免接管者与本地双写同一 Run
                            if not await self.runs.acquire_lease(context.run_id, owner, ttl_seconds=self.lease_seconds):
                                raise RunLeaseLost(f"Run {context.run_id} 的租约已被其他 Runtime 抢占")
                            last_renewal = time.monotonic()
                        await self._assert_not_cancelled(context)
                stage_span.set_attribute("stage.usage_tokens", context.budget_usage.llm_tokens)
                return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
