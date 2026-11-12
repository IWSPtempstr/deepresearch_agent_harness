"""T-402 冻结的动作级契约：ActionSpec / ActionRubric / ActionResult /
ObservationSummary / ExecutionState，以及旧 TaskNode/TaskRubric/CoreActLoopState
的只读数据适配器。

设计来源：docs/specs/coreact-consolidation/design.md §3 与 output-contract.md §4。

关键不变量（由字段校验强制）：
- ActionResult 把"工具返回成功"（execution_status）与"证据满足动作目标"
  （verification.status）分开；二者可独立失败；
- ObservationSummary 是账本的有损有界摘要：findings 必须携带 evidence_ids，
  条数/长度受显式上限约束；禁止把无证据解释伪装成 finding；
- ExecutionState.next_operation 显式区分 execute/report/complete，不得只凭
  RunStatus.EXECUTING 推断下一阶段；
- 动作身份跨重试稳定：action_id 不变、attempt_id 按次数派生；active_result_refs
  只指向最新有效 attempt，旧 attempt 记录保留（T-403/404 落实）。

版本策略：schema_version 高于本实现一律拒绝（UnknownSchemaVersion，见
research_brief.py）。旧 LoopState 的升级适配是**只读**的：映射不了的字段进
`unmapped` 报告，不虚构语义、不自动重新执行。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .research_brief import (
    EvidenceAssessment,
    UnknownSchemaVersion,
    _reject_unknown_version,
)

ACTION_MODELS_SCHEMA_VERSION = 1
# RG-01：rubric 指导协议版本。v1 = 旧 TaskRubric 适配记录（宽松只读兼容），
# v2 = 新写入（严格不变量：权重上限/必需准则/权重和），旧记录仍可按 v1 读取。
RUBRIC_SCHEMA_VERSION = 2

EXECUTION_STATUS = Literal["succeeded", "failed", "cancelled", "unknown"]
VERIFICATION_STATUS = Literal["passed", "failed", "unknown", "not_run"]

# RG-01（rubric-guidance-followup §4.4）：rubric 指导协议的冻结枚举。
# source 记录准则来源（受控固定配置 / 模型生成 / 确定性回退），用于审计；
# phase 为 RubricBuilder 的确定性阶段提示；pre_action 区分动作前建议门的
# 强制级别（off=不运行，advisory=建议性，required=违反即不得派发）。
RUBRIC_SOURCE = Literal["model", "deterministic_fallback", "fixed"]
RUBRIC_PHASE = Literal["explore", "deepen", "synthesize"]
PRE_ACTION_MODE = Literal["off", "advisory", "required"]
PRE_ACTION_STATUS = Literal["pass", "fail", "unknown", "not_run"]
CHECK_KIND = Literal[
    "evidence_presence", "source_policy", "output_schema",
    "claim_support", "no_duplicate_query",
]


def pre_action_blocks(mode: str, status: str) -> bool:
    """protocol helper：required 模式下 fail/unknown 必须阻止派发（RG-03 落实）。

    advisory/off 模式不阻止（advisory unknown 只记录 degraded，不是验证通过）。
    """
    return str(mode) == "required" and str(status) in {"fail", "unknown"}


def text_sha256(text: str) -> str:
    """稳定内容哈希（canonical UTF-8；注入记录与 rubric 指纹共用）。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def canonical_sha256(payload: Any) -> str:
    """对 JSON 可序列化 payload 的确定性哈希（sort_keys + 紧凑分隔符）。"""
    rendered = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class ActionSpec(BaseModel):
    """单动作执行契约（冻结接口；可由 TaskNode 适配）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=ACTION_MODELS_SCHEMA_VERSION, ge=1)
    action_id: str
    task_id: str
    target_question_ids: list[str] = Field(default_factory=list)
    query: str
    tool: str
    depends_on: list[str] = Field(default_factory=list)
    rubric_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    budget_allocation: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        _reject_unknown_version("action_spec", value, ACTION_MODELS_SCHEMA_VERSION)
        return value

    @property
    def attempt_id(self) -> str:
        """跨恢复稳定的 attempt 身份：action_id + 次数（output-contract §4）。"""
        return f"{self.action_id}_attempt{self.attempt}"

    def next_attempt(self, *, query: str | None = None) -> ActionSpec:
        """派生定向重试动作：身份稳定、次数 +1；可选仅改写 query。"""
        payload = self.model_dump(mode="json")
        payload["attempt"] = self.attempt + 1
        if query is not None:
            payload["query"] = query
        return ActionSpec.model_validate(payload)


class GuidanceCriterion(BaseModel):
    """单条原子准则（RG-01 结构化 schema）。

    - check_kind 与 CoreActVerifier 的确定性检查种类对齐（kind 为旧 TaskRubric
      字段名的兼容别名）；
    - weight 有限非负、required 至少一条（硬门不得被清空）；
    - min_evidence/required_terms 是确定性检查参数（旧 TaskRubric dump 兼容）。
    网页/模型输出只能补充研究质量标准，不能借此删除 SourcePolicy/预算等硬门
    （由 RubricBuilder 的固定准则合并与 required 校验保证）。
    """

    # frozen：准则内容是 content_hash 的一部分——冻结保证嵌套内容在哈希计算后
    # 不可变（RG-01 review：外层 frozen 不传递，必须嵌套一并冻结）。
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    # 注意：不在字段层做长度/strip 约束——历史 v1 记录必须原样保留（空串、
    # 空白、带空格的 ID/描述均不得被改写或拒绝）；v2 新写入的空白校验在
    # ActionRubric 版本分支执行（min_length 等共享约束同样移除）。
    criterion_id: str = ""
    description: str = ""
    weight: float = Field(default=1.0, ge=0.0, allow_inf_nan=False)
    required: bool = True
    check_kind: CHECK_KIND = Field(validation_alias=AliasChoices("check_kind", "kind"))
    expected_evidence: str = ""
    min_evidence: int = Field(default=0, ge=0)
    required_terms: tuple[str, ...] = Field(default_factory=tuple)
    # 单条准则来源（混合 rubric 审计：模型补充 + 受控固定硬门）
    origin: RUBRIC_SOURCE | None = None


class ActionRubric(BaseModel):
    """动作验收 rubric（冻结接口；由 TaskRubric/确定性生成器/RubricBuilder 适配）。

    objective/expected_output/evidence_requirements 进入模型执行上下文；
    语义检查是可选判断类型（verifier_kind），不可用/非法输出记 unknown，
    不得以关键词覆盖冒充语义支持（T-405 落实）。

    RG-01 增量（output-contract §4.1）：content_hash 为准则内容的稳定指纹
    （不含 task_id 绑定），动作前建议与动作后验收必须携带同一 hash；
    source/phase/generator_model/prompt_version/trajectory_ref/hash 为来源审计
    字段；fallback_reason 记录模型生成失败回退确定性生成的原因。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=RUBRIC_SCHEMA_VERSION, ge=1)
    rubric_id: str
    task_id: str = ""
    version: int = 1
    objective: str = ""
    expected_output: str = ""
    # 哈希内容不可变（transitively frozen）：冻结字段 + tuple 序列，嵌套内容
    # 在 content_hash 计算后无法原地修改（RG-01 review finding 1）
    evidence_requirements: tuple[str, ...] = Field(default_factory=tuple)
    criteria: tuple[GuidanceCriterion, ...] = Field(min_length=1)
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    max_retries: int = Field(default=1, ge=0, le=1)
    verifier_kind: Literal["deterministic", "semantic_optional", "semantic_required"] = "deterministic"
    generator: str = "deterministic_v1"
    # RG-01：指导协议增量（均有默认值，旧记录/旧 payload 兼容）
    content_hash: str = ""
    source: RUBRIC_SOURCE = "deterministic_fallback"
    phase: RUBRIC_PHASE | None = None
    generator_model: str | None = None
    prompt_version: str | None = None
    trajectory_ref: str | None = None
    trajectory_hash: str | None = None
    pre_action: PRE_ACTION_MODE = "off"
    fallback_reason: str | None = None
    # RG-02：phase 规则版本持久化（§4.1 审计字段；旧记录为 None）
    phase_rule_version: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        _reject_unknown_version("action_rubric", value, RUBRIC_SCHEMA_VERSION)
        return value

    def _hash_payload(self) -> dict[str, Any]:
        """参与 content_hash 的稳定内容（排除绑定与审计字段）。"""
        return {
            "rubric_id": self.rubric_id,
            "version": self.version,
            "objective": self.objective,
            "expected_output": self.expected_output,
            "evidence_requirements": list(self.evidence_requirements),
            "criteria": [criterion.model_dump(mode="json") for criterion in self.criteria],
            "threshold": self.threshold,
            "verifier_kind": self.verifier_kind,
            "phase": self.phase,
            "source": self.source,
            "pre_action": self.pre_action,
        }

    def compute_content_hash(self) -> str:
        """准则内容指纹：动作前/后同 rubric 必须相同（不含 task_id 绑定）。"""
        return canonical_sha256(self._hash_payload())

    def bind(self, *, task_id: str, rubric_id: str | None = None) -> "ActionRubric":
        """把 step 级 rubric 绑定到具体动作：只改绑定字段，content_hash 不变。"""
        payload = self.model_dump(mode="json")
        payload["task_id"] = task_id
        if rubric_id:
            # 绑定不得改变准则内容；rubric_id 变更需显式重建而非绑定
            raise ValueError("bind() 不允许改写 rubric_id（同一准则身份必须稳定）")
        return ActionRubric.model_validate(payload)

    @model_validator(mode="after")
    def _finalize_content_hash(self) -> "ActionRubric":
        # v2（新写入）严格不变量；v1（旧 TaskRubric 适配/历史快照）只读兼容：
        # 历史 rubric 可能有权重 >10、超过 8 个判词或全员 optional——这些记录
        # 仍须可恢复（不因新约束被拒绝），严格性由 RubricBuilder/新写入保证。
        if self.schema_version >= 2:
            total_weight = sum(criterion.weight for criterion in self.criteria)
            if total_weight <= 0:
                raise ValueError("criteria 权重之和必须大于 0（空评测条件不得通过）")
            if not any(criterion.required for criterion in self.criteria):
                raise ValueError("至少一条准则必须 required（SourcePolicy/完成硬门不得被清空）")
            for criterion in self.criteria:
                # v2 新写入：空白归一化校验（v1 历史记录原样保留，不在此改写）
                if not str(criterion.criterion_id).strip() or not str(criterion.description).strip():
                    raise ValueError("criterion_id/description 不得为空白")
                if any(not str(term).strip() for term in criterion.required_terms):
                    raise ValueError("required_terms 不得包含空白词项")
                if criterion.weight > 10.0:
                    raise ValueError(f"criterion {criterion.criterion_id!r} 权重超过 10")
                if len(criterion.required_terms) > 8:
                    raise ValueError(f"criterion {criterion.criterion_id!r} 判词超过 8 条")
        computed = self.compute_content_hash()
        if self.content_hash and self.content_hash != computed:
            raise ValueError(
                f"content_hash 与 rubric 内容不一致（声明 {self.content_hash[:12]}…，"
                f"实算 {computed[:12]}…）"
            )
        if not self.content_hash:
            object.__setattr__(self, "content_hash", computed)
        return self


