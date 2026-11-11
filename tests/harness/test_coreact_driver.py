"""T-204 driver-level tests: observation-driven action loop and bounded retry."""
import json

from deepresearch_agent.agents.multi_agent.core.plan_spec import (
    AcceptanceCriteria,
    PlanSpec,
    ProblemStatement,
    TaskGraph,
    TaskNode,
)
from deepresearch_agent.agents.multi_agent.planner.base_planner import PlannerResult
from deepresearch_agent.agents.multi_agent.planner.clarifier import ClarificationResult
from deepresearch_agent.harness.contracts import RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.harness.coreact_state import CoreActLoopState
from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver
from deepresearch_agent.harness.run_context import RunContext


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


def action(query, description=None):
    return {
        "ready_to_report": False,
        "task": {
            "task_type": "web_search",
            "description": description or f"搜索 {query}",
            "query": query,
        },
        "reason": "收集证据",
    }


def action_by_id(task_id, query, description=None):
    payload = action(query, description)
    payload["task"]["task_id"] = task_id
    return payload


def ready(reason="证据覆盖目标"):
    return {"ready_to_report": True, "task": None, "reason": reason}


def plan_spec():
    return PlanSpec(
        source_mode="web",
        problem_statement=ProblemStatement(original_query="调研检索增强技术"),
        task_graph=TaskGraph(nodes=[
            TaskNode(task_id="plan_1", task_type="web_search", source_mode="web", description="检索增强 基础概念", parameters={"query": "检索增强 基础概念"}),
            TaskNode(task_id="plan_2", task_type="web_search", source_mode="web", description="检索增强 局限与评测", parameters={"query": "检索增强 局限与评测"}),
        ], execution_mode="sequential"),
        acceptance_criteria=AcceptanceCriteria(
            min_evidence_count=1,
            completion_conditions=["检索增强是一种技术路线"],
        ),
    )


def make_context(**overrides):
    values = dict(
        run_id="run-coreact-test",
        session_id="session-coreact-test",
        trigger_message_id="msg-coreact-test",
        source_mode=SourceMode.WEB,
        workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        status=RunStatus.PLANNING,
        original_query="调研检索增强技术",
        resolved_query="调研检索增强技术",
        config_snapshot={"orchestration": "coreact_v1", "min_evidence": 1},
    )
    values.update(overrides)
    return RunContext(**values)


class FakeOrchestrator:
    def __init__(self, llm, execution_outcomes):
        self.controller_model = llm
        self.execution_outcomes = list(execution_outcomes)
        self.executed = []
        self.execute_calls = 0

    def execute(self, state, planner_result, **kwargs):
        self.execute_calls += 1
        return []

    def plan(self, state, *, assumptions=None):
        plan = plan_spec()
        state.plan = plan
        return PlannerResult(
            plan_spec=plan,
            clarification=ClarificationResult(needs_clarification=False),
            executor_signal=plan.to_execution_signal(),
        )

    def execute_action(self, state, task, *, progress_callback=None):
        self.executed.append(task.model_copy(deep=True))
        success, evidence_text = self.execution_outcomes.pop(0)
        call = type("Call", (), {
            "tool_name": task.task_type,
            "tool_call_id": f"call_{len(self.executed)}",
            "args": dict(task.parameters),
            "result": {"answer": evidence_text} if success else None,
            "status": "success" if success else "failed",
            "error": None if success else "search failed",
        })()
        evidence = []
        if success and evidence_text:
            evidence.append(type("Evidence", (), {
                "result_id": f"ev_{len(self.executed)}",
                "evidence": evidence_text,
                "source_mode": "web",
                "source_id": f"https://source-{len(self.executed)}.example/page",
            })())
        metadata = type("Metadata", (), {"environment": {}})()
        record = type("Record", (), {
            "record_id": f"record_{len(self.executed)}",
            "task_id": task.task_id,
            "tool_calls": [call],
            "evidence": evidence,
            "metadata": metadata,
            "reflection": None,
        })()
        state.execution_records.append(record)
        if state.execution_context is not None:
            state.execution_context.tool_call_history.append({"args": dict(task.parameters), "task_id": task.task_id})
            if success and task.task_id not in state.execution_context.completed_task_ids:
                state.execution_context.completed_task_ids.append(task.task_id)
        if state.plan:
            state.plan.update_task_status(task.task_id, "completed" if success else "failed")
        if progress_callback:
            progress_callback("task.started", task, None)
            progress_callback("task.completed", task, record)
        return success, None if success else "search_failed", record

    def report(self, *args, **kwargs):
        raise AssertionError("driver.execute must not implicitly report")


def test_coreact_uses_observation_to_choose_next_action():
    llm = ScriptedLLM([action("检索增强生成 基础论文"), action("检索增强生成 局限与评测"), ready()])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线；基础论文摘要"), (True, "检索增强是一种技术路线；局限与评测结果")])
    driver = PlanExecuteReportDriver(make_context(), orchestrator)

    import asyncio
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())

    assert [task.parameters["query"] for task in orchestrator.executed] == [
        "检索增强生成 基础论文", "检索增强生成 局限与评测"
    ]
    assert "近期已执行查询：" in llm.prompts[1]
    assert "检索增强生成 基础论文" in llm.prompts[1]
    assert driver.coreact_loop is not None and driver.coreact_loop.done
    assert driver.coreact_loop.step_index == 2
    assert all("coreact" in record.metadata.environment for record in driver.state.execution_records)


