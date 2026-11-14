"""T-402：冻结的 ResearchBrief/ActionSpec/ActionResult/ObservationSummary/
EvidenceAssessment/ExecutionState 接口与 ResearchRunResult 视图测试。

验收（tasks.md T-402）：JSON 往返、未知版本拒绝、来源约束、并行/重试身份、
旧 payload 转换、Markdown 报告状态、ActionResult execution/verification 分离。
"""
import copy
import json

import pytest
from pydantic import ValidationError

from deepresearch_agent.harness.action_models import (
    ACTION_MODELS_SCHEMA_VERSION,
    MAX_FINDINGS,
    MAX_LIST_ITEMS,
    ActionResult,
    ActionSpec,
    ActionVerdict,
    ExecutionState,
    LegacyStateUnsupported,
    ObservationSummary,
    action_rubric_from_task_rubric,
    action_spec_from_task_node,
    build_observation_summary,
    execution_state_from_coreact_loop,
)
from deepresearch_agent.harness.coreact_state import (
    ContextState,
    CoreActLoopState,
    PendingAction,
)
from deepresearch_agent.harness.research_brief import (
    RESEARCH_BRIEF_SCHEMA_VERSION,
    BudgetPolicy,
    EvidenceAssessment,
    ResearchBrief,
    SourceMode,
    canonical_role_models,
    merge_assessments,
)
from deepresearch_agent.harness.run_result import ResearchRunResult


def make_brief(**overrides):
    payload = {
        "brief_id": "brief_demo",
        "goal": "回答示例研究问题",
        "questions": [{"question_id": "q1", "text": "示例问题的核心是什么？"}],
        "allowed_sources": [SourceMode.WEB],
    }
    payload.update(overrides)
    return ResearchBrief.model_validate(payload)


def make_task(**overrides):
    payload = {
        "task_id": "task_demo",
        "task_type": "web_search",
        "source_mode": "web",
        "description": "检索示例主题",
        "parameters": {"query": "示例主题 现状"},
    }
    payload.update(overrides)
    return payload


# ---------- JSON 往返 ----------

def test_research_brief_json_roundtrip_preserves_hash():
    brief = make_brief()
    restored = ResearchBrief.from_payload(json.loads(json.dumps(brief.model_dump(mode="json"))))
    assert restored == brief
    assert restored.content_hash() == brief.content_hash()


def test_action_models_json_roundtrip():
    spec = ActionSpec(
        action_id="action_task_demo", task_id="task_demo", query="示例主题", tool="web_search",
        target_question_ids=["q1"], rubric_id="rubric_task_demo_v1",
    )
    summary = build_observation_summary(
        action_id=spec.action_id, attempt_id=spec.attempt_id,
        findings=[{"statement": "发现一", "evidence_ids": ["ev_1"]}],
    )
    result = ActionResult(
        run_id="run_demo", action_id=spec.action_id, task_id=spec.task_id,
        attempt_id=spec.attempt_id, attempt=1,
        execution_status="succeeded", verification=ActionVerdict(status="passed", verifier_version="v1"),
        evidence_refs=["ev_1"], observation=summary,
    )
    state = ExecutionState(inflight_actions=[spec], committed_action_ids=[], observations=[summary])
    for model in (spec, summary, result, state):
        restored = type(model).model_validate(json.loads(json.dumps(model.model_dump(mode="json"))))
        assert restored == model


def test_research_run_result_json_roundtrip():
    payload = {
        "run_id": "run_demo", "status": "completed",
        "result": {"format": "markdown", "report": "# 研究报告\n\n正文 [ev_1]", "evidence_ids": ["ev_1"]},
        "verification": {"status": "passed", "checks": [{"check_id": "c1", "kind": "min_evidence", "passed": True}]},
        "limitations": ["仅 web 单来源"],
    }
    result = ResearchRunResult.model_validate(payload)
    restored = ResearchRunResult.from_payload(json.loads(json.dumps(result.model_dump(mode="json"))))
    assert restored == result


