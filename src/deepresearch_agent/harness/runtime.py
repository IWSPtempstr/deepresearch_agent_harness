"""Persistent Harness Runtime driving Context -> Plan -> Execute -> Report -> Verify."""
from __future__ import annotations


# 由 harness/runtime.py 拆分（T-014），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, _attach_prefix_usage, _load_context, _safe_stage_after, _transition, _pause_at_safe_boundary, _assert_not_cancelled。
import json
from typing import Any, Callable
from deepresearch_agent.harness.budgets import BudgetLimits
from deepresearch_agent.harness.checkpoints import CheckpointManager
from deepresearch_agent.harness.contracts import ContractEvaluator, RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.harness.evidence import EvidenceLedger
from deepresearch_agent.harness.event_bus import EventBus
from deepresearch_agent.harness.errors import RunCancelled
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.harness.state_machine import StateMachine
from deepresearch_agent.persistence.artifact_store import ArtifactStore
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository,
)
WorkflowFactory = Callable[[RunContext, EventBus], Any]

from deepresearch_agent.harness.runtime_execution import RuntimeExecutionMixin
from deepresearch_agent.harness.runtime_persistence import RuntimePersistMixin

class HarnessRuntime(RuntimeExecutionMixin, RuntimePersistMixin):


    def __init__(
        self,
        *,
        run_repository: RunRepository,
        message_repository: MessageRepository,
        event_repository: EventRepository,
        checkpoint_repository: CheckpointRepository,
        evidence_repository: EvidenceRepository,
        contract_repository: ContractRepository,
        workflow_factory: WorkflowFactory,
        trajectory_repository: PlanTaskToolRepository | None = None,
        artifact_store: ArtifactStore | None = None,
        artifact_repository: ArtifactRepository | None = None,
        event_bus: EventBus | None = None,
        context_builder: Any | None = None,
        memory_extractor: Any | None = None,
        skill_distiller: Any | None = None,
        prefix_tracker: Any | None = None,
        lease_seconds: int = 30,
    ):
        self.runs = run_repository
        self.messages = message_repository
        self.events = event_bus or EventBus(event_repository)
        self.checkpoints = CheckpointManager(checkpoint_repository)
        self.evidence_repository = evidence_repository
        self.evidence_ledger = EvidenceLedger(evidence_repository)
        self.contracts = ContractEvaluator(contract_repository)
        self.trajectory = trajectory_repository
        self.artifact_store = artifact_store
        self.artifact_repository = artifact_repository
        self.workflow_factory = workflow_factory
        self.context_builder = context_builder
        self.memory_extractor = memory_extractor
        self.skill_distiller = skill_distiller
        self.prefix_tracker = prefix_tracker
        self.state_machine = StateMachine()
        self.lease_seconds = lease_seconds
        # 续约间隔取 TTL 的 1/3：租约到期被抢占后，本地最多再写这么久就会让位
        self.lease_renew_seconds = max(1.0, lease_seconds / 3)

    def _attach_prefix_usage(self, context: RunContext) -> None:
        """把当前 Run 的前缀缓存用量回填到 budget usage（幂等，可重复调用）。"""
        if self.prefix_tracker is None:
            return
        snapshot = self.prefix_tracker.run_snapshot(context.run_id)
        if not snapshot:
            return
        totals = snapshot.get("totals") or {}
        context.budget_usage.prefix_cache_requests = int(totals.get("requests", 0))
        context.budget_usage.prefix_cache_hit_tokens = int(totals.get("hit_tokens", 0))
        context.budget_usage.prefix_cache_miss_tokens = int(totals.get("miss_tokens", 0))
        reported_tokens = int(totals.get("input_tokens", 0)) + int(totals.get("output_tokens", 0))
        context.budget_usage.llm_tokens = max(context.budget_usage.llm_tokens, reported_tokens)

    async def _load_context(self, run_id: str) -> RunContext:
        run = await self.runs.get(run_id)
        if run is None:
            raise ValueError(f"Run 不存在: {run_id}")
        restored = await self.checkpoints.restore(run_id)
        config_snapshot = json.loads(run.config_snapshot_json or "{}")
        if restored is not None:
            persisted_status = None if run.status == "pausing" else RunStatus(run.status)
            if persisted_status is RunStatus.INTERRUPTED:
                restored.resume_from_status = self._safe_stage_after(restored.status)
                # A step-driven loop may still have actions left: resuming must
                # continue the loop instead of jumping straight to REPORTING.
                if (
                    restored.status is RunStatus.EXECUTING
                    and self._coreact_continuation_pending(restored)
                ):
                    restored.resume_from_status = RunStatus.EXECUTING
                restored.status = RunStatus.INTERRUPTED
            elif persisted_status is RunStatus.PAUSED:
                restored.status = RunStatus.PAUSED
                # 暂停确认与 checkpoint 分开提交，可能出现「Run 已 paused、快照
                # 仍是旧阶段」。以 Run 行的 pause_resume_status 为准，避免从
                # queued 重跑已提交阶段。
                target = config_snapshot.get("pause_resume_status")
                if isinstance(target, str):
                    try:
                        restored.resume_from_status = RunStatus(target)
                    except ValueError:
                        pass
            elif persisted_status is not None:
                restored.status = persisted_status
            restored.cancellation_requested = bool(run.cancellation_requested)
            restored.config_snapshot.update(config_snapshot)
            return restored
        message = await self.messages.get(run.trigger_message_id)
        if message is None:
            raise ValueError(f"Run {run_id} 的触发消息不存在")
        limits = json.loads(run.budget_json or "{}") or {}
        return RunContext(
            run_id=run.run_id, session_id=run.session_id, trigger_message_id=run.trigger_message_id,
            source_mode=SourceMode(run.source_mode), workflow_mode=WorkflowMode(run.workflow_mode),
            status=(
                RunStatus(run.current_stage or "queued")
                if run.status == "pausing" else RunStatus(run.status)
            ), original_query=message.content,
            config_snapshot=config_snapshot,
            model_snapshot=json.loads(run.model_snapshot_json or "{}"),
            budget_limits=BudgetLimits.model_validate(limits),
            resume_from_status=(
                RunStatus(config_snapshot.get("pause_resume_status", "queued"))
                if run.status == "paused" else None
            ),
        )

    @staticmethod
    def _safe_stage_after(checkpoint_status: RunStatus) -> RunStatus:
        """Return the first operation not committed by a stage-end checkpoint."""
        return {
            RunStatus.CONTEXT_BUILDING: RunStatus.PLANNING,
            RunStatus.PLANNING: RunStatus.EXECUTING,
            RunStatus.REPLANNING: RunStatus.EXECUTING,
            RunStatus.EXECUTING: RunStatus.REPORTING,
            RunStatus.RETRYING: RunStatus.EXECUTING,
            RunStatus.REPORTING: RunStatus.VERIFYING,
            RunStatus.VERIFYING: RunStatus.VERIFYING,
        }.get(checkpoint_status, RunStatus.QUEUED)

    @staticmethod
    def _coreact_continuation_pending(context: RunContext) -> bool:
        """Whether an EXECUTING checkpoint still has an unfinished action loop.

        A stage-end EXECUTING checkpoint means "the batch finished" and resumes
        at REPORTING. A step-driven (coreact) checkpoint means "actions are
        committed one by one", so an unfinished loop must resume at EXECUTING.
        Legacy snapshots carry no coreact marker and keep the old mapping.
        """
        from deepresearch_agent.harness.coreact_state import loop_state_from_workflow_state

        state = loop_state_from_workflow_state(context.workflow_state)
        return state is not None and state.continuation_pending()

    async def _transition(
        self,
        context: RunContext,
        target: RunStatus,
        *,
        event_type: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.state_machine.validate(context.status, target)
        context.status = target
        self._attach_prefix_usage(context)
        current_stage = (
            context.resume_from_status.value
            if target is RunStatus.PAUSED and context.resume_from_status is not None
            else target.value
        )
        await self.runs.update_status(context.run_id, status=target.value, current_stage=current_stage, error_code=error_code, error_message=error_message, usage={"limits": context.budget_limits.model_dump(), "usage": context.budget_usage.model_dump()})
        event_payload = {"status": target.value, "error_code": error_code, **(payload or {})}
        if target is RunStatus.PAUSED:
            event_payload["resume_from_status"] = current_stage
        await self.events.publish(context.run_id, event_type or "run.stage_changed", stage=target.value, payload=event_payload)

    async def _pause_at_safe_boundary(self, context: RunContext) -> bool:
        run = await self.runs.get(context.run_id)
        # BC-02：取消优先。pause 与 cancel 同时存在时不得先落 PAUSED，否则
        # 取消请求会被暂停终态掩盖；交回主循环的 _assert_not_cancelled 处理。
        if run is not None and run.cancellation_requested:
            return False
        pause_requested = bool(
            run and json.loads(run.config_snapshot_json or "{}").get("pause_requested")
        )
        if run is None or (run.status != "pausing" and not pause_requested):
            return False
        context.resume_from_status = context.status
        # 先持久化续跑阶段，再写暂停状态：两者分开提交时，崩溃窗口内最多是
        # 「仍在运行但已记录安全阶段」，不会出现「paused 但不知道从哪继续」。
        await self.runs.set_pause_resume_status(context.run_id, context.resume_from_status.value)
        await self._transition(context, RunStatus.PAUSED, event_type="run.paused")
        await self.checkpoints.save(context, "paused")
        await self.runs.clear_pause_request(context.run_id)
        return True

    async def _assert_not_cancelled(self, context: RunContext) -> None:
        run = await self.runs.get(context.run_id)
        if run is not None and run.cancellation_requested:
            context.cancellation_requested = True
            raise RunCancelled("Run 已请求取消")
