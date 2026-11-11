def test_coreact_surface_imports():
    """T-408 冒烟：唯一 coreact 面的公共模块可导入（退役模块不再存在）。"""
    from deepresearch_agent.agents.multi_agent.orchestrator import MultiAgentOrchestrator  # noqa: F401
    from deepresearch_agent.agents.multi_agent.planner import BriefBuilder  # noqa: F401
    from deepresearch_agent.harness import RunStatus, SourceMode  # noqa: F401
    from deepresearch_agent.harness.action_runner import ActionRunner  # noqa: F401
    from deepresearch_agent.persistence import ArtifactStore, Database  # noqa: F401
