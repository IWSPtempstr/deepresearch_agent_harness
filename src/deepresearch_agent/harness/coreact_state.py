"""coreact_v1 步级循环的可持久化状态（T-015）。

序列化位置：`RunContext.workflow_state["coreact"]`。恢复规则（runtime 层）：
EXECUTING checkpoint 携带本状态且 `done=false` → 停在 executing 续跑；
否则按旧映射推进 REPORTING。版本字段用于未来 schema 演进。
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

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


class CoreActLoopState(BaseModel):
    """步级循环游标。checkpoint_version 不作游标（它计 checkpoint 写次数）。"""

    engine: Literal["coreact_v1"] = COREACT_ENGINE
    version: int = COREACT_STATE_VERSION
    phase: Literal["decide", "act", "verify", "done"] = "decide"
    # 已完成动作数与 controller 调用总数分开计数；每步可含首次决策+一次 retry 决策。
    step_index: int = 0
    decisions_used: int = 0
    # 提交中的动作：崩溃恢复时若 pending_action 非空且 done=false，
    # 该动作视为未提交（恢复后重做——外部调用可能重复一次，见 proposal §4）
    pending_action: Optional[PendingAction] = None
    stop_reason: Optional[str] = None
    done: bool = False
    # 连续无进展计数（验证失败且无新增证据）：达到上限即 ready_to_report
    no_progress_count: int = 0
    failure_reasons: list[str] = Field(default_factory=list)

    def continuation_pending(self) -> bool:
        """是否为"执行中断"型 continuation checkpoint 的判据。"""
        return self.engine == COREACT_ENGINE and not self.done

    def to_workflow_state(self) -> Dict[str, Any]:
        return {COREACT_STATE_KEY: self.model_dump()}


def loop_state_from_workflow_state(
    workflow_state: Optional[Dict[str, Any]],
) -> Optional[CoreActLoopState]:
    """从 RunContext.workflow_state 安全还原；无/畸形返回 None（按 legacy 处理）。

    pending_action 内的 task/rubric 是原始 dict（跨版本写入），此处逐一
    model_validate：无效的 pending（如版本偏斜的 rubric kind）不使整个
    状态降级为 legacy，而是清空 pending——恢复后从 decide 重新决策，
    丢失语义与崩溃窗口一致（≤1 个动作）。
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
    if state.pending_action is not None:
        if not _pending_action_valid(state.pending_action):
            state.pending_action = None
            # pending 被清空后循环必须重新决策，不能停留在 act/verify 中段
            state.phase = "decide"
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
