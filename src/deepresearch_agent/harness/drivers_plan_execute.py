# 由 harness/drivers_plan_execute.py 拆分（T-016），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, _restore, plan, execute。
import asyncio
from typing import Any
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec, TaskNode
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.orchestrator import MultiAgentOrchestrator
from deepresearch_agent.harness.coreact_controller import CoreActController, DecisionContext
from deepresearch_agent.harness.coreact_state import (
    CoreActLoopState,
    CoreActPaused,
    PendingAction,
    loop_state_from_workflow_state,
)
from deepresearch_agent.harness.errors import NeedsUserInput
from deepresearch_agent.agents.multi_agent.planner.base_planner import PlannerResult
from deepresearch_agent.agents.multi_agent.reporter.base_reporter import ReportResult, SectionContent
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
from deepresearch_agent.evolution.skill_compiler import compile_runtime_policy, policy_prompt
from .run_context import RunContext

from deepresearch_agent.harness.coreact_loop import CoreActLoopMixin
from deepresearch_agent.harness.coreact_support import CoreActSupportMixin
from deepresearch_agent.harness.report_pipeline import ReportPipelineStagedMixin
from deepresearch_agent.harness.driver_adapters import DriverAdaptersMixin

class PlanExecuteReportDriver(CoreActLoopMixin, CoreActSupportMixin, ReportPipelineStagedMixin, DriverAdaptersMixin):
    """plan_execute_report 外壳；按 Run 戳记分派 staged_v1 或 coreact_v1。"""

    supports_step_commit = True

    def __init__(self, context: RunContext, orchestrator: MultiAgentOrchestrator, *, events=None):
        self.context = context
        self.orchestrator = orchestrator
        self.events = events
        self.orchestration = str((context.config_snapshot or {}).get("orchestration") or "staged_v1")
        self.coreact_loop: CoreActLoopState | None = None
        self._coreact_controller = CoreActController(llm=getattr(orchestrator, "controller_model", None))
        # Harness 注入；直接实例化 driver 的旧测试/离线工具无需提供
        self.step_commit = None
        # 步间暂停/租约探测（由 runtime 注入）；返回 True 时在动作边界处理
        self.pause_requested = None
        self.lease_probe = None
        self.selected_skill = (context.context_snapshot or {}).get("selected_skill") or None
        self.skill_policy = compile_runtime_policy(self.selected_skill, source_mode=context.source_mode.value)
        runtime_snapshot = dict(context.context_snapshot or context.config_snapshot)
        runtime_snapshot["active_skill_policy"] = self.skill_policy
        runtime_snapshot["skill_policy_consumers"] = ["planner", "worker", "reflection", "reporter", "verifier"] if self.skill_policy else []
        self.state = PlanExecuteState(
            session_id=context.session_id, run_id=context.run_id,
            source_mode=context.source_mode.value, workflow_mode=context.workflow_mode.value,
            budget_state={"limits": context.budget_limits.model_dump(), "usage": context.budget_usage.model_dump()},
            context_snapshot=runtime_snapshot,
            # Planning must reason about the user's request, not the serialized
            # system/context envelope. The latter remains available separately.
            input=(context.resolved_query or context.original_query) + policy_prompt(self.skill_policy),
        )
        self.planner_result: PlannerResult | None = None
        self.report_result: ReportResult | None = None
        self._repair_pending = False
        self._restore(context.workflow_state)

    def _restore(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        # Snapshot implementation marker is authoritative; missing marker means legacy staged_v1.
        self.orchestration = str(payload.get("orchestration") or "staged_v1")
        raw_state = payload.get("state")
        if raw_state:
            self.state = PlanExecuteState.model_validate(raw_state)
            if isinstance(self.state.plan, dict):
                self.state.plan = PlanSpec.model_validate(self.state.plan)
            self.state.execution_records = [
                item if isinstance(item, ExecutionRecord) else ExecutionRecord.model_validate(item)
                for item in self.state.execution_records
            ]
            # ExecutionRecord.evidence is List[Any] for circular-import reasons;
            # checkpoint JSON restore therefore leaves dicts that downstream
            # evidence/report adapters expect to be RetrievalResult instances.
            for record in self.state.execution_records:
                record.evidence = [
                    evidence if isinstance(evidence, RetrievalResult) else RetrievalResult.from_dict(evidence)
                    for evidence in record.evidence
                ]
        if payload.get("planner_result"):
            self.planner_result = PlannerResult.model_validate(payload["planner_result"])
        if payload.get("report_result"):
            self.report_result = ReportResult.model_validate(payload["report_result"])
        self._repair_pending = bool(payload.get("repair_pending"))
        self.coreact_loop = loop_state_from_workflow_state({"coreact": payload.get("coreact")})

    async def plan(self, failures: list[str] | None = None) -> None:
        self.planner_result = await asyncio.to_thread(
            self.orchestrator.plan, self.state, assumptions=failures or None,
        )
        if self.planner_result.plan_spec is None:
            if self.planner_result.needs_clarification():
                raise NeedsUserInput("needs_user_input")
            raise RuntimeError("Planner 未生成可执行 PlanSpec")
        if self.orchestration == "coreact_v1":
            self.state.plan = self.planner_result.plan_spec
            if self.coreact_loop is None or self.coreact_loop.done:
                self.coreact_loop = CoreActLoopState()
            if failures:
                self.coreact_loop.phase = "decide"
                self.coreact_loop.pending_action = None
                self.coreact_loop.done = False
                self.coreact_loop.stop_reason = None

    async def execute(self) -> None:
        if self.planner_result is None:
            raise RuntimeError("缺少 PlannerResult")
        if self.orchestration == "coreact_v1":
            await self._execute_coreact_loop()
            return
        execution_token_ceiling = max(
            0,
            self.context.budget_limits.max_llm_tokens
            - REPORT_RESERVED_TOKENS
            - VERIFICATION_RESERVED_TOKENS,
        )
        loop = asyncio.get_running_loop()

        def progress_callback(kind, task, record):
            if self.events is None:
                return
            future = asyncio.run_coroutine_threadsafe(
                self._publish_execution_progress(kind, task, record), loop,
            )
            future.result(timeout=10)

        records = await asyncio.to_thread(
            self.orchestrator.execute,
            self.state,
            self.planner_result,
            stop_predicate=lambda: (
                self.context.budget_usage.llm_tokens >= execution_token_ceiling
            ),
            progress_callback=progress_callback,
        )
        existing = {item.record_id for item in self.state.execution_records}
        self.state.execution_records.extend(item for item in records if item.record_id not in existing)
