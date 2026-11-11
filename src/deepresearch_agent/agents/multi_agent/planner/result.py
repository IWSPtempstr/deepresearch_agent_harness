"""T-408：PlannerResult 自 base_planner.py 迁移（base_planner 已随旧规划退役）。

独立模块避免 plan_spec ↔ clarifier 循环导入（result → clarifier → core.state →
plan_spec 为有向无环依赖）。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from deepresearch_agent.agents.multi_agent.core.plan_spec import (
    PlanExecutionSignal,
    PlanSpec,
)
from deepresearch_agent.agents.multi_agent.planner.clarifier import ClarificationResult


class PlannerResult(BaseModel):
    """Planner综合输出（BriefBuilder 为唯一生产实现）。"""

    plan_spec: Optional[PlanSpec] = Field(default=None, description="最终生成的PlanSpec")
    clarification: ClarificationResult = Field(description="澄清结果")
    task_decomposition: Optional[dict] = Field(
        default=None,
        description="任务分解详细结果（旧 TaskDecomposer 输出，仅存档兼容）",
    )
    review_outcome: Optional[dict] = Field(
        default=None,
        description="计划审校产出详情（旧 PlanReviewer 输出，仅存档兼容）",
    )
    executor_signal: Optional[PlanExecutionSignal] = Field(
        default=None,
        description="传递给Executor的计划信号",
    )

    def needs_clarification(self) -> bool:
        """是否仍需澄清"""
        if not self.clarification.needs_clarification:
            return False
        if not self.clarification.questions:
            return False
        # 未生成PlanSpec时视为待澄清
        return self.plan_spec is None

    def executor_signal_json(self) -> Optional[str]:
        """将执行信号转换为JSON字符串，便于跨组件传递"""
        if self.executor_signal is None:
            return None
        import json

        return json.dumps(self.executor_signal.model_dump(mode="json"), ensure_ascii=False, indent=2)
