"""S3：多来源 Run 执行层——组合路由、跨通道派发、冻结集合准入。"""

import pytest

from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanExecutionSignal, TaskNode
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor
from deepresearch_agent.harness import SourceMode
from deepresearch_agent.harness.coreact_controller import CoreActController, DecisionContext
from deepresearch_agent.harness.errors import AppError
from deepresearch_agent.harness.policies import SourcePolicy
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext
from deepresearch_agent.retrieval.router import CompositeRetrievalProvider, RetrievalRouter
from deepresearch_agent.retrieval.web_aggregate import AggregateWebProvider


class FakeProvider:
    def __init__(self, mode: SourceMode, name: str):
        self.mode = mode
        self.provider_name = name
        self.calls = []

    async def search(self, query, *, top_k, search_depth, filters, call_context):
        self.calls.append({"query": query, "context": call_context})
        return [RetrievalResult(
            result_id=f"raw-{self.provider_name}-{query[:8]}",
            granularity="Chunk", evidence=f"{self.provider_name}:{query}",
            metadata=RetrievalMetadata(source_id=f"doc-{self.provider_name}", source_type="chunk", content_hash="a" * 64),
            source="tavily_search" if self.mode == SourceMode.WEB else "hybrid_search",
            source_mode=self.mode.value, score=0.9,
        )]


def _router():
    graphrag = FakeProvider(SourceMode.GRAPHRAG, "graphrag")
    web = AggregateWebProvider(
        tavily=FakeProvider(SourceMode.WEB, "tavily"),
        scholar=FakeProvider(SourceMode.SCHOLAR, "scholar"),
    )
    return RetrievalRouter({SourceMode.GRAPHRAG: graphrag, SourceMode.WEB: web}), graphrag, web


def _state(sources: list[str], primary: str = "web"):
    return PlanExecuteState(
        session_id="s", run_id="r", source_mode=primary,
        workflow_mode="plan_execute_report", input="q",
        context_snapshot={"sources": sources},
    )


def _task(task_id: str, task_type: str, channel: str, query="研究问题"):
    return TaskNode(
        task_id=task_id, task_type=task_type, source_mode=channel,
        description=query, parameters={"query": query},
    )


def _signal(task: TaskNode, primary: str = "web"):
    return PlanExecutionSignal(
        plan_id="p", version=1, source_mode=primary, execution_mode="sequential",
        tasks=[task.model_dump()], execution_sequence=[task.task_id],
        assumptions=[], acceptance_criteria={},
    )


def test_composite_provider_dispatches_by_tool_channel():
    router, graphrag, _ = _router()
    provider = router.for_modes([SourceMode.WEB, SourceMode.GRAPHRAG])
    assert isinstance(provider, CompositeRetrievalProvider)
    assert provider.mode is SourceMode.WEB and provider.sources[0] is SourceMode.WEB

    import asyncio

    for tool, expected in (
        ("hybrid_search", SourceMode.GRAPHRAG),
        ("web_search", SourceMode.WEB),
        ("scholar_search", SourceMode.WEB),
    ):
        results = asyncio.run(provider.search(
            "问题", top_k=3, search_depth="basic",
            filters=SearchFilters(strategy=tool),
            call_context=ToolCallContext(run_id="r", source_mode=SourceMode.WEB),
        ))
        assert results and results[0].source_mode == expected.value

    # graphrag 通道的 call_context 必须按通道改写（provider 断言可见）
    assert graphrag.calls[0]["context"].source_mode is SourceMode.GRAPHRAG

    with pytest.raises(AppError) as exc:
        asyncio.run(provider.search(
            "问题", top_k=1, search_depth="basic",
            filters=SearchFilters(strategy="unknown_tool"),
            call_context=ToolCallContext(run_id="r", source_mode=SourceMode.WEB),
        ))
    assert exc.value.code.value == "SOURCE_UNAVAILABLE"


def test_for_modes_fails_fast_on_unconfigured_channel():
    router, _, _ = _router()
    with pytest.raises(AppError) as exc:
        router.for_modes([SourceMode.WEB, SourceMode.SCHOLAR])
    assert exc.value.code.value == "SOURCE_UNAVAILABLE"
    # 单来源语义保持原样
    provider = router.for_modes([SourceMode.GRAPHRAG])
    assert provider.mode is SourceMode.GRAPHRAG


def _run_executor(task: TaskNode, state: PlanExecuteState):
    router, _, _ = _router()
    executor = RetrievalExecutor(router=router)
    return executor.execute_task(task, state, _signal(task, state.source_mode))


def test_executor_runs_multi_source_tasks_and_labels_channels():
    state = _state(["web", "graphrag"])
    cases = [
        (_task("t_web", "web_search", "web"), "tavily", SourceMode.WEB),
        (_task("t_scholar", "scholar_search", "web"), "scholar", SourceMode.WEB),
        (_task("t_graph", "hybrid_search", "graphrag"), "graphrag", SourceMode.GRAPHRAG),
    ]
    for task, label, channel in cases:
        result = _run_executor(task, state)
        assert result.success is True, result.error
        call = result.record.tool_calls[0]
        assert call.source_mode == channel.value
        assert result.record.metadata.environment["provider"] == label
        assert result.record.evidence and result.record.evidence[0].source_mode == channel.value


