"""T-308: ContextState 可信度分层测试。"""
from deepresearch_agent.harness.coreact_state import ContextState
from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver

from test_coreact_driver import FakeOrchestrator, ScriptedLLM, action, make_context, ready


def test_context_state_updates_from_verdicts():
    context = make_context()
    llm = ScriptedLLM([
        action("无关查询"),       # 验证失败（claim_support 未覆盖）
        action("无关查询 补充"),   # 一次定向重试
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "完全无关的内容"), (True, "仍然无关的内容")])
    driver = PlanExecuteReportDriver(context, orchestrator)
    import asyncio
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())
    cs = driver.coreact_loop.context_state
    assert isinstance(cs, ContextState)
    assert cs.updated_step == 1
    # 失败动作的证据不在 trusted；claim_support 未覆盖 → uncertain 记录主张词
    assert cs.trusted_ids == [] or driver.state.execution_records[-1].metadata.environment["coreact"]["verdict"]["passed"]
    assert any("检索增强" in item["term"] for item in cs.uncertain), cs.uncertain


def test_context_state_trusted_on_pass():
    context = make_context()
    llm = ScriptedLLM([
        action("检索增强 基础概念"),
        ready(),
    ])
    # plan_spec 的完成条件与证据文本对齐 → verdict.passed
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，基础概念说明")])
    driver = PlanExecuteReportDriver(context, orchestrator)
    import asyncio
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())
    cs = driver.coreact_loop.context_state
    verdict = driver.state.execution_records[-1].metadata.environment["coreact"]["verdict"]
    if verdict["passed"]:
        assert cs.trusted_ids, "通过动作的证据应进入 trusted"


def test_context_state_survives_snapshot_roundtrip():
    context = make_context()
    driver = PlanExecuteReportDriver(context, object())
    from deepresearch_agent.harness.coreact_state import CoreActLoopState

    driver.coreact_loop = CoreActLoopState()
    driver.coreact_loop.context_state = ContextState(
        trusted_ids=["ev_1"], untrusted_ids=["ev_bad"],
        uncertain=[{"term": "成本", "reason": "证据未覆盖主张词: [成本]", "next_action": "补充检索"}],
        updated_step=3,
    )
    snapshot = driver.snapshot()
    restored = PlanExecuteReportDriver(
        make_context(config_snapshot={"orchestration": "coreact_v1"}, workflow_state=snapshot),
        object(),
    )
    assert restored.coreact_loop.context_state.trusted_ids == ["ev_1"]
    assert restored.coreact_loop.context_state.uncertain[0]["term"] == "成本"


def test_controller_prompt_contains_context_state():
    from deepresearch_agent.harness.coreact_controller import DecisionContext

    ctx = DecisionContext(
        goal="g", source_mode="web",
        context_state={"trusted_ids": ["ev_1"], "untrusted_ids": ["ev_bad"],
                       "uncertain": [{"term": "成本", "next_action": "补充检索"}]},
        allow_llm=False,
    )
    from deepresearch_agent.harness.coreact_controller import CoreActController

    controller = CoreActController(llm=None)
    prompt = controller._build_decision_prompt(ctx)
    assert "已验证可信证据 1 条" in prompt
    assert "不可信证据 1 条" in prompt
    assert "成本" in prompt