class ActionCriterionVerdict(BaseModel):
    """单条准则的判定（动作前建议与动作后验收共用形状）。"""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    status: Literal["pass", "fail", "unknown", "not_run"] = "not_run"
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class ActionVerdict(BaseModel):
    """动作验收结论（独立于执行状态；RG-01 增补 rubric 关联与逐条判定）。"""

    model_config = ConfigDict(extra="forbid")

    status: VERIFICATION_STATUS = "not_run"
    verifier_version: str = ""
    failed_criteria: list[str] = Field(default_factory=list)
    retry_reason: str | None = None
    weighted_score: float = Field(default=0.0, ge=0.0, le=1.0)
    # RG-01：动作前后必须可核对同一 rubric 身份
    rubric_id: str = ""
    rubric_hash: str = ""
    criteria: list[ActionCriterionVerdict] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    degraded_reason: str | None = None


class PreActionVerification(BaseModel):
    """动作前建议门判定（RG-03 使用；RG-01 只冻结形状）。

    status：pass=全部已判定准则通过；fail=存在失败准则；unknown=无失败但存在
    无法事前判定的准则（或建议器不可用）；not_run=未运行（默认/旧记录）。
    degraded=true 表示建议性降级继续（不是验证通过）；required 模式下
    fail/unknown 均不得派发。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=ACTION_MODELS_SCHEMA_VERSION, ge=1)
    status: PRE_ACTION_STATUS = "not_run"
    rubric_id: str = ""
    rubric_hash: str = ""
    verifier: str = ""
    verifier_version: str = ""
    criteria: list[ActionCriterionVerdict] = Field(default_factory=list)
    advice: list[str] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    proposal_revision_used: bool = False

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        _reject_unknown_version("pre_action_verification", value, ACTION_MODELS_SCHEMA_VERSION)
        return value


class RubricInjectionRecord(BaseModel):
    """Rubric 注入证据（§4.4：不能只写 injected=true）。

    记录实际注入目标的 messages 哈希/引用，用于证明注入生效而非声明式标记。
    """

    model_config = ConfigDict(extra="forbid")

    injection_id: str = Field(min_length=1)
    rubric_id: str = Field(min_length=1)
    rubric_hash: str = Field(min_length=1)
    action_id: str = ""
    targets: list[Literal["controller", "worker"]] = Field(min_length=1)
    messages_hash: str = Field(pattern=r"^[0-9a-f]{16,128}$")
    messages_ref: str | None = None
    step_index: int = Field(default=0, ge=0)


class ObservationFinding(BaseModel):
    """一条带证据引用的发现。"""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    question_ids: list[str] = Field(default_factory=list)

    @field_validator("evidence_ids")
    @classmethod
    def _non_blank_ids(cls, value: list[str]) -> list[str]:
        if any(not str(item).strip() for item in value):
            raise ValueError("evidence_ids 不得包含空白 ID")
        return value


class ObservationSummary(BaseModel):
    """有界观察摘要（Controller 的唯一动作后输入，账本的有损视图）。

    - findings 每条必须携带当前 Run 的真实 evidence_ids（合并时由 ActionRunner
      做账本校验；此处只强制"有引用"）；
    - 无证据的解释/猜测不得进入 findings，应放入 unanswered_questions；
    - 条数与字符上限可配置；硬上限（防 prompt 膨胀）在模型层强制。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=ACTION_MODELS_SCHEMA_VERSION, ge=1)
    action_id: str
    attempt_id: str
    findings: list[ObservationFinding] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    suggested_followups: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        _reject_unknown_version("observation_summary", value, ACTION_MODELS_SCHEMA_VERSION)
        return value

    @model_validator(mode="after")
    def _bounded(self) -> ObservationSummary:
        if len(self.findings) > MAX_FINDINGS:
            raise ValueError(f"findings {len(self.findings)} 超过硬上限 {MAX_FINDINGS}")
        for finding in self.findings:
            if len(finding.statement) > MAX_FINDING_CHARS:
                raise ValueError(f"finding 长度 {len(finding.statement)} 超过硬上限 {MAX_FINDING_CHARS}")
        overflow = [
            name
            for name, items in (
                ("contradictions", self.contradictions),
                ("unanswered_questions", self.unanswered_questions),
                ("suggested_followups", self.suggested_followups),
            )
            if len(items) > MAX_LIST_ITEMS
        ]
        if overflow:
            raise ValueError(f"{'/'.join(overflow)} 超过硬上限 {MAX_LIST_ITEMS}")
        return self

    @property
    def evidence_ids(self) -> list[str]:
        """本摘要引用的全部证据 ID（保持出现顺序去重）。"""
        seen: dict[str, None] = {}
        for finding in self.findings:
            for evidence_id in finding.evidence_ids:
                seen.setdefault(evidence_id, None)
        return list(seen)


