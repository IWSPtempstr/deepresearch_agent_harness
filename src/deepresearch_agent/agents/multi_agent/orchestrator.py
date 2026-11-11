"""
多Agent总编排器（T-408：仅保留 coreact 所需的组件方法）。

整流程外壳 run()（Planner→批量执行→Reporter）与 OrchestratorResult/Metrics
已随 staged 引擎退役——Harness 经 plan/execute_action/report 显式组合组件，
Controller 是唯一下一步动作决策者。
"""
from typing import Optional, Sequence, Any, Protocol
import logging

from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.planner.result import PlannerResult
from deepresearch_agent.agents.multi_agent.executor.worker_coordinator import WorkerCoordinator
from deepresearch_agent.agents.multi_agent.reporter.base_reporter import (
    BaseReporter,
    ReportResult,
)

_LOGGER = logging.getLogger(__name__)


class OrchestratorConfig:
    """coreact 装配消费（create_coreact_bundle）；原整流程外壳开关已随 run() 退役。"""

    auto_generate_report: bool = True
    stop_on_clarification: bool = True
    strict_plan_signal: bool = True

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _PlannerProtocol(Protocol):
    """BriefBuilder 鸭子类型（generate_plan 同签名）。"""

    def generate_plan(self, state: PlanExecuteState, *, assumptions: Optional[Sequence[str]] = None) -> PlannerResult: ...


class MultiAgentOrchestrator:
    """coreact 组件编排：plan / execute_action / report 显式组合。"""

    def __init__(
        self,
        *,
        planner: _PlannerProtocol,
        worker_coordinator: WorkerCoordinator,
        reporter: BaseReporter,
        config: Optional[OrchestratorConfig] = None,
    ) -> None:
        self._planner = planner
        self._worker = worker_coordinator
        self._reporter = reporter
        self.config = config or OrchestratorConfig()

    def plan(
        self,
        state: PlanExecuteState,
        *,
        assumptions: Optional[Sequence[str]] = None,
    ) -> PlannerResult:
        """Run only the BriefBuilder stage for Harness checkpointing."""
        return self._planner.generate_plan(
            state,
            assumptions=list(assumptions) if assumptions else None,
        )

    @property
    def controller_model(self):
        """T-016：coreact controller 角色模型（由 bundle 注入）；缺省回落 planner LLM。"""
        return getattr(self, "_controller_model", None) or getattr(self._planner, "_llm", None)

    @controller_model.setter
    def controller_model(self, value):
        self._controller_model = value

    def execute_action(self, state: PlanExecuteState, task, *, progress_callback=None):
        """Execute one controller-selected task without exposing coordinator internals."""
        return self._worker.execute_action(
            state, task, progress_callback=progress_callback,
        )

    def report(
        self,
        state: PlanExecuteState,
        *,
        report_type: Optional[str] = None,
    ) -> ReportResult:
        """Run only the Reporter/ConsistencyChecker stage."""
        return self._reporter.generate_report(state, report_type=report_type)

    def recheck_report_consistency(
        self,
        report_content: str,
        evidence: Sequence[Any],
    ):
        """Re-run the Reporter's consistency checker after a targeted repair."""
        return self._reporter.recheck_consistency(report_content, evidence)
