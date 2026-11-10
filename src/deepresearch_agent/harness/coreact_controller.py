"""coreact_v1 步级决策控制器（T-015/T-203）。

决策顺序（design §3）：
1. LLM 结构化决策（可选；allow_llm 且未超决策上限）——畸形 JSON / 非法
   TaskNode / 调用异常均计为一次失败尝试，最多 2 次后转入确定性兜底；
2. 确定性兜底：候选任务图仍有可执行任务 → 取第一个（调用方保证已按
   依赖过滤）；否则 ready_to_report。

控制器只做结构层校验（TaskNode 可构造、task_type 在枚举内、source_mode
一致）；SourcePolicy 工具白名单由派发层（retrieval_executor）权威执行，
控制器不重复也不放行。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError

from deepresearch_agent.agents.multi_agent.core.plan_spec import TASK_TYPE_CHOICES, TaskNode

logger = logging.getLogger(__name__)

CONTROLLER_VERSION = "coreact_controller_v1"
MAX_MALFORMED_ATTEMPTS = 2  # 每步决策的总尝试上限（畸形/异常合并计数）
MAX_DECISION_CONTENT_CHARS = 20_000  # LLM 回传内容上限，防资源耗尽
SOURCE_TASK_TYPES = {
    "graphrag": {"local_search", "global_search", "hybrid_search", "naive_search", "chain_exploration"},
    "web": {"web_search"},
    "scholar": {"scholar_search"},
}

_DECISION_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


class ControllerDecision(BaseModel):
    """一步的决策结果：要么产出动作，要么宣布可以进入报告。"""

    action: Optional[TaskNode] = None
    ready_to_report: bool = False
    reason: str = ""
    # 决策来源：llm | fallback_candidate | fallback_ready | fallback_llm_failed
    source: str = "llm"


class DecisionContext(BaseModel):
    """传给控制器的单步上下文（全部为摘要，不携带原始工具输出）。"""

    goal: str
    source_mode: str
    eligible_tasks: List[Dict[str, Any]] = Field(default_factory=list)  # 候选图任务 dump
    completed_count: int = 0
    evidence_count: int = 0
    recent_queries: List[str] = Field(default_factory=list)
    failed_rubric_reasons: List[str] = Field(default_factory=list)
    remaining_tokens: int = 0
    decisions_used: int = 0
    max_decisions: int = 8
    allow_llm: bool = False


class CoreActController:
    def __init__(self, *, llm: Any = None, version: str = CONTROLLER_VERSION):
        self.llm = llm
        self.version = version

    def decide(self, context: DecisionContext) -> ControllerDecision:
        """主入口。LLM 失败自动降级；决策上限耗尽直接确定性兜底。"""
        if context.allow_llm and context.decisions_used < context.max_decisions:
            for _ in range(MAX_MALFORMED_ATTEMPTS):
                try:
                    decision = self._decide_with_llm(context)
                except Exception as exc:  # noqa: BLE001 —— LLM/解析故障统一降级
                    logger.warning("coreact 决策失败，将降级: %s", exc)
                    continue
                if decision is not None:
                    decision.source = "llm"
                    return decision
            logger.warning("coreact 决策连续畸形 %d 次，转入确定性兜底", MAX_MALFORMED_ATTEMPTS)
        return self._decide_deterministic(context, reason="llm_failed" if context.allow_llm else "llm_disabled")

    # ---------- LLM 路径 ----------

    def _decide_with_llm(self, context: DecisionContext) -> Optional[ControllerDecision]:
        prompt = self._build_decision_prompt(context)
        response = self.llm.invoke([{"role": "user", "content": prompt}])
        content = response.content if hasattr(response, "content") else str(response)
        payload = self._parse_decision_json(content)
        if payload is None:
            return None
        return self._decision_from_payload(payload, context)

    def _build_decision_prompt(self, context: DecisionContext) -> str:
        tool_menu = {
            "web": "web_search（联网搜索；参数 query）",
            "scholar": "scholar_search（学术检索；参数 query）",
            "graphrag": "local_search / global_search / hybrid_search / naive_search / chain_exploration",
        }.get(context.source_mode, "web_search")
        budget_line = (
            f"剩余 token 预算约 {context.remaining_tokens}，预算紧张时应尽快 ready_to_report。"
            if context.remaining_tokens < 20000
            else "预算充足，可继续探索。"
        )
        gap_lines = (
            "\n".join(f"- {reason}" for reason in context.failed_rubric_reasons[:3])
            or "-（暂无验证失败记录）"
        )
        recent = "、".join(f"“{q}”" for q in context.recent_queries[:5]) or "（无）"
        candidates = (
            "\n".join(
                f"- [{task.get('task_id')}] {task.get('task_type')}: {str(task.get('description'))[:120]}"
                for task in context.eligible_tasks[:5]
            )
            or "-（候选计划已耗尽，可自行提出新动作或宣布完成）"
        )
        return f"""你是深度研究控制器，负责选择下一个研究动作。

