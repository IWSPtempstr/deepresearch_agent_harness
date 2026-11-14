"""Deterministic source policy enforced immediately before retrieval."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.harness.errors import AppError, ErrorCode


class SourcePolicy:
    # 通道 → 允许的**任务类型**（Controller/ActionRunner 的准入口径）。
    # S1：web 是聚合通道，允许 Tavily 与 OpenAlex/arXiv 两类检索任务；
    # scholar 键保留给历史 Run 的只读恢复。
    SOURCE_TASK_TYPES = {
        SourceMode.GRAPHRAG: {"local_search", "global_search", "hybrid_search", "naive_search", "chain_exploration"},
        SourceMode.WEB: {"web_search", "scholar_search"},
        SourceMode.SCHOLAR: {"scholar_search"},
    }
    # 通道 → 允许的**工具名**（provider 层，包含历史别名）。
    ALLOWED_TOOLSETS = {
        SourceMode.GRAPHRAG: set(SOURCE_TASK_TYPES[SourceMode.GRAPHRAG]),
        SourceMode.WEB: set(SOURCE_TASK_TYPES[SourceMode.WEB]) | {"tavily_search"},
        SourceMode.SCHOLAR: {"scholar_search"},
    }
    # 工具 → 通道（组合 router 的派发依据；多来源时防止跨通道误派）。
    TOOL_SOURCE = {
        "local_search": SourceMode.GRAPHRAG,
        "global_search": SourceMode.GRAPHRAG,
        "hybrid_search": SourceMode.GRAPHRAG,
        "naive_search": SourceMode.GRAPHRAG,
        "chain_exploration": SourceMode.GRAPHRAG,
        "web_search": SourceMode.WEB,
        "tavily_search": SourceMode.WEB,
        "scholar_search": SourceMode.WEB,
    }
    # 同一通道内不同 API 的预算计数归属（S1：tavily/scholar 分开记账）。
    TOOL_API = {
        "web_search": "tavily",
        "tavily_search": "tavily",
        "scholar_search": "scholar",
    }
    WEB_ALLOWED_ARGUMENTS = {
        "query", "top_k", "search_depth", "include_domains", "exclude_domains",
        "start_date", "end_date", "topic",
    }

    def assert_tool_allowed(self, source_mode: SourceMode | str, tool_name: str) -> None:
        mode = SourceMode(source_mode)
        if tool_name not in self.ALLOWED_TOOLSETS[mode]:
            raise AppError(
                ErrorCode.SOURCE_POLICY_VIOLATION,
                f"{mode.value} Run 不允许调用工具 {tool_name}",
                details={"source_mode": mode.value, "tool_name": tool_name},
            )

    def sanitize_web_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        rejected = sorted(set(arguments) - self.WEB_ALLOWED_ARGUMENTS)
        if rejected:
            raise AppError(
                ErrorCode.SOURCE_POLICY_VIOLATION,
                "Web 检索参数包含不允许外发的字段",
                details={"rejected_fields": rejected},
            )
        return {key: value for key, value in arguments.items() if key in self.WEB_ALLOWED_ARGUMENTS}


__all__ = ["SourcePolicy"]
