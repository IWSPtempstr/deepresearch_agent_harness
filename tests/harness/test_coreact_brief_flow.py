"""T-406：BriefBuilder 替换重复规划与 Reflector 测试。

验收（tasks.md T-406）：澄清/摘要/多方向研究、三来源模式、无 reflection 派发、
ReportService 输入与预算 fallback、无旧规划/反思入口调用、角色模型一致性。
"""
import asyncio

from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.integration.multi_agent_factory import MultiAgentFactory
from deepresearch_agent.agents.multi_agent.planner.brief_builder import BriefBuilder, BriefBuilderConfig
from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver
from deepresearch_agent.harness.research_brief import canonical_role_models

from test_coreact_driver import ScriptedLLM, action, make_context, ready


def make_state(query: str, *, source_mode: str = "web") -> PlanExecuteState:
    return PlanExecuteState(input=query, source_mode=source_mode, run_id="run-brief-test")


# ---------- BriefBuilder 单元行为 ----------

def test_simple_request_shortcut_single_task():
    """短定义/摘要问题 → 单个有界检索任务（不凑长文、无澄清依赖）。"""
    builder = BriefBuilder()  # 无 LLM：确定性路径
    state = make_state("检索增强技术是什么")
    result = builder.generate_plan(state)
    assert result.plan_spec is not None
    nodes = result.plan_spec.task_graph.nodes
    assert len(nodes) == 1
    assert nodes[0].task_type == "web_search"
    assert nodes[0].source_mode == "web"
    assert result.clarification.needs_clarification is False


def test_multi_direction_candidate_graph_without_reflection():
    """多方向研究：候选图全部就绪、无 reflection/custom 类型、来源冻结。"""
    builder = BriefBuilder()
    state = make_state("调研检索增强技术在医疗问答中的落地路径与风险评估")
    result = builder.generate_plan(state)
    nodes = result.plan_spec.task_graph.nodes
    assert 2 <= len(nodes) <= 4
    # Controller 是唯一决策者：候选全部 pending 就绪（依赖为空），不预执行
    assert all(not node.depends_on for node in nodes)
    forbidden = {"reflection", "custom", "deep_research", "deeper_research"}
    assert all(node.task_type not in forbidden for node in nodes)
    assert all(node.source_mode == "web" for node in nodes)
    # 候选查询各不相同（多方向）
    queries = [str(node.parameters["query"]) for node in nodes]
    assert len(set(queries)) == len(queries)


def test_three_source_modes_produce_whitelisted_task_types():
    for mode, expected in (
        ("web", {"web_search", "scholar_search"}),
        ("graphrag", {"hybrid_search"}),
        ("scholar", {"scholar_search"}),
    ):
        builder = BriefBuilder()
        state = make_state("调研跨来源检索质量评估", source_mode=mode)
        result = builder.generate_plan(state)
        assert {node.task_type for node in result.plan_spec.task_graph.nodes} == expected, mode
        assert result.plan_spec.source_mode == mode
        # S1：web 聚合通道的学术候选与 Tavily 候选都属于同一冻结来源
        assert all(node.source_mode == mode for node in result.plan_spec.task_graph.nodes)


def test_evidence_gap_failure_feeds_followup_candidate():
    """final evidence 缺口（min_evidence 等）→ 补充检索候选任务，不回旧 Planner。"""
    builder = BriefBuilder(config=BriefBuilderConfig(max_candidate_tasks=5))
    state = make_state("调研检索增强技术的评测基准")
    result = builder.generate_plan(state, assumptions=["min_evidence", "insufficient_source_evidence"])
    queries = [str(node.parameters.get("query", "")) for node in result.plan_spec.task_graph.nodes]
    assert any("补充证据" in query for query in queries)
    # 假设随计划可追溯
    assert "min_evidence" in result.plan_spec.assumptions


def test_clarification_gating_with_stubbed_clarifier():
    """澄清门：未满足且不允许未澄清 → plan_spec=None（NeedsUserInput 路径）；
    允许未澄清 → 继续生成候选图。"""

    class StubClarifier:
        def __init__(self, satisfied: bool):
            self.satisfied = satisfied

        def analyze(self, context):
            from deepresearch_agent.agents.multi_agent.planner.clarifier import ClarificationResult

            return ClarificationResult(
                needs_clarification=not self.satisfied,
                questions=[] if self.satisfied else ["请明确目标读者与时间范围"],
                raw_response="stub",
            )

    class StubLLM:
        pass  # 仅作哨兵：BriefBuilder 以 llm 非空判定澄清可用

    blocking = BriefBuilder(llm=StubLLM(), clarifier=StubClarifier(satisfied=False),
                            config=BriefBuilderConfig(allow_unclarified_plan=False))
    blocked = blocking.generate_plan(make_state("调研一个宽泛的话题"))
    assert blocked.plan_spec is None
    assert blocked.clarification.needs_clarification is True

    permissive = BriefBuilder(llm=StubLLM(), clarifier=StubClarifier(satisfied=False),
                              config=BriefBuilderConfig(allow_unclarified_plan=True))
    allowed = permissive.generate_plan(make_state("调研一个宽泛的话题"))
    assert allowed.plan_spec is not None


