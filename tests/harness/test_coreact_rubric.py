"""T-201: rubric / LoopState 核心模型单测。"""

import pytest
from pydantic import ValidationError

from deepresearch_agent.agents.multi_agent.core.rubric import (
    CriterionKind,
    RubricCriterion,
    StepVerdict,
    TaskRubric,
    build_task_rubric,
    rubric_from_task,
)
from deepresearch_agent.harness.coreact_state import (
    COREACT_STATE_KEY,
    CoreActLoopState,
    PendingAction,
    loop_state_from_workflow_state,
)


class TestTaskRubric:
    def test_build_deterministic_and_repeatable(self):
        kwargs = dict(
            description="调研 X 的技术路线",
            source_mode="web",
            min_evidence=2,
            completion_conditions=["明确技术路线对比", "给出成本数字"],
        )
        r1 = build_task_rubric("task_a", **kwargs)
        r2 = build_task_rubric("task_a", **kwargs)
        assert r1 == r2  # 确定性：同输入同 rubric
        kinds = [c.kind for c in r1.criteria]
        assert kinds == ["evidence_presence", "source_policy", "output_schema", "claim_support", "no_duplicate_query"]
        assert r1.threshold == 0.5 and r1.max_retries == 1
        evidence = next(c for c in r1.criteria if c.kind == "evidence_presence")
        assert evidence.min_evidence == 2
        claim = next(c for c in r1.criteria if c.kind == "claim_support")
        # 4 字滑窗应同时覆盖两个条件的核心词
        assert any("技术路线" in t for t in claim.required_terms)
        assert any("成本" in t for t in claim.required_terms)

    def test_no_terms_drops_claim_support_and_reweights(self):
        rubric = build_task_rubric("task_b", description="d", source_mode="web", min_evidence=1)
        kinds = [c.kind for c in rubric.criteria]
        assert "claim_support" not in kinds
        assert rubric.criteria[0].weight == 2.0
        assert len(rubric.criteria) >= 3  # source_policy/output_schema/no_dup 仍在

    def test_max_retries_is_bounded_to_one(self):
        from deepresearch_agent.agents.multi_agent.core.rubric import RUBRIC_SCHEMA_VERSION

        with pytest.raises(ValidationError):
            # 直接构造走校验（model_copy 不重校验）
            TaskRubric(
                rubric_id="r",
                task_id="t",
                version=RUBRIC_SCHEMA_VERSION,
                criteria=[RubricCriterion(criterion_id="c1", kind="output_schema", description="d")],
                max_retries=2,
            )

    def test_rubric_from_task_uses_acceptance_conditions(self):
        class FakeTask:
            task_id = "task_x"
            description = "研究检索增强技术"
            source_mode = "scholar"
            acceptance = {"completion_conditions": ["列出代表性论文", "比较训练方法"]}

        rubric = rubric_from_task(FakeTask(), min_evidence=1)
        claim = next(c for c in rubric.criteria if c.kind == "claim_support")
        assert claim.required_terms, "完成条件应产出判词"
        evidence = next(c for c in rubric.criteria if c.kind == "evidence_presence")
        assert evidence.min_evidence == 1

    def test_verdict_serialization_roundtrip(self):
        rubric = build_task_rubric("task_c", description="d", source_mode="web", min_evidence=1)
        verdict = StepVerdict(
            rubric_id=rubric.rubric_id,
            task_id=rubric.task_id,
            passed=False,
            weighted_score=0.4,
            retry_reason="证据不足，需补充一手来源",
        )
        payload = verdict.to_metadata()
        assert StepVerdict.model_validate(payload) == verdict


class TestCoreActLoopState:
    def test_continuation_pending_only_when_not_done(self):
        running = CoreActLoopState(phase="act")
        assert running.continuation_pending() is True
        finished = CoreActLoopState(phase="done", done=True)
        assert finished.continuation_pending() is False

    def test_workflow_state_roundtrip(self):
        state = CoreActLoopState(
            phase="verify",
            step_index=3,
            pending_action=PendingAction(task={"task_id": "t1"}, rubric={"rubric_id": "r1"}, attempt=2),
            no_progress_count=1,
        )
        raw = state.to_workflow_state()
        assert COREACT_STATE_KEY in raw
        assert loop_state_from_workflow_state(raw) == state

    def test_missing_or_foreign_state_returns_none(self):
        assert loop_state_from_workflow_state(None) is None
        assert loop_state_from_workflow_state({}) is None
        assert loop_state_from_workflow_state({"coreact": {"engine": "other"}}) is None
        assert loop_state_from_workflow_state({"coreact": "junk"}) is None

    def test_engine_is_literal(self):
        with pytest.raises(ValidationError):
            CoreActLoopState(engine="staged_v1")

    def test_rubric_from_task_tolerates_bad_acceptance(self):
        class NoAcceptance:
            task_id = "t1"
            description = "d"
            source_mode = "web"
            acceptance = None

        class JunkAcceptance:
            task_id = "t2"
            description = "d"
            source_mode = "web"
            acceptance = "not-a-dict"

        for task in (NoAcceptance(), JunkAcceptance()):
            rubric = rubric_from_task(task, min_evidence=1)
            kinds = [c.kind for c in rubric.criteria]
            assert "claim_support" not in kinds  # 无判词则无该 criterion
            assert rubric.criteria[0].weight == 2.0  # 权重回退到证据计数

    def test_terms_from_conditions_empty(self):
        from deepresearch_agent.agents.multi_agent.core.rubric import _terms_from_conditions

        assert _terms_from_conditions([]) == []
        assert _terms_from_conditions(["!!!", "---"]) == []  # 无可抽取词

    def test_rubric_numeric_constraints_rejected(self):
        base = dict(rubric_id="r", task_id="t")
        criterion = RubricCriterion(criterion_id="c1", kind="output_schema", description="d")
        with pytest.raises(ValidationError):
            TaskRubric(**base, criteria=[criterion], threshold=1.5)
        with pytest.raises(ValidationError):
            RubricCriterion(criterion_id="c2", kind="output_schema", description="d", weight=-1.0)

    def test_loop_state_restore_numeric_coercion(self):
        # pydantic 宽松模式：数字字符串可还原；垃圾值拒绝
        state = CoreActLoopState.model_validate(
            {"engine": "coreact_v1", "phase": "decide", "step_index": "4", "no_progress_count": "0"}
        )
        assert state.step_index == 4 and isinstance(state.step_index, int)
        with pytest.raises(ValidationError):
            CoreActLoopState.model_validate({"engine": "coreact_v1", "step_index": "abc"})