def test_coreact_retries_failed_step_once_with_targeted_feedback():
    llm = ScriptedLLM([
        action_by_id("plan_1", "检索增强生成 定义"),  # 首步决策选择候选计划项
        action_by_id("plan_1", "检索增强生成 一手来源和定义", "补充一手来源"),  # 同 rubric 定向修订 query
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "无关结果"), (True, "检索增强是一种技术路线，定义说明")])
    driver = PlanExecuteReportDriver(make_context(), orchestrator)

    import asyncio
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())

    print("DBG_RETRY", [(t.task_id, t.parameters.get("query")) for t in orchestrator.executed], llm.prompts[1][:200] if len(llm.prompts)>1 else None)
    assert len(orchestrator.executed) == 2
    assert orchestrator.executed[0].task_id == orchestrator.executed[1].task_id
    assert orchestrator.executed[1].parameters["query"] == "检索增强生成 一手来源和定义"
    assert driver.context.budget_usage.task_retries == 1
    attempts = [record.metadata.environment["coreact"]["attempt"] for record in driver.state.execution_records]
    assert attempts == [1, 2]
    assert driver.state.execution_records[-1].metadata.environment["coreact"]["verdict"]["passed"] is True


def test_coreact_no_candidates_stops_at_controller_without_claiming_completion():
    llm = ScriptedLLM([ready("初始计划无可执行任务")])
    orchestrator = FakeOrchestrator(llm, [])
    driver = PlanExecuteReportDriver(make_context(), orchestrator)
    import asyncio
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())
    assert driver.coreact_loop.done is True
    assert driver.coreact_loop.stop_reason == "初始计划无可执行任务"
    assert driver.report_result is None  # ready_to_report 不是 Contract pass，也不直接 completed


def test_coreact_retry_failure_is_persisted_to_next_decision_context():
    llm = ScriptedLLM([
        action("检索增强 首次查询"),
        action("检索增强 补充查询"),
        ready("最多尝试两次，进入报告"),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "无关结果 A"), (True, "仍无关结果 B")])
    driver = PlanExecuteReportDriver(make_context(), orchestrator)
    import asyncio
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())
    assert len(orchestrator.executed) == 2
    assert "证据未覆盖主张词" in llm.prompts[2]
    assert driver.coreact_loop.failure_reasons


def test_retry_same_query_is_rejected_before_second_external_call():
    llm = ScriptedLLM([
        action("检索增强 重复查询"),
        action("检索增强 重复查询"),  # controller 再次返回完全相同 query
        ready("达到无进展限制"),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "无关结果")])
    driver = PlanExecuteReportDriver(make_context(), orchestrator)
    import asyncio
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())
    assert len(orchestrator.executed) == 1  # retry duplicate rejected before dispatch
    assert any("重复查询" in reason for reason in driver.coreact_loop.failure_reasons)


def test_retry_decision_counts_against_total_decision_limit():
    llm = ScriptedLLM([
        action("首次查询"),
        action("定向重试查询"),
        action("不能再发出的第三个决策"),
    ])
    ctx = make_context()
    ctx.budget_limits.max_plan_tasks = 1  # max_decisions = max(2, 2*1)=2
    orchestrator = FakeOrchestrator(llm, [(True, "无关 A"), (True, "无关 B")])
    driver = PlanExecuteReportDriver(ctx, orchestrator)
    import asyncio
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())
    assert driver.coreact_loop.decisions_used == 2
    assert len(llm.prompts) == 2  # 首次动作决策 + 一次 retry 决策
    assert driver.coreact_loop.stop_reason in {"max_decisions_reached", "invalid_action_limit"}
    assert llm.answers == [action("不能再发出的第三个决策")]  # 决策总上限后不再调用 LLM


def test_staged_snapshot_and_legacy_staged_driver_execute_once():
    ctx = make_context(config_snapshot={})
    orchestrator = FakeOrchestrator(ScriptedLLM([]), [])
    driver = PlanExecuteReportDriver(ctx, orchestrator)
    assert driver.orchestration == "staged_v1"
    import asyncio
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())
    assert orchestrator.execute_calls == 1  # legacy batch 不得双执行
    assert driver.snapshot()["orchestration"] == "staged_v1"


def test_snapshot_orchestration_marker_overrides_missing_context_config():
    context = make_context(config_snapshot={"orchestration": "coreact_v1"})
    orchestrator = FakeOrchestrator(ScriptedLLM([ready()]), [])
    original = PlanExecuteReportDriver(context, orchestrator)
    original.coreact_loop = CoreActLoopState(step_index=3, decisions_used=4)
    snapshot = original.snapshot()

    restored = PlanExecuteReportDriver(
        make_context(config_snapshot={}, workflow_state=snapshot),
        FakeOrchestrator(ScriptedLLM([]), []),
    )
    assert restored.orchestration == "coreact_v1"
    assert restored.coreact_loop.step_index == 3
    assert restored.coreact_loop.decisions_used == 4

    legacy_snapshot = dict(snapshot)
    legacy_snapshot.pop("orchestration")
    legacy = PlanExecuteReportDriver(
        make_context(config_snapshot={"orchestration": "coreact_v1"}, workflow_state=legacy_snapshot),
        FakeOrchestrator(ScriptedLLM([]), []),
    )
    assert legacy.orchestration == "staged_v1"  # 无 marker 的历史快照固定走 legacy


def test_scholar_search_is_registered_as_retrieval_action():
    from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor

    assert RetrievalExecutor().can_handle("scholar_search")


def test_skipped_candidate_status_is_valid_and_not_ready_for_execution():
    from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskNode

    selected = TaskNode(task_id="selected", task_type="web_search", source_mode="web", description="done", status="completed")
    unselected = TaskNode(task_id="unselected", task_type="web_search", source_mode="web", description="not chosen", status="skipped")
    graph = TaskGraph(nodes=[selected, unselected])
    assert graph.get_ready_tasks(["selected"]) == []
    restored = TaskGraph.model_validate(graph.model_dump(mode="json"))
    assert restored.nodes[1].status == "skipped"

