"""FastAPI application entry wired to the persistent Harness main chain."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.api.v1 import router as api_router
from backend.app.services import ChatService, EventStreamService, RunService
from deepresearch_agent.config import settings
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence import Database
from deepresearch_agent.queueing import create_run_queue
from deepresearch_agent.events_fanout import RedisEventFanout
from deepresearch_agent.persistence.repositories import MessageRepository, RunRepository, SessionRepository
from deepresearch_agent.memory import MemoryService
from deepresearch_agent.persistence.repositories import AuditRepository, MemoryRepository
from deepresearch_agent.persistence.repositories import LearningReviewRepository, SkillRepository
from deepresearch_agent.evolution import PromotionPolicy, SkillEvaluator


def _error(request: Request, *, status_code: int, code: str, message: str, retryable: bool = False, details=None):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "retryable": retryable, "details": details or {}}, "request_id": getattr(request.state, "request_id", "unknown")},
    )


def create_app(
    *, database_url: str | None = None, workflow_factory=None,
    artifact_root: str | Path | None = None, skills_root: str | Path | None = None,
    auto_resume: bool | None = None,
) -> FastAPI:
    if not settings.LOCAL_MVP_SINGLE_WORKER and not settings.REDIS_URL:
        raise RuntimeError("单机模式必须设置 FASTAPI_WORKERS=1；多实例部署请配置 REDIS_URL 切换平台模式（Run 执行由独立 worker 消费）")
    database = Database(database_url or settings.APP_DATABASE_URL)
    # 平台模式共享一个 Redis 扇出：API 侧发布的控制事件与 worker 侧发布的
    # Run 事件都经它推给任意实例的 SSE 订阅者
    event_fanout = RedisEventFanout.from_url(settings.REDIS_URL) if settings.REDIS_URL else None
    run_service = RunService(
        database, workflow_factory=workflow_factory,
        artifact_root=artifact_root or settings.ARTIFACT_ROOT,
        skills_root=skills_root or settings.SKILLS_ROOT,
        # 平台模式：API 只投递唤醒信号，执行在 worker 进程（P0-2）
        queue=create_run_queue(settings.REDIS_URL) if settings.REDIS_URL else None,
        event_fanout=event_fanout,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await database.create_schema()
        await run_service.startup_recovery(auto_resume=settings.AUTO_RESUME_RUNS if auto_resume is None else auto_resume)
        yield
        await run_service.shutdown()
        await database.close()

    app = FastAPI(title="DeepResearch HybridRAG Local MVP", version="0.4.0", lifespan=lifespan)
    app.state.database = database
    app.state.run_service = run_service
    app.state.chat_service = ChatService(
        SessionRepository(database), RunRepository(database), MessageRepository(database), run_service,
    )
    app.state.event_stream = EventStreamService(run_service.events, run_service.runs, run_service.event_bus, fanout=event_fanout)
    app.state.memory_service = MemoryService(
        MemoryRepository(database), AuditRepository(database),
        user_max_tokens=settings.MEMORY_USER_MAX_TOKENS,
        project_max_tokens=settings.MEMORY_PROJECT_MAX_TOKENS,
        user_max_chars=settings.MEMORY_USER_MAX_CHARS,
        project_max_chars=settings.MEMORY_PROJECT_MAX_CHARS,
    )
    skill_repository = SkillRepository(database)
    app.state.skill_services = {
        "repository": skill_repository,
        "registry": run_service.skill_registry,
        "evaluator": SkillEvaluator(skill_repository, case_runner=run_service.run_evaluation_case if workflow_factory is None else None),
        "promotion": run_service.skill_promotion if workflow_factory is None else PromotionPolicy(
            skill_repository, run_service.skill_registry, AuditRepository(database),
        ),
        "learning": run_service.skill_learning,
        "reviews": LearningReviewRepository(database),
        "events": run_service.event_bus,
        "test_mode": workflow_factory is not None,
    }
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.FRONTEND_ORIGINS), allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        status_code = {
            ErrorCode.NOT_FOUND: 404, ErrorCode.CONFLICT: 409, ErrorCode.IDEMPOTENCY_CONFLICT: 409,
            ErrorCode.INVALID_SOURCE_MODE: 422, ErrorCode.TAVILY_API_KEY_MISSING: 503,
            ErrorCode.SOURCE_UNAVAILABLE: 503, ErrorCode.DATABASE_UNAVAILABLE: 503,
        }.get(exc.code, 400)
        return _error(request, status_code=status_code, code=exc.code.value, message=exc.message, retryable=exc.retryable, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return _error(request, status_code=422, code="VALIDATION_ERROR", message="请求参数校验失败", details={"errors": exc.errors()})

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        return _error(request, status_code=500, code="INTERNAL_ERROR", message="服务内部错误")

    app.include_router(api_router)
    return app


app = create_app()