def test_research_brief_contract_frozen_sources():
    """ResearchBrief 契约随候选图生成：来源冻结、验收/问题可追溯、预算口径冻结。"""
    builder = BriefBuilder(config=BriefBuilderConfig(min_evidence=2))
    state = make_state("调研检索增强技术的评测基准")
    result = builder.generate_plan(state)
    brief_payload = state.context_snapshot["research_brief"]
    assert brief_payload["goal"] == "调研检索增强技术的评测基准"
    assert brief_payload["allowed_sources"] == ["web"]
    assert brief_payload["budget_policy"]["min_evidence"] == 2
    assert brief_payload["acceptance_criteria"]
    # 冻结校验：brief 来源与 Run 冻结来源一致；修订接口不提供任何改写来源的
    # 入口（T-402 保证"扩大来源"不可表达）
    from deepresearch_agent.harness.research_brief import ResearchBrief
    import inspect

    brief = ResearchBrief.model_validate(brief_payload)
    assert brief.source_mode.value == "web"
    assert not {"allowed_sources", "source_mode"} & set(inspect.signature(brief.with_revision).parameters)


# ---------- 工厂装配：核心工厂不注册旧 Reflector ----------

def test_coreact_bundle_uses_brief_builder_and_structurally_excludes_reflector():
    """T-408：reflection 执行器类已物理删除——"不注册旧 Reflector"由结构保证。"""
    import importlib.util

    assert importlib.util.find_spec("deepresearch_agent.agents.multi_agent.executor.reflector") is None

    bundle = MultiAgentFactory.create_coreact_bundle()
    from deepresearch_agent.agents.multi_agent.planner.brief_builder import BriefBuilder as BB

    assert isinstance(bundle.planner, BB)
    assert all(type(executor).__name__ == "RetrievalExecutor" for executor in bundle.worker.executors)
    # 旧 planner 角色映射为 brief（角色模型一致性）
    mapped = canonical_role_models({"planner": {"model": "m-planner"}})
    assert mapped["brief"] == {"model": "m-planner"}


def test_factory_planner_pipeline_end_to_end():
    """orchestrator.plan → BriefBuilder 候选图（不触发旧 planner/反射组件）。"""
    bundle = MultiAgentFactory.create_coreact_bundle()
    state = make_state("检索增强技术是什么")
    result = bundle.orchestrator.plan(state)
    assert result.plan_spec is not None
    assert len(result.plan_spec.task_graph.nodes) == 1  # 简单捷径
    assert state.context_snapshot["research_brief"]["allowed_sources"] == ["web"]


# ---------- driver 流程：无 reflection 派发 + 缺口回环 ----------

class BriefBackedOrchestrator:
    """真实 BriefBuilder + 可控执行（不引入旧 planner/reflector 代码路径）。"""

    def __init__(self, controller_llm, evidence_texts: list[str]):
        self._planner = BriefBuilder()
        self.controller_model = controller_llm
        self._evidence = list(evidence_texts)
        self.executed: list[str] = []
        self.dispatched_queries: list[str] = []

    def plan(self, state, *, assumptions=None):
        return self._planner.generate_plan(state, assumptions=assumptions)

    def execute_action(self, state, task, *, progress_callback=None):
        self.executed.append(task.task_type)
        self.dispatched_queries.append(str((task.parameters or {}).get("query", "")))
        from deepresearch_agent.agents.multi_agent.core.execution_record import (
            ExecutionMetadata, ExecutionRecord, ToolCall,
        )
        from deepresearch_agent.agents.multi_agent.core.retrieval_result import (
            RetrievalMetadata, RetrievalResult,
        )

        text = self._evidence.pop(0) if self._evidence else "检索增强是一种技术路线：结合检索与生成"
        result = RetrievalResult(
            granularity="Chunk", evidence=text,
            metadata=RetrievalMetadata(source_id="s1", source_type="webpage"),
            source="tavily_search", source_mode="web",
        )
        setattr(result, "result_id", f"ev_{len(self.executed)}")
        record = ExecutionRecord(
            task_id=task.task_id, session_id=state.session_id, worker_type="retrieval_executor",
            tool_calls=[ToolCall(
                tool_name="web_search", tool_call_id=f"call-{state.run_id}-{task.task_id}-a1",
                source_mode="web", args=dict(task.parameters), result={"answer": text}, status="success",
            )],
            evidence=[result],
            metadata=ExecutionMetadata(worker_type="retrieval_executor", tool_calls_count=1),
        )
        record.metadata.environment["coreact"] = {}
        state.execution_records.append(record)
        if state.execution_context is not None and task.task_id not in state.execution_context.completed_task_ids:
            state.execution_context.completed_task_ids.append(task.task_id)
        return True, None, record


