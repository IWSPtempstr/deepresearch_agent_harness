"""T-402 冻结的研究契约：ResearchBrief / EvidenceAssessment / 模型角色规则。

设计来源：docs/specs/coreact-consolidation/design.md §3 与 output-contract.md。
本模块是唯一 coreact 路径的输入契约层：

- ResearchBrief 冻结研究目标、问题、输出要求与**唯一**来源；需求修订必须显式
  版本化，且不得通过修订扩大来源；
- EvidenceAssessment 是 claim 级检查结论：`supported` 只表示"某项检查支持指定
  claim"，不等于独立事实认证（文案与字段命名不得暗示事实真值）；
- 模型角色冻结规则（design §8）：model_snapshot.role_models 冻结实际
  brief/controller/reporter/verifier 身份；旧 `planner` key 在适配层映射为
  `brief`，显式 `brief` key 优先，不现算覆盖历史模型选择。

版本策略：schema_version 高于本实现的请求/状态一律拒绝（UnknownSchemaVersion），
不允许把未知版本静默当作当前版本或空状态处理。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import SourceMode

RESEARCH_BRIEF_SCHEMA_VERSION = 2


class UnknownSchemaVersion(ValueError):
    """Payload 声明的 schema 版本高于本实现（无法安全解读，拒绝而非降级）。"""

    def __init__(self, kind: str, declared: Any, supported: int):
        self.kind = kind
        self.declared = declared
        self.supported = supported
        super().__init__(
            f"{kind} schema_version={declared!r} 高于实现支持的 {supported}；拒绝解读"
        )


def _reject_unknown_version(kind: str, declared: Any, supported: int) -> None:
    """拒绝高于实现支持的 schema_version。

    缺失（None/缺键）按字段默认值 v1 处理——payload 未声明版本视为当前版本；
    非法非整数类型由字段的 int 类型校验负责。显式声明且 > supported 的版本
    一律拒绝（UnknownSchemaVersion），不静默当作当前版本解读。
    """
    try:
        version = int(declared)
    except (TypeError, ValueError):
        return  # 缺失/非数值：交给字段类型校验；缺失即默认 v1
    if version > supported:
        raise UnknownSchemaVersion(kind, version, supported)


# 各冻结来源允许的检索工具白名单（与 coreact_controller.SOURCE_TASK_TYPES 对齐）。
SOURCE_TOOL_WHITELIST: dict[str, frozenset[str]] = {
    "graphrag": frozenset({"local_search", "global_search", "hybrid_search", "naive_search", "chain_exploration"}),
    # S1：web 是聚合通道——Tavily 与 OpenAlex/arXiv 都是该通道内的 API。
    "web": frozenset({"web_search", "scholar_search"}),
    # 只读兼容：历史 scholar Run 仍按其冻结白名单恢复执行。
    "scholar": frozenset({"scholar_search"}),
}


class BudgetPolicy(BaseModel):
    """Brief 冻结的预算口径（源自 BudgetLimits 的子集；执行时受 Harness 硬上限再约束）。"""

    model_config = ConfigDict(extra="forbid")

    min_evidence: int = Field(default=1, ge=1)
    max_actions: int = Field(default=8, ge=1)
    max_llm_tokens: int = Field(default=200000, ge=1)
    max_concurrency: int = Field(default=4, ge=1)
    max_task_retries: int = Field(default=2, ge=0)

    @classmethod
    def from_budget_limits(cls, limits: Any) -> BudgetPolicy:
        """从 harness.budgets.BudgetLimits 冻结一份只读口径。"""
        data = limits if isinstance(limits, dict) else limits.model_dump()
        return cls(
            min_evidence=max(1, int(data.get("min_evidence", 1))) if "min_evidence" in data else 1,
            max_actions=int(data.get("max_plan_tasks", 8)),
            max_llm_tokens=int(data.get("max_llm_tokens", 200000)),
            max_concurrency=int(data.get("max_concurrency", 4)),
            max_task_retries=int(data.get("max_task_retries", 2)),
        )


class ResearchQuestion(BaseModel):
    """Brief 内的研究问题（有稳定 ID，供 ActionSpec.target_question_ids 引用）。"""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    text: str = Field(min_length=1)
    required: bool = True


class ResearchBrief(BaseModel):
    """唯一 coreact 路径的研究输入契约。

    不变量：
    - v1：allowed_sources 必须恰好一个（历史单来源冻结）；
    - v2：allowed_sources 可多元素，首个为主通道（runs.source_mode），集合在
      创建时冻结、运行中不得扩大；
    - 短报告场景 required_sections 可为空，但输出必须保留"结论—依据—局限"；
    - 修订只能经 with_revision()（版本 +1），任何字段变更不落版本即无效。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=RESEARCH_BRIEF_SCHEMA_VERSION, ge=1)
    brief_id: str
    version: int = Field(default=1, ge=1)
    goal: str = Field(min_length=1)
    questions: list[ResearchQuestion] = Field(min_length=1)
    output_requirements: list[str] = Field(default_factory=list)
    allowed_sources: list[SourceMode] = Field(min_length=1)
    required_sections: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    budget_policy: BudgetPolicy = Field(default_factory=BudgetPolicy)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        _reject_unknown_version("research_brief", value, RESEARCH_BRIEF_SCHEMA_VERSION)
        return value

    @field_validator("allowed_sources")
    @classmethod
    def _no_duplicate_sources(cls, value: list[SourceMode]) -> list[SourceMode]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_sources 不得重复")
        return value

    @field_validator("questions")
    @classmethod
    def _unique_question_ids(cls, questions: list[ResearchQuestion]) -> list[ResearchQuestion]:
        ids = [question.question_id for question in questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question_id 必须唯一")
        return questions

    @model_validator(mode="after")
    def _freeze_sources(self) -> ResearchBrief:
        allowed = list(self.allowed_sources)
        if len(allowed) != len(set(allowed)):
            raise ValueError("allowed_sources 不得重复")
        if self.schema_version <= 1 and len(allowed) != 1:
            raise ValueError("v1 ResearchBrief 只允许单一来源；多来源必须使用 schema_version=2")
        if not allowed:
            raise ValueError("allowed_sources 不得为空")
        return self

    @property
    def source_mode(self) -> SourceMode:
        """冻结的**主通道**（allowed_sources 首个元素）。"""
        return self.allowed_sources[0]

    @property
    def effective_sources(self) -> tuple[SourceMode, ...]:
        """冻结的来源集合（多来源 v2；v1/单来源为单元素）。"""
        return tuple(self.allowed_sources)

    @property
    def allowed_tools(self) -> frozenset[str]:
        """冻结集合内所有通道允许工具的白名单并集。"""
        tools: set[str] = set()
        for mode in self.allowed_sources:
            tools.update(SOURCE_TOOL_WHITELIST.get(mode.value, frozenset()))
        return frozenset(tools)

    def content_hash(self) -> str:
        """Canonical JSON hash（不含自身派生值；跨进程稳定）。"""
        payload = self.model_dump(mode="json", exclude={"schema_version"})
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def with_revision(
        self,
        *,
        goal: str | None = None,
        questions: list[ResearchQuestion] | None = None,
        output_requirements: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> ResearchBrief:
        """显式版本化修订：source_mode/allowed_sources/budget_policy 不可修订。"""
        payload = self.model_dump(mode="json", exclude={"schema_version"})
        payload["version"] = self.version + 1
        if goal is not None:
            payload["goal"] = goal
        if questions is not None:
            payload["questions"] = [item if isinstance(item, dict) else item.model_dump(mode="json") for item in questions]
        if output_requirements is not None:
            payload["output_requirements"] = list(output_requirements)
        if acceptance_criteria is not None:
            payload["acceptance_criteria"] = list(acceptance_criteria)
        return ResearchBrief.model_validate(payload)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ResearchBrief:
        """带版本门禁的反序列化入口：未知版本拒绝，不静默降级。"""
        _reject_unknown_version("research_brief", payload.get("schema_version"), RESEARCH_BRIEF_SCHEMA_VERSION)
        return cls.model_validate(payload)


class EvidenceAssessment(BaseModel):
    """Claim 级证据评估（QUEST 思想的冻结接口）。

    状态语义（output-contract §4）：
    - available：账本中存在证据，尚未做针对性检查；
    - supported：**某项检查**（check_kind/verifier/version 可追溯）支持该 claim；
    - disputed：检查发现冲突证据或明确反驳；
    - unresolved：证据不足/检查不可用/超时。
    supported ≠ 事实认证；检查类型与版本必须随行记录，避免审计伪归因。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=RESEARCH_BRIEF_SCHEMA_VERSION, ge=1)
    claim_id: str
    claim_text: str = Field(default="", max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["available", "supported", "disputed", "unresolved"] = "available"
    check_kind: str | None = None  # deterministic proxy | semantic | rubric_coverage ...
    verifier: str | None = None
    verifier_version: str | None = None
    reason: str = ""
    next_action: str | None = None
    updated_step: int = Field(default=0, ge=0)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        _reject_unknown_version("evidence_assessment", value, RESEARCH_BRIEF_SCHEMA_VERSION)
        return value

    @model_validator(mode="after")
    def _attribution_required_when_conclusive(self) -> EvidenceAssessment:
        # 结论性状态必须可追溯检查来源；否则审计时会把模型判定误归因为核验。
        if self.status in {"supported", "disputed"} and not (self.check_kind and self.verifier and self.verifier_version):
            raise ValueError(
                f"status={self.status} 必须携带 check_kind/verifier/verifier_version（可追溯归因）"
            )
        if self.status != "available" and not self.evidence_ids and self.status != "unresolved":
            raise ValueError(f"status={self.status} 必须携带至少一个 evidence_id")
        return self


def merge_assessments(assessments: list[EvidenceAssessment]) -> list[EvidenceAssessment]:
    """按 claim_id 去重合并；同 claim 取 updated_step 最大者（后写优先）。"""
    by_claim: dict[str, EvidenceAssessment] = {}
    for item in assessments:
        existing = by_claim.get(item.claim_id)
        if existing is None or item.updated_step >= existing.updated_step:
            by_claim[item.claim_id] = item
    return [by_claim[key] for key in sorted(by_claim)]


# ---------- 模型角色冻结规则（design §8） ----------

CANONICAL_ROLES: tuple[str, ...] = ("brief", "controller", "reporter", "verifier")
# 旧角色 → 新角色映射：planner 的规划职责由 BriefBuilder 承接。
LEGACY_ROLE_ALIASES: dict[str, str] = {"planner": "brief"}


def canonical_role_models(role_models: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """把快照中的 role_models 归一化为规范角色键。

    - 旧 `planner` key 映射为 `brief`；显式 `brief` key 优先（不覆盖历史显式选择）；
    - 只保留规范角色键；未知 key 原样保留在 `legacy` 子字典供审计，不参与装配；
    - 输出为新 dict（不修改入参），值原样透传（不现算、不改写模型 ID）。
    """
    source = role_models or {}
    canonical: dict[str, dict[str, Any]] = {}
    legacy: dict[str, Any] = {}
    for key, value in source.items():
        mapped = LEGACY_ROLE_ALIASES.get(str(key))
        if mapped is not None:
            if mapped not in canonical and value:
                canonical[mapped] = dict(value)
            legacy[str(key)] = value  # 空值的显式角色也进 legacy 供审计
        elif str(key) in CANONICAL_ROLES:
            if value:
                canonical[str(key)] = dict(value)
            else:
                legacy[str(key)] = value  # 空值不参与装配，但保留审计痕迹
        else:
            legacy[str(key)] = value
    result = dict(canonical)
    if legacy:
        result["legacy"] = legacy
    return result