# 研究目标
{context.goal}

# 冻结信息源
{context.source_mode}（可用工具：{tool_menu}）

# 当前进度
- 已完成动作数：{context.completed_count}；账本证据数：{context.evidence_count}
- 近期已执行查询：{recent}
- 近期验证失败原因：
{gap_lines}
- {budget_line}

# 候选计划中的可执行任务
{candidates}

# 要求
1. 若继续研究：给出一个新动作（可从候选中选择，也可自行提出），query 必须与近期查询不同角度。
2. 若证据已足够回答目标（或继续探索边际收益过低）：置 ready_to_report=true。
3. 只输出 JSON，不要输出其他文本。格式：
{{"ready_to_report": false, "task": {{"task_type": "web_search", "description": "...", "query": "..."}}, "reason": "一句话理由"}}
或
{{"ready_to_report": true, "task": null, "reason": "证据已覆盖目标"}}"""

    def _parse_decision_json(self, content: str) -> Optional[Dict[str, Any]]:
        raw = str(content or "")
        # 长度检查必须在 strip 之前——strip 会削掉首尾填充，绕过上限
        if not raw or len(raw) > MAX_DECISION_CONTENT_CHARS:
            return None
        text = raw.strip()
        # 依次尝试：各围栏块（按出现顺序）→ 整段文本；首个有效 dict 即接受
        candidates = [m.group(1) for m in _DECISION_FENCE.finditer(text)]
        candidates.append(text)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def _decision_from_payload(
        self, payload: Dict[str, Any], context: DecisionContext
    ) -> Optional[ControllerDecision]:
        reason = str(payload.get("reason") or "")
        if payload.get("ready_to_report") is True:
            return ControllerDecision(ready_to_report=True, reason=reason)
        task_payload = payload.get("task")
        if not isinstance(task_payload, dict):
            return None  # 既不 ready 也没有合法动作 → 视为畸形
        provided_id = task_payload.get("task_id")
        if provided_id:
            # 候选任务只能精确引用当前可执行集合，禁止模型伪造/越权引用依赖任务
            matched = next(
                (item for item in context.eligible_tasks if str(item.get("task_id")) == str(provided_id)),
                None,
            )
            if matched is None:
                return None
            try:
                task = TaskNode.model_validate(matched)
            except ValidationError:
                return None
            requested_type = task_payload.get("task_type")
            if requested_type and requested_type != task.task_type:
                return None
            # Candidate identity/source/type are frozen by the planner; the
            # controller may refine only the query/description for a targeted retry.
            query = task_payload.get("query")
            if isinstance(query, str) and query.strip():
                task.parameters = {**task.parameters, "query": query.strip()}
            description = task_payload.get("description")
            if isinstance(description, str) and description.strip():
                task.description = description.strip()
            if task.source_mode != context.source_mode or not self._task_type_allowed(
                context.source_mode, str(task.task_type)
            ):
                return None
            return ControllerDecision(action=task, reason=reason)
        merged = {
            "task_type": task_payload.get("task_type"),
            "description": task_payload.get("description"),
            "source_mode": context.source_mode,
            "parameters": {"query": task_payload.get("query") or task_payload.get("description")},
        }
        try:
            task = TaskNode.model_validate(merged)
        except ValidationError:
            return None
        if str(task.task_type) not in TASK_TYPE_CHOICES:
            return None
        if not self._task_type_allowed(context.source_mode, str(task.task_type)):
            return None
        return ControllerDecision(action=task, reason=reason)

    @staticmethod
    def _task_type_allowed(source_mode: str, task_type: str) -> bool:
        return task_type in SOURCE_TASK_TYPES.get(source_mode, set())

    # ---------- 确定性兜底 ----------

    def _decide_deterministic(self, context: DecisionContext, *, reason: str) -> ControllerDecision:
        for payload in context.eligible_tasks:
            try:
                task = TaskNode.model_validate(payload)
            except ValidationError:
                continue
            if task.source_mode == context.source_mode and self._task_type_allowed(
                context.source_mode, str(task.task_type)
            ):
                return ControllerDecision(
                    action=task, reason=reason or "候选计划仍有可执行任务", source="fallback_candidate"
                )
        return ControllerDecision(
            ready_to_report=True,
            reason=reason or "无兼容候选任务，进入报告",
            source="fallback_ready",
        )
