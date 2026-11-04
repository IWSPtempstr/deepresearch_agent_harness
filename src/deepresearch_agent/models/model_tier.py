"""T-009：按任务难度分层的模型路由。

原则（对应 proposal「模型路由」验收项）：
- **难度派生是纯函数**：输入 RunCreate config_snapshot / 请求信号，输出
  (model_id, tier, reason)。无 LLM 参与、确定性、可单测。
- **模型 ID 全部配置化**：tier → model 映射来自 settings（LLM_TIER_*_MODEL），
  空值回落全局 OPENAI_LLM_MODEL；不把厂商/型号写死在代码里。
- **显式覆盖优先**：config_snapshot["model_tier"] 为 "simple"/"standard"/
  "complex" 时跳过推导（评测/隔离运行用）。
- **每次路由留痕**：tier 与 reason 写入 run.model_snapshot_json，可审计。

层级语义：
- simple  —— 简要问答、短报告（brief）：低成本/低延迟优先
- standard —— 常规分析与默认研究：通用模型
- complex —— 详细/长文档报告、高证据门槛请求：高能力模型
"""

from __future__ import annotations

from typing import Any, Literal

Tier = Literal["simple", "standard", "complex"]

_DETAILED_MARKERS = ("详细", "深入", "全面", "长篇", "报告", "研究报告")
# 语言无关的强信号（英文常用研究措辞）
_DETAILED_MARKERS_EN = ("detailed", "in-depth", "comprehensive", "long report", "research report")


def resolve_model_tier(config_snapshot: dict[str, Any] | None, *, content: str = "") -> tuple[Tier, str]:
    """从 Run 上下文推导模型层级。返回 (tier, reason)。"""
    snapshot = config_snapshot or {}
    explicit = str(snapshot.get("model_tier") or "").strip().lower()
    if explicit in {"simple", "standard", "complex"}:
        return explicit, "explicit model_tier in config_snapshot"

    if snapshot.get("report_type") == "long_document":
        return "complex", "report_type=long_document"
    if int(snapshot.get("min_evidence") or 0) >= 3:
        return "complex", "min_evidence>=3 (detailed request)"
    lowered = content.lower()
    if any(marker in content for marker in _DETAILED_MARKERS) or any(marker in lowered for marker in _DETAILED_MARKERS_EN):
        return "complex", "detailed markers in content"

    if snapshot.get("deep_research_max_iterations", 1) > 1:
        return "standard", "deep_research_max_iterations>1"
    return "simple", "default brief request"


def resolve_model_for(config_snapshot: dict[str, Any] | None, *, content: str = "", settings: Any = None) -> dict[str, str]:
    """返回 {"model": ..., "tier": ..., "reason": ...}；settings 缺省取全局配置。"""
    tier, reason = resolve_model_tier(config_snapshot, content=content)
    settings = settings or _default_settings()
    fallback = settings.OPENAI_LLM_MODEL
    model_by_tier = {
        "simple": settings.LLM_TIER_SIMPLE_MODEL or fallback,
        "standard": settings.LLM_TIER_STANDARD_MODEL or fallback,
        "complex": settings.LLM_TIER_COMPLEX_MODEL or fallback,
    }
    return {"model": model_by_tier[tier], "tier": tier, "reason": reason}


def _default_settings():
    from deepresearch_agent.config import settings

    return settings
