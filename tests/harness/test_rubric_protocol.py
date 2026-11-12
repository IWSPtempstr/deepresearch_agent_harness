"""RG-01：rubric 指导协议冻结测试（rubric-guidance-followup §4.4）。

验收（RG-01）：schema roundtrip、非法权重/空条件、旧记录 not_run、
required/advisory 与 pre/post 判定分离；无运行外部仓库代码。
"""
import json

import pytest
from pydantic import ValidationError

from deepresearch_agent.agents.multi_agent.core.rubric import build_task_rubric
from deepresearch_agent.harness.action_models import (
    RUBRIC_SCHEMA_VERSION,
    ActionRubric,
    ActionVerdict,
    ActionResult,
    ExecutionState,
    GuidanceCriterion,
    PreActionVerification,
    RubricInjectionRecord,
    action_rubric_from_task_rubric,
)


def make_rubric(**overrides):
    payload = {
        "schema_version": RUBRIC_SCHEMA_VERSION,
        "rubric_id": "rubric_step_1_v1",
        "version": 1,
        "objective": "为当前研究方向补充可验证证据",
        "expected_output": "带账本证据引用的研究发现",
        "criteria": [
            {
                "criterion_id": "c_evidence",
                "description": "至少一条账本内新证据",
                "weight": 1.5,
                "required": True,
                "check_kind": "evidence_presence",
                "min_evidence": 1,
            },
            {
                "criterion_id": "c_source",
                "description": "工具符合冻结来源白名单",
                "weight": 1.0,
                "required": True,
                "check_kind": "source_policy",
            },
        ],
        "source": "deterministic_fallback",
        "phase": "explore",
    }
    payload.update(overrides)
    return ActionRubric.model_validate(payload)


# ---------- schema roundtrip ----------

def test_action_rubric_roundtrip_preserves_hash_and_provenance():
    rubric = make_rubric(
        generator_model="m-demo", prompt_version="rubric_prompt_v1",
        trajectory_ref="trajectory:run-1:step-2", trajectory_hash="a" * 64,
        pre_action="advisory",
    )
    restored = ActionRubric.model_validate(json.loads(json.dumps(rubric.model_dump(mode="json"))))
    assert restored == rubric
    assert restored.content_hash == rubric.content_hash
    assert restored.phase == "explore"
    assert restored.source == "deterministic_fallback"
    assert restored.pre_action == "advisory"


def test_content_hash_covers_criteria_but_not_task_binding():
    base = make_rubric()
    bound = base.bind(task_id="task_1")
    assert bound.task_id == "task_1"
    assert bound.content_hash == base.content_hash  # 绑定不改变准则内容指纹
    changed = make_rubric(threshold=0.6)
    assert changed.content_hash != base.content_hash
    changed_criteria = make_rubric(criteria=[
        {"criterion_id": "c_evidence", "description": "更严格", "weight": 2.0,
         "check_kind": "evidence_presence", "min_evidence": 2},
    ])
    assert changed_criteria.content_hash != base.content_hash


def test_bind_rejects_rubric_id_rewrite():
    with pytest.raises(ValueError, match="rubric_id"):
        make_rubric().bind(task_id="t1", rubric_id="other")


# ---------- 非法权重与空条件 ----------

@pytest.mark.parametrize("weight", [-0.1, float("nan"), float("inf")])
def test_negative_or_non_finite_weight_rejected(weight):
    with pytest.raises(ValidationError):
        GuidanceCriterion(criterion_id="c", description="d", check_kind="evidence_presence", weight=weight)


def test_empty_criteria_rejected():
    with pytest.raises(ValidationError):
        make_rubric(criteria=[])


def test_zero_total_weight_rejected():
    with pytest.raises(ValidationError, match="权重之和"):
        make_rubric(criteria=[
            {"criterion_id": "c", "description": "d", "check_kind": "output_schema", "weight": 0.0},
        ])


def test_no_required_criterion_rejected():
    with pytest.raises(ValidationError, match="required"):
        make_rubric(criteria=[
            {"criterion_id": "c", "description": "d", "check_kind": "output_schema",
             "weight": 1.0, "required": False},
        ])


