"""T-406：BriefBuilder —— 唯一 coreact 路径的研究简报构建器。

职责（design §2）：
- 生成 ResearchBrief 契约（目标/问题/验收/输出要求/冻结来源）；
- 生成候选任务图（controller 的建议池——**不预执行整图**，Controller 是唯一
  下一步动作决策者）；
- 可选澄清（复用 Clarifier；无 LLM 时跳过，保持离线路径确定性）；
- 简单请求捷径（短定义/摘要问题 → 单个有界检索任务）；
- final evidence 缺口（min_evidence 等验证失败）转化为补充检索候选，反馈给
  Controller，而非回旧 Planner/Reflector 流水线。

明确不做：不生成 reflection 任务、不调用 TaskDecomposer/PlanReviewer、
不注册/依赖 Reflector。BasePlanner 保留给 staged 路径（T-408 退役）。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

from pydantic import BaseModel, Field

from deepresearch_agent.agents.multi_agent.core.plan_spec import (
    AcceptanceCriteria,
    PlanSpec,
    ProblemStatement,
    TaskGraph,
    TaskNode,
)
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.planner.result import PlannerResult
from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.harness.clarifications import has_answers, merge_into_history
from deepresearch_agent.harness.research_brief import BudgetPolicy, ResearchBrief

logger = logging.getLogger(__name__)

# 各冻结来源的候选任务工具（与 SourcePolicy 白名单一致）
SOURCE_TASK_TYPE: dict[str, str] = {
    "graphrag": "hybrid_search",
    "web": "web_search",
    "scholar": "scholar_search",
}

# 多方向研究候选（确定性；不依赖 LLM）
RESEARCH_DIRECTIONS: tuple[str, ...] = (
    "核心概念与定义",
    "现状与关键进展",
    "争议与对比",
    "局限与开放问题",
)

# 简单请求捷径标记（与既有 compact report 判定同源）
SIMPLE_REQUEST_MARKERS: tuple[str, ...] = (
    "概括", "总结", "要点", "列出", "是什么", "简述", "summarize", "list ", "what is",
)

# 触发补充检索的验证失败标记（与 recovery.classify_verification_failures 的
# replan 类别对齐；source_match 单独出现时同样需要补充检索而非空转）
EVIDENCE_GAP_MARKERS: tuple[str, ...] = (
    "min_evidence", "no_source_evidence", "insufficient_source_evidence", "claim_support",
    "source_match",
)


class BriefBuilderConfig(BaseModel):
    max_candidate_tasks: int = Field(default=4, ge=1, le=8)
    allow_unclarified_plan: bool = True
    default_domain: str = "通用"
    min_evidence: int = Field(default=1, ge=1)


class BriefBuilder:
    """生成 ResearchBrief + 候选任务图；duck-compatible with BasePlanner.generate_plan。"""

    def __init__(self, *, llm: Any = None, clarifier: Any = None, config: BriefBuilderConfig | None = None) -> None:
        self.config = config or BriefBuilderConfig()
        # 澄清是可选项：无 LLM 时不构建（离线/测试路径保持确定性，不触模型客户端）
        self._clarifier = clarifier if (clarifier is not None and llm is not None) else None
        self._llm = llm

    # ---------- 入口（与 BasePlanner.generate_plan 同签名） ----------

    def generate_plan(self, state: PlanExecuteState, *, assumptions: Optional[Sequence[str]] = None) -> PlannerResult:
        context = self._ensure_plan_context(state)
        failures = list(assumptions or [])
        refined_query = (context.refined_query or context.original_query or "").strip()
        clarified_before = has_answers(state.context_snapshot)

        # 1）简单请求捷径：短定义/摘要 → 单个有界检索任务（不凑长文）
        simple = self._simple_plan(state, context, failures)
        if simple is not None:
            return self._finalize(state, context, simple, clarification=None)

        # 2）可选澄清（仅在提供 LLM 时分析；未满足且不允许未澄清 → 待用户输入）。
        # 与 BasePlanner 的有意分歧：failures 非空（缺口 replan）时不再被澄清
        # 阻塞——运行中途回环不应重新要求用户输入；澄清问题仍记录进历史供审计。
        clarification = None
        if self._clarifier is not None:
            clarification = self._clarifier.analyze(context)
            self._record_clarification(context, clarification)
            if (
                not clarification.is_satisfied(context)
                and not self.config.allow_unclarified_plan
                and not failures
                and not clarified_before
            ):
                logger.info("BriefBuilder：澄清未满足，暂停计划生成")
                return PlannerResult(
                    plan_spec=None,
                    clarification=clarification,
                    task_decomposition=None,
                    review_outcome=None,
                )

        # 3）候选任务图：多方向研究（全部就绪、依赖为空——Controller 逐一决策）
        plan_spec = self._build_candidate_graph(state, context, refined_query, failures)
        return self._finalize(state, context, plan_spec, clarification=clarification)

    # ---------- 各阶段 ----------

    @staticmethod
    def _default_task_type(state: PlanExecuteState) -> str:
        """S1：web 聚合通道按冻结的可用 API 选择默认检索工具。

        `web_apis` 缺失（旧快照/直接构造）时保持历史默认 web_search。
        """
        if state.source_mode == "web":
            apis = (state.context_snapshot or {}).get("web_apis")
            if isinstance(apis, dict) and apis.get("tavily") is False:
                return "scholar_search"
        return SOURCE_TASK_TYPE.get(state.source_mode, "web_search")

    def _simple_plan(self, state: PlanExecuteState, context: Any, failures: List[str]) -> Optional[PlanSpec]:
        query = (context.refined_query or context.original_query or "").strip()
        if len(query) > 160 or not any(marker in query.lower() for marker in SIMPLE_REQUEST_MARKERS):
            return None
        task_type = self._default_task_type(state)
        node = TaskNode(
            task_type=task_type,
            source_mode=state.source_mode,
            description=query,
            priority=1,
            parameters={"query": query, "top_k": 6},
        )
        return PlanSpec(
            source_mode=state.source_mode,
            problem_statement=ProblemStatement(original_query=context.original_query, refined_query=query),
            assumptions=list(failures),
            task_graph=TaskGraph(nodes=[node], execution_mode="sequential"),
            acceptance_criteria=AcceptanceCriteria(min_evidence_count=max(1, self.config.min_evidence)),
            status="executing",
        )

    def _build_candidate_graph(
        self,
        state: PlanExecuteState,
        context: Any,
        refined_query: str,
        failures: List[str],
    ) -> PlanSpec:
        task_type = self._default_task_type(state)
        nodes: List[TaskNode] = []
        # S1：web 聚合通道为学术 API 预留一个候选（确定性路径也能用上 scholar_search）；
        # Tavily 候选仍占多数，Controller 仍是唯一决策者。若 Tavily 不可用，
        # 所有候选都走 scholar_search（不生成注定 SOURCE_UNAVAILABLE 的动作）。
        academic_slot = (
            state.source_mode == "web" and task_type == "web_search"
            and self.config.max_candidate_tasks >= 2
        )
        direction_count = min(
            self.config.max_candidate_tasks - (1 if academic_slot else 0),
            len(RESEARCH_DIRECTIONS),
        )
        for direction in RESEARCH_DIRECTIONS[:direction_count]:
            query = f"{refined_query} {direction}"
            nodes.append(TaskNode(
                task_type=task_type,
                source_mode=state.source_mode,
                description=f"{refined_query}——{direction}",
                priority=2,
                parameters={"query": query},
            ))
        if academic_slot:
            nodes.append(TaskNode(
                task_type="scholar_search",
                source_mode=state.source_mode,
                description=f"{refined_query}——学术文献与综述",
                priority=2,
                parameters={"query": f"{refined_query} 学术研究 综述", "top_k": 5},
            ))
        # final evidence 缺口 → 追加补充检索候选（反馈 Controller，而非回旧 Planner）
        if any(marker in failure for failure in failures for marker in EVIDENCE_GAP_MARKERS):
            follow_up_query = f"{refined_query} 补充证据 最新数据"
            if all(str(node.parameters.get("query", "")).strip() != follow_up_query for node in nodes):
                nodes.append(TaskNode(
                    task_type=task_type,
                    source_mode=state.source_mode,
                    description=f"{refined_query}——证据缺口补充检索",
                    priority=1,
                    parameters={"query": follow_up_query},
                ))
        return PlanSpec(
            source_mode=state.source_mode,
            problem_statement=ProblemStatement(original_query=context.original_query, refined_query=refined_query),
            assumptions=list(failures),
            task_graph=TaskGraph(nodes=nodes, execution_mode="parallel"),
            acceptance_criteria=AcceptanceCriteria(
                min_evidence_count=max(1, self.config.min_evidence),
                completion_conditions=[refined_query],
            ),
            status="executing",
        )

    def _finalize(
        self,
        state: PlanExecuteState,
        context: Any,
        plan_spec: PlanSpec,
        *,
        clarification: Any | None,
    ) -> PlannerResult:
        """写回状态：候选图 + ResearchBrief 契约（context_snapshot 持久化随 checkpoint）。"""
        plan_spec.source_mode = state.source_mode
        state.plan = plan_spec
        state.plan_context = context
        brief = self._build_brief(state, context, plan_spec)
        state.context_snapshot = {
            **(state.context_snapshot or {}),
            "research_brief": brief.model_dump(mode="json"),
        }
        state.update_timestamp()
        clarification = clarification or self._no_clarification()
        return PlannerResult(
            plan_spec=plan_spec,
            clarification=clarification,
            executor_signal=plan_spec.to_execution_signal(),
        )

    def _build_brief(self, state: PlanExecuteState, context: Any, plan_spec: PlanSpec) -> ResearchBrief:
        """ResearchBrief 冻结契约：来源=Run 冻结来源（不得扩大）；问题/验收可追溯。

        replan 再生成时显式版本化（design §3：需求修订须显式版本化）——
        goal 未变则保留原 brief，变更则经 with_revision 版本 +1。
        """
        refined_query = (context.refined_query or context.original_query or "").strip()
        questions = [{"question_id": "q1", "text": refined_query, "required": True}]
        budget_limits = (state.budget_state or {}).get("limits") or {}
        policy = BudgetPolicy.from_budget_limits(budget_limits)
        # Run 预算缺失（离线/测试）时以配置口径兜底；两者取严
        policy = policy.model_copy(update={"min_evidence": max(policy.min_evidence, self.config.min_evidence)})
        previous_payload = (state.context_snapshot or {}).get("research_brief")
        if isinstance(previous_payload, dict):
            try:
                previous = ResearchBrief.model_validate(previous_payload)
            except Exception:  # noqa: BLE001 —— 损坏的历史 brief 不阻塞重规划
                previous = None
            if previous is not None:
                if previous.goal == refined_query and previous.budget_policy.min_evidence == policy.min_evidence:
                    return previous
                return previous.with_revision(
                    goal=refined_query,
                    questions=questions,
                    acceptance_criteria=[condition for condition in plan_spec.acceptance_criteria.completion_conditions],
                )
        return ResearchBrief(
            brief_id=f"brief_{state.run_id}",
            goal=refined_query,
            questions=questions,
            output_requirements=["结论附就近证据引用", "无证据内容进入局限，不作推断"],
            allowed_sources=[SourceMode(state.source_mode)],
            acceptance_criteria=[condition for condition in plan_spec.acceptance_criteria.completion_conditions],
            budget_policy=policy,
        )

    @staticmethod
    def _record_clarification(context: Any, clarification: Any) -> None:
        """澄清问题进入 plan_context 历史（审计轨迹；与 BasePlanner 同语义）。"""
        history = getattr(context, "clarification_history", None)
        if history is None or clarification is None:
            return
        for question in getattr(clarification, "questions", None) or []:
            entry = {"question": str(question)}
            if entry not in history:
                history.append(entry)

    @staticmethod
    def _no_clarification():
        from deepresearch_agent.agents.multi_agent.planner.clarifier import ClarificationResult

        return ClarificationResult(needs_clarification=False)

    @staticmethod
    def _ensure_plan_context(state: PlanExecuteState) -> Any:
        from deepresearch_agent.agents.multi_agent.core.state import PlanContext

        if state.plan_context is None:
            state.plan_context = PlanContext(original_query=state.input or "")
        # BC-01：已回答的澄清问答对进入规划上下文（Clarifier.is_satisfied 可见；
        # 同一 Run 不重复要求同一声明）。
        merge_into_history(state.context_snapshot, state.plan_context.clarification_history)
        return state.plan_context
