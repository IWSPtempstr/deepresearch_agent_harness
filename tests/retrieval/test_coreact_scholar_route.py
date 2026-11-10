"""Regression: scholar_search must use the provider/SourcePolicy path, not legacy tool registry."""

from deepresearch_agent.agents.multi_agent.core.plan_spec import (
    PlanExecutionSignal,
    TaskNode,
)
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor
from deepresearch_agent.harness.contracts import SourceMode


class ScholarProvider:
    mode = SourceMode.SCHOLAR
    provider_name = "scholar"

    def __init__(self):
        self.calls = []

    async def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return []


def test_scholar_search_routes_through_provider_and_source_policy():
    provider = ScholarProvider()
    executor = RetrievalExecutor(provider=provider)
    task = TaskNode(
        task_id="task_scholar",
        task_type="scholar_search",
        source_mode="scholar",
        description="检索论文",
        parameters={"query": "agentic research"},
    )
    state = PlanExecuteState(
        session_id="session-scholar",
        run_id="run-scholar",
        source_mode="scholar",
        workflow_mode="plan_execute_report",
        input="agentic research",
    )
    signal = PlanExecutionSignal(
        plan_id="plan-scholar",
        version=1,
        source_mode="scholar",
        execution_mode="sequential",
        tasks=[task.model_dump()],
        execution_sequence=[task.task_id],
        assumptions=[],
        acceptance_criteria={},
    )

    result = executor.execute_task(task, state, signal)

    assert result.success is True
    assert provider.calls and provider.calls[0][0] == "agentic research"
    assert provider.calls[0][1]["call_context"].source_mode is SourceMode.SCHOLAR
    assert result.record.tool_calls[0].tool_name == "scholar_search"