# ---------- 未知版本拒绝 ----------

@pytest.mark.parametrize(
    ("model_type", "field", "value"),
    [
        (ResearchBrief, "schema_version", RESEARCH_BRIEF_SCHEMA_VERSION + 1),
        (ActionSpec, "schema_version", ACTION_MODELS_SCHEMA_VERSION + 1),
        (ActionResult, "schema_version", ACTION_MODELS_SCHEMA_VERSION + 1),
        (ObservationSummary, "schema_version", ACTION_MODELS_SCHEMA_VERSION + 1),
        (ExecutionState, "schema_version", ACTION_MODELS_SCHEMA_VERSION + 1),
        (EvidenceAssessment, "schema_version", RESEARCH_BRIEF_SCHEMA_VERSION + 1),
        (ResearchRunResult, "schema_version", 2),
    ],
)
def test_unknown_schema_version_rejected(model_type, field, value):
    payload: dict = {
        ResearchBrief: {"brief_id": "b", "goal": "g", "questions": [{"question_id": "q", "text": "t"}], "allowed_sources": ["web"]},
        ActionSpec: {"action_id": "a", "task_id": "t", "query": "q", "tool": "web_search"},
        ActionResult: {"run_id": "r", "action_id": "a", "task_id": "t", "attempt_id": "a_attempt1"},
        ExecutionState: {},
        ObservationSummary: {"action_id": "a", "attempt_id": "a_attempt1"},
        EvidenceAssessment: {"claim_id": "c1"},
        ResearchRunResult: {"run_id": "r", "status": "completed", "result": {"report": "x"}, "verification": {"status": "passed"}},
    }[model_type]
    payload = dict(payload, **{field: value})
    with pytest.raises(ValidationError) as excinfo:
        model_type.model_validate(payload)
    assert "高于实现支持" in str(excinfo.value)


def test_action_rubric_unknown_version_rejected():
    # RG-01：rubric 独立 schema 版本（RUBRIC_SCHEMA_VERSION=2，旧适配记录按 v1）
    from deepresearch_agent.harness.action_models import ActionRubric, RUBRIC_SCHEMA_VERSION

    with pytest.raises(ValidationError, match="高于实现支持"):
        ActionRubric.model_validate({
            "rubric_id": "r1", "task_id": "t1",
            "criteria": [{"criterion_id": "c", "kind": "evidence_presence", "description": "d"}],
            "schema_version": RUBRIC_SCHEMA_VERSION + 1,
        })


def test_unknown_coreact_loop_state_version_rejected_by_adapter():
    raw = CoreActLoopState(
        pending_action=PendingAction(task=make_task(), rubric={"rubric_id": "r", "task_id": "task_demo", "criteria": [
            {"criterion_id": "c", "kind": "evidence_presence", "description": "d"}]}),
    ).model_dump(mode="json")
    raw["version"] = 99
    with pytest.raises(LegacyStateUnsupported):
        execution_state_from_coreact_loop(raw)


def test_unknown_engine_rejected_by_adapter():
    raw = CoreActLoopState().model_dump(mode="json")
    raw["engine"] = "coreact_v9"
    with pytest.raises(LegacyStateUnsupported):
        execution_state_from_coreact_loop(raw)


# ---------- 来源约束 ----------

def test_brief_rejects_sources_inconsistent_with_source_mode():
    with pytest.raises(ValidationError, match="完全一致"):
        make_brief(allowed_sources=[SourceMode.WEB, SourceMode.GRAPHRAG])


def test_brief_rejects_duplicate_allowed_sources():
    with pytest.raises(ValidationError, match="重复"):
        make_brief(allowed_sources=[SourceMode.WEB, SourceMode.WEB])


