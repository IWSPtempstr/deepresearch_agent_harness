"""RG-04：报告质量维度与前后判定衔接测试（followup §4.4/§4.5）。

验收：前后 hash 相同、失败历史保留、预算耗尽/畸形判分、草稿与最终结果隔离；
质量维度明确非官方分数且不参与 completed 判定。
"""
import asyncio
import json

import pytest

from deepresearch_agent.harness.quality_assessment import (
    QUALITY_DIMENSIONS,
    QUALITY_PROMPT_VERSION,
    ReportQualityAssessment,
    assess_report_quality,
)

from test_coreact_driver import FakeOrchestrator, ScriptedLLM, action, make_context, ready


def dimensions_payload(status="pass", extra=None):
    dims = {name: {"status": status, "reason": "诊断理由"} for name in QUALITY_DIMENSIONS}
    if extra:
        dims.update(extra)
    return json.dumps({"dimensions": dims}, ensure_ascii=False)


def judge_with(payload: str):
    def judge(report, query):
        return json.loads(payload), payload, "prompt" * 20

    return judge


# ---------- 维度协议：非官方、无分数、未执行不造分 ----------

def test_quality_dimensions_are_nonofficial_and_scoreless():
    assessment = ReportQualityAssessment()
    assert assessment.advisory_only is True
    assert "非官方" in assessment.note or "不是官方" in assessment.note
    assert set(assessment.empty_dimensions().dimensions) == set(QUALITY_DIMENSIONS)
    dumped = json.dumps(assessment.model_dump())
    assert "score" not in dumped  # 协议本身没有分数字段


def test_quality_not_run_without_judge_or_budget():
    no_judge = assess_report_quality(judge=None, report="# 报告", query="问题")
    assert no_judge.status == "not_run"
    assert no_judge.usage["reason"] == "judge_unavailable"
    assert no_judge.prompt_version is None

    over_budget = assess_report_quality(
        judge=judge_with(dimensions_payload()), report="# 报告", query="问题",
        remaining_tokens=100, reserve_tokens=1000,
    )
    assert over_budget.status == "not_run"
    assert over_budget.usage["reason"] == "budget_reserved"
    assert over_budget.usage["calls"] == 0


def test_quality_pass_and_fail_status_aggregation():
    passed = assess_report_quality(
        judge=judge_with(dimensions_payload()), report="# 报告", query="问题",
        remaining_tokens=10_000,
    )
    assert passed.status == "pass"
    assert passed.prompt_version == QUALITY_PROMPT_VERSION
    assert all(item.status == "pass" for item in passed.dimensions.values())

    failed = assess_report_quality(
        judge=judge_with(dimensions_payload(extra={"insight": {"status": "fail", "reason": "仅是罗列"}})),
        report="# 报告", query="问题", remaining_tokens=10_000,
    )
    assert failed.status == "fail"  # 诊断失败：仅展示，不参与 Contract
    assert failed.advisory_only is True

    mixed = assess_report_quality(
        judge=judge_with(dimensions_payload(extra={"insight": {"status": "unknown", "reason": "不足"}})),
        report="# 报告", query="问题", remaining_tokens=10_000,
    )
    assert mixed.status == "unknown"


def test_quality_malformed_and_score_payloads_do_not_fabricate_pass():
    def boom(report, query):
        raise RuntimeError("judge down")

    broken = assess_report_quality(judge=boom, report="# 报告", query="问题", remaining_tokens=10_000)
    assert broken.status == "not_run"
    assert broken.usage["calls"] == 1  # 失败尝试计一次
    assert broken.usage["response_known"] is False

    def not_json(report, query):
        return {}, "不是 JSON", "p"

    malformed = assess_report_quality(
        judge=not_json, report="# 报告", query="问题", remaining_tokens=10_000,
    )
    assert malformed.status == "not_run"
    assert "malformed" in malformed.usage["reason"]

    scored = assess_report_quality(
        judge=judge_with(json.dumps({
            "dimensions": {name: {"status": "pass", "reason": "ok", "score": 95}
                           for name in QUALITY_DIMENSIONS},
        })),
        report="# 报告", query="问题", remaining_tokens=10_000,
    )
    # 带分数字段的维度按 unknown 处理（采用任何数值都等于伪造分数）
    assert all(item.status == "unknown" for item in scored.dimensions.values())
    assert scored.status == "unknown"


# ---------- 前后判定同 rubric 身份（RG-04 关联验收） ----------

