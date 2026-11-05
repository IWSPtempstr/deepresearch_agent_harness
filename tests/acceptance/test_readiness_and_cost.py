"""T-007：readiness 端点、LLM 成本天花板、告警规则文件有效性。

覆盖：
1. /ready 单机模式：DB 健康→ready；DB 不可达→503；
2. /ready 成本天花板：spent >= ceiling → 503 + exhausted 标记；
3. ChatService.send 成本门禁：超限抛 LLM_RATE_LIMITED（429 语义）；
4. alert-rules.yml/prometheus.yml 可解析且规则引用的指标已注册。
"""

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from backend.app.main import create_app


@pytest.fixture
def app_client(tmp_path):
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'ready.db').as_posix()}"
    app = create_app(database_url=db_url)
    with TestClient(app) as client:
        yield client, tmp_path


def test_ready_ok_single_machine(app_client):
    client, _ = app_client
    response = client.get("/api/v1/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["components"]["database"]["status"] == "healthy"
    # 单机模式无队列：非必需组件自动通过
    assert body["components"]["queue_redis"]["required"] is False


async def test_ready_503_when_db_unreachable(tmp_path, monkeypatch):
    """DB 不可达 → readiness 503。

    不走 TestClient：lifespan 内抛异常会污染 portal 事件循环（后续测试报
    "Event loop is closed"）；直接调用路由协程并检查返回。
    """
    from unittest.mock import AsyncMock

    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'gone.db').as_posix()}"
    app = create_app(database_url=db_url)
    database = app.state.database
    broken_connect = AsyncMock(side_effect=OperationalError("stmt", {}, ConnectionError("db down")))
    stub_engine = type(
        "E", (),
        {"connect": broken_connect, "dispose": AsyncMock(return_value=None)},
    )()
    monkeypatch.setattr(database, "engine", stub_engine)

    from backend.app.api.v1.health import readiness

    class FakeRequest:
        pass

    request = FakeRequest()
    request.app = app
    response = await readiness(request)
    assert response.status_code == 503
    import json as _json

    body = _json.loads(response.body)
    assert body["ready"] is False
    assert body["components"]["database"]["status"] == "unavailable"


def test_ready_503_on_daily_token_ceiling(tmp_path, monkeypatch):
    import deepresearch_agent.config.settings as settings_mod
    import backend.app.api.v1.health as health_mod

    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'cost.db').as_posix()}"
    monkeypatch.setattr(settings_mod, "LLM_DAILY_TOKEN_CEILING", 100)
    app = create_app(database_url=db_url)
    with TestClient(app) as client:
        monkeypatch.setattr(health_mod, "_tokens_since_midnight", _async_return(500))
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        gate = response.json()["components"]["llm_cost_gate"]
        assert gate["exhausted"] is True and gate["spent_tokens"] == 500
        # 低于阈值恢复
        monkeypatch.setattr(health_mod, "_tokens_since_midnight", _async_return(50))
        assert client.get("/api/v1/ready").status_code == 200


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


async def test_send_rejected_when_daily_ceiling_exhausted(tmp_path, monkeypatch):
    """成本门禁在请求路径：spent >= ceiling → LLM_RATE_LIMITED（429 语义）。"""
    import deepresearch_agent.config.settings as settings_mod
    from backend.app.schemas import MessageCreate, MessageSend, SessionCreate
    from backend.app.services import ChatService
    from backend.app.services.run_service import RunService
    from deepresearch_agent.harness.errors import AppError, ErrorCode
    from deepresearch_agent.persistence import Database
    from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository

    monkeypatch.setattr(settings_mod, "LLM_DAILY_TOKEN_CEILING", 10)
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'cost2.db').as_posix()}")
    await database.create_schema()
    service = ChatService(
        SessionRepository(database), RunRepository(database),
        __import__("deepresearch_agent.persistence.repositories", fromlist=["MessageRepository"]).MessageRepository(database),
        RunService(database, workflow_factory=lambda c, e=None: None),
    )
    session = await service.sessions.create(SessionCreate(title="cost"))
    async def _spent(*args, **kwargs):
        return 999

    monkeypatch.setattr(service, "_tokens_spent_today", _spent)
    with pytest.raises(AppError) as exc_info:
        await service.send(
            session.session_id,
            MessageSend(content="hi", client_message_id="c1", source_mode="graphrag"),
        )
    assert exc_info.value.code == ErrorCode.LLM_RATE_LIMITED
    await database.close()


def test_alert_rules_reference_registered_metrics():
    """告警规则引用的指标名必须已在 metrics.py 注册（防漂移）。"""
    import importlib

    import prometheus_client

    rules = yaml.safe_load(Path("observability/alert-rules.yml").read_text(encoding="utf-8"))
    assert rules["groups"], "至少一个告警组"
    registry_names = set()
    for collector in prometheus_client.REGISTRY.collect():
        registry_names.add(collector.name)
        for sample in collector.samples:
            registry_names.add(sample.name)
    registered = {name.removeprefix("dra_") for name in registry_names if name.startswith("dra_")}
    expression_text = "".join(
        str(rule.get("expr", "")) for group in rules["groups"] for rule in group["rules"]
    )
    referenced = set()
    for name in registry_names:
        short = name.removeprefix("dra_")
        if short and short in expression_text:
            referenced.add(short)
    assert referenced, "告警规则应至少引用一个项目指标"
    import re as _re

    referenced_names = set(_re.findall(r"dra_[a-z_]+", expression_text))
    assert referenced_names, "告警应引用 dra_ 前缀指标"
    for token in referenced_names:
        assert token in registry_names or token.removeprefix("dra_") in registered, \
            f"告警引用未注册指标: {token}"


def test_prometheus_config_includes_alert_rules():
    config = yaml.safe_load(Path("observability/prometheus.yml").read_text(encoding="utf-8"))
    assert any("alert-rules.yml" in rule for rule in config.get("rule_files", []))
