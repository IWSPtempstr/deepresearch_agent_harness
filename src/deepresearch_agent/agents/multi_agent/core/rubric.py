"""任务级 rubric 模型与确定性生成（T-015）。

设计依据 docs/specs/coreact-orchestration/design.md：
- rubric 是"当前任务的可验证原子标准"列表，每条 criterion 独立判定；
- 生成是确定性的（来自任务描述 + AcceptanceCriteria + 冻结 source_mode），
  语义富化不在本期范围；
- threshold 默认 0.5（Co-ReAct 论文的 τ），max_retries 固定 1（Co-ReAct 语义），
  全局 BudgetManager.max_task_retries 仍是硬上限。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

RUBRIC_SCHEMA_VERSION = 1

CriterionKind = Literal[
    "evidence_presence",   # 证据计数/存在性
    "source_policy",       # 来源/工具白名单
    "output_schema",       # 动作有可消费输出
    "claim_support",       # 关键主张有证据支撑（确定性代理：关键词邻近）
    "no_duplicate_query",  # 不重复已执行且无增量的查询
]


class RubricCriterion(BaseModel):
    criterion_id: str
    kind: CriterionKind
    description: str
    weight: float = Field(default=1.0, ge=0.0)
    required: bool = True
    # evidence_presence 用：本动作至少需要的新增证据条数
    min_evidence: int = Field(default=1, ge=0)
    # claim_support 用：必须被证据覆盖的关键词/短语（确定性代理）
    required_terms: List[str] = Field(default_factory=list)


class TaskRubric(BaseModel):
    """单个动作的验收 rubric。"""

    rubric_id: str
    task_id: str
    version: int = RUBRIC_SCHEMA_VERSION
    criteria: List[RubricCriterion] = Field(min_length=1)
    # 加权满足比例阈值（Co-ReAct τ）
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # 固定 1：重试语义由全局 BudgetManager.max_task_retries 再加一道硬上限
    max_retries: int = Field(default=1, ge=0, le=1)
    # 语义检查只有在该开关开启且预算门控通过时才执行（T-202）
    semantic_check: bool = False
    # 提示词/生成器版本，随 verdict 一起持久化以便审计
    generator: str = "deterministic_v1"


class CriterionVerdict(BaseModel):
    criterion_id: str
    kind: CriterionKind
    passed: bool
    reason: str = ""
    evidence_ids: List[str] = Field(default_factory=list)


class StepVerdict(BaseModel):
    """一次动作（含其重试）的最终验证结果。"""

    rubric_id: str
    task_id: str
    attempt: int = 1
    passed: bool
    weighted_score: float = Field(default=0.0, ge=0.0, le=1.0)
    threshold: float = 0.5
    criteria: List[CriterionVerdict] = Field(default_factory=list)
    # 未通过时的定向重试指引（喂给下一次执行）
    retry_reason: Optional[str] = None
    evidence_ids: List[str] = Field(default_factory=list)
    verifier: str = "deterministic_v1"
    schema_version: int = RUBRIC_SCHEMA_VERSION
    # T-405：语义判定三态（pass/fail/unknown/not_run）——unknown 表示检查
    # 不可用/非法输出/预算门未过；required 语义检查的 unknown 不得按 pass。
    semantic_status: Literal["pass", "fail", "unknown", "not_run"] = "not_run"
    semantic_reason: Optional[str] = None

    def to_metadata(self) -> Dict[str, Any]:
        return self.model_dump()


def build_task_rubric(
    task_id: str,
    *,
    description: str,
    source_mode: str,
    min_evidence: int = 1,
    completion_conditions: Optional[List[str]] = None,
    required_terms: Optional[List[str]] = None,
    semantic_check: bool = False,
) -> TaskRubric:
    """从任务描述与 AcceptanceCriteria 确定性生成 rubric。

    - evidence_presence：AcceptanceCriteria.min_evidence_count（下限 1）；
    - source_policy / output_schema：所有动作必备；
    - claim_support：有 required_terms（来自 completion_conditions 的关键词）
      才生成，否则跳过——没有可判词就不做代理判定；
    - no_duplicate_query：检索类动作默认携带。
    """
    criteria: List[RubricCriterion] = [
        RubricCriterion(
            criterion_id=f"{task_id}_evidence_presence",
            kind="evidence_presence",
            description=f"动作产出至少 {min_evidence} 条进入账本的新证据",
            weight=1.5,
            min_evidence=max(1, int(min_evidence)),
        ),
        RubricCriterion(
            criterion_id=f"{task_id}_source_policy",
            kind="source_policy",
            description=f"动作工具与来源符合冻结的 source_mode={source_mode}",
            weight=1.0,
        ),
        RubricCriterion(
            criterion_id=f"{task_id}_output_schema",
            kind="output_schema",
            description="动作产生了可消费的执行输出（记录/证据）",
            weight=1.0,
        ),
    ]
    # required_terms 显式给定优先；否则从完成条件确定性抽取判词
    if not required_terms:
        required_terms = _terms_from_conditions(completion_conditions or [])
    terms = [t.strip() for t in (required_terms or []) if t and t.strip()]
    if terms:
        criteria.append(
            RubricCriterion(
                criterion_id=f"{task_id}_claim_support",
                kind="claim_support",
                description="证据内容覆盖任务关键主张词（确定性代理：标题/摘要含词）",
                weight=1.5,
                required_terms=terms[:8],
            )
        )
    else:
        # 无可判词时把权重转回证据数量，保持总分可判
        criteria[0].weight = 2.0
    criteria.append(
        RubricCriterion(
            criterion_id=f"{task_id}_no_duplicate_query",
            kind="no_duplicate_query",
            description="未重复已执行且无增量的查询",
            weight=0.5,
            required=False,
        )
    )
    rubric_id = f"rubric_{task_id}_v{RUBRIC_SCHEMA_VERSION}"
    return TaskRubric(
        rubric_id=rubric_id,
        task_id=task_id,
        criteria=criteria,
        semantic_check=semantic_check,
    )


def rubric_from_task(task: Any, *, min_evidence: int = 1) -> TaskRubric:
    """便捷入口：从 TaskNode + PlanSpec.acceptance_criteria 生成。

    completion_conditions 的词项作为 claim_support 的 required_terms。
    """
    acceptance = getattr(task, "acceptance", None)
    completion_conditions: List[str] = []
    if isinstance(acceptance, dict):
        completion_conditions = list(acceptance.get("completion_conditions") or [])
    return build_task_rubric(
        task.task_id,
        description=task.description,
        source_mode=str(getattr(task, "source_mode", "graphrag")),
        min_evidence=max(min_evidence, 1),
        completion_conditions=completion_conditions,
        required_terms=_terms_from_conditions(completion_conditions),
    )


def _terms_from_conditions(conditions: List[str]) -> List[str]:
    """从完成条件中抽取可判定词项（确定性代理）。

    - 短中文串（2-6 字）整体保留；
    - 更长的中文串切 4 字滑窗（步长 2），保证证据摘要有子串命中机会；
    - 英文/数字 token 原样保留；
    - 去重、上限 8 条——没有可判词就不生成 claim_support criterion。
    """
    import re

    terms: List[str] = []
    seen = set()

    def _push(token: str) -> None:
        key = token.lower()
        if token and key not in seen:
            seen.add(key)
            terms.append(token)

    for condition in conditions or []:
        for run in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{1,20}", condition):
            if re.fullmatch(r"[\u4e00-\u9fff]+", run):
                if len(run) <= 4:
                    _push(run)
                else:
                    for start in range(0, len(run) - 3, 2):
                        _push(run[start : start + 4])
            else:
                _push(run)
    return terms[:8]


def terms_from_conditions(conditions: List[str]) -> List[str]:
    """公开入口：从完成条件抽取可判定词项（确定性代理，供 RubricBuilder 复用）。"""
    return _terms_from_conditions(conditions)
