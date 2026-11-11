"""T-206: rubric_coverage 全局契约检查单测。"""
from types import SimpleNamespace

import asyncio

from deepresearch_agent.harness.contracts import ContractEvaluator
from deepresearch_agent.harness.contracts import RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.agents.multi_agent.core.execution_record import (
    ExecutionMetadata,
    ExecutionRecord,
)
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.harness.verifiers.deterministic import DeterministicVerifiers


def evidence(eid, summary, title=""):
    return SimpleNamespace(evidence_id=eid, summary=summary, title=title, source_mode="web", source_id=f"s-{eid}")


RUBRIC = {
    "rubric_id": "rubric_t1_v1",
    "task_id": "t1",
    "criteria": [
        {"criterion_id": "t1_claim", "kind": "claim_support", "description": "d",
         "weight": 1.5, "required": True, "min_evidence": 1,
         "required_terms": ["检索增强"]},
        {"criterion_id": "t1_presence", "kind": "evidence_presence", "description": "d",
         "weight": 1.5, "required": True, "min_evidence": 1, "required_terms": []},
    ],
}


def make_report(citations):
    body = "".join(f"这是关于该主题的一条实质性研究主张，包含细节与结论段。[^{cid}] " for cid in citations)
    return "# 研究报告\n\n" + body


EVIDENCE = [
    evidence("ev_aaa", "检索增强生成是一种结合检索与生成的技术路线"),
    evidence("ev_bbb", "完全无关的另一段证据内容"),
]


class TestRubricCoverageVerifier:
    def test_pass_when_cited_evidence_covers_terms(self):
        verifier = DeterministicVerifiers(run_id="r", source_mode="web", report=make_report(["ev_aaa"]), evidence=EVIDENCE)
        check = verifier.rubric_coverage([RUBRIC])
        assert check.passed is True

    def test_fail_when_cited_evidence_does_not_cover_terms(self):
        # 引用了不相干证据——表面合规，实际未覆盖主张词
        verifier = DeterministicVerifiers(run_id="r", source_mode="web", report=make_report(["ev_bbb"]), evidence=EVIDENCE)
        check = verifier.rubric_coverage([RUBRIC])
        assert check.passed is False
        assert check.observed["actual"]["unmet"][0]["term"] == "检索增强"

    def test_fail_when_no_citations_at_all(self):
        verifier = DeterministicVerifiers(run_id="r", source_mode="web", report="没有任何引用的主张文本。", evidence=EVIDENCE)
        check = verifier.rubric_coverage([RUBRIC])
        assert check.passed is False

    def test_unknown_terms_from_rubric_not_accepted_from_reporter(self):
        # reporter 伪造的 rubric 无法注入：这里模拟的是执行记录里的 rubric；
        # verifier 只读账本证据，reporter 自报的 ledger ids 不参与本检查。
        forged = {"rubric_id": "rubric_fake", "criteria": [
            {"criterion_id": "f", "kind": "claim_support", "required": True,
             "required_terms": ["不存在的词"]},
        ]}
        verifier = DeterministicVerifiers(run_id="r", source_mode="web", report=make_report(["ev_aaa"]), evidence=EVIDENCE)
        check = verifier.rubric_coverage([forged])
        assert check.passed is False  # 账本内没有任何证据覆盖该词

    def test_non_required_criteria_ignored(self):
        rubric = {"rubric_id": "r2", "criteria": [
            {"criterion_id": "opt", "kind": "claim_support", "required": False, "required_terms": ["不存在的词"]},
        ]}
        verifier = DeterministicVerifiers(run_id="r", source_mode="web", report=make_report(["ev_aaa"]), evidence=EVIDENCE)
        # 无 required 判词 → 空转通过（与步级 verifier 的"无判词跳过"一致），
        # 避免合法无判词 rubric 永久不可修复失败
        assert verifier.rubric_coverage([rubric]).passed is True