def test_empty_description_rejected_at_v2_level():
    """空串描述：v1 历史读取保留；v2 新写入拒绝（空白校验在版本分支）。"""
    with pytest.raises(ValidationError, match="不得为空白"):
        ActionRubric(schema_version=2, rubric_id="r", task_id="t", criteria=[
            {"criterion_id": "c", "description": "", "check_kind": "output_schema"},
        ])


def test_declared_hash_mismatch_rejected():
    rubric = make_rubric()
    payload = rubric.model_dump(mode="json")
    payload["content_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="content_hash"):
        ActionRubric.model_validate(payload)


def test_blank_required_term_rejected_at_v2_rubric_level():
    """v2 新写入拒绝空白判词；v1 历史记录原样保留（见版本化兼容测试）。"""
    with pytest.raises(ValidationError, match="required_terms"):
        ActionRubric(schema_version=2, rubric_id="r", task_id="t", criteria=[
            {"criterion_id": "c", "description": "d", "check_kind": "claim_support",
             "required_terms": ["ok", "  "]},
        ])
    # v1 允许并原样保留（历史记录语义不改写）
    legacy = ActionRubric.model_validate({
        "schema_version": 1, "rubric_id": "lr", "task_id": "t",
        "criteria": [{"criterion_id": "c", "description": "d", "kind": "claim_support",
                      "required_terms": [" raw "]}],
    })
    assert legacy.criteria[0].required_terms == (" raw ",)


# ---------- 旧 TaskRubric 兼容 ----------

def test_legacy_task_rubric_adapts_to_guidance_schema():
    legacy = build_task_rubric(
        "task_1", description="检索增强 基础概念", source_mode="web",
        min_evidence=1, completion_conditions=["检索增强是一种技术路线"],
        semantic_check=True,
    )
    adapted = action_rubric_from_task_rubric(legacy, objective="获取事实")
    assert adapted.rubric_id == legacy.rubric_id
    assert len(adapted.criteria) == len(legacy.criteria)
    # 旧字段 kind → check_kind；min_evidence/required_terms 保留
    by_id = {criterion.criterion_id: criterion for criterion in adapted.criteria}
    evidence = by_id[f"task_1_evidence_presence"]
    assert evidence.check_kind == "evidence_presence"
    assert evidence.min_evidence == 1
    claim = by_id.get("task_1_claim_support")
    assert claim is not None and claim.required_terms
    # 旧 rubric 无前向字段 → 默认来源与 not_run 语义
    assert adapted.source == "deterministic_fallback"
    assert adapted.phase is None
    assert adapted.content_hash


# ---------- ActionResult：旧记录 not_run 与 pre/post 独立 ----------

def test_action_result_without_pre_field_defaults_to_not_run():
    legacy_payload = {
        "run_id": "run_1", "action_id": "action_t1", "task_id": "t1",
        "attempt_id": "action_t1_attempt1", "execution_status": "succeeded",
        "verification": {"status": "passed", "verifier_version": "v1"},
    }
    result = ActionResult.model_validate(legacy_payload)
    assert result.pre_action_verification.status == "not_run"
    assert result.pre_action_verification.rubric_hash == ""
    # 旧记录不因缺字段被改写判定
    assert result.verification.status == "passed"


def test_pre_and_post_verdicts_can_diverge():
    pre = PreActionVerification(
        status="fail", rubric_id="r1", rubric_hash="h" * 64,
        verifier="pre_advisor", verifier_version="v1",
        criteria=[{"criterion_id": "c_source", "status": "fail", "reason": "工具越权"}],
        advice=["改用 web_search"], degraded=False,
    )
    result = ActionResult(
        run_id="run_1", action_id="action_t1", task_id="t1",
        attempt_id="action_t1_attempt1", execution_status="succeeded",
        verification=ActionVerdict(status="passed", rubric_id="r1", rubric_hash="h" * 64),
        pre_action_verification=pre,
    )
    assert result.pre_action_verification.status == "fail"
    assert result.verification.status == "passed"  # 前后独立，不混同


def test_pre_action_status_enum_validation():
    with pytest.raises(ValidationError):
        PreActionVerification(status="maybe")


def test_unknown_pre_action_version_rejected():
    with pytest.raises(ValidationError, match="高于实现支持"):
        PreActionVerification.model_validate({"schema_version": 99})


# ---------- 注入记录：哈希/目标必需 ----------

def test_injection_record_requires_messages_hash_and_targets():
    record = RubricInjectionRecord(
        injection_id="inj_1", rubric_id="r1", rubric_hash="h" * 64,
        action_id="action_t1", targets=["controller"], messages_hash="a" * 64, step_index=2,
    )
    restored = RubricInjectionRecord.model_validate(json.loads(json.dumps(record.model_dump(mode="json"))))
    assert restored == record
    with pytest.raises(ValidationError):
        RubricInjectionRecord(
            injection_id="inj_2", rubric_id="r1", rubric_hash="h" * 64,
            targets=[], messages_hash="a" * 64,
        )
    with pytest.raises(ValidationError):
        RubricInjectionRecord(
            injection_id="inj_3", rubric_id="r1", rubric_hash="h" * 64,
            targets=["controller"], messages_hash="short",
        )


# ---------- ExecutionState 旧快照兼容 ----------

def test_execution_state_without_injections_still_validates():
    legacy = {"next_operation": "execute"}
    state = ExecutionState.model_validate(legacy)
    assert state.rubric_injections == []
    state_with = ExecutionState(
        rubric_injections=[RubricInjectionRecord(
            injection_id="inj_1", rubric_id="r1", rubric_hash="h" * 64,
            action_id="a1", targets=["worker"], messages_hash="a" * 64,
        )],
    )
    restored = ExecutionState.model_validate(json.loads(json.dumps(state_with.model_dump(mode="json"))))
    assert len(restored.rubric_injections) == 1


# ---------- source/phase/pre_action 枚举 ----------

def test_source_phase_and_pre_action_enums_are_frozen():
    with pytest.raises(ValidationError):
        make_rubric(source="webpage_claimed")
    with pytest.raises(ValidationError):
        make_rubric(phase="training")
    with pytest.raises(ValidationError):
        make_rubric(pre_action="mandatory")
    # 固定准则来源受控
    assert make_rubric(source="fixed", phase="deepen", pre_action="required").source == "fixed"


# ---------- 版本化只读兼容（RG-01 review 修复） ----------

def test_legacy_v1_rubric_reads_leniently_while_v2_is_strict():
    """历史 v1 记录（权重 >10、判词 >8、全员 optional）必须仍可恢复；
    新写入 v2 才强制严格不变量。"""
    legacy = {
        "schema_version": 1,
        "rubric_id": "legacy_rubric",
        "task_id": "t1",
        "criteria": [{
            "criterion_id": "c1", "description": "历史准则",
            "weight": 11.0, "required": False, "check_kind": "claim_support",
            "required_terms": [f"词{i}" for i in range(9)],
        }],
    }
    restored = ActionRubric.model_validate(legacy)
    assert restored.schema_version == 1
    assert restored.criteria[0].weight == 11.0
    assert len(restored.criteria[0].required_terms) == 9
    # v1 读取不得改写准则身份/匹配语义（不 strip、不拒绝）
    raw = ActionRubric.model_validate({
        "schema_version": 1, "rubric_id": "lr2", "task_id": "t",
        "criteria": [{"criterion_id": " c ", "description": " d ", "kind": "claim_support",
                      "required_terms": [" term "]}],
    })
    assert raw.criteria[0].criterion_id == " c "
    assert raw.criteria[0].description == " d "
    assert raw.criteria[0].required_terms == (" term ",)
    # v1 空串 ID/描述历史上被 dict/RubricCriterion 接受：读取路径原样保留
    empty = ActionRubric.model_validate({
        "schema_version": 1, "rubric_id": "le", "task_id": "t",
        "criteria": [{"criterion_id": "", "description": "", "kind": "claim_support"}],
    })
    assert empty.criteria[0].criterion_id == ""
    assert empty.criteria[0].description == ""
    # v2 新写入拒绝空串（strip 校验在版本分支）
    with pytest.raises(ValidationError, match="不得为空白"):
        ActionRubric.model_validate({
            "schema_version": 2, "rubric_id": "le2", "task_id": "t",
            "criteria": [{"criterion_id": "", "description": "", "check_kind": "output_schema"}],
        })
    with pytest.raises(ValidationError, match="权重超过 10"):
        ActionRubric.model_validate({
            **legacy, "schema_version": 2,
            "criteria": [{**legacy["criteria"][0], "required": True}],
        })
    with pytest.raises(ValidationError, match="required"):
        ActionRubric.model_validate({
            "schema_version": 2, "rubric_id": "r2", "task_id": "t1",
            "criteria": [{"criterion_id": "c", "description": "d",
                          "check_kind": "output_schema", "weight": 1.0, "required": False}],
        })


def test_whitespace_only_criterion_text_rejected_at_v2_level():
    with pytest.raises(ValidationError, match="不得为空白"):
        ActionRubric(schema_version=2, rubric_id="r", task_id="t", criteria=[
            {"criterion_id": "  ", "description": "d", "check_kind": "output_schema"},
        ])
    with pytest.raises(ValidationError, match="不得为空白"):
        ActionRubric(schema_version=2, rubric_id="r", task_id="t", criteria=[
            {"criterion_id": "c", "description": " \t ", "check_kind": "output_schema"},
        ])


def test_rubric_hashed_content_is_transitively_immutable():
    """RG-01 review：嵌套准则与序列在 content_hash 计算后不可原地修改。"""
    rubric = make_rubric()
    with pytest.raises(Exception):  # frozen 嵌套模型：字段不可赋值
        rubric.criteria[0].min_evidence = 99
    with pytest.raises(Exception):  # tuple：不可清空
        object.__getattribute__(rubric, "criteria").clear()
    with pytest.raises(Exception):
        rubric.evidence_requirements.append("extra")
    # 修改尝试后哈希保持一致（未被绕过）
    assert rubric.content_hash == ActionRubric.model_validate(
        json.loads(json.dumps(rubric.model_dump(mode="json")))
    ).content_hash


# ---------- 注入证据：hex 格式与有界 ----------

def test_injection_messages_hash_must_be_hex():
    with pytest.raises(ValidationError):
        RubricInjectionRecord(
            injection_id="inj", rubric_id="r1", rubric_hash="h" * 64,
            targets=["controller"], messages_hash="z" * 64,
        )


def test_rubric_injections_are_bounded():
    record = RubricInjectionRecord(
        injection_id="inj", rubric_id="r1", rubric_hash="h" * 64,
        targets=["controller"], messages_hash="a" * 64,
    )
    with pytest.raises(ValidationError, match="硬上限"):
        ExecutionState(rubric_injections=[record] * 33)


# ---------- required / advisory：unknown 的放行规则（协议层） ----------

def test_pre_action_blocks_only_required_fail_or_unknown():
    from deepresearch_agent.harness.action_models import pre_action_blocks

    assert pre_action_blocks("required", "fail") is True
    assert pre_action_blocks("required", "unknown") is True
    assert pre_action_blocks("required", "pass") is False
    assert pre_action_blocks("required", "not_run") is False
    assert pre_action_blocks("advisory", "fail") is False
    assert pre_action_blocks("advisory", "unknown") is False
    assert pre_action_blocks("off", "fail") is False


def test_required_unknown_roundtrip_preserved():
    """required rubric 的 unknown 判定必须原样保留（运行期阻止在 RG-03）。"""
    rubric = make_rubric(pre_action="required")
    verdict = PreActionVerification(
        status="unknown", rubric_id=rubric.rubric_id, rubric_hash=rubric.content_hash,
        verifier="pre_advisor", verifier_version="v1",
        criteria=[{"criterion_id": "c_evidence", "status": "unknown", "reason": "动作前无法判定"}],
        degraded=False,
    )
    restored = PreActionVerification.model_validate(json.loads(json.dumps(verdict.model_dump(mode="json"))))
    assert restored.status == "unknown"
    assert restored.degraded is False
    from deepresearch_agent.harness.action_models import pre_action_blocks

    assert pre_action_blocks(rubric.pre_action, restored.status) is True
