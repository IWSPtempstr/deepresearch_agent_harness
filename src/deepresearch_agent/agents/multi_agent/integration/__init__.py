"""多智能体集成工具（T-408：唯一 coreact 装配工厂；legacy facade 已退役）。"""

from deepresearch_agent.agents.multi_agent.integration.multi_agent_factory import (
    MultiAgentFactory,
    OrchestratorBundle,
)

__all__ = [
    "MultiAgentFactory",
    "OrchestratorBundle",
]