class TestRepairRouting:
    def test_classify_routes_rubric_coverage_to_repair(self):
        from deepresearch_agent.harness.recovery import classify_verification_failures

        decision = classify_verification_failures(["rubric_coverage"])
        assert decision.action == "repair_report"

    def test_driver_repair_report_result_none_is_honest_false(self):
        from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver

        context = RunContext(
            run_id="run-repair", session_id="s", trigger_message_id="m",
            source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
            status=RunStatus.REPORTING, original_query="q",
        )
        driver = PlanExecuteReportDriver(context, object())
        # report_result 为 None 时诚实失败（不能在无报告时宣称修复）
        assert asyncio.run(driver.repair_report(["rubric_coverage"])) is False

    def test_driver_repair_enters_rebuild_for_rubric_coverage(self):
        """非空报告 + rubric_coverage 失败：通过 supported 门并触发确定性重建。"""
        from deepresearch_agent.agents.multi_agent.core.plan_spec import (
            PlanSpec,
            ProblemStatement,
            TaskGraph,
        )
        from deepresearch_agent.agents.multi_agent.core.retrieval_result import (
            RetrievalMetadata,
            RetrievalResult,
        )
        from deepresearch_agent.agents.multi_agent.planner.result import PlannerResult
        from deepresearch_agent.agents.multi_agent.planner.clarifier import ClarificationResult
        from deepresearch_agent.agents.multi_agent.reporter.report_schemas import (
            ReportOutline,
            ReportResult,
        )
        from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver

        context = RunContext(
            run_id="run-repair2", session_id="s2", trigger_message_id="m2",
            source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
            status=RunStatus.REPORTING, original_query="调研检索增强技术",
            config_snapshot={"required_sections": []},
        )
        driver = PlanExecuteReportDriver(context, object())
        plan = PlanSpec(
            source_mode="web",
            problem_statement=ProblemStatement(original_query="调研检索增强技术"),
            task_graph=TaskGraph(nodes=[], execution_mode="sequential"),
        )
        driver.state.plan = plan
        driver.planner_result = PlannerResult(
            plan_spec=plan, clarification=ClarificationResult(needs_clarification=False),
        )
        result = RetrievalResult(
            granularity="Chunk",
            evidence="检索增强生成是一种技术路线，结合检索与生成",
            metadata=RetrievalMetadata(source_id="https://example.com/r", source_type="webpage"),
            source="tavily_search",
            source_mode="web",
        )
        record = ExecutionRecord(
            task_id="t1", session_id=driver.state.session_id, worker_type="retrieval_executor",
            tool_calls=[], evidence=[result],
            metadata=ExecutionMetadata(worker_type="retrieval_executor"),
        )
        record.metadata.environment["coreact"] = {"rubric": RUBRIC}
        driver.state.execution_records.append(record)
        driver.report_result = ReportResult(
            outline=ReportOutline(report_type="long_document", title="t", abstract="a", sections=[]),
            sections=[],
            final_report=make_report(["ev_missing"]),  # 引用了账本外证据 → rubric_coverage 失败
        )
        # supported 门通过（否则直接 False 且不重建）；重建后报告引用账本内证据
        assert asyncio.run(driver.repair_report(["rubric_coverage"])) is True
        assert "ev_" + result.result_id.replace("ev_", "") in driver.report_result.final_report or result.result_id in driver.report_result.final_report
        # 重建出的报告必须引用账本内证据 id
        assert result.result_id in driver.report_result.final_report


class TestEvaluatorRouting:
    async def test_rubric_failure_is_recoverable_and_routed(self):
        evaluator = ContractEvaluator()
        verdict = await evaluator.evaluate(
            run_id="r", source_mode="web", report=make_report(["ev_bbb"]),
            evidence=EVIDENCE, min_evidence=2, rubrics=[RUBRIC],
        )
        assert "rubric_coverage" in verdict.failures
        assert verdict.recoverable is True  # 路由到 repair

    async def test_no_rubrics_check_skipped(self):
        evaluator = ContractEvaluator()
        verdict = await evaluator.evaluate(
            run_id="r", source_mode="web", report=make_report(["ev_aaa", "ev_bbb"]),
            evidence=EVIDENCE, min_evidence=2, rubrics=None, consistency_passed=True,
        )
        kinds = [check.kind for check in verdict.checks]
        assert "rubric_coverage" not in kinds
        assert verdict.passed is True

    async def test_check_persists_with_new_kind(self, tmp_path):
        from deepresearch_agent.persistence import Database
        from deepresearch_agent.persistence.repositories import ContractRepository
        from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
        from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository

        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'contract.db').as_posix()}")
        await db.create_schema()
        try:
            session = await SessionRepository(db).create(SessionCreate(title="rubric"))
            _, run, _ = await RunRepository(db).create_for_user_message(
                MessageCreate(session_id=session.session_id, role="user", content="研究问题",
                              client_message_id=f"client-{session.session_id}"),
                RunCreate(session_id=session.session_id, trigger_message_id="atomic",
                          source_mode="web", workflow_mode="plan_execute_report", min_evidence=2),
            )
            evaluator = ContractEvaluator(ContractRepository(db))
            verdict = await evaluator.evaluate(
                run_id=run.run_id, source_mode="web", report=make_report(["ev_bbb"]),
                evidence=EVIDENCE, min_evidence=2, rubrics=[RUBRIC],
            )
            assert "rubric_coverage" in verdict.failures
            stored = await ContractRepository(db).list_for_run(run.run_id)
            assert any(check.kind == "rubric_coverage" for check in stored)
        finally:
            await db.close()