def test_brief_revision_cannot_expand_sources():
    brief = make_brief()
    revised = brief.with_revision(goal="修订后的目标")
    assert revised.version == brief.version + 1
    assert revised.source_mode == brief.source_mode
    # 修订接口不提供任何改写来源的入口（签名仅 goal/questions/output_requirements/
    # acceptance_criteria），多来源组合在构造时被拒绝——"扩大来源"不可表达
    import inspect

    parameters = inspect.signature(brief.with_revision).parameters
    assert not {"allowed_sources", "source_mode"} & set(parameters)
    assert revised.content_hash() != brief.content_hash()  # 修订必须可审计（hash 变化）


def test_brief_tools_follow_frozen_source():
    # S1：web 是聚合通道（Tavily + OpenAlex/arXiv）
    assert make_brief().allowed_tools == frozenset({"web_search", "scholar_search"})
    assert make_brief(allowed_sources=[SourceMode.SCHOLAR]).allowed_tools == frozenset({"scholar_search"})


# ---------- 并行/重试身份 ----------

def test_attempt_identity_stable_across_retries():
    spec = ActionSpec(action_id="action_t1", task_id="t1", query="q", tool="web_search")
    assert spec.attempt_id == "action_t1_attempt1"
    retry = spec.next_attempt(query="q2")
    assert retry.action_id == spec.action_id  # 身份稳定
    assert retry.attempt == 2 and retry.attempt_id == "action_t1_attempt2"
    with pytest.raises(ValidationError):
        ActionResult(run_id="r", action_id="a", task_id="t", attempt_id="mismatch", attempt=2)


def test_inflight_batch_ids_unique():
    batch = [
        ActionSpec(action_id=f"action_t{i}", task_id=f"t{i}", query=f"q{i}", tool="web_search")
        for i in range(3)
    ]
    state = ExecutionState(inflight_actions=batch)
    assert [item.action_id for item in state.inflight_actions] == [f"action_t{i}" for i in range(3)]
    with pytest.raises(ValidationError, match="唯一"):
        ExecutionState(inflight_actions=[batch[0], batch[0]])


def test_record_commit_moves_action_from_inflight_to_committed():
    state = ExecutionState(inflight_actions=[
        ActionSpec(action_id="action_t1", task_id="t1", query="q1", tool="web_search"),
        ActionSpec(action_id="action_t2", task_id="t2", query="q2", tool="web_search"),
    ])
    committed = state.record_commit("action_t1")
    assert [item.action_id for item in committed.inflight_actions] == ["action_t2"]
    assert committed.committed_action_ids == ["action_t1"]
    # 幂等：重复提交同一动作不产生重复行
    assert committed.record_commit("action_t1").committed_action_ids == ["action_t1"]
    assert committed.committed_action_ids == ["action_t1"]


# ---------- 旧 payload 转换 ----------

def test_task_node_converts_to_action_spec():
    from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskNode

    task = TaskNode.model_validate(make_task())
    spec = action_spec_from_task_node(task, target_question_ids=["q1"], rubric_id="rubric_x")
    assert spec.action_id == "action_task_demo"
    assert spec.query == "示例主题 现状"
    assert spec.tool == "web_search"
    # 只读适配：原 TaskNode 不被修改
    assert task.status == "pending"


def test_task_rubric_converts_to_action_rubric():
    from deepresearch_agent.agents.multi_agent.core.rubric import build_task_rubric

    rubric = build_task_rubric("task_demo", description="d", source_mode="web", min_evidence=1)
    adapted = action_rubric_from_task_rubric(rubric, objective="获取现状事实")
    assert adapted.rubric_id == rubric.rubric_id
    assert len(adapted.criteria) == len(rubric.criteria)
    assert adapted.objective == "获取现状事实"


