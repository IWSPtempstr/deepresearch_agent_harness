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

from typing import Any, Literal

from pydantic import (
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

EXECUTION_STATUS = Literal["succeeded", "failed", "cancelled", "unknown"]
VERIFICATION_STATUS = Literal["passed", "failed", "unknown", "not_run"]


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


class ActionRubric(BaseModel):
    """动作验收 rubric（冻结接口；由 TaskRubric/确定性生成器适配）。

    objective/expected_output/evidence_requirements 进入模型执行上下文；
    语义检查是可选判断类型（verifier_kind），不可用/非法输出记 unknown，
    不得以关键词覆盖冒充语义支持（T-405 落实）。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=ACTION_MODELS_SCHEMA_VERSION, ge=1)
    rubric_id: str
    task_id: str
    version: int = 1
    objective: str = ""
    expected_output: str = ""
    evidence_requirements: list[str] = Field(default_factory=list)
    criteria: list[dict[str, Any]] = Field(min_length=1)
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    max_retries: int = Field(default=1, ge=0, le=1)
    verifier_kind: Literal["deterministic", "semantic_optional", "semantic_required"] = "deterministic"
    generator: str = "deterministic_v1"

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        _reject_unknown_version("action_rubric", value, ACTION_MODELS_SCHEMA_VERSION)
        return value


class ActionVerdict(BaseModel):
    """动作验证结论（独立于执行状态）。"""

    model_config = ConfigDict(extra="forbid")

    status: VERIFICATION_STATUS = "not_run"
    verifier_version: str = ""
    failed_criteria: list[str] = Field(default_factory=list)
    retry_reason: str | None = None
    weighted_score: float = Field(default=0.0, ge=0.0, le=1.0)


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
    verification: ActionVerdict = Field(default_factory=ActionVerdict)
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
    if loop_state.pending_action is not None:
        pending = loop_state.pending_action
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
