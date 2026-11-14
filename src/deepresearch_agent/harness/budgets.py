"""Per-Run budget snapshots and deterministic consumption checks."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field


class BudgetExceeded(RuntimeError):
    def __init__(self, metric: str, observed: float, limit: float):
        self.metric, self.observed, self.limit = metric, observed, limit
        observed_text = f"{observed:.2f}" if metric == "wall_time_seconds" else f"{observed:g}"
        limit_text = f"{limit:g}"
        super().__init__(f"预算耗尽: {metric}={observed_text} 超过上限 {limit_text}")


class BudgetLimits(BaseModel):
    wall_time_seconds: int = Field(default=1800, ge=1)
    max_plan_tasks: int = Field(default=8, ge=1)
    max_tool_calls: int = Field(default=30, ge=1)
    max_tavily_calls: int = Field(default=20, ge=1)
    # S1：web 聚合通道中的学术 API（OpenAlex/arXiv）独立计数，避免串账。
    max_scholar_api_calls: int = Field(default=20, ge=1)
    max_replans: int = Field(default=2, ge=0)
    max_task_retries: int = Field(default=2, ge=0)
    max_llm_tokens: int = Field(default=200000, ge=1)
    max_concurrency: int = Field(default=4, ge=1)
    tool_timeout_seconds: int = Field(default=60, ge=1)


class BudgetUsage(BaseModel):
    plan_tasks: int = 0
    tool_calls: int = 0
    tavily_calls: int = 0
    scholar_api_calls: int = 0
    replans: int = 0
    task_retries: int = 0
    llm_tokens: int = 0
    # 时效口径：仅在 assert_available()/snapshot()/refresh_elapsed() 调用时刷新。
    # 两次检查之间直接读 usage.elapsed_seconds 会滞后于真实值；执行期由心跳
    # 每秒 snapshot 刷新一次，跨检查点的精确时长统计应以事件时间戳为准。
    elapsed_seconds: float = 0.0
    # 前缀缓存观测（由 PrefixCacheTracker 在 Run 结束时回填）
    prefix_cache_requests: int = 0
    prefix_cache_hit_tokens: int = 0
    prefix_cache_miss_tokens: int = 0


class BudgetManager:
    def __init__(self, limits: BudgetLimits | dict[str, Any], usage: BudgetUsage | dict[str, Any] | None = None):
        self.limits = limits if isinstance(limits, BudgetLimits) else BudgetLimits.model_validate(limits)
        self.usage = usage if isinstance(usage, BudgetUsage) else BudgetUsage.model_validate(usage or {})
        self._started = time.monotonic() - self.usage.elapsed_seconds

    def snapshot(self) -> dict[str, Any]:
        self.refresh_elapsed()
        return {"limits": self.limits.model_dump(), "usage": self.usage.model_dump()}

    def refresh_elapsed(self) -> float:
        """elapsed 刷新的唯一入口：读取精确已耗时长时统一走这里。"""
        self.usage.elapsed_seconds = max(0.0, time.monotonic() - self._started)
        return self.usage.elapsed_seconds

    def assert_available(self) -> None:
        self._check("wall_time_seconds", self.refresh_elapsed(), self.limits.wall_time_seconds)
        self._check("plan_tasks", self.usage.plan_tasks, self.limits.max_plan_tasks)
        self._check("llm_tokens", self.usage.llm_tokens, self.limits.max_llm_tokens)
        self._check("tool_calls", self.usage.tool_calls, self.limits.max_tool_calls)
        self._check("tavily_calls", self.usage.tavily_calls, self.limits.max_tavily_calls)
        self._check("scholar_api_calls", self.usage.scholar_api_calls, self.limits.max_scholar_api_calls)
        self._check("replans", self.usage.replans, self.limits.max_replans)
        self._check("task_retries", self.usage.task_retries, self.limits.max_task_retries)

    def remaining_tokens(self) -> int:
        """剩余 LLM Token 配额——预算口径唯一出处，调用方勿再手写 limits-usage。"""
        return max(0, self.limits.max_llm_tokens - self.usage.llm_tokens)

    def has_token_reserve(self, required: int) -> bool:
        """Whether a later stage still has its reserved token envelope."""
        return self.remaining_tokens() >= max(0, required)

    @staticmethod
    def _check(metric: str, observed: float, limit: float) -> None:
        if observed > limit:
            raise BudgetExceeded(metric, observed, limit)

    def consume_tool(self, *, tavily: bool = False, scholar: bool = False, count: int = 1) -> None:
        self.usage.tool_calls += count
        if tavily:
            self.usage.tavily_calls += count
        if scholar:
            self.usage.scholar_api_calls += count
        self.assert_available()

    def observe_plan_tasks(self, count: int) -> None:
        """以本次观察值【覆盖】计划任务计数——非增量累加。

        与 observe_tokens 同为幂等重放口径：从执行记录恢复时直接写入累计值，
        较小的观察值会静默压低计数（重放语义所需）。累加语义请用 consume_*。
        """
        self.usage.plan_tasks = max(0, count)
        self.assert_available()

    def observe_tokens(self, count: int) -> None:
        """以本次观察值【覆盖】累计 Token（非增量）——幂等重放口径。

        取 max(usage, count) 防重放回退；来源是工作流上报的累计值而非单次
        消耗，累加语义请用 consume_tokens。
        """
        self.usage.llm_tokens = max(self.usage.llm_tokens, max(0, count))
        self.assert_available()

    def consume_tokens(self, count: int) -> None:
        self.usage.llm_tokens += max(0, count)
        self.assert_available()

    def consume_retry(self) -> None:
        self.usage.task_retries += 1
        self.assert_available()

    def consume_replan(self) -> None:
        self.usage.replans += 1
        self.assert_available()