def test_coreact_loop_state_converts_to_execution_state():
    loop = CoreActLoopState(
        pending_action=PendingAction(
            task=make_task(), attempt=2, retry_reason="证据不足",
            rubric={"rubric_id": "r1", "task_id": "task_demo", "criteria": [
                {"criterion_id": "c", "kind": "evidence_presence", "description": "d"}]},
        ),
        context_state=ContextState(uncertain=[{"term": "术语", "reason": "未覆盖", "next_action": "补检索"}]),
    )
    state = execution_state_from_coreact_loop(loop)
    assert state.next_operation == "execute"
    assert len(state.inflight_actions) == 1
    inflight = state.inflight_actions[0]
    assert inflight.action_id == "action_task_demo" and inflight.attempt == 2
    # rubric 正文与重试反馈随 in-flight 保留（T-404 恢复不丢重试语义）
    assert "action_task_demo" in state.inflight_rubrics
    assert state.inflight_rubrics["action_task_demo"].rubric_id == "r1"
    assert state.retry_reasons == {"action_task_demo": "证据不足"}
    assert state.assessments[0].status == "unresolved"
    assert state.assessments[0].claim_text == "术语"
    # 本例无 trusted/untrusted 证据分层：不得虚构证据级映射
    assert "context_state_evidence_ids" not in state.unmapped
    assert state.unmapped["legacy_counters"]["decisions_used"] == 0


def test_coreact_loop_state_evidence_layering_recorded_as_unmapped():
    loop = CoreActLoopState(context_state=ContextState(trusted_ids=["ev_1"], untrusted_ids=["ev_2"]))
    state = execution_state_from_coreact_loop(loop)
    carried = state.unmapped["context_state_evidence_ids"]
    assert carried["trusted_ids"] == ["ev_1"] and carried["untrusted_ids"] == ["ev_2"]


def test_malformed_pending_action_rejected_explicitly():
    loop = CoreActLoopState(
        pending_action=PendingAction(
            task={"task_id": "task_x", "task_type": "not_a_real_tool"},  # 缺 description/source_mode
            rubric={"rubric_id": "r", "task_id": "task_x", "criteria": [
                {"criterion_id": "c", "kind": "evidence_presence", "description": "d"}]},
        ),
    )
    with pytest.raises(LegacyStateUnsupported, match="pending_action"):
        execution_state_from_coreact_loop(loop)


def test_done_loop_state_maps_to_report_not_complete():
    """旧 done 语义是"研究循环结束、待报告"；complete 只能由最终 Contract 判定，
    legacy 状态不可推导（否则中断 Run 会伪称已完成）。"""
    loop = CoreActLoopState(done=True, stop_reason="no_evidence_progress")
    assert execution_state_from_coreact_loop(loop).next_operation == "report"
    loop_report = CoreActLoopState(done=True, stop_reason=None)
    assert execution_state_from_coreact_loop(loop_report).next_operation == "report"


# ---------- ExecutionState：显式 next_operation ----------

def test_execution_state_next_operation_explicit():
    assert ExecutionState().next_operation == "execute"
    assert ExecutionState(next_operation="report").next_operation == "report"
    assert ExecutionState(next_operation="complete").next_operation == "complete"
    with pytest.raises(ValidationError):
        ExecutionState(next_operation="unknown_stage")


# ---------- ActionResult execution/verification 分离 ----------

def test_action_result_execution_success_but_verification_failed():
    result = ActionResult(
        run_id="run_demo", action_id="action_t1", task_id="t1", attempt_id="action_t1_attempt1",
        execution_status="succeeded",
        verification=ActionVerdict(status="failed", verifier_version="v1", failed_criteria=["c1"]),
        error=None,
    )
    assert result.execution_status == "succeeded" and result.verification.status == "failed"


def test_action_result_execution_failure_keeps_attempt_record():
    result = ActionResult(
        run_id="run_demo", action_id="action_t1", task_id="t1", attempt_id="action_t1_attempt1",
        execution_status="failed",
        verification=ActionVerdict(status="not_run"),
        error="provider timeout",
        usage={"tool_calls": 1, "input_tokens": 0, "output_tokens": 0},
    )
    assert result.verification.status == "not_run"  # 未执行到验证
    assert result.error == "provider timeout"