class ActionResult(BaseModel):
    """动作结果（冻结接口；JSON 可序列化，output-contract §4 为单一权威）。

    execution_status 与 verification 可独立失败；失败尝试保留其记录与费用，
    active attempt 由 ExecutionState.active_result_refs 选择。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=ACTION_MODELS_SCHEMA_VERSION, ge=1)
    run_id: str
    action_id: str
    task_id: str
    attempt_id: str
    attempt: int = Field(default=1, ge=1)
    execution_status: EXECUTION_STATUS = "unknown"
    # 动作后验收（既有字段；RG-01 起携带 rubric_id/hash 与逐条判定）
    verification: ActionVerdict = Field(default_factory=ActionVerdict)
    # 动作前建议（RG-01 冻结；缺省 not_run，旧记录缺该字段即解释为未运行）
    pre_action_verification: PreActionVerification = Field(default_factory=PreActionVerification)
    evidence_refs: list[str] = Field(default_factory=list)
    observation: ObservationSummary | None = None
    record_refs: list[str] = Field(default_factory=list)
    delta_ref: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        _reject_unknown_version("action_result", value, ACTION_MODELS_SCHEMA_VERSION)
        return value

    @model_validator(mode="after")
    def _identity_consistent(self) -> ActionResult:
        if self.attempt_id != f"{self.action_id}_attempt{self.attempt}":
            raise ValueError(
                f"attempt_id {self.attempt_id!r} 与 action_id/attempt "
                f"({self.action_id!r}/{self.attempt}) 派生关系不一致"
            )
        if self.observation is not None and self.observation.action_id != self.action_id:
            raise ValueError("observation.action_id 与结果归属动作不一致")
        return self


class ExecutionState(BaseModel):
    """coreact 执行状态（冻结接口；checkpoint 持久化的唯一新格式）。

    - next_operation 显式区分执行中与完成（execute/report/complete）；
    - inflight_actions 保存**整个 in-flight 批次**，inflight_rubrics 逐动作携带
      其验收 rubric（design §6：派发前全部动作、rubric、attempt、next_operation
      一并落盘，T-404 恢复不丢副动作与重试语义）；
    - retry_reasons: action_id → 上一次验证失败的定向反馈（attempt=2 时注入执行）；
    - active_result_refs: action_id → 最新有效 attempt_id（仅引用已提交动作）；
      失败历史不删除。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=ACTION_MODELS_SCHEMA_VERSION, ge=1)
    implementation_version: str = "coreact_v2"
    next_operation: Literal["execute", "report", "complete"] = "execute"
    inflight_actions: list[ActionSpec] = Field(default_factory=list)
    inflight_rubrics: dict[str, ActionRubric] = Field(default_factory=dict)
    committed_action_ids: list[str] = Field(default_factory=list)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    retry_reasons: dict[str, str] = Field(default_factory=dict)
    active_result_refs: dict[str, str] = Field(default_factory=dict)
    observations: list[ObservationSummary] = Field(default_factory=list)
    assessments: list[EvidenceAssessment] = Field(default_factory=list)
    # RG-01：rubric 注入证据（有界；每步最多一条 controller + 每条动作 worker）
    rubric_injections: list[RubricInjectionRecord] = Field(default_factory=list)

    @field_validator("rubric_injections")
    @classmethod
    def _bounded_injections(cls, value: list[RubricInjectionRecord]) -> list[RubricInjectionRecord]:
        if len(value) > 32:
            raise ValueError("rubric_injections 超过硬上限 32 条")
        return value
    # 只读升级适配时无法映射的旧字段清单（audit-only；不参与新路径执行）
    unmapped: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        _reject_unknown_version("execution_state", value, ACTION_MODELS_SCHEMA_VERSION)
        return value

    @field_validator("committed_action_ids")
    @classmethod
    def _unique_committed(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("committed_action_ids 必须唯一（逐动作提交不得重复核销）")
        return value

    @field_validator("inflight_actions")
    @classmethod
    def _unique_inflight(cls, value: list[ActionSpec]) -> list[ActionSpec]:
        ids = [item.action_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("inflight_actions 的 action_id 必须唯一")
        return value

    @model_validator(mode="after")
    def _bookkeeping_consistent(self) -> ExecutionState:
        inflight_ids = {item.action_id for item in self.inflight_actions}
        # 已提交动作不得同时处于 in-flight（重复核销保护）；
        # active 引用只能指向已提交动作（结果在提交时落账）。
        overlap = inflight_ids & set(self.committed_action_ids)
        if overlap:
            raise ValueError(f"动作同时处于 in-flight 与 committed: {sorted(overlap)}")
        known_ids = inflight_ids | set(self.committed_action_ids)
        ghost_refs = set(self.active_result_refs) - set(self.committed_action_ids)
        if ghost_refs:
            raise ValueError(f"active_result_refs 引用了未提交动作: {sorted(ghost_refs)}")
        for action_id, attempt_id in self.active_result_refs.items():
            if not _attempt_id_matches(action_id, attempt_id):
                raise ValueError(
                    f"active_result_refs[{action_id!r}]={attempt_id!r} 与 attempt_id 派生规则不符"
                )
        # rubric/retry 账目同样不允许孤儿键（既不在 in-flight 也未提交）
        orphan_rubrics = set(self.inflight_rubrics) - known_ids
        if orphan_rubrics:
            raise ValueError(f"inflight_rubrics 携带未知动作: {sorted(orphan_rubrics)}")
        orphan_reasons = set(self.retry_reasons) - known_ids
        if orphan_reasons:
            raise ValueError(f"retry_reasons 携带未知动作: {sorted(orphan_reasons)}")
        return self

    def record_commit(self, action_id: str) -> ExecutionState:
        """逐动作提交：从 in-flight 移除并记入已提交集合。

        幂等（重复提交同一已提交动作返回原状态）；未知动作/未派发动作提交是
        账目错误，拒绝而非静默入账。
        """
        if action_id in self.committed_action_ids:
            return self
        if action_id not in {item.action_id for item in self.inflight_actions}:
            raise ValueError(f"提交了未派发的动作: {action_id!r}")
        payload = self.model_dump(mode="json")
        payload["inflight_actions"] = [
            item for item in payload["inflight_actions"] if item["action_id"] != action_id
        ]
        payload["inflight_rubrics"] = {
            key: value for key, value in payload["inflight_rubrics"].items() if key != action_id
        }
        payload["retry_reasons"] = {
            key: value for key, value in payload["retry_reasons"].items() if key != action_id
        }
        payload["committed_action_ids"] = [*payload["committed_action_ids"], action_id]
        return ExecutionState.model_validate(payload)


# ---------- 硬上限（模型层强制；可配置上限在 builder 层） ----------

MAX_FINDINGS = 12
MAX_FINDING_CHARS = 800
MAX_LIST_ITEMS = 12
# T-405：循环状态内保留的观察/评估条数上限（有界上下文，防 prompt 膨胀）
MAX_OBSERVATIONS_KEPT = 12


def _attempt_id_matches(action_id: str, attempt_id: str) -> bool:
    """active_result_refs 值必须满足 `<action_id>_attempt<N>` 派生规则。"""
    import re

    return re.fullmatch(f"{re.escape(action_id)}_attempt\\d+", attempt_id) is not None


def build_observation_summary(
    *,
    action_id: str,
    attempt_id: str,
    findings: list[dict[str, Any]] | list[ObservationFinding],
    contradictions: list[str] | None = None,
    unanswered_questions: list[str] | None = None,
    suggested_followups: list[str] | None = None,
    max_findings: int = 8,
    max_chars: int = 500,
) -> ObservationSummary:
    """确定性抽取的观察摘要 builder（T-405 的模型化摘要将在此接口上扩展）。

    - 先按 max_findings 截取再校验：被截掉的候选项不参与校验（输入顺序即优先级）；
    - 逐条截断到 max_chars（确定性边界，不做语义压缩）；
    - 无 evidence_ids 的候选 finding 拒绝并抛 ValidationError（无证据解释不得
      伪装成发现；调用方应将其内容放入 unanswered_questions）。
    """
    normalized: list[ObservationFinding] = []
    for item in findings[:max(0, max_findings)]:
        finding = item if isinstance(item, ObservationFinding) else ObservationFinding.model_validate(item)
        statement = finding.statement if len(finding.statement) <= max_chars else finding.statement[: max_chars - 1] + "…"
        normalized.append(
            ObservationFinding(
                statement=statement,
                evidence_ids=list(dict.fromkeys(finding.evidence_ids)),
                question_ids=list(finding.question_ids),
            )
        )
    return ObservationSummary(
        action_id=action_id,
        attempt_id=attempt_id,
        findings=normalized,
        contradictions=[str(item)[:MAX_FINDING_CHARS] for item in (contradictions or [])][:MAX_LIST_ITEMS],
        unanswered_questions=[str(item)[:MAX_FINDING_CHARS] for item in (unanswered_questions or [])][:MAX_LIST_ITEMS],
        suggested_followups=[str(item)[:MAX_FINDING_CHARS] for item in (suggested_followups or [])][:MAX_LIST_ITEMS],
    )


# ---------- 旧 payload 的只读数据适配器 ----------


def action_spec_from_task_node(
    task: Any,
    *,
    run_id: str = "",
    target_question_ids: list[str] | None = None,
    rubric_id: str | None = None,
    attempt: int = 1,
    budget_allocation: dict[str, Any] | None = None,
) -> ActionSpec:
    """TaskNode → ActionSpec（只读适配；不改变原 TaskNode）。

    action_id 派生自 task_id（跨重试/恢复稳定）。query 取 parameters 中的
    query/search_query/question，否则回退 description（与 coreact 支持层同规则）。
    """
    from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskNode

    if not isinstance(task, TaskNode):
        task = TaskNode.model_validate(task)
    params = task.parameters or {}
    query = next(
        (str(params[key]).strip() for key in ("query", "search_query", "question") if isinstance(params.get(key), str) and params[key].strip()),
        str(task.description or "").strip(),
    )
    return ActionSpec(
        action_id=f"action_{task.task_id}",
        task_id=task.task_id,
        target_question_ids=list(target_question_ids or []),
        query=query,
        tool=str(task.task_type),
        depends_on=list(task.depends_on or []),
        rubric_id=rubric_id,
        attempt=attempt,
        budget_allocation=dict(budget_allocation or {}),
    )


def action_rubric_from_task_rubric(rubric: Any, *, objective: str = "") -> ActionRubric:
    """TaskRubric → ActionRubric（只读适配；criterion 结构原样携带）。

    旧 TaskRubric.semantic_check=True 是"预算门控内的可选语义检查"，
    映射为 verifier_kind=semantic_optional，不丢失语义支持标记。
    """
    from deepresearch_agent.agents.multi_agent.core.rubric import TaskRubric

    if not isinstance(rubric, TaskRubric):
        rubric = TaskRubric.model_validate(rubric)
    return ActionRubric(
        # 旧 TaskRubric 适配属于只读兼容路径：按 v1 宽松校验（不因新约束拒绝历史）
        schema_version=1,
        rubric_id=rubric.rubric_id,
        task_id=rubric.task_id,
        version=rubric.version,
        objective=objective,
        criteria=[criterion.model_dump(mode="json") for criterion in rubric.criteria],
        threshold=rubric.threshold,
        max_retries=rubric.max_retries,
        verifier_kind="semantic_optional" if rubric.semantic_check else "deterministic",
        generator=rubric.generator,
    )


class LegacyStateUnsupported(ValueError):
    """旧状态携带高于本实现的版本或无法安全映射的结构；拒绝转换（不清空重跑）。"""


def execution_state_from_coreact_loop(loop_state: Any, *, run_id: str = "") -> ExecutionState:
    """CoreActLoopState → ExecutionState（只读升级适配；不启动旧引擎）。

    映射规则：
    - engine != coreact_v1、version 高于实现或结构畸形 → LegacyStateUnsupported（显式拒绝）；
    - done（含 stop_reason）→ next_operation=report（旧 done 语义是"待报告"，
      complete 只能由最终 Contract 判定）；未 done → execute；
    - pending_action → inflight_actions 单元素批次 + inflight_rubrics/retry_reasons
      （attempt 原样保留，重试反馈不丢）；
    - ContextState.uncertain → claim 级 unresolved assessment（可追溯字段缺省
      status=unresolved，无需 check 归因）；trusted/untrusted ID 列表进 unmapped
      （assessment 是 claim 级，不虚构 claim 归属）；
    - decisions_used/step_index/failure_reasons 无对应语义 → unmapped 记录。
    """
    from deepresearch_agent.harness.coreact_state import (
        COREACT_ENGINE,
        COREACT_STATE_VERSION,
        CoreActLoopState,
    )

    if not isinstance(loop_state, CoreActLoopState):
        # 畸形/未知引擎的旧 payload 在 model_validate 即被 Literal 拒绝；
        # 统一转为显式 unsupported，调用方按"待人工处理"分流而非清空重跑。
        try:
            loop_state = CoreActLoopState.model_validate(loop_state)
        except ValidationError as exc:
            raise LegacyStateUnsupported(f"旧状态无法安全解读: {exc}") from exc
    if loop_state.engine != COREACT_ENGINE:
        raise LegacyStateUnsupported(f"未知引擎 {loop_state.engine!r}，拒绝适配（不清空重跑）")
    try:
        _reject_unknown_version("coreact_loop_state", loop_state.version, COREACT_STATE_VERSION)
    except UnknownSchemaVersion as exc:
        raise LegacyStateUnsupported(str(exc)) from exc
    if loop_state.version < 1:
        raise LegacyStateUnsupported(f"coreact_loop_state.version={loop_state.version} 非法")

    unmapped: dict[str, Any] = {}
    inflight: list[ActionSpec] = []
    inflight_rubrics: dict[str, ActionRubric] = {}
    retry_reasons: dict[str, str] = {}
    # T-404：整批次 in-flight（pending_batch 优先；旧单动作快照经 inflight_items
    # 回退）——迁移视图不得静默丢弃次要动作。
    for pending in loop_state.inflight_items():
        # pending 内的 task/rubric 是跨版本写入的原始 dict（见 coreact_state）：
        # 畸形 pending 与畸形外层一致地显式拒绝，交"待人工处理"分流。
        try:
            spec = action_spec_from_task_node(
                pending.task,
                rubric_id=str(pending.rubric.get("rubric_id") or "") or None,
                attempt=pending.attempt,
            )
            inflight_rubrics[spec.action_id] = action_rubric_from_task_rubric(pending.rubric)
        except ValidationError as exc:
            raise LegacyStateUnsupported(f"旧 pending_action 无法安全解读: {exc}") from exc
        inflight.append(spec)
        if pending.retry_reason:
            retry_reasons[spec.action_id] = pending.retry_reason
    context_state = loop_state.context_state
    if context_state.trusted_ids or context_state.untrusted_ids:
        unmapped["context_state_evidence_ids"] = {
            "trusted_ids": list(context_state.trusted_ids),
            "untrusted_ids": list(context_state.untrusted_ids),
            "reason": "旧状态为证据级分层；assessment 为 claim 级，不虚构 claim 归属",
        }
    assessments = [
        EvidenceAssessment(
            claim_id=f"uncertain_{index}",
            claim_text=str(item.get("term", "")),
            status="unresolved",
            reason=str(item.get("reason", "")),
            next_action=str(item.get("next_action", "")),
            updated_step=int(item.get("updated_step", context_state.updated_step) or context_state.updated_step),
        )
        for index, item in enumerate(context_state.uncertain or [])
    ]

    # 旧 done 语义是"研究循环结束、待报告"（stop_reason 记录结束原因），
    # 报告是否生成/验收由 Harness 判定——legacy 状态不可推导 complete。
    next_operation: str = "report" if loop_state.done else "execute"
    unmapped.setdefault("legacy_counters", {
        "step_index": loop_state.step_index,
        "decisions_used": loop_state.decisions_used,
        "no_progress_count": loop_state.no_progress_count,
        "failure_reasons": list(loop_state.failure_reasons),
        "phase": loop_state.phase,
        "stop_reason": loop_state.stop_reason,
    })
    return ExecutionState(
        implementation_version=f"adapter_from_{loop_state.engine}",
        next_operation=next_operation,  # type: ignore[arg-type]
        inflight_actions=inflight,
        inflight_rubrics=inflight_rubrics,
        retry_reasons=retry_reasons,
        assessments=assessments,
        unmapped=unmapped,
    )
