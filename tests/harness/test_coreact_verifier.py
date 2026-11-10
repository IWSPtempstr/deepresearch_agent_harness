"""T-202: coreact_verifier 步级验证单测。"""

from types import SimpleNamespace

import pytest

from deepresearch_agent.agents.multi_agent.core.rubric import (
    TaskRubric,
    build_task_rubric,
)
from deepresearch_agent.harness.coreact_verifier import CoreActVerifier


def make_record(*, tool_name="web_search", status="success", result=None, evidence_ids=(), texts=()):
    calls = [
        SimpleNamespace(tool_name=tool_name, status=status, result=result if result is not None else "ok")
    ]
    evidence = [
        SimpleNamespace(result_id=eid, evidence=text)
        for eid, text in zip(evidence_ids, texts)
    ]
    return SimpleNamespace(tool_calls=calls, evidence=evidence)


def make_task(query="检索增强生成技术路线"):
    return SimpleNamespace(
        task_id="task_1",
        description="调研检索增强技术路线",
        source_mode="web",
        parameters={"query": query},
    )


RUBRIC = build_task_rubric(
    "task_1",
    description="调研检索增强技术路线",
    source_mode="web",
    min_evidence=1,
    required_terms=["检索增强"],
)
ALLOWED = {"web_search", "tavily_search"}


class TestDeterministicChecks:
    def test_all_pass_when_ledger_backed_evidence_covers_terms(self):
        records = [make_record(evidence_ids=["ev_1"], texts=["Retrieval-Augmented Generation 即检索增强生成技术"])]
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(), records=records,
            ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
        )
        assert verdict.passed is True
        assert verdict.weighted_score == pytest.approx(1.0)
        assert verdict.evidence_ids == ["ev_1"]

    def test_evidence_not_in_ledger_fails_presence(self):
        records = [make_record(evidence_ids=["ev_ghost"], texts=["检索增强生成技术"])]
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(), records=records,
            ledger_ids=set(), executed_queries=set(), allowed_tools=ALLOWED,
        )
        assert verdict.passed is False
        failed = {c.criterion_id: c for c in verdict.criteria}
        assert failed["task_1_evidence_presence"].passed is False
        assert "少于要求" in failed["task_1_evidence_presence"].reason

    def test_policy_violation_tool_fails(self):
        records = [make_record(tool_name="local_search", evidence_ids=["ev_1"], texts=["检索增强生成技术"])]
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(), records=records,
            ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
        )
        assert verdict.passed is False
        failed = {c.criterion_id: c for c in verdict.criteria}
        assert failed["task_1_source_policy"].passed is False
        assert "local_search" in failed["task_1_source_policy"].reason

    def test_internal_tool_does_not_violate_policy(self):
        records = [make_record(tool_name="answer_validator", result={"ok": True})]
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(), records=records,
            ledger_ids=set(), executed_queries=set(), allowed_tools=ALLOWED,
        )
        policy = {c.criterion_id: c for c in verdict.criteria}["task_1_source_policy"]
        assert policy.passed is True  # 内部工具不算违规
        assert verdict.passed is False  # 但无证据仍失败

    def test_duplicate_query_fails_non_required_criterion(self):
        records = [make_record(evidence_ids=["ev_1"], texts=["检索增强生成技术"])]
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(query="重复查询"), records=records,
            ledger_ids={"ev_1"}, executed_queries={"重复查询"}, allowed_tools=ALLOWED,
        )
        dup = {c.criterion_id: c for c in verdict.criteria}["task_1_no_duplicate_query"]
        assert dup.passed is False and dup.reason == "重复执行无增量查询"
        # 非 required 且权重小：总分被拉低但 required 全过 + 阈值 0.5 仍通过
        assert verdict.passed is True

    def test_claim_terms_missing_lists_terms(self):
        records = [make_record(evidence_ids=["ev_1"], texts=["完全无关的内容"])]
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(), records=records,
            ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
        )
        claim = {c.criterion_id: c for c in verdict.criteria}["task_1_claim_support"]
        assert claim.passed is False and "检索增强" in claim.reason




class TestVerdictAggregation:
    def test_retry_reason_lists_failed_criteria(self):
        records = [make_record(status="failed", result=None)]
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(), records=records,
            ledger_ids=set(), executed_queries=set(), allowed_tools=ALLOWED,
        )
        assert verdict.passed is False
        assert verdict.retry_reason and "evidence_presence" in verdict.retry_reason


def semantic_rubric() -> TaskRubric:
    return RUBRIC.model_copy(update={"semantic_check": True})


