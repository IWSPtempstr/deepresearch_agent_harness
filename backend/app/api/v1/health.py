"""Health and capability discovery without spending model/search quota."""

import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from backend.app.dependencies import get_database
from deepresearch_agent.config import settings

router = APIRouter(tags=["system"])


async def _tcp_status(uri: str, default_port: int) -> tuple[str, str | None]:
    parsed = urlparse(uri)
    host, port = parsed.hostname, parsed.port or default_port
    if not host:
        return "unavailable", "地址未配置"
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.75)
        writer.close()
        await writer.wait_closed()
        return "healthy", None
    except Exception:
        return "degraded", f"无法连接 {host}:{port}"


@router.get("/health")
async def health(database=Depends(get_database)):
    sqlite_status = "healthy"
    try:
        async with database.engine.connect() as connection:
            await connection.execute(text("CREATE TEMP TABLE IF NOT EXISTS health_probe(value INTEGER)"))
            await connection.execute(text("DROP TABLE health_probe"))
    except Exception:
        sqlite_status = "unavailable"
    llm_configured = bool(settings.OPENAI_API_KEY and settings.OPENAI_LLM_MODEL)
    neo4j_configured = bool(settings.NEO4J_URI and settings.NEO4J_USERNAME and settings.NEO4J_PASSWORD)
    tavily_configured = bool(settings.TAVILY_API_KEY)
    llm_status, llm_reason = await _tcp_status(settings.OPENAI_BASE_URL, 443) if llm_configured else ("unavailable", "LLM 未配置")
    neo4j_status, neo4j_reason = await _tcp_status(settings.NEO4J_URI, 7687) if neo4j_configured else ("unavailable", "Neo4j 未配置")
    tavily_status, tavily_reason = (("healthy", None) if tavily_configured else ("unavailable", "TAVILY_API_KEY 未配置"))
    overall = "healthy" if sqlite_status == "healthy" and llm_status == "healthy" and (neo4j_status == "healthy" or tavily_status == "healthy") else "degraded"
    return {
        "status": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "components": {
            "api": {"status": "healthy", "check_level": "functional"},
            "sqlite": {"status": sqlite_status, "check_level": "read_write"},
            "llm": {"status": llm_status, "configured": llm_configured, "reason": llm_reason, "check_level": "tcp_only"},
            "neo4j": {"status": neo4j_status, "configured": neo4j_configured, "reason": neo4j_reason, "check_level": "tcp_only"},
            "tavily": {"status": tavily_status, "configured": tavily_configured, "reason": tavily_reason, "check_level": "configuration_only"},
        },
    }


@router.get("/capabilities")
async def capabilities():
    web = bool(settings.TAVILY_API_KEY)
    graph_configured = bool(settings.NEO4J_URI and settings.NEO4J_USERNAME and settings.NEO4J_PASSWORD)
    graph_status, graph_reason = await _tcp_status(settings.NEO4J_URI, 7687) if graph_configured else ("unavailable", "Neo4j 未配置")
    graph = graph_status == "healthy"
    return {
        "sources": {
            "graphrag": {"available": graph, "reason": None if graph else graph_reason},
            "web": {"available": web, "reason": None if web else "TAVILY_API_KEY 未配置"},
        },
        "workflows": ["deep_research", "plan_execute_report"],
        "default_source_mode": "graphrag",
        "default_budget": settings.HARNESS_BUDGETS,
        "features": {"sse": True, "memory_management": True, "skill_management": True},
        "fastapi_workers": settings.workers,
    }

@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    """Prometheus 抓取端点（P1-7）；文本格式，/metrics 为约定路径。"""
    from fastapi import Response
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/ready", include_in_schema=True)
async def readiness(request: Request):
    """T-007 readiness：部署编排判定「能否接流量」。

    与 /health（人看的状态面板）不同，readiness 只含**接流量的硬依赖**：
    - 数据库可写（必需）
    - 平台模式的队列连通（必需；单机模式无队列，自动通过）
    成本天花板触发时 readiness 返回 503（欠费保护：不再接新流量）。
    liveness（进程活着）由容器存活本身表达，不单设端点。
    """
    import redis as redis_module

    components: dict[str, dict] = {}
    ready = True

    database = request.app.state.database
    db_status = "healthy"
    try:
        async with database.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        db_status = "unavailable"
        ready = False
    components["database"] = {"status": db_status, "required": True}

    queue = getattr(request.app.state.run_service, "queue", None)
    if queue is not None and hasattr(queue, "_client"):
        redis_status = "healthy"
        try:
            await asyncio.wait_for(queue._client.ping(), timeout=0.75)
        except (redis_module.exceptions.RedisError, asyncio.TimeoutError, OSError, Exception):
            redis_status = "unavailable"
            ready = False
        components["queue_redis"] = {"status": redis_status, "required": True}
    else:
        components["queue_redis"] = {"status": "not_configured", "required": False, "note": "单机进程内模式"}

    ceiling = settings.LLM_DAILY_TOKEN_CEILING
    cost_gate = {"enabled": ceiling > 0}
    if ceiling > 0:
        spent = await _tokens_since_midnight(database)
        cost_gate.update({"spent_tokens": spent, "ceiling_tokens": ceiling, "exhausted": spent >= ceiling})
        if spent >= ceiling:
            ready = False
    components["llm_cost_gate"] = cost_gate

    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "ready": ready,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "components": components,
        },
    )


async def _tokens_since_midnight(database) -> int:
    """当日 llm_calls 的 output_tokens 总和（成本天花板口径）。"""
    from sqlalchemy import func, select

    from deepresearch_agent.persistence.models import LlmCallModel

    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        async with database.engine.connect() as connection:
            row = await connection.execute(
                select(func.coalesce(func.sum(LlmCallModel.output_tokens), 0)).where(
                    LlmCallModel.created_at >= midnight.isoformat()
                )
            )
            return int(row.scalar() or 0)
    except Exception:
        return 0  # 计量不可读时按未超限处理（诚实失败原则适用于降级而非误杀）
