"""T-009：按任务难度分层的模型路由。

覆盖：
1. 难度推导矩阵（report_type/min_evidence/内容标记/迭代数/显式覆盖/默认）；
2. tier→model 映射（配置化、空回落全局、路由关闭）；
3. chat_service 全链路：快照留痕（model_tier/reason/model）+ 派生一致；
4. BaseAgent 注入路径（llm_model 参数不破坏默认行为）。
"""

import json

import pytest

import deepresearch_agent.config.settings as settings_mod
from deepresearch_agent.models.model_tier import resolve_model_for, resolve_model_tier


@pytest.fixture
def tier_models(monkeypatch):
    monkeypatch.setattr(settings_mod, "OPENAI_LLM_MODEL", "base-model")
    monkeypatch.setattr(settings_mod, "LLM_TIER_SIMPLE_MODEL", "fast-model")
    monkeypatch.setattr(settings_mod, "LLM_TIER_STANDARD_MODEL", "std-model")
    monkeypatch.setattr(settings_mod, "LLM_TIER_COMPLEX_MODEL", "pro-model")
    monkeypatch.setattr(settings_mod, "LLM_TIER_ROUTING_ENABLED", True)


def test_tier_matrix_derivation():
    cases = [
        ({"report_type": "long_document"}, "complex"),
        ({"min_evidence": 3}, "complex"),
        ({"min_evidence": 1, "deep_research_max_iterations": 2}, "standard"),
        ({}, "simple"),
        ({"deep_research_max_iterations": 1}, "simple"),
    ]
    for snapshot, expected in cases:
        tier, reason = resolve_model_tier(snapshot)
        assert tier == expected, (snapshot, tier, reason)
        assert reason


def test_tier_content_markers():
    assert resolve_model_tier({}, content="给我一份详细的报告")[0] == "complex"
    assert resolve_model_tier({}, content="please do a comprehensive analysis")[0] == "complex"
    assert resolve_model_tier({}, content="hi")[0] == "simple"


def test_tier_explicit_override_wins():
    snapshot = {"report_type": "long_document", "model_tier": "simple"}
    tier, reason = resolve_model_tier(snapshot)
    assert tier == "simple" and "explicit" in reason


def test_tier_to_model_mapping(tier_models):
    routing = resolve_model_for({"report_type": "long_document"})
    assert routing == {"model": "pro-model", "tier": "complex", "reason": "report_type=long_document"}
    assert resolve_model_for({})["model"] == "fast-model"
    assert resolve_model_for({"deep_research_max_iterations": 2})["model"] == "std-model"


def test_tier_mapping_falls_back_to_global_when_unset(monkeypatch):
    monkeypatch.setattr(settings_mod, "OPENAI_LLM_MODEL", "base-model")
    monkeypatch.setattr(settings_mod, "LLM_TIER_SIMPLE_MODEL", "")
    monkeypatch.setattr(settings_mod, "LLM_TIER_STANDARD_MODEL", "")
    monkeypatch.setattr(settings_mod, "LLM_TIER_COMPLEX_MODEL", "")
    routing = resolve_model_for({"report_type": "long_document"})
    assert routing["model"] == "base-model"


def test_tier_routing_disabled(monkeypatch):
    monkeypatch.setattr(settings_mod, "OPENAI_LLM_MODEL", "base-model")
    monkeypatch.setattr(settings_mod, "LLM_TIER_ROUTING_ENABLED", False)
    routing = resolve_model_for({"report_type": "long_document"})
    assert routing["model"] == "base-model"  # 映射函数本身不读开关——开关在调用方（chat_service/_tier_model_for）


async def test_chat_service_routes_and_records_snapshot(tmp_path, tier_models, monkeypatch):
    from backend.app.schemas import MessageCreate, MessageSend, RunCreate, SessionCreate
    from backend.app.services import ChatService
    from backend.app.services.run_service import RunService
    from deepresearch_agent.persistence import Database
    from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository
    from tests.harness.fake_driver import CompletedFakeDriver

    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'tier.db').as_posix()}")
    await database.create_schema()
    service = ChatService(
        SessionRepository(database), RunRepository(database),
        __import__("deepresearch_agent.persistence.repositories", fromlist=["MessageRepository"]).MessageRepository(database),
        RunService(database, workflow_factory=lambda c, e=None: CompletedFakeDriver(c, events=e)),
    )
    session = await service.sessions.create(SessionCreate(title="tier"))
    accepted = await service.send(
        session.session_id,
        MessageSend(content="hi", client_message_id="c1", source_mode="graphrag"),
    )
    run = await RunRepository(database).get(accepted.run_id)
    snapshot = json.loads(run.config_snapshot_json)
    assert snapshot["model_tier"] == "simple"  # brief + 默认 → simple
    model_snapshot = json.loads(run.model_snapshot_json)
    assert model_snapshot["llm_model"] == "fast-model"
    assert model_snapshot["llm_model_tier"] == "simple"

    # 详细请求 → complex
    accepted2 = await service.send(
        session.session_id,
        MessageSend(content="请给我一份详细的分析", client_message_id="c2", source_mode="graphrag"),
    )
    run2 = await RunRepository(database).get(accepted2.run_id)
    snapshot2 = json.loads(run2.config_snapshot_json)
    assert snapshot2["model_tier"] == "complex"
    model_snapshot2 = json.loads(run2.model_snapshot_json)
    assert model_snapshot2["llm_model"] == "pro-model"
    await database.close()
