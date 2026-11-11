"""
Executor层对外接口（T-408：唯一 coreact 执行器栈）
"""

from deepresearch_agent.agents.multi_agent.executor.base_executor import (
    BaseExecutor,
    ExecutorConfig,
    TaskExecutionResult,
)
from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor
from deepresearch_agent.agents.multi_agent.executor.worker_coordinator import WorkerCoordinator

__all__ = [
    "BaseExecutor",
    "ExecutorConfig",
    "TaskExecutionResult",
    "RetrievalExecutor",
    "WorkerCoordinator",
]
