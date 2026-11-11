"""coreact_v1 步级循环的可持久化状态（T-015；T-404 扩展多动作 intent）。

序列化位置：`RunContext.workflow_state["coreact"]`。恢复规则（runtime 层）：
EXECUTING checkpoint 携带本状态且 `done=false` → 停在 executing 续跑；
否则按旧映射推进 REPORTING。版本字段用于未来 schema 演进。

T-404 耐久恢复语义：
- `pending_batch` 是**整个 in-flight 批次**（派发前随 intent 一并落盘），
  崩溃恢复不丢次要动作；`pending_action` 保留为批首动作（旧快照/兼容读取）；
- `committed_action_ids` 记录已逐动作提交（行已落库）的动作——恢复不重做
  已提交结果；数据库行幂等不等于外部调用 exactly-once；
- `next_operation` 显式序列化（execute/report），不得只凭 RunStatus.EXECUTING
  推断下一阶段。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

COREACT_ENGINE = "coreact_v1"
COREACT_STATE_VERSION = 1
COREACT_STATE_KEY = "coreact"


class CoreActPaused(RuntimeError):
    """Raised when a pause is requested at an action boundary."""


class PendingAction(BaseModel):
    """已决定、正在执行或等待验证的动作（含其 rubric 与重试计数）。"""

    task: Dict[str, Any]  # TaskNode.model_dump()——避免跨层模型耦合
    rubric: Dict[str, Any]  # TaskRubric.model_dump()
    attempt: int = Field(default=1, ge=1)
    # 上一次验证失败的定向反馈（attempt=2 时注入执行）
    retry_reason: Optional[str] = None

    @property
    def action_id(self) -> str:
        return f"action_{self.task.get('task_id', '')}"

    @property
    def rubric_id(self) -> str:
        return str(self.rubric.get("rubric_id") or "")


class ContextState(BaseModel):
    """T-016：可信度分层的上下文状态（QUEST 思想）。

    - trusted：通过步级验证动作的账本证据 ID；
    - untrusted：source_policy/重复查询违规动作的证据 ID（不主动复用）；
    - uncertain：claim_support 未覆盖的主张词 + 建议下一步动作。
    Evidence Ledger 仍是唯一事实源；本状态只存 id 与元信息。
    """

    trusted_ids: list[str] = Field(default_factory=list)
    untrusted_ids: list[str] = Field(default_factory=list)
    uncertain: list[dict[str, str]] = Field(default_factory=list)  # {term, reason, next_action}
    updated_step: int = 0


class CoreActLoopState(BaseModel):
    """步级循环游标。checkpoint_version 不作游标（它计 checkpoint 写次数）。"""

    engine: Literal["coreact_v1"] = COREACT_ENGINE
    version: int = COREACT_STATE_VERSION
    phase: Literal["decide", "act", "verify", "done"] = "decide"
    # 已完成动作数与 controller 调用总数分开计数；每步可含首次决策+一次 retry 决策。
    step_index: int = 0
    decisions_used: int = 0
    # T-404：整个 in-flight 批次（派发前落盘）。空列表表示无 in-flight 批次。
    pending_batch: List[PendingAction] = Field(default_factory=list)
    # 提交中的动作（兼容读取：批首动作；恢复重放入口）。
    pending_action: Optional[PendingAction] = None
    # T-404：已逐动作提交（行已落库）的动作 id；恢复不得重做。
    committed_action_ids: List[str] = Field(default_factory=list)
    stop_reason: Optional[str] = None
    done: bool = False
    # 连续无进展计数（验证失败且无新增证据）：达到上限即 ready_to_report
    no_progress_count: int = 0
    failure_reasons: list[str] = Field(default_factory=list)
    context_state: ContextState = Field(default_factory=ContextState)
    # T-405：有界观察摘要（账本的有损视图，findings 携带真实 evidence_ids）——
    # Controller 从真实研究发现决策，而非只看计数与失败理由。
    observations: List[Dict[str, Any]] = Field(default_factory=list)
    # T-405：claim 级评估（EvidenceAssessment dump；supported≠事实认证）。
    assessments: List[Dict[str, Any]] = Field(default_factory=list)

    @property
    def next_operation(self) -> Literal["execute", "report"]:
        """显式下一操作：done=循环结束待报告；complete 只能由最终 Contract 判定。"""
        return "report" if self.done else "execute"

    def inflight_items(self) -> List[PendingAction]:
        """当前未提交的 in-flight 动作：pending_batch 优先，旧快照回退 pending_action。"""
        if self.pending_batch:
            return list(self.pending_batch)
        return [self.pending_action] if self.pending_action is not None else []

    def continuation_pending(self) -> bool:
        """是否为"执行中断"型 continuation checkpoint 的判据。"""
        return self.engine == COREACT_ENGINE and not self.done

    def to_workflow_state(self) -> Dict[str, Any]:
        return {COREACT_STATE_KEY: self.model_dump() | {"next_operation": self.next_operation}}


def loop_state_from_workflow_state(
    workflow_state: Optional[Dict[str, Any]],
) -> Optional[CoreActLoopState]:
    """从 RunContext.workflow_state 安全还原；无/畸形返回 None（按 legacy 处理）。

    pending_batch/pending_action 内的 task/rubric 是原始 dict（跨版本写入），
    此处逐项 model_validate：无效项**逐条剔除**（不使整个状态降级为 legacy、
    不丢其余 in-flight 动作）；全部无效时清空游标——恢复后从 decide 重新决策，
    丢失语义与崩溃窗口一致（≤1 个批次）。
    """
    if not isinstance(workflow_state, dict):
        return None
    raw = workflow_state.get(COREACT_STATE_KEY)
    if not isinstance(raw, dict) or raw.get("engine") != COREACT_ENGINE:
        return None
    try:
        state = CoreActLoopState.model_validate(raw)
    except Exception:
        return None

    # 批次归一：pending_batch 逐项校验（无效项剔除，不丢其余 in-flight 动作）；
    # pending_action 保持原样（旧单动作快照的兼容读取走 inflight_items() 回退）。
    valid: List[PendingAction] = []
    for item in state.pending_batch:
        if _pending_action_valid(item):
            valid.append(item)
        else:
            logger.warning(
                "coreact 恢复：剔除无效 in-flight 动作（action_id=%s，恢复后不再重放）",
                item.action_id,
            )
            # 剔除进入 failure_reasons 供 controller/审计可见（不只日志）
            state.failure_reasons.append(f"恢复剔除无效 in-flight 动作: {item.action_id}")
    state.pending_batch = valid
    # T-405：observations/assessments 是持久化的摘要视图——畸形条目逐条剔除
    # （损坏 checkpoint 不得把任意内容注入 controller prompt），全部无效则清空。
    valid_observations = [
        item for item in state.observations
        if isinstance(item, dict)
        and isinstance(item.get("findings"), list)
        and all(
            isinstance(finding, dict)
            and isinstance(finding.get("statement"), str)
            and isinstance(finding.get("evidence_ids"), list)
            for finding in item.get("findings")
        )
    ]
    if len(valid_observations) != len(state.observations):
        logger.warning(
            "coreact 恢复：剔除 %d 条畸形观察摘要", len(state.observations) - len(valid_observations),
        )
    state.observations = valid_observations
    valid_assessments = [
        item for item in state.assessments
        if isinstance(item, dict)
        and isinstance(item.get("claim_id"), str)
        and item.get("claim_id")
        and item.get("status") in {"available", "supported", "disputed", "unresolved"}
    ]
    if len(valid_assessments) != len(state.assessments):
        logger.warning(
            "coreact 恢复：剔除 %d 条畸形 claim 评估", len(state.assessments) - len(valid_assessments),
        )
    state.assessments = valid_assessments
    if state.pending_action is not None and not _pending_action_valid(state.pending_action):
        # 旧单动作游标无效：清空（T-016 行为）
        state.pending_action = None
    if state.pending_action is None and not valid:
        # 游标全部无效：恢复后必须重新决策，不能停留在 act/verify 中段
        state.phase = "decide"
    elif state.pending_action is None and valid:
        # 兼容读取兜底：快照未写 pending_action（旧格式/历史版本）时以批首动作
        # 兜底；新格式快照中 None 是持久化事实（批首已提交），inflight_items()
        # 以 pending_batch 为准，此兜底不影响其语义。
        state.pending_action = valid[0]
    return state


def _pending_action_valid(pending: PendingAction) -> bool:
    from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskNode
    from deepresearch_agent.agents.multi_agent.core.rubric import TaskRubric

    try:
        TaskNode.model_validate(pending.task)
        TaskRubric.model_validate(pending.rubric)
        return True
    except Exception:
        return False