def test_coreact_flow_with_brief_builder_has_no_reflection_dispatch():
    """全流程：BriefBuilder 规划 → scripted controller → 执行轨迹无 reflection。"""
    llm = ScriptedLLM([
        action("检索增强技术 核心概念与定义"),
        ready(),
    ])
    orchestrator = BriefBackedOrchestrator(llm, ["检索增强技术是一种结合检索与生成的技术路线"])
    driver = PlanExecuteReportDriver(make_context(), orchestrator)
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())

    assert driver.coreact_loop.done
    assert all(task_type == "web_search" for task_type in orchestrator.executed)
    assert "reflection" not in set(orchestrator.executed)
    plan_types = {node.task_type for node in driver.state.plan.task_graph.nodes}
    assert "reflection" not in plan_types and "custom" not in plan_types
    # ResearchBrief 已随 plan 落入可持久化上下文
    assert driver.state.context_snapshot["research_brief"]["goal"]


def test_evidence_gap_replan_reenters_controller_loop():
    """min_evidence 缺口 → replan 路径走 BriefBuilder（候选含补充检索）→
    coreact 循环重新进入 decide 且补充候选可实际派发（不回旧 Planner/Reflector）。"""
    llm = ScriptedLLM([
        action("调研检索增强技术的评测基准 补充证据 最新数据"),
        ready(),
    ])
    orchestrator = BriefBackedOrchestrator(
        llm,
        ["调研检索增强技术的评测基准包括召回率与忠实度，可支撑补充检索结论"],
    )
    driver = PlanExecuteReportDriver(make_context(), orchestrator)
    asyncio.run(driver.plan())
    assert driver.coreact_loop is not None and not driver.coreact_loop.done

    asyncio.run(driver.plan(failures=["min_evidence", "insufficient_source_evidence"]))
    queries = [str(node.parameters.get("query", "")) for node in driver.state.plan.task_graph.nodes]
    assert any("补充证据" in query for query in queries)
    # plan() 对 coreact 的语义：失败后循环回到 decide（Controller 重新决策）
    assert driver.coreact_loop.phase == "decide"
    assert driver.coreact_loop.done is False

    # 补充候选经 controller 决策实际可派发（admission/去重不拒）：
    # 首个决策即选中补充检索动作并真实执行
    asyncio.run(driver.execute())
    assert orchestrator.executed == ["web_search"]
    assert "补充证据" in orchestrator.dispatched_queries[-1]
    assert "reflection" not in set(orchestrator.executed)


def test_budget_fallback_report_via_coreact_rubrics():
    """预算 fallback：reserved budget 报告路径真实执行（不依赖旧 planner），
    coreact rubric 仍随最终 Contract 提交。"""
    from deepresearch_agent.config.settings import REPORT_RESERVED_TOKENS, VERIFICATION_RESERVED_TOKENS

    llm = ScriptedLLM([action("检索增强技术 核心概念与定义"), ready(), ready()])
    context = make_context()
    orchestrator = BriefBackedOrchestrator(
        llm, ["检索增强技术是一种结合检索与生成的技术路线，证据充分"],
    )
    driver = PlanExecuteReportDriver(context, orchestrator)
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())
    # 执行后耗尽预算余量（模拟长 Run 尾部）→ reserved budget fallback 命中
    context.budget_usage.llm_tokens = (
        context.budget_limits.max_llm_tokens - (REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS) + 100
    )
    assert driver._requires_reserved_budget_fallback() is True

    report = asyncio.run(driver.report())
    assert report.strip()
    rubrics = driver.coreact_rubrics()
    assert rubrics, "coreact rubric 应经执行记录元数据暴露给最终 Contract"


def build_rubric_for(task_id: str):
    from deepresearch_agent.agents.multi_agent.core.rubric import build_task_rubric

    return build_task_rubric(task_id, description="d", source_mode="web", min_evidence=1)