def test_active_result_refs_only_reference_committed_actions():
    state = ExecutionState(
        active_result_refs={"action_t1": "action_t1_attempt2"},
        committed_action_ids=["action_t1"],
    )
    # 失败 attempt（attempt1）不被引用但保留在持久层；active 只指最新有效
    assert state.active_result_refs == {"action_t1": "action_t1_attempt2"}
    # 引用未提交动作 / 派生规则不符 → 拒绝（防止账目漂移）
    with pytest.raises(ValidationError, match="未提交"):
        ExecutionState(
            active_result_refs={"action_ghost": "action_ghost_attempt1"},
            committed_action_ids=["action_t1"],
        )
    with pytest.raises(ValidationError, match="派生规则"):
        ExecutionState(
            active_result_refs={"action_t1": "attempt-2-mismatch"},
            committed_action_ids=["action_t1"],
        )


def test_record_commit_rejects_ghost_and_committed_inflight_overlap():
    state = ExecutionState(inflight_actions=[
        ActionSpec(action_id="action_t1", task_id="t1", query="q1", tool="web_search"),
    ])
    with pytest.raises(ValidationError, match="同时"):
        ExecutionState(
            inflight_actions=[ActionSpec(action_id="action_t1", task_id="t1", query="q1", tool="web_search")],
            committed_action_ids=["action_t1"],
        )
    with pytest.raises(ValueError, match="未派发"):
        state.record_commit("action_ghost")
    assert state.committed_action_ids == []


def test_record_commit_clears_rubric_and_retry_reason():
    rubric_payload = {
        "rubric_id": "r1", "task_id": "t1",
        "criteria": [{"criterion_id": "c", "kind": "evidence_presence", "description": "d"}],
    }
    state = ExecutionState(
        inflight_actions=[ActionSpec(action_id="action_t1", task_id="t1", query="q1", tool="web_search")],
        inflight_rubrics={"action_t1": rubric_payload},
        retry_reasons={"action_t1": "证据不足"},
    )
    committed = state.record_commit("action_t1")
    assert committed.inflight_rubrics == {}
    assert committed.retry_reasons == {}
    assert committed.committed_action_ids == ["action_t1"]


def test_execution_state_rejects_orphan_rubric_and_retry_keys():
    orphan_rubric = {
        "rubric_id": "r_ghost", "task_id": "t_ghost",
        "criteria": [{"criterion_id": "c", "kind": "evidence_presence", "description": "d"}],
    }
    with pytest.raises(ValidationError, match="inflight_rubrics"):
        ExecutionState(inflight_rubrics={"action_ghost": orphan_rubric})
    with pytest.raises(ValidationError, match="retry_reasons"):
        ExecutionState(retry_reasons={"action_ghost": "reason"})


# ---------- ObservationSummary 边界 ----------

def test_findings_require_evidence_ids():
    with pytest.raises(ValidationError):
        build_observation_summary(
            action_id="a", attempt_id="a_attempt1",
            findings=[{"statement": "无证据的猜测", "evidence_ids": []}],
        )


def test_summary_builder_truncates_deterministically():
    findings = [
        {"statement": "f" + str(i) * 10, "evidence_ids": [f"ev_{i}"]}
        for i in range(MAX_FINDINGS + 6)
    ]
    summary = build_observation_summary(action_id="a", attempt_id="a_attempt1", findings=findings, max_findings=5)
    assert len(summary.findings) == 5
    long_summary = build_observation_summary(
        action_id="a", attempt_id="a_attempt1",
        findings=[{"statement": "x" * 900, "evidence_ids": ["ev_1"]}],
    )
    assert len(long_summary.findings[0].statement) <= 500


def test_rubric_adapter_preserves_semantic_check_flag():
    from deepresearch_agent.agents.multi_agent.core.rubric import build_task_rubric

    rubric = build_task_rubric("task_demo", description="d", source_mode="web", semantic_check=True)
    adapted = action_rubric_from_task_rubric(rubric)
    assert adapted.verifier_kind == "semantic_optional"
    plain = action_rubric_from_task_rubric(
        build_task_rubric("task_demo", description="d", source_mode="web")
    )
    assert plain.verifier_kind == "deterministic"


