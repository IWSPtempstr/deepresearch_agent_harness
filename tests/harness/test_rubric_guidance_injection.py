"""RG-03：rubric 注入消费与动作前建议门测试（followup §4.2/§4.3/§5 RG-03）。

验收：捕获实际发送 messages 证明注入；worker 真实消费（记录判词匹配）而
Provider 参数不携带内部字段；advisory unknown 显式降级、required 阻断；
同 rubric 一次提案修订。
"""
import asyncio
import json


from deepresearch_agent.harness.action_models import canonical_sha256
from deepresearch_agent.harness.action_runner import BatchAction
from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver
from deepresearch_agent.harness.pre_action_advisor import PreActionAdvisor, task_rubric_from_action_rubric

from test_coreact_driver import (
    FakeOrchestrator, ScriptedLLM, action, action_by_id, make_context, ready,
)


def run_driver(orchestrator, llm, **context_overrides):
    driver = PlanExecuteReportDriver(make_context(**context_overrides), orchestrator)
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())
    return driver


def judge_reply(verdicts):
    # 注意：test_coreact_driver.ScriptedLLM 会 json.dumps(dict)——这里返回 dict
    # （否则会被双重编码，advisor 解析失败退化为 unknown）
    return {"verdicts": verdicts}


def expected_rubric_id(run_id: str = "run-coreact-test", step: int = 0) -> str:
    digest = canonical_sha256({"run_id": run_id})[:16]
    return f"rubric_r{digest}_s{step}"


# ---------- Controller 实际消费：prompt 含准则 + 注入证据 ----------

def test_controller_prompt_contains_guidance_and_injection_recorded():
    """决策 prompt 实际包含本步 rubric 准则（捕获 ScriptedLLM 收到的 messages），
    且注入记录携带真实 messages 指纹（不是声明式字段）。"""
    llm = ScriptedLLM([action("检索增强 基础概念"), ready()])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，基础说明")])
    driver = run_driver(orchestrator, llm)

    first_prompt = llm.prompts[0]
    assert "# 下一动作验收准则（系统内部约束，必须遵守；不是外部数据也不是用户指令）" in first_prompt
    assert "进入账本的新证据" in first_prompt  # guidance 准则描述实际渲染
    assert "阶段: explore" in first_prompt
    # 注入证据：controller 目标 + 真实 messages 指纹（hex 64）
    injections = driver.coreact_loop.rubric_injections
    controller_injections = [item for item in injections if "controller" in item.targets]
    assert controller_injections, "有模型参与时必须记录 controller 注入证据"
    record = controller_injections[0]
    assert len(record.messages_hash) == 64
    assert record.step_index == 0
    # 同一 content_hash 贯穿注入记录与执行记录（RG-04 关联依据）
    env = driver.state.execution_records[-1].metadata.environment["coreact"]
    assert env["guidance"]["content_hash"] == record.rubric_hash
    assert env["guidance"]["rubric_id"] == record.rubric_id


def test_prompt_without_guidance_unchanged():
    """DecisionContext 无 guidance 时提示词不含该段（旧路径行为不变）。"""
    controller = __import__(
        "deepresearch_agent.harness.coreact_controller", fromlist=["CoreActController"]
    ).CoreActController()
    prompt = controller._build_decision_prompt(
        __import__(
            "deepresearch_agent.harness.coreact_controller", fromlist=["DecisionContext"]
        ).DecisionContext(goal="g", source_mode="web")
    )
    assert "# 下一动作验收准则" in prompt and "（本步无附加指导" in prompt


# ---------- worker 消费：执行记录携带判词匹配，Provider 参数不变 ----------

