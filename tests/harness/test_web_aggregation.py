"""S1：scholar API 纳入 web 聚合通道。

覆盖：白名单/policy/controller 一致、聚合 provider 路由与证据归属、预算分账、
(tool, query) 去重、API 边界归一化、legacy scholar 只读兼容。
"""

import pytest

from backend.app.schemas import MessageSend
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanExecutionSignal, TaskNode
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor
from deepresearch_agent.agents.multi_agent.planner.brief_builder import BriefBuilder
from deepresearch_agent.evolution.skill_compiler import compile_runtime_policy
from deepresearch_agent.harness import SourceMode
from deepresearch_agent.harness.action_models import action_spec_from_task_node
from deepresearch_agent.harness.action_runner import ActionRunner, BatchAction
from deepresearch_agent.harness.budgets import BudgetExceeded, BudgetLimits, BudgetManager
from deepresearch_agent.harness.coreact_controller import SOURCE_TASK_TYPES
from deepresearch_agent.harness.dedupe import query_dedupe_key
from deepresearch_agent.harness.errors import AppError
from deepresearch_agent.harness.policies import SourcePolicy
from deepresearch_agent.harness.research_brief import SOURCE_TOOL_WHITELIST
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext
from deepresearch_agent.retrieval.router import RetrievalRouter, _LazyProvider
from deepresearch_agent.retrieval.web_aggregate import AggregateWebProvider
from deepresearch_agent.agents.multi_agent.core.rubric import build_task_rubric
import time
from concurrent.futures import ThreadPoolExecutor


class FakeProvider:
    def __init__(self, mode: SourceMode, name: str):
        self.mode = mode
        self.provider_name = name
        self.calls = []

    async def search(self, query, *, top_k, search_depth, filters, call_context):
        self.calls.append({"query": query, "filters": filters, "context": call_context})
        return [RetrievalResult(
            result_id=f"raw-{self.provider_name}",
            granularity="Chunk",
            evidence=f"{self.provider_name} evidence for {query}",
            metadata=RetrievalMetadata(
                source_id=f"doc-{self.provider_name}",
                source_type="chunk",
                content_hash="a" * 64,
            ),
            source="scholar_search" if self.provider_name == "scholar" else "tavily_search",
            source_mode=self.mode.value,
            score=0.9,
        )]


def _aggregate():
    tavily = FakeProvider(SourceMode.WEB, "tavily")
    scholar = FakeProvider(SourceMode.SCHOLAR, "scholar")
    return AggregateWebProvider(tavily=tavily, scholar=scholar), tavily, scholar


def test_whitelist_policy_and_controller_agree_on_web_tools():
    assert SOURCE_TOOL_WHITELIST["web"] == {"web_search", "scholar_search"}
    assert SourcePolicy.SOURCE_TASK_TYPES[SourceMode.WEB] == {"web_search", "scholar_search"}
    assert SourcePolicy.ALLOWED_TOOLSETS[SourceMode.WEB] >= {"web_search", "tavily_search", "scholar_search"}
    assert SOURCE_TASK_TYPES["web"] == {"web_search", "scholar_search"}
    assert SourcePolicy.TOOL_SOURCE["scholar_search"] is SourceMode.WEB


@pytest.mark.asyncio
async def test_aggregate_routes_and_normalizes_source_mode():
    provider, tavily, scholar = _aggregate()
    common = dict(top_k=5, search_depth="basic",
                  call_context=ToolCallContext(run_id="run", source_mode=SourceMode.WEB))

    web_results = await provider.search(
        "graph rag", filters=SearchFilters(strategy="web_search"), **common,
    )
    scholar_results = await provider.search(
        "graph rag", filters=SearchFilters(strategy="scholar_search"), **common,
    )
    assert tavily.calls and scholar.calls
    assert tavily.calls[0]["filters"].strategy == "web_search"
    assert scholar.calls[0]["filters"].strategy == "scholar_search"
    assert [item.source_mode for item in web_results] == ["web"]
    assert [item.source_mode for item in scholar_results] == ["web"]
    assert scholar_results[0].metadata.extra["provider"] == "scholar"
    assert web_results[0].metadata.extra["provider"] == "tavily"