def test_pre_and_post_verdicts_share_rubric_hash(monkeypatch):
    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "advisory"
    )
    llm = ScriptedLLM([
        action("检索增强 基础概念"),
        {"verdicts": []},  # 动作前 unknown → advisory 降级继续
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [(True, "检索增强是一种技术路线，基础说明")])
    driver = __import__(
        "deepresearch_agent.harness.drivers_plan_execute", fromlist=["PlanExecuteReportDriver"]
    ).PlanExecuteReportDriver(make_context(), orchestrator)
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())

    env = driver.state.execution_records[-1].metadata.environment["coreact"]
    guidance_hash = env["guidance"]["content_hash"]
    pre_hash = env["pre_action_verification"]["rubric_hash"]
    post_hash = env["rubric_hash"]
    assert guidance_hash and len(guidance_hash) == 64
    assert pre_hash == guidance_hash  # 动作前与指导同一 rubric
    assert post_hash == guidance_hash  # 动作后与动作前同一 rubric
    # 前后判定分离：pre=unknown（advisory 降级）、post 独立判定
    assert env["pre_action_verification"]["status"] == "unknown"
    assert env["verdict"]["rubric_id"] == env["pre_action_verification"]["rubric_id"]


def test_failed_attempt_history_retained_with_hash(monkeypatch):
    """失败尝试保留（含 hash），active 只指向最新有效结果。"""
    monkeypatch.setattr(
        "deepresearch_agent.harness.coreact_loop.COREACT_PRE_ACTION_MODE", "advisory"
    )
    llm = ScriptedLLM([
        action("检索增强 无关查询"),
        {"verdicts": []},
        action("检索增强 修正查询"),
        {"verdicts": []},
        ready(),
    ])
    orchestrator = FakeOrchestrator(llm, [
        (True, "完全无关的内容"),
        (True, "检索增强是一种技术路线，修正说明"),
    ])
    driver = __import__(
        "deepresearch_agent.harness.drivers_plan_execute", fromlist=["PlanExecuteReportDriver"]
    ).PlanExecuteReportDriver(make_context(), orchestrator)
    asyncio.run(driver.plan())
    asyncio.run(driver.execute())

    records = driver.state.execution_records
    attempts = [record.metadata.environment["coreact"] for record in records]
    assert len(attempts) >= 2  # attempt1（失败）+ attempt2（修正）都保留
    hashes = {item["rubric_hash"] for item in attempts}
    assert len(hashes) == 1  # 同一 guidance 身份贯穿全部尝试
    assert all(item["rubric_hash"] for item in attempts)


# ---------- 报告管线接入（默认关闭；开启后仅写报告诊断） ----------

def test_report_pipeline_attaches_quality_when_enabled(monkeypatch):
    import deepresearch_agent.harness.report_pipeline as report_pipeline

    monkeypatch.setattr(report_pipeline, "REPORT_QUALITY_ASSESSMENT_ENABLED", True)
    llm = ScriptedLLM([
        # plan() 不耗 LLM；report() 的质量诊断消耗这一条
        {"dimensions": {name: {"status": "pass", "reason": "ok"} for name in QUALITY_DIMENSIONS}},
    ])
    orchestrator = FakeOrchestrator(llm, [])
    context = make_context(original_query="检索增强是什么", resolved_query="检索增强是什么")
    driver = __import__(
        "deepresearch_agent.harness.drivers_plan_execute", fromlist=["PlanExecuteReportDriver"]
    ).PlanExecuteReportDriver(context, orchestrator)
    asyncio.run(driver.plan())
    # 质量诊断准入需要预留之外的充足余量：抬高 Run 预算上限
    context = driver.context
    context.budget_limits.max_llm_tokens = 500_000
    report = asyncio.run(driver.report())

    assert report.strip()
    assessment = driver.report_result.report_metrics["quality_assessment"]
    assert assessment["status"] == "pass"
    assert assessment["advisory_only"] is True
    assert set(assessment["dimensions"]) == set(QUALITY_DIMENSIONS)
    assert assessment["judge_model"] is None or isinstance(assessment["judge_model"], str)