def test_summary_builder_discards_overflow_candidates_before_validation():
    """条数超限保留前 max_findings 条：被截掉的候选不参与校验。"""
    findings = [{"statement": "f", "evidence_ids": ["ev_1"]} for _ in range(4)]
    findings.append({"statement": "", "evidence_ids": []})  # 位于截断位之后，不应触发校验
    summary = build_observation_summary(action_id="a", attempt_id="a_attempt1", findings=findings, max_findings=4)
    assert len(summary.findings) == 4


def test_summary_hard_bounds_reject_overflow():
    with pytest.raises(ValidationError, match="上限"):
        build_observation_summary(
            action_id="a", attempt_id="a_attempt1",
            findings=[{"statement": "x" * 4000, "evidence_ids": ["ev_1"]}],
            max_chars=4000,
        )


def test_observation_summary_list_overflow_rejected():
    with pytest.raises(ValidationError, match="上限"):
        ObservationSummary.model_validate({
            "action_id": "a", "attempt_id": "a_attempt1",
            "suggested_followups": [f"f{i}" for i in range(MAX_LIST_ITEMS + 1)],
        })
    with pytest.raises(ValidationError, match="上限"):
        ObservationSummary.model_validate({
            "action_id": "a", "attempt_id": "a_attempt1",
            "contradictions": [f"c{i}" for i in range(MAX_LIST_ITEMS + 1)],
        })


def test_schema_version_zero_and_negative_rejected():
    with pytest.raises(ValidationError):
        make_brief(schema_version=0)
    with pytest.raises(ValidationError):
        ExecutionState(schema_version=-1)


def test_summary_collects_unique_evidence_ids():
    summary = build_observation_summary(
        action_id="a", attempt_id="a_attempt1",
        findings=[
            {"statement": "f1", "evidence_ids": ["ev_1", "ev_2"]},
            {"statement": "f2", "evidence_ids": ["ev_2", "ev_3"]},
        ],
    )
    assert summary.evidence_ids == ["ev_1", "ev_2", "ev_3"]


# ---------- EvidenceAssessment ----------

def test_assessment_conclusive_requires_attribution():
    with pytest.raises(ValidationError, match="check_kind"):
        EvidenceAssessment(claim_id="c1", status="supported", evidence_ids=["ev_1"])
    ok = EvidenceAssessment(
        claim_id="c1", status="supported", evidence_ids=["ev_1"],
        check_kind="claim_support", verifier="keyword_proxy", verifier_version="v1",
    )
    assert ok.status == "supported"


def test_assessment_merge_dedup_keeps_latest_step():
    a1 = EvidenceAssessment(claim_id="c1", status="available", evidence_ids=["ev_1"], updated_step=1)
    a2 = EvidenceAssessment(
        claim_id="c1", status="supported", evidence_ids=["ev_2"],
        check_kind="claim_support", verifier="v", verifier_version="1", updated_step=3,
    )
    merged = merge_assessments([a1, a2, a1])
    assert len(merged) == 1 and merged[0].status == "supported"


# ---------- ResearchRunResult 状态不变量 ----------

def test_completed_requires_passed_verification():
    with pytest.raises(ValidationError):
        ResearchRunResult.model_validate({
            "run_id": "r", "status": "completed", "result": {"report": "# x"},
            "verification": {"status": "unknown"},
        })


def test_non_completed_result_default_null_and_failed_draft_isolated():
    result = ResearchRunResult.model_validate({"run_id": "r", "status": "failed"})
    assert result.result is None
    with pytest.raises(ValidationError):
        ResearchRunResult.model_validate({
            "run_id": "r", "status": "failed", "result": {"report": "# 草稿"},
        })
    draft = ResearchRunResult.model_validate({
        "run_id": "r", "status": "failed", "draft": {"report": "# 草稿", "verified": False},
    })
    assert draft.draft["verified"] is False