@pytest.mark.asyncio
async def test_aggregate_missing_tavily_is_explicit_not_silent_fallback():
    scholar = FakeProvider(SourceMode.SCHOLAR, "scholar")
    provider = AggregateWebProvider(tavily=None, scholar=scholar)
    with pytest.raises(AppError) as exc:
        await provider.search(
            "q", top_k=5, search_depth="basic",
            filters=SearchFilters(strategy="web_search"),
            call_context=ToolCallContext(run_id="r", source_mode=SourceMode.WEB),
        )
    assert exc.value.code.value == "SOURCE_UNAVAILABLE"

    with pytest.raises(AppError) as exc:
        await provider.search(
            "q", top_k=5, search_depth="basic",
            filters=SearchFilters(strategy="unknown_tool"),
            call_context=ToolCallContext(run_id="r", source_mode=SourceMode.WEB),
        )
    assert exc.value.code.value == "SOURCE_POLICY_VIOLATION"


def test_executor_routes_scholar_tool_through_web_aggregate():
    provider, _, _ = _aggregate()
    executor = RetrievalExecutor(provider=provider)
    task = TaskNode(
        task_id="task_scholar", task_type="scholar_search", source_mode="web",
        description="检索论文", parameters={"query": "agentic research"},
    )
    state = PlanExecuteState(
        session_id="s", run_id="r", source_mode="web",
        workflow_mode="plan_execute_report", input="agentic research",
    )
    signal = PlanExecutionSignal(
        plan_id="p", version=1, source_mode="web", execution_mode="sequential",
        tasks=[task.model_dump()], execution_sequence=[task.task_id],
        assumptions=[], acceptance_criteria={},
    )
    result = executor.execute_task(task, state, signal)
    assert result.success is True
    assert result.record.tool_calls[0].tool_name == "scholar_search"
    assert result.record.metadata.environment["provider"] == "scholar"
    assert result.record.evidence and result.record.evidence[0].source_mode == "web"


def test_router_aggregates_web_and_keeps_legacy_scholar_route():
    provider, _, _ = _aggregate()
    scholar = FakeProvider(SourceMode.SCHOLAR, "scholar")
    router = RetrievalRouter({
        SourceMode.WEB: provider,
        SourceMode.SCHOLAR: scholar,
    })
    assert router.for_mode("web") is provider
    assert router.for_mode("scholar") is scholar


def test_budget_counts_tavily_and_scholar_separately():
    manager = BudgetManager(BudgetLimits(max_tavily_calls=1, max_scholar_api_calls=1))
    manager.consume_tool(tavily=True)
    manager.consume_tool(scholar=True)
    assert manager.usage.tavily_calls == 1 and manager.usage.scholar_api_calls == 1
    with pytest.raises(BudgetExceeded):
        manager.consume_tool(tavily=True)


def test_same_query_different_tools_are_not_duplicates():
    actions = []
    for task_type in ("web_search", "scholar_search"):
        task = TaskNode(
            task_id=f"task_{task_type}", task_type=task_type, source_mode="web",
            description="q", parameters={"query": "same query"},
        )
        rubric = build_task_rubric(task.task_id, description="q", source_mode="web", min_evidence=1)
        actions.append(BatchAction(
            spec=action_spec_from_task_node(task, rubric_id=rubric.rubric_id),
            task=task, rubric=rubric,
        ))
    runner = ActionRunner(
        execute_action=lambda *_: None, max_parallel=1, max_concurrency=1,
        allowed_tools={"web_search", "scholar_search"},
    )
    report = runner.admit(actions, completed_task_ids=[], executed_queries=[])
    assert len(report.admitted) == 2

    duplicate = runner.admit(
        actions[:1], completed_task_ids=[],
        executed_queries=[query_dedupe_key("web_search", "same query")],
    )
    assert not duplicate.admitted
    assert duplicate.rejections[0].reason_code == "duplicate_query"


def test_api_boundary_normalizes_legacy_scholar_to_web():
    payload = MessageSend(
        client_message_id="c1", content="q", source_mode="scholar",
    )
    assert payload.source_mode is SourceMode.WEB


