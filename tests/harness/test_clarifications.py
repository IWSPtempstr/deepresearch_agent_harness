"""BC-01：澄清请求/回答协议与消费端。

覆盖：请求/回答形状与边界、幂等/冲突、Run 行原子记录、ContextBuilder 消费
（不重复澄清、回答进入 resolved query/上下文）、BriefBuilder 不再被同一澄清
阻塞。
"""

import json

import pytest
import pytest_asyncio

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.planner.brief_builder import BriefBuilder, BriefBuilderConfig
from deepresearch_agent.context import ContextBuilder, ContextCompactor
from deepresearch_agent.harness import SourceMode, WorkflowMode
from deepresearch_agent.harness.clarifications import (
    MAX_ANSWER_HISTORY,
    apply_answer,
    answers_block,
    build_request,
    clarification_key,
    parse_answers,
    pending_request,
    public_view,
    request_from_config,
    store_request,
)
from deepresearch_agent.harness.errors import AppError, NeedsUserInput
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.memory import MemoryService
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import (
    AuditRepository, MemoryRepository, MessageRepository, RunRepository, SessionRepository,
)
from deepresearch_agent.sessions import SessionSearchService


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'bc01.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


def _memory_service(database):
    return MemoryService(MemoryRepository(database), AuditRepository(database))


def _builder(database):
    sessions, messages = SessionRepository(database), MessageRepository(database)
    return ContextBuilder(
        messages=messages, sessions=sessions, memory_service=_memory_service(database),
        session_search=SessionSearchService(messages, sessions),
        compactor=ContextCompactor(sessions, messages, threshold_tokens=1000, protect_recent_messages=4),
        max_tokens=4000, max_chars=16000, recent_turns=4,
    )


async def _create_run(database, *, content="它呢？", client_id="bc01-original"):
    session = await SessionRepository(database).create(SessionCreate(title="澄清"))
    message, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content=content, client_message_id=client_id),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        ),
    )
    return session, message, run


def _context(run, message):
    return RunContext(
        run_id=run.run_id, session_id=run.session_id, trigger_message_id=message.message_id,
        source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        status="context_building", original_query=message.content,
    )


# ---------- 协议 ----------

def test_request_answer_roundtrip_and_actual_questions():
    request = build_request({}, questions=["请明确主体？", "  ", "请明确时间范围？"], source="brief", now="t0")
    assert request.question_id.startswith("clar_1_")
    assert request.questions == ["请明确主体？", "请明确时间范围？"]
    snapshot = {}
    store_request(snapshot, request)
    assert pending_request(snapshot).question_id == request.question_id
    assert public_view(request_from_config(snapshot))["questions"] == request.questions

    outcome, snapshot = apply_answer(
        snapshot, clarification_id="c1", content="主体是阶段4 API", created_at="t1",
    )
    assert outcome == "recorded"
    assert parse_answers(snapshot)[0].content == "主体是阶段4 API"
    assert request_from_config(snapshot).status == "answered"
    assert pending_request(snapshot) is None
    assert "主体是阶段4 API" in answers_block(snapshot)


def test_answer_idempotency_conflict_and_revision():
    request = build_request({}, questions=["Q?"], source="brief", now="t0")
    snapshot = {}
    store_request(snapshot, request)
    _, snapshot = apply_answer(snapshot, clarification_id="c1", content="A", created_at="t1")
    outcome, same = apply_answer(snapshot, clarification_id="c1", content="A", created_at="t2")
    assert outcome == "duplicate" and same["clarifications"] == snapshot["clarifications"]
    outcome, _ = apply_answer(snapshot, clarification_id="c1", content="B", created_at="t2")
    assert outcome == "conflict"
    outcome, _ = apply_answer(snapshot, clarification_id="c2", content="B", question_id="other", created_at="t2")
    assert outcome == "question_mismatch"

    # 已回答的请求再次触发 → revision + 1，不覆盖历史回答
    again = build_request(snapshot, questions=["Q?"], source="brief", now="t3")
    assert again.revision == 2 and again.status == "pending"
    assert len(parse_answers(snapshot)) == 1


def test_answer_history_is_bounded_and_derived_key_is_stable():
    snapshot = {}
    for index in range(MAX_ANSWER_HISTORY + 5):
        _, snapshot = apply_answer(
            snapshot, clarification_id=f"c{index}", content=f"A{index}", created_at="t",
        )
    assert len(parse_answers(snapshot)) == MAX_ANSWER_HISTORY
    assert clarification_key("run") == clarification_key("run")
    assert clarification_key("run", client_id="x") == clarification_key("run", client_id="x")
    assert clarification_key("run", client_id="x") != clarification_key("run", client_id="y")
    # 无客户端键时按内容派生：不同回答不得被误判为同一幂等提交
    assert clarification_key("run", content="A") != clarification_key("run", content="B")


