"""T-208: coreact loop end-to-end through the real HarnessRuntime (scripted LLM, no real model).

两个场景：
1. happy path：QUEUED → 步级循环（2 动作，每个动作步级提交）→ 报告 → Contract（含
   rubric_coverage）→ COMPLETED；
2. 步间暂停 → 恢复：动作 1 提交后 CoreActPaused 落 PAUSED；二次 execute_run 从
   恢复游标续跑动作 2（不重做动作 1）→ COMPLETED，且 tool_call 行不重复。
"""
import json

import pytest
from sqlalchemy import select

from deepresearch_agent.agents.multi_agent.core.plan_spec import (
    AcceptanceCriteria,
    PlanSpec,
    ProblemStatement,
    TaskGraph,
    TaskNode,
)
from deepresearch_agent.agents.multi_agent.core.retrieval_result import (
    RetrievalMetadata,
    RetrievalResult,
)
from deepresearch_agent.agents.multi_agent.planner.base_planner import PlannerResult
from deepresearch_agent.agents.multi_agent.planner.clarifier import ClarificationResult
from deepresearch_agent.agents.multi_agent.reporter.consistency_checker import ConsistencyCheckResult
from deepresearch_agent.agents.multi_agent.reporter.report_schemas import (
    ReportOutline,
    ReportResult,
)
from deepresearch_agent.harness.budgets import BudgetLimits
from deepresearch_agent.harness.contracts import SourceMode, WorkflowMode
from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.artifact_store import ArtifactStore
from deepresearch_agent.persistence.models import PlanModel, TaskModel, ToolCallModel
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository,
    CheckpointRepository,
    ContractRepository,
    EventRepository,
    EvidenceRepository,
    MessageRepository,
    RunRepository,
    SessionRepository,
)
from deepresearch_agent.persistence.repositories.trajectory import PlanTaskToolRepository

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from backend.app.services.run_service import RunService  # noqa: F401  确保 backend 可导入


# ---------------------------------------------------------------- fixtures


class Content:
    def __init__(self, content):
        self.content = content


class ScriptedLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages[-1]["content"])
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Content(json.dumps(answer, ensure_ascii=False))


def action_by_id(task_id, query, description="按候选执行"):
    return {
        "ready_to_report": False,
        "task": {"task_id": task_id, "task_type": "web_search", "description": description, "query": query},
        "reason": "候选任务",
    }


def ready(reason="证据覆盖目标"):
    return {"ready_to_report": True, "task": None, "reason": reason}


class FakeOrchestrator:
    """最小编排器：真实 RetrievalResult 证据 + 确定性引用报告。"""

    def __init__(self, llm, *, provider_name="tavily"):
        self.controller_model = llm
        self.executed = []
        self.provider_name = provider_name
        self.extra_ev_ids = []  # 恢复场景：前一段已提交、但不在本段记录中的证据 id

    def plan(self, state, *, assumptions=None):
        plan = PlanSpec(
            source_mode="web",
            problem_statement=ProblemStatement(original_query=state.input or "调研检索增强技术"),
            task_graph=TaskGraph(nodes=[
                TaskNode(
                    task_id="plan_1", task_type="web_search", source_mode="web",
                    description="检索增强 基础概念", parameters={"query": "检索增强 基础概念"},
                ),
                TaskNode(
                    task_id="plan_2", task_type="web_search", source_mode="web",
                    description="检索增强 局限与评测", parameters={"query": "检索增强 局限与评测"},
                ),
            ], execution_mode="sequential"),
            acceptance_criteria=AcceptanceCriteria(
                min_evidence_count=1, completion_conditions=["检索增强"],
            ),
        )
        state.plan = plan
        return PlannerResult(
            plan_spec=plan,
            clarification=ClarificationResult(needs_clarification=False),
            executor_signal=plan.to_execution_signal(),
        )

    def execute_action(self, state, task, *, progress_callback=None):
        self.executed.append(task.model_copy(deep=True))
        result = RetrievalResult(
            granularity="Chunk",
            evidence=f"检索增强生成（{task.parameters.get('query', '')}）是一种结合检索与生成的技术路线，相关研究仍在推进。",
            metadata=RetrievalMetadata(
                source_id=f"https://source-{len(self.executed)}.example/page",
                source_type="webpage",
                title=f"来源 {len(self.executed)}：检索增强研究",
            ),
            source="tavily_search",
            source_mode="web",
        )
        from deepresearch_agent.agents.multi_agent.core.execution_record import (
            ExecutionMetadata,
            ExecutionRecord,
            ToolCall,
        )

        record = ExecutionRecord(
            task_id=task.task_id,
            session_id=state.session_id,
            worker_type="retrieval_executor",
            tool_calls=[ToolCall(
                tool_name="web_search",
                tool_call_id=f"call-{state.run_id}-{task.task_id}-a{getattr(task, 'attempt', 1)}",
                source_mode="web",
                args=dict(task.parameters),
                result={"answer": str(result.evidence)},
                status="success",
            )],
            evidence=[result],
            metadata=ExecutionMetadata(worker_type="retrieval_executor", tool_calls_count=1),
        )
        record.metadata.environment["coreact"] = {}
        state.execution_records.append(record)
        if state.execution_context is not None:
            state.execution_context.tool_call_history.append({"args": dict(task.parameters), "task_id": task.task_id})
            if task.task_id not in state.execution_context.completed_task_ids:
                state.execution_context.completed_task_ids.append(task.task_id)
        if state.plan:
            state.plan.update_task_status(task.task_id, "completed")
        return True, None, record

    def report(self, state, *, report_type=None):
        ev_ids = [item.result_id for record in state.execution_records for item in record.evidence]
        all_ids = self.extra_ev_ids + [eid for eid in ev_ids if eid not in self.extra_ev_ids]
        claims = [
            f"检索增强生成的基础概念已有明确界定。[^{all_ids[0]}]",
            f"其局限与评测结论也已收集。[^{all_ids[-1]}]",
        ]
        final_report = "# 研究报告\n\n" + "\n\n".join(claims) + "\n\n## 局限\n\n当前证据来源有限。"
        return ReportResult(
            outline=ReportOutline(report_type="long_document", title="研究报告", abstract="摘要", sections=[]),
            sections=[],
            final_report=final_report,
            consistency_check=ConsistencyCheckResult(is_consistent=True, issues=[], raw_response=None),
        )

    def recheck_report_consistency(self, report, evidence):
        return ConsistencyCheckResult(is_consistent=True, issues=[], raw_response=None)