def test_needs_user_input_requires_clarification():
    with pytest.raises(ValidationError, match="clarification"):
        ResearchRunResult.model_validate({"run_id": "r", "status": "needs_user_input"})


def test_run_result_covers_all_persistent_statuses_including_cancelling():
    from deepresearch_agent.harness.contracts import RunStatus

    statuses = {item.value for item in RunStatus}
    declared = ResearchRunResult.model_fields["status"].annotation
    # 冻结视图必须覆盖 RunStatus 全部成员（含 cancelling 等过渡态）
    for status in statuses:
        payload = {"run_id": "r", "status": status}
        if status == "needs_user_input":
            payload["clarification"] = {"question_id": "q", "questions": ["请补充"]}
        if status == "completed":
            payload.update(result={"report": "# x"}, verification={"status": "passed"})
        ResearchRunResult.model_validate(payload)
    assert "cancelling" in str(declared)


def test_run_result_draft_requires_unverified_flag():
    with pytest.raises(ValidationError, match="verified=false"):
        ResearchRunResult.model_validate({
            "run_id": "r", "status": "failed", "draft": {"report": "# 草稿", "verified": True},
        })
    with pytest.raises(ValidationError, match="verified=false"):
        ResearchRunResult.model_validate({
            "run_id": "r", "status": "failed", "draft": {"report": "# 草稿"},
        })


def test_run_result_rejects_non_markdown_format():
    with pytest.raises(ValidationError):
        ResearchRunResult.model_validate({
            "run_id": "r", "status": "completed", "result": {"format": "html", "report": "<p>x</p>"},
            "verification": {"status": "passed"},
        })


def test_completed_rejects_error_view():
    with pytest.raises(ValidationError, match="error"):
        ResearchRunResult.model_validate({
            "run_id": "r", "status": "completed", "result": {"report": "# x"},
            "verification": {"status": "passed"},
            "error": {"code": "BUDGET_EXHAUSTED"},
        })


def test_budget_exhausted_carries_error_view():
    result = ResearchRunResult.model_validate({
        "run_id": "r", "status": "budget_exhausted",
        "error": {"code": "BUDGET_EXHAUSTED", "message": "token 预算耗尽", "retryable": False},
        "limitations": ["已完成 2/5 个研究问题"],
    })
    assert result.error.code == "BUDGET_EXHAUSTED"


# ---------- 预算与模型角色冻结 ----------

def test_budget_policy_freezes_from_budget_limits():
    from deepresearch_agent.harness.budgets import BudgetLimits

    policy = BudgetPolicy.from_budget_limits(BudgetLimits(max_plan_tasks=5, max_concurrency=2))
    assert policy.max_actions == 5 and policy.max_concurrency == 2
    restored = BudgetPolicy.model_validate(json.loads(json.dumps(policy.model_dump(mode="json"))))
    assert restored == policy


def test_canonical_role_models_maps_legacy_planner_to_brief():
    canonical = canonical_role_models({"planner": {"model": "m-planner"}, "reporter": {"model": "m-reporter"}})
    assert canonical["brief"] == {"model": "m-planner"}
    assert canonical["reporter"] == {"model": "m-reporter"}
    assert canonical["legacy"]["planner"] == {"model": "m-planner"}


def test_canonical_role_models_explicit_brief_wins_over_legacy():
    canonical = canonical_role_models({"planner": {"model": "old"}, "brief": {"model": "new"}})
    assert canonical["brief"] == {"model": "new"}


def test_canonical_role_models_does_not_mutate_input():
    snapshot = {"planner": {"model": "m"}}
    original = copy.deepcopy(snapshot)
    canonical_role_models(snapshot)
    assert snapshot == original


def test_canonical_role_models_empty_passthrough():
    assert canonical_role_models(None) == {}
    assert canonical_role_models({}) == {}