def test_driver_merges_run_answers_into_state_snapshot():
    """恢复的 checkpoint 可能早于本次回答；driver 必须从 Run 行补合并。"""
    from types import SimpleNamespace

    from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver

    request = build_request({}, questions=["Q?"], source="brief", now="t0")
    snapshot = {}
    store_request(snapshot, request)
    _, snapshot = apply_answer(snapshot, clarification_id="c1", content="A", created_at="t1")
    context = RunContext(
        run_id="run-driver-merge", session_id="ses", trigger_message_id="msg",
        source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        status="queued", original_query="它呢？", config_snapshot=snapshot,
    )
    driver = PlanExecuteReportDriver(context, SimpleNamespace(controller_model=None))
    assert driver.state.context_snapshot["clarifications"][0]["content"] == "A"


def test_legacy_question_falls_back_to_readonly_request():
    snapshot = {"clarification_question": "旧 Run 的通用提示"}
    request = request_from_config(snapshot)
    assert request.question_id == "clar_legacy" and request.questions == ["旧 Run 的通用提示"]


# ---------- Run 行：原子记录与幂等 ----------

@pytest.mark.asyncio
async def test_repository_records_answer_message_atomically(database):
    _, message, run = await _create_run(database)
    runs, messages = RunRepository(database), MessageRepository(database)
    request = build_request({}, questions=["请明确主体？"], source="context_builder", now="t0")
    await runs.set_clarification_request(run.run_id, request.model_dump(mode="json"))
    await runs.update_status(run.run_id, status="needs_user_input", current_stage="needs_user_input")

    outcome, message_id = await runs.record_clarification_answer(
        run.run_id, message_id="msg_clar_1", client_message_id="clarification-1",
        content="主体是阶段4 API", clarification_id="c1", question_id=request.question_id,
    )
    assert outcome == "recorded"
    stored = await runs.get(run.run_id)
    assert stored.status == "queued" and stored.current_stage == "queued"
    snapshot = json.loads(stored.config_snapshot_json)
    assert request_from_config(snapshot).status == "answered"
    assert parse_answers(snapshot)[0].message_id == "msg_clar_1"
    rows = await messages.list_for_session(run.session_id)
    assert [row.message_id for row in rows if row.message_id == "msg_clar_1"] == ["msg_clar_1"]

    duplicate, same_id = await runs.record_clarification_answer(
        run.run_id, message_id="msg_clar_2", client_message_id="clarification-1",
        content="主体是阶段4 API", clarification_id="c1", question_id=request.question_id,
    )
    assert duplicate == "duplicate" and same_id == "msg_clar_1"
    assert len([row for row in await messages.list_for_session(run.session_id) if row.run_id == run.run_id]) == 1

    with pytest.raises(AppError) as conflict:
        await runs.record_clarification_answer(
            run.run_id, message_id="msg_clar_3", client_message_id="clarification-1",
            content="另一个回答", clarification_id="c1", question_id=request.question_id,
        )
    assert conflict.value.code.value == "IDEMPOTENCY_CONFLICT"


# ---------- 消费端 ----------

@pytest.mark.asyncio
async def test_context_builder_consumes_answer_and_stops_reasking(database):
    _, message, run = await _create_run(database)
    context = _context(run, message)
    with pytest.raises(NeedsUserInput):
        await _builder(database).build(context)
    request = request_from_config(context.config_snapshot)
    assert request is not None and request.questions
    assert "原问题" in request.questions[0]

    snapshot = context.config_snapshot
    _, snapshot = apply_answer(
        snapshot, clarification_id="c1", content="要继续讨论第 4 阶段的 API 权限", created_at="t1",
    )
    resumed = _context(run, message)
    resumed.config_snapshot.update(snapshot)
    built = await _builder(database).build(resumed)  # 不再抛 NeedsUserInput
    assert "要继续讨论第 4 阶段的 API 权限" in built.resolved_query
    assert "要继续讨论第 4 阶段的 API 权限" in built.model_input
    assert built.context_snapshot["clarifications"][0]["content"] == "要继续讨论第 4 阶段的 API 权限"


def test_brief_builder_does_not_reblock_after_answer():
    class StubClarifier:
        def analyze(self, context):
            from deepresearch_agent.agents.multi_agent.planner.clarifier import ClarificationResult

            return ClarificationResult(
                needs_clarification=True, questions=["请明确目标读者与时间范围"],
            )

    class StubLLM:
        pass

    builder = BriefBuilder(
        llm=StubLLM(), clarifier=StubClarifier(),
        config=BriefBuilderConfig(allow_unclarified_plan=False),
    )
    blocked = PlanExecuteState(input="调研一个宽泛的话题", source_mode="web", run_id="run-bc01")
    assert builder.generate_plan(blocked).plan_spec is None

    answered = PlanExecuteState(input="调研一个宽泛的话题", source_mode="web", run_id="run-bc01")
    request = build_request({}, questions=["请明确目标读者与时间范围"], source="brief", now="t0")
    snapshot = {}
    store_request(snapshot, request)
    _, snapshot = apply_answer(snapshot, clarification_id="c1", content="面向工程团队，近两年", created_at="t1")
    answered.context_snapshot = {"clarifications": snapshot["clarifications"]}
    result = builder.generate_plan(answered)
    assert result.plan_spec is not None
    assert answered.plan_context.clarification_history[0]["answer"] == "面向工程团队，近两年"
