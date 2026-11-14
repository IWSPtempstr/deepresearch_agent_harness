"""S2：Run 级多来源冻结契约（v2）与 v1 只读兼容。"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError

from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.planner.brief_builder import BriefBuilder
from deepresearch_agent.harness import SourceMode
from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver
from deepresearch_agent.harness.research_brief import (
    RESEARCH_BRIEF_SCHEMA_VERSION,
    ResearchBrief,
    UnknownSchemaVersion,
)
from deepresearch_agent.harness.run_context import RunContext, sources_from_config


def _brief(**overrides):
    payload = {
        "brief_id": "b1", "goal": "g",
        "questions": [{"question_id": "q1", "text": "问题"}],
        "allowed_sources": [SourceMode.WEB, SourceMode.GRAPHRAG],
    }
    payload.update(overrides)
    return ResearchBrief(**payload)


def test_v2_multi_source_brief_and_union_tools():
    brief = _brief()
    assert brief.schema_version == RESEARCH_BRIEF_SCHEMA_VERSION == 2
    assert brief.source_mode is SourceMode.WEB
    assert brief.effective_sources == (SourceMode.WEB, SourceMode.GRAPHRAG)
    assert brief.allowed_tools == frozenset({
        "web_search", "scholar_search",
        "local_search", "global_search", "hybrid_search", "naive_search", "chain_exploration",
    })
    roundtrip = ResearchBrief.model_validate(brief.model_dump(mode="json"))
    assert roundtrip.effective_sources == brief.effective_sources
    assert roundtrip.content_hash() == brief.content_hash()


def test_v1_is_read_only_single_source_and_v2_rejects_duplicates():
    legacy = {
        "schema_version": 1, "brief_id": "b", "goal": "g",
        "questions": [{"question_id": "q", "text": "t"}],
        "allowed_sources": ["web"],
    }
    assert ResearchBrief.from_payload(legacy).source_mode is SourceMode.WEB
    with pytest.raises(ValidationError):
        ResearchBrief.from_payload({**legacy, "allowed_sources": ["web", "graphrag"]})
    with pytest.raises(ValidationError):
        _brief(allowed_sources=[SourceMode.WEB, SourceMode.WEB])
    with pytest.raises(UnknownSchemaVersion):
        ResearchBrief.from_payload({**legacy, "schema_version": 3})


def test_revision_cannot_change_frozen_sources():
    brief = _brief()
    revised = brief.with_revision(goal="g2")
    assert revised.version == 2
    assert revised.effective_sources == brief.effective_sources
    import inspect

    assert "allowed_sources" not in inspect.signature(brief.with_revision).parameters


def test_run_context_sources_validation_and_fallback():
    common = dict(
        run_id="r", session_id="s", trigger_message_id="m",
        source_mode=SourceMode.WEB, workflow_mode="plan_execute_report",
        status="queued", original_query="q",
    )
    ctx = RunContext(**common, sources=(SourceMode.WEB, SourceMode.GRAPHRAG))
    assert ctx.effective_sources == (SourceMode.WEB, SourceMode.GRAPHRAG)
    assert RunContext(**common).effective_sources == (SourceMode.WEB,)
    with pytest.raises(ValidationError):
        RunContext(**common, sources=(SourceMode.GRAPHRAG, SourceMode.WEB))
    with pytest.raises(ValidationError):
        RunContext(**common, sources=(SourceMode.WEB, SourceMode.WEB))


def test_sources_from_config_is_tolerant_and_primary_first():
    assert sources_from_config({}, SourceMode.WEB) == (SourceMode.WEB,)
    assert sources_from_config({"sources": ["graphrag", "web"]}, SourceMode.WEB) == (
        SourceMode.WEB, SourceMode.GRAPHRAG,
    )
    assert sources_from_config({"sources": ["web", "bogus", 3]}, SourceMode.WEB) == (
        SourceMode.WEB,
    )
    assert sources_from_config({"sources": []}, SourceMode.WEB) == (SourceMode.WEB,)
    # 冻结列表缺主通道 = 损坏配置：fail closed 回退单通道，不制造并集
    assert sources_from_config({"sources": ["graphrag"]}, SourceMode.WEB) == (SourceMode.WEB,)


def test_driver_context_snapshot_cannot_expand_frozen_sources():
    context = RunContext(
        run_id="r", session_id="s", trigger_message_id="m",
        source_mode=SourceMode.WEB, sources=(SourceMode.WEB,),
        workflow_mode="plan_execute_report", status="queued", original_query="q",
        config_snapshot={"sources": ["web"]},
        context_snapshot={"sources": ["web", "graphrag"], "stable": "x"},
    )
    driver = PlanExecuteReportDriver(context, SimpleNamespace(controller_model=None))
    assert driver.state.context_snapshot["sources"] == ["web"]
    assert driver.state.context_snapshot["stable"] == "x"

    # config 缺失 sources（旧 Run）：checkpoint/context 里的来源一律移除
    legacy = RunContext(
        run_id="r2", session_id="s", trigger_message_id="m",
        source_mode=SourceMode.WEB, workflow_mode="plan_execute_report",
        status="queued", original_query="q",
        context_snapshot={"sources": ["web", "graphrag"]},
    )
    legacy_driver = PlanExecuteReportDriver(legacy, SimpleNamespace(controller_model=None))
    assert "sources" not in legacy_driver.state.context_snapshot


def test_brief_builder_rejects_future_brief_version():
    from deepresearch_agent.harness.research_brief import UnknownSchemaVersion as Unknown

    state = PlanExecuteState(
        session_id="s", run_id="r-future", source_mode="web",
        workflow_mode="plan_execute_report", input="调研检索增强技术",
        context_snapshot={
            "sources": ["web"],
            "research_brief": {
                "schema_version": 99, "brief_id": "b", "goal": "g",
                "questions": [{"question_id": "q", "text": "t"}],
                "allowed_sources": ["web"],
            },
        },
    )
    with pytest.raises(Unknown):
        BriefBuilder().generate_plan(state)


def test_brief_builder_replan_keeps_frozen_sources():
    state = PlanExecuteState(
        session_id="s", run_id="r-replan", source_mode="web",
        workflow_mode="plan_execute_report", input="调研检索增强技术",
        context_snapshot={"sources": ["web", "graphrag"]},
    )
    BriefBuilder().generate_plan(state)
    BriefBuilder().generate_plan(state, assumptions=["min_evidence"])
    assert state.context_snapshot["research_brief"]["allowed_sources"] == ["web", "graphrag"]
    # goal/预算未变：replan 保留原 brief（版本不推进，来源集合不变）
    assert state.context_snapshot["research_brief"]["version"] == 1


def test_driver_planning_snapshot_keeps_frozen_sources():
    context = RunContext(
        run_id="r", session_id="s", trigger_message_id="m",
        source_mode=SourceMode.WEB, sources=(SourceMode.WEB, SourceMode.GRAPHRAG),
        workflow_mode="plan_execute_report", status="queued", original_query="q",
        config_snapshot={"sources": ["web", "graphrag"], "min_evidence": 1},
        context_snapshot={"stable_snapshot_id": "x"},
    )
    driver = PlanExecuteReportDriver(context, SimpleNamespace(controller_model=None))
    assert driver.state.context_snapshot["sources"] == ["web", "graphrag"]
    assert driver.state.context_snapshot["stable_snapshot_id"] == "x"


def test_brief_builder_freezes_multi_source_brief():
    state = PlanExecuteState(
        session_id="s", run_id="r", source_mode="web",
        workflow_mode="plan_execute_report", input="调研检索增强技术",
        context_snapshot={"sources": ["web", "graphrag"]},
    )
    result = BriefBuilder().generate_plan(state)
    payload = state.context_snapshot["research_brief"]
    assert payload["allowed_sources"] == ["web", "graphrag"]
    assert result.plan_spec is not None
    brief = ResearchBrief.model_validate(payload)
    assert brief.effective_sources == (SourceMode.WEB, SourceMode.GRAPHRAG)


@pytest_asyncio.fixture
async def database(tmp_path):
    from deepresearch_agent.persistence import Database

    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 's2.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_runtime_rejects_checkpoint_primary_mismatch(database, tmp_path):
    """checkpoint 主通道 ≠ Run 行主通道：大声失败，不静默改写冻结来源。"""
    from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
    from deepresearch_agent.harness.runtime import HarnessRuntime
    from deepresearch_agent.persistence import ArtifactStore
    from deepresearch_agent.persistence.repositories import (
        ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
        EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository, SessionRepository,
    )

    session = await SessionRepository(database).create(SessionCreate(title="s2"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="q", client_message_id="s2-1"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.WEB, workflow_mode="plan_execute_report",
            config_snapshot={"sources": ["web"]}, budget={},
        ),
    )
    # 伪造 checkpoint：主通道 graphrag（与 Run 行 web 冲突）
    stale = RunContext(
        run_id=run.run_id, session_id=session.session_id,
        trigger_message_id=run.trigger_message_id,
        source_mode=SourceMode.GRAPHRAG, workflow_mode="plan_execute_report",
        status="executing", original_query="q",
    )
    runtime = HarnessRuntime(
        run_repository=RunRepository(database), message_repository=MessageRepository(database),
        event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
        evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
        trajectory_repository=PlanTaskToolRepository(database),
        workflow_factory=lambda context, events=None: None,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        artifact_repository=ArtifactRepository(database), lease_seconds=5,
    )
    await runtime.checkpoints.save(stale, "executing")
    with pytest.raises(ValueError, match="source_mode"):
        await runtime._load_context(run.run_id)
