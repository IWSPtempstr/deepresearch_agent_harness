"""租户上下文解析（P2-8）：API key hash 认证 + 上下文注入。

兼容语义：库中无任何 api_keys 时，所有请求落到 'default' 租户——既有单机
用法与现有测试零变化；一旦创建过 api_keys，无 key/坏 key 请求返回 401 语义。
TENANT_AUTH_REQUIRED=true 可在无 key 时也强制要求认证。
"""

from __future__ import annotations

import contextvars
import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import func, select

from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.database import Database

DEFAULT_TENANT_ID = "default"

# 进程内当前租户：仓储盖章、Run 执行体与 LLM 网关限流键共用
_current_tenant: contextvars.ContextVar[str] = contextvars.ContextVar("tenant_id", default=DEFAULT_TENANT_ID)


def current_tenant_id() -> str:
    return _current_tenant.get()


def set_current_tenant(tenant_id: str) -> contextvars.Token:
    return _current_tenant.set(tenant_id or DEFAULT_TENANT_ID)


def hash_api_key(raw_key: str) -> str:
    """API key 只存 hash（sha256）；raw key 仅在创建响应里出现一次。"""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """返回 (raw_key, key_hash)。"""
    raw = f"dra-{secrets.token_urlsafe(32)}"
    return raw, hash_api_key(raw)


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    authenticated: bool  # 是否凭有效 api key 认证（default 兜底为 False）


class TenantRegistry:
    def __init__(self, database: Database):
        self.database = database

    async def any_api_keys(self) -> bool:
        from deepresearch_agent.persistence.models import ApiKeyModel

        async with self.database.sessions() as session:
            count = (await session.execute(select(func.count()).select_from(ApiKeyModel))).scalar_one()
        return count > 0

    async def resolve(self, raw_key: str | None) -> TenantContext:
        from deepresearch_agent.config import settings
        from deepresearch_agent.persistence.models import ApiKeyModel

        required = settings.TENANT_AUTH_REQUIRED or await self.any_api_keys()
        if not raw_key:
            if required:
                raise AppError(ErrorCode.TENANT_UNAUTHENTICATED, "缺少 X-API-Key 请求头", retryable=False)
            return TenantContext(tenant_id=DEFAULT_TENANT_ID, authenticated=False)
        async with self.database.sessions() as session:
            row = (await session.execute(
                select(ApiKeyModel).where(ApiKeyModel.key_hash == hash_api_key(raw_key), ApiKeyModel.revoked_at.is_(None))
            )).scalar_one_or_none()
        if row is None:
            raise AppError(ErrorCode.TENANT_UNAUTHENTICATED, "API key 无效或已吊销", retryable=False)
        return TenantContext(tenant_id=row.tenant_id, authenticated=True)

    async def create_tenant(self, name: str, budget: dict | None = None) -> str:
        import json

        from deepresearch_agent.persistence.models import TenantModel
        from deepresearch_agent.persistence.repositories.utils import new_id, utc_now_iso

        tenant_id = new_id("ten")
        async with self.database.transaction() as session:
            session.add(TenantModel(tenant_id=tenant_id, name=name, budget_json=json.dumps(budget or {}), created_at=utc_now_iso()))
        return tenant_id

    async def create_api_key(self, tenant_id: str, label: str = "") -> str:
        from deepresearch_agent.persistence.models import ApiKeyModel
        from deepresearch_agent.persistence.repositories.utils import new_id, utc_now_iso

        raw, key_hash = generate_api_key()
        async with self.database.transaction() as session:
            session.add(ApiKeyModel(key_id=new_id("key"), tenant_id=tenant_id, key_hash=key_hash, label=label, created_at=utc_now_iso()))
        return raw


class TenantMiddleware:
    """纯 ASGI 中间件：解析 X-API-Key 并把租户写入 contextvar。

    上下文在中间件任务内设置，对同一请求的后续 endpoint/依赖可见。
    """

    def __init__(self, app, registry: TenantRegistry):
        self.app = app
        self.registry = registry

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        try:
            context = await self.registry.resolve(headers.get("x-api-key"))
        except AppError as exc:
            response_payload = {
                "error": {"code": exc.code.value, "message": exc.message, "retryable": False, "details": {}},
                "request_id": "-",
            }
            import json

            body = json.dumps(response_payload).encode("utf-8")
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        token = set_current_tenant(context.tenant_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_tenant.reset(token)
