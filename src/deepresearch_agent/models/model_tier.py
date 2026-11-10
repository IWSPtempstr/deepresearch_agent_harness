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

# T-016：角色级模型分层。角色 → 默认 tier（Anthropic 模式：lead 强模型、
# subagent 便宜模型）。可用 env 覆盖：LLM_ROLE_TIER_DEFAULTS="planner:complex,worker:simple"
Role = Literal["planner", "worker", "reporter", "controller"]
_ROLE_TIERS_DEFAULT: dict[str, Tier] = {
    "planner": "complex",
    "worker": "simple",
    "reporter": "standard",
    "controller": "standard",
}

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
    return {"model": _model_for_tier(tier, settings), "tier": tier, "reason": reason}


def resolve_role_model(
    config_snapshot: dict[str, Any] | None,
    *,
    role: str,
    settings: Any = None,
) -> dict[str, str]:
    """T-016：按角色解析模型。四层顺序（先到先得）：

    1. 显式覆盖：config_snapshot[f"{role}_model"]（评测/隔离运行用）；
    2. 角色默认 tier（LLM_ROLE_TIER_DEFAULTS 覆盖内置 ROLE_TIERS_DEFAULT）；
    3. Run 级 tier（resolve_model_for 的结果，即 Run 难度模型）；
    4. 全局 OPENAI_LLM_MODEL。
    """
    settings = settings or _default_settings()
    snapshot = config_snapshot or {}
    explicit = str(snapshot.get(f"{role}_model") or "").strip()
    if explicit:
        return {"model": explicit, "tier": f"explicit-{role}", "reason": f"explicit {role}_model in config_snapshot"}

    role_tier = _role_tier(role, settings)
    run_tier, _run_reason = resolve_model_tier(snapshot)
    fallback = _model_for_tier(run_tier, settings)
    model_by_tier = {
        "simple": settings.LLM_TIER_SIMPLE_MODEL or fallback,
        "standard": settings.LLM_TIER_STANDARD_MODEL or fallback,
        "complex": settings.LLM_TIER_COMPLEX_MODEL or fallback,
    }
    model = model_by_tier[role_tier]
    reason = f"role tier {role_tier} (defaults for {role})"
    return {"model": model, "tier": role_tier, "reason": reason}


def _role_tier(role: str, settings: Any) -> Tier:
    """角色默认 tier：env LLM_ROLE_TIER_DEFAULTS 覆盖内置映射；未知角色回落 standard。"""
    overrides: dict[str, Tier] = {}
    raw = str(getattr(settings, "LLM_ROLE_TIER_DEFAULTS", "") or "").strip()
    if raw:
        for pair in raw.split(","):
            if ":" not in pair:
                continue
            name, _, tier = pair.partition(":")
            name, tier = name.strip(), tier.strip().lower()
            if name and tier in {"simple", "standard", "complex"}:
                overrides[name] = tier  # type: ignore[assignment]
    return overrides.get(role, _ROLE_TIERS_DEFAULT.get(role, "standard"))


def _model_for_tier(tier: str, settings: Any) -> str:
    fallback = settings.OPENAI_LLM_MODEL
    return {
        "simple": settings.LLM_TIER_SIMPLE_MODEL or fallback,
        "standard": settings.LLM_TIER_STANDARD_MODEL or fallback,
        "complex": settings.LLM_TIER_COMPLEX_MODEL or fallback,
    }[tier]


def _default_settings():
    from deepresearch_agent.config import settings

    return settings