def test_report_pipeline_default_off_records_not_run():
    llm = ScriptedLLM([{"dimensions": {name: {"status": "pass", "reason": "ok"} for name in QUALITY_DIMENSIONS}}])
    orchestrator = FakeOrchestrator(llm, [])
    context = make_context(original_query="检索增强是什么", resolved_query="检索增强是什么")
    driver = __import__(
        "deepresearch_agent.harness.drivers_plan_execute", fromlist=["PlanExecuteReportDriver"]
    ).PlanExecuteReportDriver(context, orchestrator)
    asyncio.run(driver.plan())
    asyncio.run(driver.report())

    assessment = driver.report_result.report_metrics["quality_assessment"]
    assert assessment["status"] == "not_run"  # 默认关闭：不调用、不造分
    assert assessment["usage"]["calls"] == 0
    assert len(llm.prompts) == 0  # 未触发任何模型调用


# ---------- 报告 DTO：从 checkpoint 只读暴露（草稿/失败隔离） ----------

@pytest.fixture
async def database(tmp_path):
    from deepresearch_agent.persistence import Database

    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'quality-dto.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


async def _create_run(database) -> str:
    from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
    from deepresearch_agent.harness.contracts import SourceMode, WorkflowMode
    from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository

    session = await SessionRepository(database).create(SessionCreate(title="quality-dto"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="问题",
                      client_message_id="quality-dto-1"),
        RunCreate(session_id=session.session_id, trigger_message_id="assigned-atomically",
                  source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
                  config_snapshot={"min_evidence": 1}),
    )
    return run.run_id


async def test_report_dto_exposes_checkpointed_quality_assessment(database):
    from backend.app.api.v1.runs import _quality_assessment_for
    from deepresearch_agent.harness.checkpoints import CheckpointManager
    from deepresearch_agent.harness.contracts import RunStatus, SourceMode, WorkflowMode
    from deepresearch_agent.harness.run_context import RunContext
    from deepresearch_agent.persistence.repositories import CheckpointRepository

    run_id = await _create_run(database)
    assessment = {
        "status": "pass",
        "dimensions": {name: {"status": "pass", "reason": "ok"} for name in QUALITY_DIMENSIONS},
        "note": "非官方诊断",
        "advisory_only": True,
        "usage": {"calls": 1, "estimated_tokens": 123},
    }

    async def _save(status: RunStatus, checkpoint_stage: str) -> None:
        context = RunContext(
            run_id=run_id, session_id="s", trigger_message_id="m",
            source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
            status=status, original_query="问题",
        )
        context.workflow_state = {"report_result": {"report_metrics": {"quality_assessment": assessment}}}
        await CheckpointManager(CheckpointRepository(database)).save(context, checkpoint_stage)

    # completed（已验收）→ 白名单视图（usage 只透出 calls）
    await _save(RunStatus.COMPLETED, "completed")
    restored = await _quality_assessment_for(database, run_id, "completed")
    assert restored is not None
    assert restored["status"] == "pass"
    assert set(restored["dimensions"]) == set(QUALITY_DIMENSIONS)
    assert restored["usage"] == {"calls": 1}
    assert "estimated_tokens" not in restored["usage"]

    # 失败/中间态运行：即使 checkpoint 里有报告快照也不暴露（草稿隔离）
    assert await _quality_assessment_for(database, run_id, "failed") is None
    assert await _quality_assessment_for(database, run_id, "verifying") is None

    # 无 checkpoint 的 run → None
    other_run = await _create_run(database)
    assert await _quality_assessment_for(database, other_run, "completed") is None


async def test_report_dto_corrupt_shapes_never_raise(database):
    """嵌套损坏形状（顶层数组 / workflow_state 为数组 / 非 dict）一律 None。"""
    from backend.app.api.v1.runs import _quality_assessment_for

    run_id = await _create_run(database)
    from sqlalchemy import text as _sql

    for index, bad in enumerate(('[1, 2, 3]', '{"workflow_state": [1, 2]}',
                                 '{"workflow_state": {"report_result": [1]}}',
                                 '{"workflow_state": {"report_result": {"report_metrics": "x"}}}',
                                 'not-json')):
        async with database.sessions() as session:
            await session.execute(_sql(
                "insert into checkpoints (checkpoint_id, run_id, version, stage, state_json, "
                "state_hash, schema_version, created_at) values "
                "(:cid, :rid, :ver, 'completed', :sj, :hash, 1, '2026-01-01T00:00:00Z')"
            ), {"cid": f"ckpt_bad_{index}", "rid": run_id, "ver": 90 + index,
                "sj": bad, "hash": "0" * 64})
            await session.commit()
        assert await _quality_assessment_for(database, run_id, "completed") is None