def test_executor_rejects_task_outside_frozen_sources():
    state = _state(["web"])
    with pytest.raises(ValueError, match="冻结来源集合"):
        _run_executor(_task("t_g", "hybrid_search", "graphrag"), state)


def test_controller_accepts_tasks_within_frozen_sources():
    controller = CoreActController()
    context = DecisionContext(
        goal="g", source_mode="web", sources=["web", "graphrag"],
        eligible_tasks=[
            _task("t_g", "hybrid_search", "graphrag").model_dump(mode="json"),
        ],
    )
    decision = controller._decide_deterministic(context, reason="候选")
    assert decision.action is not None and decision.action.source_mode == "graphrag"

    blocked = DecisionContext(
        goal="g", source_mode="web", sources=["web"],
        eligible_tasks=[_task("t_g", "hybrid_search", "graphrag").model_dump(mode="json")],
    )
    assert controller._decide_deterministic(blocked, reason="候选").action is None


def test_policy_tool_source_maps_scholar_to_web_channel():
    assert SourcePolicy.TOOL_SOURCE["scholar_search"] is SourceMode.WEB
    assert SourcePolicy.TOOL_SOURCE["hybrid_search"] is SourceMode.GRAPHRAG


def test_executor_via_production_timeout_wrapper():
    """回归 B1：run_service 注入的是 TimeoutBoundProvider(组合 provider)；
    通道解析/证据归属必须对包装层透明。"""
    from deepresearch_agent.retrieval.base import TimeoutBoundProvider

    router, _, _ = _router()
    provider = TimeoutBoundProvider(
        router.for_modes([SourceMode.WEB, SourceMode.GRAPHRAG]), timeout_seconds=60,
    )
    executor = RetrievalExecutor(provider=provider)
    state = _state(["web", "graphrag"])
    cases = [
        (_task("t_web", "web_search", "web"), "tavily", SourceMode.WEB),
        (_task("t_scholar", "scholar_search", "web"), "scholar", SourceMode.WEB),
        (_task("t_graph", "hybrid_search", "graphrag"), "graphrag", SourceMode.GRAPHRAG),
    ]
    for task, label, channel in cases:
        result = executor.execute_task(task, state, _signal(task, state.source_mode))
        assert result.success is True, result.error
        assert result.record.tool_calls[0].source_mode == channel.value
        assert result.record.metadata.environment["provider"] == label


def test_executor_rejects_channel_tool_mismatch():
    router, _, _ = _router()
    executor = RetrievalExecutor(router=router)
    state = _state(["web", "graphrag"])
    # graphrag 通道 + web 工具：任务通道与工具所属通道不一致（执行器按动作失败记账）
    result = executor.execute_task(
        _task("t_bad", "web_search", "graphrag"), state,
        _signal(_task("t_bad", "web_search", "graphrag"), state.source_mode),
    )
    assert result.success is False and "所属通道" in (result.error or "")


@pytest.mark.asyncio
async def test_plan_task_rows_record_task_channel(tmp_path):
    from deepresearch_agent.persistence import Database
    from deepresearch_agent.persistence.repositories import PlanTaskToolRepository

    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 's3tasks.db').as_posix()}")
    await db.create_schema()
    try:
        from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
        from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository

        session = await SessionRepository(db).create(SessionCreate(title="s3"))
        _, run, _ = await RunRepository(db).create_for_user_message(
            MessageCreate(session_id=session.session_id, role="user", content="q", client_message_id="s3-1"),
            RunCreate(
                session_id=session.session_id, trigger_message_id="atomic",
                source_mode=SourceMode.WEB, workflow_mode="plan_execute_report",
                config_snapshot={"sources": ["web", "graphrag"]}, budget={},
            ),
        )
        repo = PlanTaskToolRepository(db)
        tasks = [
            {"task_id": "t_web", "task_type": "web_search", "source_mode": "web", "status": "running"},
            {"task_id": "t_graph", "task_type": "hybrid_search", "source_mode": "graphrag", "status": "running"},
        ]
        await repo.save_plan(
            run_id=run.run_id, plan_id="p", version=1, status="executing",
            plan={"plan_id": "p", "status": "executing", "tasks": tasks},
            tasks=tasks, source_mode="web",
        )
        from deepresearch_agent.persistence.models import TaskModel

        async with db.sessions() as db_session:
            web_row = await db_session.get(TaskModel, "t_web")
            graph_row = await db_session.get(TaskModel, "t_graph")
        assert web_row.source_mode == "web"
        assert graph_row.source_mode == "graphrag"
    finally:
        await db.close()
