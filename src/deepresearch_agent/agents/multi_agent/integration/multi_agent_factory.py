"""
构建多智能体编排器所需组件的工厂工具。
"""
from dataclasses import dataclass
from typing import Any, Optional

from deepresearch_agent.cache_manager.manager import CacheManager
from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor
from deepresearch_agent.agents.multi_agent.executor.worker_coordinator import (
    WorkerCoordinator,
)
from deepresearch_agent.agents.multi_agent.orchestrator import (
    MultiAgentOrchestrator,
    OrchestratorConfig,
)
from deepresearch_agent.agents.multi_agent.planner.brief_builder import BriefBuilder, BriefBuilderConfig
from deepresearch_agent.agents.multi_agent.reporter.base_reporter import BaseReporter
from deepresearch_agent.agents.multi_agent.reporter.base_reporter import ReporterConfig
from deepresearch_agent.config.settings import (
    MULTI_AGENT_ALLOW_UNCLARIFIED_PLAN,
    MULTI_AGENT_AUTO_GENERATE_REPORT,
    MULTI_AGENT_DEFAULT_DOMAIN,
    MULTI_AGENT_STOP_ON_CLARIFICATION,
    MULTI_AGENT_STRICT_PLAN_SIGNAL,
)
from deepresearch_agent.retrieval.base import RetrievalProvider


@dataclass
class OrchestratorBundle:
    """封装编排器所依赖的 Planner、Worker、Reporter 等组件。"""

    planner: BriefBuilder
    worker: WorkerCoordinator
    reporter: BaseReporter
    orchestrator: MultiAgentOrchestrator
    controller_model: Any = None  # T-016：coreact controller 角色模型（None=全局默认）


class MultiAgentFactory:
    """用于创建带默认配置的多智能体编排栈。"""


    @staticmethod
    def create_coreact_bundle(
        *,
        retrieval_provider: Optional[RetrievalProvider] = None,
        worker: Optional[WorkerCoordinator] = None,
        reporter: Optional[BaseReporter] = None,
        reporter_config: Optional[ReporterConfig] = None,
        cache_manager: Optional[CacheManager] = None,
        role_models: Optional[dict] = None,
        brief_config: Optional[BriefBuilderConfig] = None,
    ) -> OrchestratorBundle:
        """T-406：唯一 coreact 路径的组件装配。

        - planner = BriefBuilder（无 reflection 任务；不依赖 TaskDecomposer/
          PlanReviewer/Reflector）；
        - worker 仅含 RetrievalExecutor（ReflectionExecutor 类已随旧引擎物理
          删除——"不注册旧 Reflector"由结构保证）；
        - 角色模型：brief/controller/reporter（旧 planner key 由
          canonical_role_models 映射为 brief）。
        """
        from deepresearch_agent.harness.research_brief import canonical_role_models

        canonical = canonical_role_models(role_models)
        brief_model = (canonical.get("brief") or {}).get("model")
        reporter_model = (canonical.get("reporter") or {}).get("model")
        controller_model_id = (canonical.get("controller") or {}).get("model")
        from deepresearch_agent.models.get_models import get_llm_model

        brief_llm = get_llm_model(model=brief_model) if brief_model else None
        reporter_llm = get_llm_model(model=reporter_model) if reporter_model else None
        planner = BriefBuilder(
            llm=brief_llm,
            config=brief_config or BriefBuilderConfig(
                allow_unclarified_plan=MULTI_AGENT_ALLOW_UNCLARIFIED_PLAN,
                default_domain=MULTI_AGENT_DEFAULT_DOMAIN,
            ),
        )
        cache_manager = cache_manager or CacheManager(enable_vector_similarity=False)
        # T-408：ReflectionExecutor 类已物理删除——结构上不可能注册旧 Reflector。
        worker = worker or WorkerCoordinator(
            retrieval_provider=retrieval_provider,
            executors=[RetrievalExecutor(provider=retrieval_provider)],
        )
        if reporter is None and reporter_model:
            from deepresearch_agent.agents.multi_agent.reporter.outline_builder import OutlineBuilder
            from deepresearch_agent.agents.multi_agent.reporter.section_writer import SectionWriter

            reporter = BaseReporter(
                config=reporter_config,
                cache_manager=cache_manager,
                outline_builder=OutlineBuilder(llm=reporter_llm),
                section_writer=SectionWriter(llm=reporter_llm, config=reporter_config.section_writer if reporter_config else None),
            )
        reporter = reporter or BaseReporter(config=reporter_config, cache_manager=cache_manager)
        orchestrator = MultiAgentOrchestrator(
            planner=planner,  # type: ignore[arg-type] —— duck-compatible generate_plan
            worker_coordinator=worker,
            reporter=reporter,
            config=OrchestratorConfig(
                auto_generate_report=MULTI_AGENT_AUTO_GENERATE_REPORT,
                stop_on_clarification=MULTI_AGENT_STOP_ON_CLARIFICATION,
                strict_plan_signal=MULTI_AGENT_STRICT_PLAN_SIGNAL,
            ),
        )
        if controller_model_id:
            orchestrator.controller_model = get_llm_model(model=controller_model_id)
        return OrchestratorBundle(
            planner=planner,
            worker=worker,
            reporter=reporter,
            orchestrator=orchestrator,
            controller_model=controller_model_id,
        )


__all__ = ["MultiAgentFactory", "OrchestratorBundle"]