def test_quality_malformed_status_types_are_contained():
    """status 为 [] / {} 等不可哈希类型：按 unknown 处理，不抛异常。"""
    for bad_status in ([], {}, 3, None):
        payload = {"dimensions": {name: {"status": bad_status, "reason": "x"} for name in QUALITY_DIMENSIONS}}

        def judge(report, query, payload=payload):
            return payload, "raw", "prompt"

        result = assess_report_quality(judge=judge, report="# 报告", query="问题", remaining_tokens=100_000)
        assert result.status == "unknown"
        assert all(item.status == "unknown" for item in result.dimensions.values())


def test_quality_executed_all_unknown_is_unknown_not_not_run():
    """已执行但全部 unknown → overall unknown（不得伪装未执行；calls=1）。"""
    def judge(report, query):
        payload = {"dimensions": {name: {"status": "unknown", "reason": "信息不足"} for name in QUALITY_DIMENSIONS}}
        return payload, json.dumps(payload), "p" * 100

    result = assess_report_quality(judge=judge, report="# 报告", query="问题", remaining_tokens=100_000)
    assert result.status == "unknown"
    assert result.usage["calls"] == 1 and result.usage["response_known"] is True


def test_quality_response_level_score_rejected():
    """响应顶层 score 字段：整份诊断按 unknown（不采用任何数值）。"""
    def judge(report, query):
        payload = {
            "score": 95,
            "dimensions": {name: {"status": "pass", "reason": "ok"} for name in QUALITY_DIMENSIONS},
        }
        return payload, json.dumps(payload), "p" * 100

    result = assess_report_quality(judge=judge, report="# 报告", query="问题", remaining_tokens=100_000)
    assert result.status == "unknown"
    assert result.usage["reason"] == "score_fields_rejected"


def test_quality_failure_reason_never_carries_provider_text():
    """provider 异常文本可能含密钥：usage.reason 只留类型码。"""
    secret = "https://provider.test?api_key=SECRET_TOKEN_ABC"

    def judge(report, query):
        raise RuntimeError(secret)

    result = assess_report_quality(judge=judge, report="# 报告", query="问题", remaining_tokens=100_000)
    assert result.status == "not_run"
    assert "SECRET" not in json.dumps(result.model_dump())
    assert result.usage["reason"] == "RuntimeError"


def test_quality_admission_protects_verification_reserve():
    """准入 = 预留 + prompt 估算 + 生成配额（CJK 长报告不侵蚀预留）。"""
    def judge(report, query):
        payload = {"dimensions": {name: {"status": "pass", "reason": "ok"} for name in QUALITY_DIMENSIONS}}
        return payload, json.dumps(payload), "p"

    big_report = "检索增强是一种技术路线。" * 2_000  # 长 CJK 正文节选
    reserve = 15_000
    # 只比预留多 2000：不够 prompt 估算 + 配额 → 零调用
    blocked = assess_report_quality(
        judge=judge, report=big_report, query="问题",
        remaining_tokens=reserve + 2_000, reserve_tokens=reserve,
    )
    assert blocked.status == "not_run"
    assert blocked.usage["calls"] == 0
    assert blocked.usage["reason"] == "budget_reserved"
    est = blocked.usage["prompt_estimate"]
    # 充足余量（预留 + 估算 + 配额 + 1）→ 允许
    allowed = assess_report_quality(
        judge=judge, report=big_report, query="问题",
        remaining_tokens=reserve + est + 800 + 1, reserve_tokens=reserve,
    )
    assert allowed.status == "pass"
    assert allowed.usage["calls"] == 1


# ---------- 适配器级成本口径回归（复审 finding 2） ----------

class _Content:
    def __init__(self, content):
        self.content = content


def test_judge_no_bind_capability_is_zero_cost():
    """无 bind 能力 = 未派发：零调用零成本（不侵蚀共享预算）。"""
    from deepresearch_agent.harness.quality_assessment import build_quality_judge

    class NoBindLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            return _Content("{}")

    llm = NoBindLLM()
    judge = build_quality_judge(llm)
    result = assess_report_quality(judge=judge, report="# 报告", query="问题", remaining_tokens=100_000)
    assert llm.calls == 0
    assert result.usage["calls"] == 0 and result.usage["estimated_tokens"] == 0
    assert result.status == "not_run"