def test_brief_builder_uses_scholar_when_tavily_unavailable():
    state = PlanExecuteState(
        session_id="s", run_id="r", source_mode="web",
        workflow_mode="plan_execute_report", input="调研跨来源检索质量评估",
        context_snapshot={"web_apis": {"tavily": False, "scholar": True}},
    )
    result = BriefBuilder().generate_plan(state)
    assert {node.task_type for node in result.plan_spec.task_graph.nodes} == {"scholar_search"}


def test_skill_policy_allows_scholar_on_web_channel():
    policy = compile_runtime_policy(
        {"machine_policy": {"permissions": {
            "allowed_sources": ["web"], "allowed_tools": ["scholar_search"],
        }}},
        source_mode="web",
    )
    assert policy["permissions"]["allowed_tools"] == ["scholar_search"]

    with pytest.raises(Exception):
        compile_runtime_policy(
            {"machine_policy": {"permissions": {
                "allowed_sources": ["web"], "allowed_tools": ["local_search"],
            }}},
            source_mode="web",
        )


def test_lazy_provider_initializes_once_under_concurrency():
    created = []

    def factory():
        time.sleep(0.05)
        created.append(object())
        return FakeProvider(SourceMode.WEB, "tavily")

    lazy = _LazyProvider(mode=SourceMode.WEB, provider_name="web", factory=factory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        instances = list(pool.map(lambda _: lazy._get(), range(8)))
    assert len(created) == 1
    assert all(item is instances[0] for item in instances)


@pytest.mark.asyncio
async def test_aggregate_aclose_closes_both_apis():
    closed = []

    class Closable(FakeProvider):
        async def aclose(self):
            closed.append(self.provider_name)

    provider = AggregateWebProvider(
        tavily=Closable(SourceMode.WEB, "tavily"),
        scholar=Closable(SourceMode.SCHOLAR, "scholar"),
    )
    await provider.aclose()
    assert sorted(closed) == ["scholar", "tavily"]


def test_action_runner_accepts_legacy_raw_query_dedup():
    task = TaskNode(
        task_id="t", task_type="web_search", source_mode="web",
        description="q", parameters={"query": "legacy query"},
    )
    rubric = build_task_rubric(task.task_id, description="q", source_mode="web", min_evidence=1)
    action = BatchAction(
        spec=action_spec_from_task_node(task, rubric_id=rubric.rubric_id),
        task=task, rubric=rubric,
    )
    runner = ActionRunner(
        execute_action=lambda *_: None, max_parallel=1, max_concurrency=1,
        allowed_tools={"web_search"},
    )
    report = runner.admit(
        [action], completed_task_ids=[], executed_queries=["legacy query"],
    )
    assert not report.admitted and report.rejections[0].reason_code == "duplicate_query"


def test_legacy_raw_query_is_conservatively_cross_tool_duplicate():
    """旧表示无工具信息：raw query 对任意工具都按重复处理（保守，宁少查）。"""
    task = TaskNode(
        task_id="t", task_type="scholar_search", source_mode="web",
        description="q", parameters={"query": "legacy query"},
    )
    rubric = build_task_rubric(task.task_id, description="q", source_mode="web", min_evidence=1)
    action = BatchAction(
        spec=action_spec_from_task_node(task, rubric_id=rubric.rubric_id),
        task=task, rubric=rubric,
    )
    runner = ActionRunner(
        execute_action=lambda *_: None, max_parallel=1, max_concurrency=1,
        allowed_tools={"scholar_search"},
    )
    report = runner.admit(
        [action], completed_task_ids=[], executed_queries=["legacy query"],
    )
    assert not report.admitted and report.rejections[0].reason_code == "duplicate_query"


@pytest.mark.asyncio
async def test_scholar_provider_closes_per_call_client(monkeypatch):
    """未注入 client 时按调用创建并关闭，避免跨事件循环复用/泄漏。"""
    import deepresearch_agent.retrieval.scholar_provider as scholar_module

    created = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    class FakeClient:
        def __init__(self, **_kwargs):
            self.closed = False
            created.append(self)

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(scholar_module.httpx, "AsyncClient", FakeClient)
    provider = scholar_module.OpenAlexProvider(mailto="")
    await provider.search(
        "q", top_k=3, search_depth="basic", filters=SearchFilters(),
        call_context=ToolCallContext(run_id="r", source_mode=SourceMode.SCHOLAR),
    )
    assert len(created) == 1 and created[0].closed is True
    assert provider._client is None
