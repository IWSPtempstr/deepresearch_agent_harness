"""Serializable state required to execute or resume one research Run."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, model_validator

from .budgets import BudgetLimits, BudgetUsage
from .contracts import RunStatus, SourceMode, WorkflowMode


class RunContext(BaseModel):
    run_id: str
    session_id: str
    trigger_message_id: str
    source_mode: SourceMode
    # S2：Run 创建时冻结的来源集合（空 = 历史/单来源，按 source_mode 解释）。
    # 首元素必须等于 source_mode（主通道）。
    sources: tuple[SourceMode, ...] = ()
    workflow_mode: WorkflowMode
    status: RunStatus
    original_query: str
    resolved_query: Optional[str] = None
    plan_version: int = 0
    checkpoint_version: int = 0
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)
    model_snapshot: Dict[str, Any] = Field(default_factory=dict)
    budget_limits: BudgetLimits = Field(default_factory=BudgetLimits)
    budget_usage: BudgetUsage = Field(default_factory=BudgetUsage)
    cancellation_requested: bool = False
    # Set only while loading an interrupted Run. It records the first stage that
    # is safe to execute after the latest durable checkpoint.
    resume_from_status: Optional[RunStatus] = None
    context_snapshot: Dict[str, Any] = Field(default_factory=dict)
    used_message_ids: list[str] = Field(default_factory=list)
    model_input: Optional[str] = None
    workflow_state: Dict[str, Any] = Field(default_factory=dict)
    report: Optional[str] = None

    @model_validator(mode="after")
    def _validate_sources(self) -> "RunContext":
        if not self.sources:
            return self
        if len(self.sources) != len(set(self.sources)):
            raise ValueError("RunContext.sources 不得重复")
        if self.sources[0] != self.source_mode:
            raise ValueError("RunContext.sources 首元素必须是主通道 source_mode")
        return self

    @property
    def effective_sources(self) -> tuple[SourceMode, ...]:
        return self.sources or (self.source_mode,)


def sources_from_config(config_snapshot: Any, fallback: SourceMode) -> tuple[SourceMode, ...]:
    """从 config_snapshot 解析冻结来源集合；旧 Run 缺失时回退单通道。

    容忍未知条目（跳过而非崩溃）。冻结列表必须包含主通道：缺失/损坏/空列表
    一律回退为 `{fallback}` 单通道——绝不把不含主通道的列表“补全”成合法
    多来源集合（fail closed）。
    """
    if not isinstance(config_snapshot, dict):
        return (fallback,)
    raw = config_snapshot.get("sources")
    if not isinstance(raw, list) or not raw:
        return (fallback,)
    parsed: list[SourceMode] = []
    for item in raw:
        try:
            mode = SourceMode(str(item))
        except ValueError:
            continue
        if mode not in parsed:
            parsed.append(mode)
    if fallback not in parsed:
        return (fallback,)
    parsed.remove(fallback)
    return (fallback, *parsed)