def test_judge_bind_failure_is_zero_cost():
    from deepresearch_agent.harness.quality_assessment import build_quality_judge

    class BindFailsLLM:
        def __init__(self):
            self.calls = 0

        def bind(self, **kwargs):
            raise RuntimeError("unsupported")

        def invoke(self, messages):
            self.calls += 1
            return _Content("{}")

    llm = BindFailsLLM()
    result = assess_report_quality(
        judge=build_quality_judge(llm), report="# 报告", query="问题", remaining_tokens=100_000,
    )
    assert llm.calls == 0
    assert result.usage["calls"] == 0 and result.usage["estimated_tokens"] == 0


def test_judge_transport_failure_charges_conservative_estimate():
    """已派发但响应未知：计一次调用 + 保守估算（非零、response_known False）。"""
    from deepresearch_agent.harness.quality_assessment import (
        DEFAULT_ALLOCATION_TOKENS, build_quality_judge,
    )

    class TimeoutLLM:
        def __init__(self):
            self.calls = 0

        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1
            raise TimeoutError("deadline exceeded")

    llm = TimeoutLLM()
    result = assess_report_quality(
        judge=build_quality_judge(llm), report="# 报告", query="问题", remaining_tokens=100_000,
    )
    assert llm.calls == 1
    assert result.usage["calls"] == 1
    assert result.usage["response_known"] is False
    assert result.usage["estimated_tokens"] >= DEFAULT_ALLOCATION_TOKENS // 2


def test_judge_malformed_response_charges_full_known_cost():
    """已收到响应但解析失败：按 prompt+响应全量计费（不得记零成本）。"""
    from deepresearch_agent.harness.quality_assessment import (
        build_quality_judge, count_tokens,
    )

    garbage = "这不是 JSON" * 120

    class GarbageLLM:
        def __init__(self):
            self.calls = 0

        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1
            return _Content(garbage)

    llm = GarbageLLM()
    result = assess_report_quality(
        judge=build_quality_judge(llm), report="# 报告", query="问题", remaining_tokens=100_000,
    )
    assert llm.calls == 1
    assert result.usage["calls"] == 1
    assert result.usage["response_known"] is True
    assert result.usage["estimated_tokens"] >= count_tokens(garbage)


def test_judge_non_object_and_empty_responses_charge_known_raw():
    from deepresearch_agent.harness.quality_assessment import build_quality_judge

    class ScalarLLM:
        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            return _Content("[1, 2, 3]")

    scalar = assess_report_quality(
        judge=build_quality_judge(ScalarLLM()), report="# 报告", query="问题", remaining_tokens=100_000,
    )
    assert scalar.usage["calls"] == 1 and scalar.usage["response_known"] is True
    assert scalar.usage["estimated_tokens"] > 0

    class EmptyLLM:
        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            return _Content("")

    empty = assess_report_quality(
        judge=build_quality_judge(EmptyLLM()), report="# 报告", query="问题", remaining_tokens=100_000,
    )
    assert empty.usage["calls"] == 1
    assert empty.usage["estimated_tokens"] > 0  # 已派发：按保守估算，不为零


def test_pipeline_charges_failed_judge_cost_to_shared_budget():
    """report_pipeline 把失败调用的估算成本计入共享预算（不静默归零）。"""
    import asyncio

    import deepresearch_agent.harness.report_pipeline as report_pipeline
    from deepresearch_agent.harness.drivers_plan_execute import PlanExecuteReportDriver
    from test_coreact_driver import FakeOrchestrator, ScriptedLLM, make_context

    monkey_enabled = True
    original = report_pipeline.REPORT_QUALITY_ASSESSMENT_ENABLED
    report_pipeline.REPORT_QUALITY_ASSESSMENT_ENABLED = monkey_enabled
    try:
        # 质量诊断拿到"不是 JSON"的响应（已派发但解析失败）
        llm = ScriptedLLM(["不是 JSON 的响应内容" * 50])
        orchestrator = FakeOrchestrator(llm, [])
        context = make_context(original_query="检索增强是什么", resolved_query="检索增强是什么")
        context.budget_limits.max_llm_tokens = 500_000
        driver = PlanExecuteReportDriver(context, orchestrator)
        asyncio.run(driver.plan())
        asyncio.run(driver.report())

        assessment = driver.report_result.report_metrics["quality_assessment"]
        assert assessment["status"] == "not_run"
        assert assessment["usage"]["calls"] == 1
        assert assessment["usage"]["estimated_tokens"] > 0
        assert driver.context.budget_usage.llm_tokens >= assessment["usage"]["estimated_tokens"]
    finally:
        report_pipeline.REPORT_QUALITY_ASSESSMENT_ENABLED = original