class TestSemanticGate:
    def test_semantic_fail_appends_penalty_criterion(self):
        records = [make_record(evidence_ids=["ev_1"], texts=["检索增强生成技术"])]
        verdict = CoreActVerifier().verify(
            rubric=semantic_rubric(), task=make_task(), records=records,
            ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
            semantic_allowed=True,
            semantic_fn=lambda q, ev: {"passed": False, "reason": "证据与目标主张无因果支撑"},
        )
        assert verdict.passed is False
        penalty = next(c for c in verdict.criteria if c.criterion_id == "task_1_semantic_support")
        assert penalty.passed is False and penalty.kind == "claim_support"
        assert "task_1_semantic_support" in verdict.retry_reason

    def test_semantic_pass_does_not_rescue_deterministic_fail(self):
        records = [make_record(evidence_ids=["ev_1"], texts=["完全无关"])]
        verdict = CoreActVerifier().verify(
            rubric=semantic_rubric(), task=make_task(), records=records,
            ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
            semantic_allowed=True,
            semantic_fn=lambda q, ev: {"passed": True, "reason": "语义上很好"},
        )
        claim = {c.criterion_id: c for c in verdict.criteria}["task_1_claim_support"]
        assert claim.passed is False  # 确定性 fail 不被语义 pass 升级
        assert verdict.passed is False

    def test_semantic_malformed_degrades_silently(self):
        records = [make_record(evidence_ids=["ev_1"], texts=["检索增强生成技术"])]
        for bad_fn in (
            lambda q, ev: "not-a-dict",
            lambda q, ev: (_ for _ in ()).throw(RuntimeError("llm down")),
            lambda q, ev: None,
        ):
            verdict = CoreActVerifier().verify(
                rubric=semantic_rubric(), task=make_task(), records=records,
                ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
                semantic_allowed=True, semantic_fn=bad_fn,
            )
            assert verdict.passed is True  # 降级为确定性判定
            assert not any(c.criterion_id == "task_1_semantic_support" for c in verdict.criteria)

    def test_semantic_skipped_when_not_allowed(self):
        records = [make_record(evidence_ids=["ev_1"], texts=["检索增强生成技术"])]
        called = {"n": 0}

        def spy(q, ev):
            called["n"] += 1
            return {"passed": False}

        verdict = CoreActVerifier().verify(
            rubric=semantic_rubric(), task=make_task(), records=records,
            ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
            semantic_allowed=False, semantic_fn=spy,
        )
        assert called["n"] == 0  # 预算门控关闭时语义函数绝不执行
        assert verdict.passed is True


class TestEdgeCases:
    def test_empty_records_fails_output_and_presence(self):
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(), records=[],
            ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
        )
        failed = {c.criterion_id: c for c in verdict.criteria}
        assert failed["task_1_source_policy"].passed is False  # 无工具调用明确 fail
        assert failed["task_1_output_schema"].passed is False

    def test_dict_evidence_serialized_for_term_match(self):
        records = [make_record(evidence_ids=["ev_1"], texts=[{"content": "检索增强生成技术说明"}])]
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(), records=records,
            ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
        )
        claim = {c.criterion_id: c for c in verdict.criteria}["task_1_claim_support"]
        assert claim.passed is True  # dict 证据被 JSON 化后参与判词匹配

    def test_missing_result_id_filtered_from_ledger_backed(self):
        record = SimpleNamespace(
            tool_calls=[SimpleNamespace(tool_name="web_search", status="success", result="ok")],
            evidence=[SimpleNamespace(result_id="", evidence="检索增强生成技术")],
        )
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(), records=[record],
            ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
        )
        failed = {c.criterion_id: c for c in verdict.criteria}
        assert failed["task_1_evidence_presence"].passed is False  # 无 id 不计入账本证据

    def test_empty_tool_result_fails_output_schema(self):
        record = make_record(result="")
        verdict = CoreActVerifier().verify(
            rubric=RUBRIC, task=make_task(), records=[record],
            ledger_ids=set(), executed_queries=set(), allowed_tools=ALLOWED,
        )
        failed = {c.criterion_id: c for c in verdict.criteria}
        assert failed["task_1_output_schema"].passed is False

    def test_action_query_key_priority(self):
        task = SimpleNamespace(
            task_id="t", description="描述查询", source_mode="web",
            parameters={"search_query": "二级查询", "query": "首选查询"},
        )
        from deepresearch_agent.harness.coreact_verifier import CoreActVerifier as V
        assert V._action_query(task) == "首选查询"
        task2 = SimpleNamespace(task_id="t", description="描述查询", source_mode="web",
                                parameters={"search_query": "二级查询"})
        assert V._action_query(task2) == "二级查询"
        task3 = SimpleNamespace(task_id="t", description="描述查询", source_mode="web", parameters={})
        assert V._action_query(task3) == "描述查询"

    def test_semantic_penalty_does_not_distort_weighted_score(self):
        # 语义追加失败只影响 passed，不计入加权分母/分子（聚合对齐保护）
        records = [make_record(evidence_ids=["ev_1"], texts=["检索增强生成技术"])]
        verdict = CoreActVerifier().verify(
            rubric=semantic_rubric(), task=make_task(), records=records,
            ledger_ids={"ev_1"}, executed_queries=set(), allowed_tools=ALLOWED,
            semantic_allowed=True, semantic_fn=lambda q, ev: {"passed": False},
        )
        assert verdict.weighted_score == 1.0  # rubric 前 N 项全过
        assert verdict.passed is False  # 追加惩罚单独否决
