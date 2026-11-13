# 由 harness/drivers_plan_execute.py 拆分（T-016），方法体与原位置逐行一致。
# 本文件保留类声明与导入路径；未拆出的方法：__init__, _restore, plan, execute。
import asyncio
from datetime import datetime, timezone
from typing import Any
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.orchestrator import MultiAgentOrchestrator
from deepresearch_agent.harness.clarifications import build_request, parse_answers, store_request
from deepresearch_agent.harness.coreact_controller import CoreActController
from deepresearch_agent.harness.coreact_state import (
    CoreActLoopState,
    loop_state_from_workflow_state,
)
from deepresearch_agent.harness.errors import NeedsUserInput
from deepresearch_agent.agents.multi_agent.planner.result import PlannerResult
from deepresearch_agent.agents.multi_agent.reporter.base_reporter import ReportResult
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
        # T-408：staged 引擎已删除；新写入恒为 coreact_v1（存量 staged checkpoint
        # 的恢复在 _restore 中显式拒绝并列入人工处理，不启动旧引擎）。
        self.orchestration = str((context.config_snapshot or {}).get("orchestration") or "coreact_v1")
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
        # BC-01：Run 行上的澄清回答是权威输入；恢复的 checkpoint 快照可能早于
        # 本次回答，合并后 BriefBuilder 才能消费（不覆盖已有快照字段）。
        self.state.context_snapshot = {
            **(self.state.context_snapshot or {}),
            "clarifications": [
                answer.model_dump(mode="json") for answer in parse_answers(context.config_snapshot)
            ],
        }

    def _restore(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        # 快照戳记权威：缺失/旧值 = 旧 staged 快照（引擎已删除 → 恢复时显式拒绝）
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
                # BC-01：把澄清器给出的**实际问题**写入 Run 请求（由 Runtime
                # 落库并暴露给前端）；空问题退回一条可回答的通用问句，不造问题。
                questions = list(self.planner_result.clarification.questions or [])
                if not questions:
                    questions = ["请补充研究范围、时间条件或期望的输出形式。"]
                request = build_request(
                    self.context.config_snapshot, questions=questions, source="brief",
                    now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                )
                store_request(self.context.config_snapshot, request)
                raise NeedsUserInput("needs_user_input")
            raise RuntimeError("Planner 未生成可执行 PlanSpec")
        # T-408：唯一 coreact 路径（orchestration != coreact_v1 的存量快照在
        # _restore 标记，execute 时显式拒绝）。
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
        if self.orchestration != "coreact_v1":
            # T-408：staged 引擎已删除——存量 staged 活动快照显式拒绝
            # （人工处理清单），不静默换引擎、不清空重跑。
            from deepresearch_agent.harness.errors import AppError, ErrorCode

            raise AppError(
                ErrorCode.LEGACY_WORKFLOW_UNSUPPORTED,
                f"Run {self.context.run_id} 为旧 staged_v1 编排快照（活动状态）；"
                "staged 引擎已退役，请人工处理（转换工具 convert_legacy_checkpoints.py / 重建），不自动续跑",
            )
        await self._execute_coreact_loop()