def make_runtime(database, tmp_path, factory):
    from deepresearch_agent.harness.runtime import HarnessRuntime

    return HarnessRuntime(
        run_repository=RunRepository(database), message_repository=MessageRepository(database),
        event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
        evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
        trajectory_repository=PlanTaskToolRepository(database), workflow_factory=factory,
        artifact_store=ArtifactStore(tmp_path / "artifacts"), artifact_repository=ArtifactRepository(database),
    )


def make_driver(context, orchestrator):
    return PlanExecuteReportDriver(context, orchestrator, events=None)


async def create_coreact_run(database) -> str:
    session = await SessionRepository(database).create(SessionCreate(title="coreact-e2e"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(
            session_id=session.session_id, role="user", content="调研检索增强技术",
            client_message_id=f"client-{session.session_id}",
        ),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
            config_snapshot={"orchestration": "coreact_v1", "min_evidence": 1},
            budget=BudgetLimits(max_llm_tokens=200000).model_dump(),
        ),
    )
    return run.run_id


@pytest.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'coreact-e2e.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


# ---------------------------------------------------------------- tests


class TestCoreActE2E:
    async def test_happy_path_reaches_completed_with_step_commits(self, database, tmp_path):
        run_id = await create_coreact_run(database)
        llm = ScriptedLLM([
            action_by_id("plan_1", "检索增强 基础概念"),
            action_by_id("plan_2", "检索增强 局限与评测"),
            ready(),
        ])
        orchestrator = FakeOrchestrator(llm)
        runtime = make_runtime(database, tmp_path, lambda context, events: make_driver(context, orchestrator))

        final_context = await runtime.execute_run(run_id)

        run_row_dbg = await RunRepository(database).get(run_id)
        if final_context.status.value != "completed":
            print("DBG_FAIL:", run_row_dbg.error_code, "|", run_row_dbg.error_message)
        assert final_context.status.value == "completed", (final_context.workflow_state, run_row_dbg.error_message)
        assert [task.parameters["query"] for task in orchestrator.executed] == [
            "检索增强 基础概念", "检索增强 局限与评测",
        ]
        # 步级提交产物：计划/任务/工具调用/证据/step checkpoint 都持久化
        tool_repo = PlanTaskToolRepository(database)
        stored_evidence = await EvidenceRepository(database).list_for_run(run_id)
        assert len(stored_evidence) == 2
        async with database.sessions() as session:
            plans = list((await session.execute(select(PlanModel).where(PlanModel.run_id == run_id))).scalars())
            tasks = list((await session.execute(select(TaskModel).where(TaskModel.run_id == run_id))).scalars())
            tool_calls = list((await session.execute(select(ToolCallModel).where(ToolCallModel.run_id == run_id))).scalars())
        assert len(plans) == 1
        assert len(tasks) == 2 and all(task.status == "completed" for task in tasks)
        assert len(tool_calls) == 2 and all(call.status == "completed" for call in tool_calls)
        events = await EventRepository(database).list_after(run_id)
        step_events = [e for e in events if e.event_type == "coreact.step_committed"]
        assert len(step_events) == 2
        # Contract 检查落库且 rubric_coverage/min_evidence 通过
        checks = await ContractRepository(database).list_for_run(run_id)
        kinds = {check.kind: check.passed for check in checks}
        assert bool(kinds.get("min_evidence")) is True
        assert bool(kinds.get("rubric_coverage")) is True

    async def test_pause_mid_loop_resumes_from_next_action(self, database, tmp_path):
        run_id = await create_coreact_run(database)

        llm_a = ScriptedLLM([
            action_by_id("plan_1", "检索增强 基础概念"),
            action_by_id("plan_2", "检索增强 局限与评测"),
        ])
        orchestrator_a = FakeOrchestrator(llm_a)
        runtime_box = {}

        class PauseAfterFirstCommitDriver(PlanExecuteReportDriver):
            @property
            def step_commit(self):
                return getattr(self, "_wrapped_step_commit", None)

            @step_commit.setter
            def step_commit(self, callback):
                if callback is None:
                    self._wrapped_step_commit = None
                    return

                async def _wrapped(driver):
                    await callback(driver)
                    if driver.coreact_loop.step_index == 1:
                        await runtime_box["runtime"].runs.request_pause(driver.context.run_id)

                self._wrapped_step_commit = _wrapped

        runtime_a = make_runtime(
            database,
            tmp_path,
            lambda context, events: PauseAfterFirstCommitDriver(context, orchestrator_a, events=events),
        )
        runtime_box["runtime"] = runtime_a
        paused_context = await runtime_a.execute_run(run_id)
        assert paused_context.status.value == "paused"
        assert [task.task_id for task in orchestrator_a.executed] == ["plan_1"]
        stored_evidence_before_resume = await EvidenceRepository(database).list_for_run(run_id)
        assert len(stored_evidence_before_resume) == 1
        ev1_id = stored_evidence_before_resume[0].evidence_id

        # 恢复段：scripted LLM 只决定 plan_2 + ready，绝不重选 plan_1
        llm_b = ScriptedLLM([action_by_id("plan_2", "检索增强 局限与评测"), ready()])
        orchestrator_b = FakeOrchestrator(llm_b)
        orchestrator_b.extra_ev_ids = [ev1_id]
        runtime_b = make_runtime(database, tmp_path, lambda context, events: make_driver(context, orchestrator_b))
        final_context = await runtime_b.execute_run(run_id)
        assert final_context.status.value == "completed", (final_context.workflow_state, (await RunRepository(database).get(run_id)).error_message)
        # 不重做动作 1：本段只执行 plan_2
        assert [task.task_id for task in orchestrator_b.executed] == ["plan_2"]
        # 动作 1/2 的 tool_call 行都存在，动作 1 不重复
        tool_repo = PlanTaskToolRepository(database)
        assert await tool_repo.get_tool_call(f"call-{run_id}-plan_1-a1") is not None
        assert await tool_repo.get_tool_call(f"call-{run_id}-plan_2-a1") is not None
        stored_evidence = await EvidenceRepository(database).list_for_run(run_id)
        assert len(stored_evidence) == 2
        events = await EventRepository(database).list_after(run_id)
        step_events = [e for e in events if e.event_type == "coreact.step_committed"]
        assert [json.loads(e.payload_json)["step_index"] for e in step_events] == [1, 2]
        all_events = await count_all_events(EventRepository(database), run_id)
        assert all_events.get("tool.completed") == 2
        # Pause marker 在 resume 时被 clear
        run_row = await RunRepository(database).get(run_id)
        assert not bool(json.loads(run_row.config_snapshot_json or "{}").get("pause_requested"))
        # Contract rubric ids 必须来自持久 ledger 的 evidence_id
        assert ev1_id in {item.evidence_id for item in stored_evidence}
        checks = await ContractRepository(database).list_for_run(run_id)
        kinds = {check.kind: check.passed for check in checks}
        assert bool(kinds.get("rubric_coverage")) is True
        assert bool(kinds.get("min_evidence")) is True




async def count_all_events(repo, run_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    after = 0
    while True:
        batch = await repo.list_after(run_id, after_event_id=after, limit=1000)
        if not batch:
            break
        for item in batch:
            counts[item.event_type] = counts.get(item.event_type, 0) + 1
        after = batch[-1].event_id
        if len(batch) < 1000:
            break
    return counts
