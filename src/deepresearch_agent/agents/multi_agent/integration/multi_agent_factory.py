"""
构建多智能体编排器所需组件的工厂工具。
"""
from dataclasses import dataclass
from typing import Any, Optional

from deepresearch_agent.cache_manager.manager import CacheManager
from deepresearch_agent.agents.multi_agent.executor.worker_coordinator import (
    WorkerCoordinator,
)
from deepresearch_agent.agents.multi_agent.orchestrator import (
    MultiAgentOrchestrator,
    OrchestratorConfig,
)
from deepresearch_agent.agents.multi_agent.planner.base_planner import BasePlanner
from deepresearch_agent.agents.multi_agent.reporter.base_reporter import BaseReporter
from deepresearch_agent.agents.multi_agent.reporter.base_reporter import ReporterConfig
from deepresearch_agent.config.settings import (
    MULTI_AGENT_AUTO_GENERATE_REPORT,
    MULTI_AGENT_STOP_ON_CLARIFICATION,
    MULTI_AGENT_STRICT_PLAN_SIGNAL,
)
from deepresearch_agent.retrieval.base import RetrievalProvider
from deepresearch_agent.retrieval.router import RetrievalRouter


@dataclass
class OrchestratorBundle:
    """封装编排器所依赖的 Planner、Worker、Reporter 等组件。"""

    planner: BasePlanner
    worker: WorkerCoordinator
    reporter: BaseReporter
    orchestrator: MultiAgentOrchestrator
    controller_model: Any = None  # T-016：coreact controller 角色模型（None=全局默认）


class MultiAgentFactory:
    """用于创建带默认配置的多智能体编排栈。"""

    @staticmethod
    def create_default_bundle(
        *,
        planner: Optional[BasePlanner] = None,
        worker: Optional[WorkerCoordinator] = None,
        reporter: Optional[BaseReporter] = None,
        orchestrator_config: Optional[OrchestratorConfig] = None,
        reporter_config: Optional[ReporterConfig] = None,
        cache_manager: Optional[CacheManager] = None,
        retrieval_provider: Optional[RetrievalProvider] = None,
        retrieval_router: Optional[RetrievalRouter] = None,
        role_models: Optional[dict] = None,
    ) -> OrchestratorBundle:
        """role_models: T-016 角色级模型（{"planner": {...}, "reporter": {...}}，
        值为 model_tier.resolve_role_model 的输出）。None 时全部走全局默认。"""
        role_models = role_models or {}
        planner_model = (role_models.get("planner") or {}).get("model")
        reporter_model = (role_models.get("reporter") or {}).get("model")
        # resolve_role_model 产出模型 ID 字符串；此处经 get_llm_model 构造实例
        # （复用 gateway/failover/前缀缓存等既有客户端配置）。
        from deepresearch_agent.models.get_models import get_llm_model

        planner_llm = get_llm_model(model=planner_model) if planner_model else None
        reporter_llm = get_llm_model(model=reporter_model) if reporter_model else None
        planner = planner or BasePlanner(llm=planner_llm)
        # Evidence Card and section checkpoints use exact content/report hashes.
        cache_manager = cache_manager or CacheManager(enable_vector_similarity=False)
        worker = worker or WorkerCoordinator(
            retrieval_provider=retrieval_provider,
            retrieval_router=retrieval_router,
        )
        if reporter is None and reporter_model:
            # T-016：reporter 角色——主写作组件（OutlineBuilder/SectionWriter）
            # 注入分层模型；其余子组件（一致性检查等）不调 LLM 或走全局默认。
            from deepresearch_agent.agents.multi_agent.reporter.outline_builder import OutlineBuilder
            from deepresearch_agent.agents.multi_agent.reporter.section_writer import SectionWriter

            reporter = BaseReporter(
                config=reporter_config,
                cache_manager=cache_manager,
                outline_builder=OutlineBuilder(llm=reporter_llm),
                section_writer=SectionWriter(llm=reporter_llm, config=reporter_config.section_writer if reporter_config else None),
            )
        reporter = reporter or BaseReporter(
            config=reporter_config,
            cache_manager=cache_manager,
        )
        orchestrator_config = orchestrator_config or OrchestratorConfig(
            auto_generate_report=MULTI_AGENT_AUTO_GENERATE_REPORT,
            stop_on_clarification=MULTI_AGENT_STOP_ON_CLARIFICATION,
            strict_plan_signal=MULTI_AGENT_STRICT_PLAN_SIGNAL,
        )
        controller_model_id = (role_models.get("controller") or {}).get("model")
        orchestrator = MultiAgentOrchestrator(
            planner=planner,
            worker_coordinator=worker,
            reporter=reporter,
            config=orchestrator_config,
        )
        if controller_model_id:
            # controller 需要可 invoke 的 LLM 实例；bundle.controller_model 保留 ID 供审计
            orchestrator.controller_model = get_llm_model(model=controller_model_id)
        return OrchestratorBundle(
            planner=planner,
            worker=worker,
            reporter=reporter,
            orchestrator=orchestrator,
            controller_model=controller_model_id,
        )


__all__ = ["MultiAgentFactory", "OrchestratorBundle"]
