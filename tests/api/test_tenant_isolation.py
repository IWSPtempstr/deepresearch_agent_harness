"""多租户隔离（P2-8）：两个租户的数据严格隔离，由自动化测试证明。

场景：
1. 创建租户 A/B 各签发 API key；
2. 各自创建 Session/Message/Run 并执行完成；
3. 断言：A 的会话列表看不到 B 的会话；A 访问 B 的 Run 详情/事件/证据 → 401/403 语义；
4. 无 key 访问（存在 api_keys 后）→ 401；
5. 无任何 key 的旧路径（default 兜底）在空库上不回归。
"""

import time

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.tenancy import TenantRegistry
from deepresearch_agent.persistence import Database
from tests.harness.fake_driver import CompletedFakeDriver


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'tenant.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


def _make_app(database):
    return create_app(
        database_url=database.url,
        workflow_factory=lambda context, events=None: CompletedFakeDriver(context, events=events),
        auto_resume=False,
    )


def _run_to_completion(client: TestClient, headers: dict, content: str) -> str:
    session = client.post("/api/v1/sessions", json={"title": content[:20]}, headers=headers).json()
    accepted = client.post(f"/api/v1/sessions/{session['session_id']}/messages", json={
        "client_message_id": f"m-{session['session_id']}", "content": content,
        "source_mode": "graphrag", "workflow_mode": "deep_research",
    }, headers=headers).json()
    run_id = accepted["run_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/runs/{run_id}", headers=headers).json().get("status")
        if status in {"completed", "failed"}:
            break
        time.sleep(0.2)
    assert status == "completed", f"Run 未完成: {status}"
    return session["session_id"], run_id


async def test_two_tenants_are_strictly_isolated(database):
    registry = TenantRegistry(database)
    tenant_a = await registry.create_tenant("tenant-a")
    tenant_b = await registry.create_tenant("tenant-b")
    key_a = await registry.create_api_key(tenant_a, "a")
    key_b = await registry.create_api_key(tenant_b, "b")
    headers_a = {"X-API-Key": key_a}
    headers_b = {"X-API-Key": key_b}

    with TestClient(_make_app(database)) as client:
        session_a, run_a = _run_to_completion(client, headers_a, "租户A的研究问题")
        session_b, run_b = _run_to_completion(client, headers_b, "租户B的研究问题")
        assert session_a != session_b

        # 会话列表互不可见
        listed_a = {item["session_id"] for item in client.get("/api/v1/sessions", headers=headers_a).json()["items"]}
        listed_b = {item["session_id"] for item in client.get("/api/v1/sessions", headers=headers_b).json()["items"]}
        assert session_a in listed_a and session_b not in listed_a
        assert session_b in listed_b and session_a not in listed_b

        # 跨租户访问 Run 详情 / 事件流 / 证据 → 拒绝（403 语义）
        for path in (f"/api/v1/runs/{run_b}", f"/api/v1/runs/{run_b}/events", f"/api/v1/runs/{run_b}/evidence"):
            response = client.get(path, headers=headers_a)
            assert response.status_code == 403, f"{path} 应拒绝跨租户访问: {response.status_code}"

        # 本租户访问自己的 Run 正常
        assert client.get(f"/api/v1/runs/{run_a}", headers=headers_a).json()["status"] == "completed"

        # 会话详情同样隔离
        assert client.get(f"/api/v1/sessions/{session_b}", headers=headers_a).status_code == 404


async def test_missing_key_rejected_once_api_keys_exist(database):
    registry = TenantRegistry(database)
    await registry.create_tenant("tenant-x")
    await registry.create_api_key(await _any_tenant(registry), "x")
    with TestClient(_make_app(database)) as client:
        assert client.get("/api/v1/sessions").status_code == 401
        assert client.get("/api/v1/sessions", headers={"X-API-Key": "dra-invalid"}).status_code == 401


async def _any_tenant(registry: TenantRegistry) -> str:
    from sqlalchemy import select
    from deepresearch_agent.persistence.models import TenantModel

    async with registry.database.sessions() as session:
        row = (await session.execute(select(TenantModel))).scalars().first()
    return row.tenant_id


async def test_ops_paths_exempt_from_tenant_auth(database):
    """评审 H3：/health 与 /metrics 无 key 必须可达（healthcheck/Prometheus 依赖）。"""
    registry = TenantRegistry(database)
    tenant_x = await registry.create_tenant("tenant-x")
    await registry.create_api_key(tenant_x, "x")  # 有 key 存在 → 全局强制认证
    with TestClient(_make_app(database)) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/metrics").status_code == 200
        # 豁免仅限运维路径，业务面仍 401
        assert client.get("/api/v1/sessions").status_code == 401


async def test_default_tenant_fallback_without_api_keys(database):
    """空库（无 api_keys）时不带 key 的旧用法不回归。"""
    with TestClient(_make_app(database)) as client:
        session = client.post("/api/v1/sessions", json={"title": "legacy"}).json()
        assert session["tenant_id"] == "default" if "tenant_id" in session else True
        accepted = client.post(f"/api/v1/sessions/{session['session_id']}/messages", json={
            "client_message_id": "m-1", "content": "旧路径",
            "source_mode": "graphrag", "workflow_mode": "deep_research",
        }).json()
        assert accepted["run_id"]