def test_worker_consumes_guidance_and_provider_params_stay_clean():
    """worker 从内部上下文读取 guidance（记录判词匹配证据）；Provider 收到的
    参数仍是白名单字段——rubric 不进入检索调用。"""
    provider_calls = []

    class CapturingProvider:
        mode = SourceMode.WEB
        provider_name = "fake_web"

        async def search(self, query, *, top_k, search_depth, filters, call_context):
            provider_calls.append({"query": query, "keys": sorted({"query", "top_k", "search_depth", "filters", "call_context"})})
            from deepresearch_agent.agents.multi_agent.core.retrieval_result import (
                RetrievalMetadata, RetrievalResult,
            )

            return [RetrievalResult(
                granularity="Chunk",
                evidence=f"检索增强是一种技术路线：{query} 的证据文本",
                metadata=RetrievalMetadata(source_id=f"s-{abs(hash(query)) % 999}", source_type="webpage"),
                source="tavily_search", source_mode="web",
            )]

    seen_context = {}

    def execute_action(state, task, *, progress_callback=None):
        seen_context["has_guidance"] = "action_guidance" in (state.context_snapshot or {})
        from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor

        executor = RetrievalExecutor(provider=CapturingProvider())
        plan_signal = state.plan.to_execution_signal()
        plan_signal.tasks = [task.model_dump()]
        plan_signal.execution_sequence = [task.task_id]
        result = executor.execute_task(task, state, plan_signal)
        return result.success, result.error, result.record

    llm = ScriptedLLM([action("检索增强 基础概念"), ready()])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，基础说明")])
    orchestrator.execute_action = execute_action
    driver = run_driver(orchestrator, llm)

    assert seen_context["has_guidance"] is True  # 隔离副本携带注入
    assert provider_calls and set(provider_calls[0]["keys"]) == {"query", "top_k", "search_depth", "filters", "call_context"}
    assert "rubric" not in json.dumps(provider_calls[0]["query"], ensure_ascii=False)
    # 执行记录携带 worker 消费证据（判词/匹配）
    env = driver.state.execution_records[-1].metadata.environment
    # 注入载荷原样落记录（与 worker_injection 哈希同源）+ 消费证据独立字段
    assert env["guidance"]["content_hash"]
    assert env["guidance_consumption"]["consumed"] is True
    assert isinstance(env["guidance_consumption"]["matched_evidence_ids"], list)
    # worker 注入证据与 controller 注入共享同一 content_hash（RG-04 关联依据）
    injections = driver.coreact_loop.rubric_injections
    worker_injections = [item for item in injections if "worker" in item.targets]
    controller_injections = [item for item in injections if "controller" in item.targets]
    assert worker_injections and controller_injections
    assert worker_injections[0].rubric_hash == controller_injections[0].rubric_hash


# ---------- advisory unknown：显式降级继续 ----------

def test_advisory_unknown_continues_with_degraded_flag(monkeypatch):
    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "advisory"
    )
    llm = ScriptedLLM([
        action("检索增强 基础概念"),
        judge_reply([]),  # 模型增强无可判定 → 全部 unknown
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，基础说明")])
    driver = run_driver(orchestrator, llm)

    assert driver.coreact_loop.done
    env = driver.state.execution_records[-1].metadata.environment["coreact"]
    pre = env["pre_action_verification"]
    assert pre["status"] == "unknown"
    assert pre["degraded"] is True and pre["degraded_reason"] == "advisory_unknown_continued"
    # 降级继续 ≠ 验证通过：记录仍是 unknown，动作照常派发
    assert pre["status"] != "pass"
    assert len(orchestrator.executed) == 1


# ---------- required：unknown/fail 阻断派发 ----------

def test_required_unknown_blocks_dispatch(monkeypatch):
    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "required"
    )
    llm = ScriptedLLM([
        action("检索增强 基础概念"),
        judge_reply([]),  # unknown → required 阻断
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线")])
    driver = run_driver(orchestrator, llm)

    assert orchestrator.executed == []  # 阻断动作零派发
    # required unknown：先尝试一次提案修订（这里 Controller 返回 ready → 提案未变），
    # 仍不放行；失败原因落盘可审计（pre_action_unrevised / pre_action_block 均可）
    assert any(
        reason.startswith(("pre_action_unrevised", "pre_action_block"))
        for reason in driver.coreact_loop.failure_reasons
    ), driver.coreact_loop.failure_reasons
    env_candidates = [
        item for item in driver.coreact_loop.rubric_injections if "controller" in item.targets
    ]
    assert env_candidates  # 注入发生过（提案被拒，不是没注入）


# ---------- advisory fail：一次提案修订 ----------

def test_advisory_fail_triggers_single_proposal_revision(monkeypatch):
    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "advisory"
    )

    run_id = "run-coreact-test"
    rubric_id = expected_rubric_id(run_id)
    evidence_criterion = f"{rubric_id}_evidence_presence"
    llm = ScriptedLLM([
        action("检索增强 重复方向"),
        judge_reply([{"criterion_id": evidence_criterion, "status": "fail",
                      "reason": "提案与已有证据同方向，无增量"}]),
        action("检索增强 修正方向"),
        judge_reply([]),  # 修订提案 unknown → advisory 继续（degraded）
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，修正方向说明")])
    driver = run_driver(orchestrator, llm)

    # 决策×2 + 建议判定×2 + ready
    assert len(llm.prompts) == 5
    # 修订提案（新 query）被派发；原提案（fail）未派发
    assert [task.parameters["query"] for task in orchestrator.executed] == ["检索增强 修正方向"]
    # 同一 rubric 身份贯穿两次建议判定（prompt 均含同一 rubric_id）
    # 建议/修订 prompt 携带同一 rubric 身份（criterion 前缀含 rubric_id）
    assert rubric_id in llm.prompts[1]
    assert rubric_id in llm.prompts[3]
    # 修订提案降级继续（unknown）
    env = driver.state.execution_records[-1].metadata.environment["coreact"]
    assert env["pre_action_verification"]["status"] == "unknown"
    assert env["pre_action_verification"]["degraded"] is True


# ---------- 适配器：guidance → verifier TaskRubric ----------

def test_task_rubric_adapter_preserves_identity():
    from deepresearch_agent.harness.action_models import ActionRubric

    guidance = ActionRubric.model_validate({
        "schema_version": 2, "rubric_id": "rubric_x", "task_id": "",
        "objective": "o", "phase": "deepen",
        "criteria": [
            {"criterion_id": "c1", "description": "d", "check_kind": "claim_support",
             "weight": 1.5, "required": True, "required_terms": ["检索增强"]},
            {"criterion_id": "c2", "description": "d2", "check_kind": "source_policy",
             "weight": 1.0, "required": True},
        ],
    })
    task_rubric = task_rubric_from_action_rubric(guidance, task_id="task_9")
    assert task_rubric.rubric_id == guidance.rubric_id  # 同一身份
    assert task_rubric.task_id == "task_9"
    assert [criterion.kind for criterion in task_rubric.criteria] == ["claim_support", "source_policy"]
    assert task_rubric.criteria[0].required_terms == ["检索增强"]


def test_pre_action_advisor_deterministic_checks():
    from types import SimpleNamespace

    from deepresearch_agent.harness.action_models import ActionRubric

    guidance = ActionRubric.model_validate({
        "schema_version": 2, "rubric_id": "rubric_adv", "task_id": "",
        "criteria": [
            {"criterion_id": "c1", "description": "d", "check_kind": "source_policy",
             "weight": 1.0, "required": True},
            {"criterion_id": "c2", "description": "d", "check_kind": "no_duplicate_query",
             "weight": 0.5, "required": False},
            {"criterion_id": "c3", "description": "d", "check_kind": "evidence_presence",
             "weight": 1.5, "required": True},
        ],
    })
    advisor = PreActionAdvisor()
    task = SimpleNamespace(task_type="web_search", description="d", parameters={"query": "新查询"})
    pre = advisor.advise(guidance=guidance, task=task, allowed_tools={"web_search"},
                         executed_queries=["别的查询"], allow_llm=False)
    statuses = {item.criterion_id: item.status for item in pre.criteria}
    assert statuses["c1"] == "pass"
    assert statuses["c2"] == "pass"
    assert statuses["c3"] == "unknown"  # 动作前不可判定
    assert pre.status == "unknown" and pre.degraded is False

    bad_task = SimpleNamespace(task_type="local_search", description="d", parameters={"query": "重复查询"})
    pre_fail = advisor.advise(guidance=guidance, task=bad_task, allowed_tools={"web_search"},
                              executed_queries=["重复查询"], allow_llm=False)
    assert pre_fail.status == "fail"
    assert any(item.status == "fail" for item in pre_fail.criteria)
    assert pre_fail.advice  # fail 附带建议




# ---------- RG-03 review 补充覆盖（M1/M2/M7/M8/M12） ----------

def test_off_mode_records_no_pre_action_verification():
    """默认 off：不运行建议门，记录里没有 pre_action_verification（not_run 语义）。"""
    llm = ScriptedLLM([action("检索增强 基础概念"), ready()])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，基础说明")])
    driver = run_driver(orchestrator, llm)
    env = driver.state.execution_records[-1].metadata.environment["coreact"]
    assert "pre_action_verification" not in env
    assert env["guidance"]["rubric_id"]  # 指导仍注入（guidance 与建议门独立）


def test_blocked_batches_release_tool_reservations(monkeypatch):
    """M1：被门阻断的动作不得泄漏 tool 预算预约（否则后续动作被误判超预算）。"""
    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "required"
    )
    llm = ScriptedLLM([
        action("检索增强 基础概念"),
        judge_reply([]),      # unknown → required 阻断
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线")])
    driver = run_driver(orchestrator, llm)
    assert orchestrator.executed == []
    runner = driver._action_runner
    assert runner is not None
    assert runner.budget is not None and runner.budget.reserved == 0


def test_unchanged_proposal_leaves_no_plan_side_effects(monkeypatch):
    """M8：提案未变（revision 给出同一 query）→ 丢弃且计划图无幽灵节点。"""
    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "advisory"
    )
    from deepresearch_agent.harness.action_models import canonical_sha256

    rubric_id = f"rubric_r{canonical_sha256({'run_id': 'run-coreact-test'})[:16]}_s0"
    llm = ScriptedLLM([
        action_by_id("plan_1", "检索增强 重复方向"),
        judge_reply([{"criterion_id": f"{rubric_id}_evidence_presence",
                      "status": "fail", "reason": "无增量"}]),
        action_by_id("plan_1", "检索增强 重复方向"),  # 提案未变（同一查询）
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线")])
    driver = run_driver(orchestrator, llm)
    assert orchestrator.executed == []
    plan = driver.state.plan
    # 计划图与初始候选一致：未变提案不得追加/改写节点
    assert [node.task_id for node in plan.task_graph.nodes] == ["plan_1", "plan_2"]
    assert any(r.startswith("pre_action_unrevised") for r in driver.coreact_loop.failure_reasons)


def test_revision_rejected_by_deterministic_admission(monkeypatch):
    """M2：修订提案若违反确定性硬门（重复查询）→ 拒绝派发、记录原因、无幽灵节点。"""
    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "advisory"
    )
    from deepresearch_agent.harness.action_models import canonical_sha256

    digest = canonical_sha256({"run_id": "run-coreact-test"})[:16]
    rubric_id_s1 = f"rubric_r{digest}_s1"  # 第 2 个决策步（第 1 批提交后 step_index=1）
    llm = ScriptedLLM([
        # 第 1 步：正常执行 A（judge 全 unknown → advisory 降级继续）
        action_by_id("plan_1", "检索增强 基础概念"),
        judge_reply([]),
        # 第 2 步：B 被 judge 判 fail → 修订提案为已执行过的 A 查询（重复）
        action_by_id("plan_2", "检索增强 新方向"),
        judge_reply([{"criterion_id": f"{rubric_id_s1}_evidence_presence",
                      "status": "fail", "reason": "同方向"}]),
        action("检索增强 基础概念"),
        # 第 3 步：ready
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，基础说明")])
    driver = run_driver(orchestrator, llm)
    assert [t.parameters["query"] for t in orchestrator.executed] == ["检索增强 基础概念"]
    assert any("revision_rejected: duplicate_query" in r for r in driver.coreact_loop.failure_reasons), (
        driver.coreact_loop.failure_reasons
    )
    # 被硬门拒绝的修订提案不得在计划图留下幽灵节点
    assert [node.task_id for node in driver.state.plan.task_graph.nodes] == ["plan_1", "plan_2"]


def test_controller_injection_hash_matches_captured_prompt():
    """M10/M12：注入记录的 messages_hash 必须等于实际发送 prompt 的 sha256。"""
    import hashlib

    llm = ScriptedLLM([action("检索增强 基础概念"), ready()])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，基础说明")])
    driver = run_driver(orchestrator, llm)
    records = [item for item in driver.coreact_loop.rubric_injections if "controller" in item.targets]
    assert records
    expected = hashlib.sha256(llm.prompts[0].encode("utf-8")).hexdigest()
    assert records[0].messages_hash == expected


def test_worker_injection_hash_matches_recorded_guidance():
    """M10：worker 注入记录哈希 = 记录里实际落盘的 guidance 载荷哈希。"""
    import hashlib
    import json as _json

    from deepresearch_agent.harness.contracts import SourceMode

    class Provider:
        mode = SourceMode.WEB
        provider_name = "fake_web"

        async def search(self, query, *, top_k, search_depth, filters, call_context):
            from deepresearch_agent.agents.multi_agent.core.retrieval_result import (
                RetrievalMetadata, RetrievalResult,
            )

            return [RetrievalResult(
                granularity="Chunk", evidence=f"检索增强是一种技术路线：{query}",
                metadata=RetrievalMetadata(source_id="s-1", source_type="webpage"),
                source="tavily_search", source_mode="web",
            )]

    def execute_action(state, task, *, progress_callback=None):
        from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor

        executor = RetrievalExecutor(provider=Provider())
        signal = state.plan.to_execution_signal()
        signal.tasks = [task.model_dump()]
        signal.execution_sequence = [task.task_id]
        result = executor.execute_task(task, state, signal)
        return result.success, result.error, result.record

    llm = ScriptedLLM([action("检索增强 基础概念"), ready()])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，基础说明")])
    orchestrator.execute_action = execute_action
    driver = run_driver(orchestrator, llm)
    env = driver.state.execution_records[-1].metadata.environment
    guidance_dump = env["guidance"]
    expected = hashlib.sha256(
        _json.dumps(guidance_dump, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    worker_records = [item for item in driver.coreact_loop.rubric_injections if "worker" in item.targets]
    assert worker_records
    assert worker_records[0].messages_hash == expected


# ---------- 第二轮复审修复（M1/M2/M4/M5/M6/M7） ----------

def test_accepted_revision_transfers_reservation_and_completes(monkeypatch):
    """M1：修订替换不额外占用配额（max_tool_calls=1 时替换动作仍可派发），
    运行结束时 reservation 归零。"""
    from deepresearch_agent.harness.budgets import BudgetLimits
    from deepresearch_agent.harness.action_models import canonical_sha256

    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "advisory"
    )
    rubric_id = f"rubric_r{canonical_sha256({'run_id': 'run-coreact-test'})[:16]}_s0"
    llm = ScriptedLLM([
        action_by_id("plan_1", "检索增强 重复方向"),
        judge_reply([{"criterion_id": f"{rubric_id}_evidence_presence",
                      "status": "fail", "reason": "同方向"}]),
        action("检索增强 修正方向"),          # 修订提案（新 query）
        judge_reply([]),                      # 修订后判定 unknown → advisory 继续
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，修正说明")])
    context = make_context(budget_limits=BudgetLimits(max_tool_calls=1))
    driver = PlanExecuteReportDriver(context, orchestrator)
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())

    assert [t.parameters["query"] for t in orchestrator.executed] == ["检索增强 修正方向"]
    assert driver._action_runner.budget.reserved == 0
    env = driver.state.execution_records[-1].metadata.environment["coreact"]
    assert env["pre_action_verification"]["proposal_revision_used"] is True


def test_multiple_problem_actions_do_not_crash(monkeypatch):
    """M2：K=2 批内两个问题动作 → 单次修订后整批替换，不崩溃且预约归零。"""
    from deepresearch_agent.harness.action_models import canonical_sha256

    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "required"
    )
    monkeypatch.setattr("deepresearch_agent.harness.coreact_loop.COREACT_MAX_PARALLEL", 2)
    rubric_id = f"rubric_r{canonical_sha256({'run_id': 'run-coreact-test'})[:16]}_s0"
    fail = lambda: judge_reply([{"criterion_id": f"{rubric_id}_evidence_presence",
                                 "status": "fail", "reason": "同方向"}])
    llm = ScriptedLLM([
        action_by_id("plan_1", "检索增强 重复方向"),
        fail(), fail(),                       # 批内两个动作都判 fail
        action("检索增强 修订方向"),           # 一次修订
        judge_reply([]),                      # required unknown → 仍不得派发
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线")])
    driver = run_driver(orchestrator, llm)

    assert orchestrator.executed == []  # 修订后 unknown → required 阻断
    assert driver._action_runner.budget.reserved == 0
    assert driver.coreact_loop.pre_action_history  # 失败判定与成本入审计历史


def test_rejected_revision_does_not_mutate_existing_plan_node(monkeypatch):
    """M6：被硬门拒绝的修订不得改写既有候选节点（detached 准入）。"""
    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "advisory"
    )
    from deepresearch_agent.harness.action_models import canonical_sha256

    digest = canonical_sha256({"run_id": "run-coreact-test"})[:16]
    llm = ScriptedLLM([
        action_by_id("plan_1", "检索增强 基础概念"),
        judge_reply([]),
        action_by_id("plan_2", "检索增强 新方向"),
        judge_reply([{"criterion_id": f"rubric_r{digest}_s1_evidence_presence",
                      "status": "fail", "reason": "同方向"}]),
        action_by_id("plan_2", "检索增强 基础概念"),  # 修订为已执行查询（重复）
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，基础说明")])
    driver = run_driver(orchestrator, llm)
    plan_2 = next(node for node in driver.state.plan.task_graph.nodes if node.task_id == "plan_2")
    assert plan_2.parameters["query"] == "检索增强 新方向"  # 原值未被拒绝的提案改写


def test_crash_recovery_restores_guidance_and_pre_verdict():
    """M4：intent 持久化的 guidance/pre_verification 在恢复重放中还原。"""
    from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskNode
    from deepresearch_agent.harness.action_models import (
        ActionRubric, PreActionVerification,
    )
    from deepresearch_agent.harness.coreact_state import CoreActLoopState, PendingAction
    from deepresearch_agent.harness.pre_action_advisor import task_rubric_from_action_rubric

    guidance = ActionRubric.model_validate({
        "schema_version": 2, "rubric_id": "rubric_resume", "task_id": "plan_1",
        "phase": "explore", "phase_rule_version": "phase_rule_v1",
        "criteria": [
            {"criterion_id": "c1", "description": "证据", "check_kind": "evidence_presence",
             "weight": 1.5, "required": True, "min_evidence": 1},
            {"criterion_id": "c2", "description": "来源", "check_kind": "source_policy",
             "weight": 1.0, "required": True},
        ],
    })
    task = TaskNode(task_id="plan_1", task_type="web_search", source_mode="web",
                    description="检索增强 基础概念", parameters={"query": "检索增强 基础概念"})
    rubric = task_rubric_from_action_rubric(guidance, task_id="plan_1")
    pre = PreActionVerification(
        status="unknown", rubric_id=guidance.rubric_id, rubric_hash=guidance.content_hash,
        verifier="pre_action_advisor", verifier_version="v1", degraded=True,
        degraded_reason="advisory_unknown_continued",
    )
    llm = ScriptedLLM([ready()])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，恢复说明")])

    from deepresearch_agent.harness.contracts import SourceMode

    class Provider:
        mode = SourceMode.WEB
        provider_name = "fake_web"

        async def search(self, query, *, top_k, search_depth, filters, call_context):
            from deepresearch_agent.agents.multi_agent.core.retrieval_result import (
                RetrievalMetadata, RetrievalResult,
            )

            return [RetrievalResult(
                granularity="Chunk", evidence=f"检索增强是一种技术路线：{query}",
                metadata=RetrievalMetadata(source_id="s-1", source_type="webpage"),
                source="tavily_search", source_mode="web",
            )]

    executed_tasks: list[str] = []

    def execute_action(state, task, *, progress_callback=None):
        from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor

        executed_tasks.append(task.task_id)
        executor = RetrievalExecutor(provider=Provider())
        signal = state.plan.to_execution_signal()
        signal.tasks = [task.model_dump()]
        signal.execution_sequence = [task.task_id]
        result = executor.execute_task(task, state, signal)
        return result.success, result.error, result.record

    orchestrator.execute_action = execute_action
    driver = PlanExecuteReportDriver(make_context(), orchestrator)
    driver.coreact_loop = CoreActLoopState(
        phase="act",
        pending_action=PendingAction(
            task=task.model_dump(mode="json"), rubric=rubric.model_dump(mode="json"),
            attempt=1, guidance=guidance.model_dump(mode="json"),
            pre_verification=pre.model_dump(mode="json"),
        ),
    )
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())

    assert executed_tasks == ["plan_1"]  # 重放不重新决策
    env = driver.state.execution_records[-1].metadata.environment
    # 恢复重放后 worker 仍收到同一 guidance（真实执行器消费并落盘）
    assert env["guidance"]["content_hash"] == guidance.content_hash
    assert env["coreact"]["guidance"]["content_hash"] == guidance.content_hash
    assert env["coreact"]["pre_action_verification"]["status"] == "unknown"
    assert env["coreact"]["pre_action_verification"]["degraded"] is True
    # 恢复重放同样产生 worker 注入证据
    assert any("worker" in item.targets for item in driver.coreact_loop.rubric_injections)


def test_advisor_budget_gate_and_failed_usage_accounting():
    """M5：allow_llm=False 零调用；调用失败仍计一次（不记零成本）。"""
    from types import SimpleNamespace

    from deepresearch_agent.harness.action_models import ActionRubric

    guidance = ActionRubric.model_validate({
        "schema_version": 2, "rubric_id": "rubric_budget", "task_id": "",
        "criteria": [
            {"criterion_id": "c1", "description": "证据", "check_kind": "evidence_presence",
             "weight": 1.5, "required": True},
            {"criterion_id": "c2", "description": "来源", "check_kind": "source_policy",
             "weight": 1.0, "required": True},
        ],
    })
    task = SimpleNamespace(task_type="web_search", description="d",
                           parameters={"query": "新查询"})

    class NoCallLLM:
        def __init__(self):
            self.calls = 0

        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1
            raise RuntimeError("must not be called")

    llm = NoCallLLM()
    advisor = PreActionAdvisor(llm=llm)
    pre = advisor.advise(guidance=guidance, task=task, allowed_tools={"web_search"},
                         executed_queries=[], allow_llm=False)
    assert llm.calls == 0 and pre.usage["calls"] == 0

    class FailingLLM(NoCallLLM):
        def invoke(self, messages):
            self.calls += 1
            raise RuntimeError("transport down")

    failing = FailingLLM()
    pre_fail = PreActionAdvisor(llm=failing).advise(
        guidance=guidance, task=task, allowed_tools={"web_search"},
        executed_queries=[], allow_llm=True,
    )
    assert failing.calls == 1
    assert pre_fail.usage["calls"] == 1  # 失败尝试计入成本


# ---------- 第三轮复审修复（per-action 许可 / 共享预算 / 归档 / 重试持久化 / 失败成本） ----------

def _two_action_batch(guidance):
    from deepresearch_agent.harness.action_models import ActionSpec
    from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskNode

    task = TaskNode(task_id="plan_1", task_type="web_search", source_mode="web",
                    description="d", parameters={"query": "q"})
    spec = ActionSpec(action_id="action_plan_1", task_id="plan_1", query="q", tool="web_search")
    return BatchAction(
        spec=spec, task=task,
        rubric=task_rubric_from_action_rubric(guidance.bind(task_id="plan_1"), task_id="plan_1"),
        guidance=guidance.bind(task_id="plan_1"),
    )


def _guidance(rubric_id="rubric_gate"):
    from deepresearch_agent.harness.action_models import ActionRubric

    return ActionRubric.model_validate({
        "schema_version": 2, "rubric_id": rubric_id, "task_id": "",
        "criteria": [
            {"criterion_id": "c1", "description": "证据", "check_kind": "evidence_presence",
             "weight": 1.5, "required": True},
            {"criterion_id": "c2", "description": "来源", "check_kind": "source_policy",
             "weight": 1.0, "required": True},
        ],
    })


def test_gate_recomputes_permission_and_charges_shared_budget():
    """第 1/2 项：批内每个动作前重算许可；评估消耗计入共享 llm_tokens。"""
    from deepresearch_agent.config.settings import (
        REPORT_RESERVED_TOKENS,
        VERIFICATION_RESERVED_TOKENS,
    )
    from deepresearch_agent.harness.action_models import PreActionVerification
    from deepresearch_agent.harness.action_runner import ActionRunner, DispatchBudget

    guidance = _guidance()
    llm = ScriptedLLM([])
    orchestrator = FakeOrchestrator(llm, [])
    driver = PlanExecuteReportDriver(make_context(), orchestrator)
    driver._action_runner = ActionRunner(
        execute_action=lambda state, task: (True, None, None),
        max_parallel=2, max_concurrency=2, allowed_tools={"web_search"},
        budget=DispatchBudget(used_fn=lambda: 0, limit=30),
    )
    actions = [_two_action_batch(guidance)]
    second = BatchAction(
        spec=actions[0].spec, task=actions[0].task.model_copy(update={"task_id": "plan_2"}),
        rubric=actions[0].rubric, guidance=actions[0].guidance,
    )
    batch = [actions[0], second]
    driver._action_runner.budget.reserve(len(batch))

    permissions: list[bool] = []

    class StubAdvisor:
        def advise(self, **kwargs):
            allowed = bool(kwargs["allow_llm"])
            permissions.append(allowed)
            return PreActionVerification(
                status="unknown", rubric_id=guidance.rubric_id,
                rubric_hash=guidance.content_hash, verifier="stub", verifier_version="1",
                usage={
                    "calls": 1 if allowed else 0,
                    "estimated_tokens": 2_000 if allowed else 0,
                    "estimated": True,
                },
            )

    reserve = REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS
    gate = driver._coreact_pre_action_gate(
        admitted=batch, guidance=guidance, pre_action_mode="advisory",
        pre_action_advisor=StubAdvisor(), source_tools={"web_search"},
        executed_queries=[], completed=[], max_decisions=8,
        remaining=reserve + 1_500 + 100,  # 只够第一次
    )
    admitted, _ = asyncio.run(gate)

    assert len(admitted) == 2  # unknown → advisory 降级继续
    assert permissions == [True, False]  # 第二次调用前重算许可
    assert driver.context.budget_usage.llm_tokens == 2_000  # 共享预算已记账


def test_accepted_revision_archives_original_assessments(monkeypatch):
    """第 3 项：修订接受时原批次判定（含成本）先归档，不静默丢失。"""
    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "advisory"
    )
    from deepresearch_agent.harness.action_models import canonical_sha256

    rubric_id = f"rubric_r{canonical_sha256({'run_id': 'run-coreact-test'})[:16]}_s0"
    llm = ScriptedLLM([
        action_by_id("plan_1", "检索增强 重复方向"),
        judge_reply([{"criterion_id": f"{rubric_id}_evidence_presence",
                      "status": "fail", "reason": "同方向"}]),
        action("检索增强 修正方向"),
        judge_reply([]),
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，修正说明")])
    driver = run_driver(orchestrator, llm)

    assert [t.parameters["query"] for t in orchestrator.executed] == ["检索增强 修正方向"]
    reasons = [item["reason"] for item in driver.coreact_loop.pre_action_history]
    assert "batch_replaced_by_revision" in reasons
    assert driver.coreact_loop.pre_action_history[0]["verification"]["status"] == "fail"


def test_retry_intent_persists_guidance_and_pre_verdict():
    """第 4 项：修正重试的 PendingAction 同样持久化 guidance/pre_verification。"""
    from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskNode
    from deepresearch_agent.harness.action_models import (
        ActionSpec, PreActionVerification,
    )
    from deepresearch_agent.harness.coreact_state import CoreActLoopState

    guidance = _guidance("rubric_retry")
    llm = ScriptedLLM([action("检索增强 修正后查询")])
    orchestrator = FakeOrchestrator(llm, [])
    driver = PlanExecuteReportDriver(make_context(), orchestrator)
    driver.coreact_loop = CoreActLoopState(phase="act")
    task = TaskNode(task_id="plan_1", task_type="web_search", source_mode="web",
                    description="原查询", parameters={"query": "原查询"})
    spec = ActionSpec(action_id="action_plan_1", task_id="plan_1", query="原查询", tool="web_search")
    action_obj = BatchAction(
        spec=spec, task=task,
        rubric=task_rubric_from_action_rubric(guidance.bind(task_id="plan_1"), task_id="plan_1"),
        guidance=guidance.bind(task_id="plan_1"),
    )
    pre = PreActionVerification(
        status="unknown", rubric_id=guidance.rubric_id, rubric_hash=guidance.content_hash,
        verifier="pre_action_advisor", verifier_version="1",
    )
    correction = asyncio.run(
        driver._coreact_correction(8, guidance, {"action_plan_1": pre})
    )

    class Verdict:
        retry_reason = "证据不足"

    retry = asyncio.run(correction(action_obj, Verdict(), None))
    assert retry is not None and retry.guidance is not None
    pending = driver.coreact_loop.pending_action
    assert pending is not None
    assert pending.guidance["content_hash"] == guidance.content_hash
    assert pending.pre_verification["status"] == "unknown"
    assert pending.attempt == 2


def test_advisor_failed_call_preserves_known_consumption():
    """第 5 项：畸形响应按已知成本记账；传输失败按保守估算（不记零成本）。"""
    from types import SimpleNamespace

    guidance = _guidance("rubric_cost")
    task = SimpleNamespace(task_type="web_search", description="d",
                           parameters={"query": "q"})
    pre = PreActionVerification = __import__(
        "deepresearch_agent.harness.action_models", fromlist=["PreActionVerification"]
    ).PreActionVerification

    class MalformedLLM:
        def __init__(self):
            self.calls = 0

        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1
            class Content:
                content = "这不是 JSON 但确实返回了 200 个字符" * 5

            return Content()

    malformed = MalformedLLM()
    result = PreActionAdvisor(llm=malformed).advise(
        guidance=guidance, task=task, allowed_tools={"web_search"},
        executed_queries=[], allow_llm=True,
    )
    assert malformed.calls == 1
    assert result.usage["calls"] == 1
    assert result.usage["estimated_tokens"] > 0
    assert result.usage["response_known"] is True

    class TransportFailLLM(MalformedLLM):
        def invoke(self, messages):
            self.calls += 1
            raise TimeoutError("deadline")

    transport = TransportFailLLM()
    result2 = PreActionAdvisor(llm=transport).advise(
        guidance=guidance, task=task, allowed_tools={"web_search"},
        executed_queries=[], allow_llm=True,
    )
    assert result2.usage["calls"] == 1
    assert result2.usage["estimated_tokens"] > 0  # 保守估算，非零
    assert result2.usage["response_known"] is False


def test_advisor_bind_failure_is_zero_cost_and_oversized_response_costed_fully():
    """第 4 轮修复：bind 失败 = 零调用零成本；超长响应按全量计算成本。"""
    from types import SimpleNamespace

    guidance = _guidance("rubric_cost2")
    task = SimpleNamespace(task_type="web_search", description="d", parameters={"query": "q"})

    class BindFailsLLM:
        def __init__(self):
            self.calls = 0

        def bind(self, **kwargs):
            raise RuntimeError("bind unsupported")

        def invoke(self, messages):
            self.calls += 1
            raise AssertionError("不得调用")

    llm = BindFailsLLM()
    result = PreActionAdvisor(llm=llm).advise(
        guidance=guidance, task=task, allowed_tools={"web_search"},
        executed_queries=[], allow_llm=True,
    )
    assert llm.calls == 0
    assert result.usage == {"calls": 0, "estimated_tokens": 0}

    class OversizedLLM:
        def __init__(self):
            self.calls = 0

        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1

            class Content:
                # 超长但完整返回（生成配额内）：内容有多样性，token 数接近线性
                content = "".join(f"第{i}条 证据内容说明；" for i in range(220))

            return Content()

    oversized = OversizedLLM()
    result2 = PreActionAdvisor(llm=oversized).advise(
        guidance=guidance, task=task, allowed_tools={"web_search"},
        executed_queries=[], allow_llm=True,
    )
    assert oversized.calls == 1
    assert result2.usage["response_known"] is True
    # 全量响应计入成本（截断到 2000 字符会显著低估）
    assert result2.usage["estimated_tokens"] >= 900
