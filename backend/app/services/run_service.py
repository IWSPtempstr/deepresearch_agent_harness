"""Single-process background scheduler connected to the persistent Harness."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from deepresearch_agent.agents.multi_agent.integration.multi_agent_factory import MultiAgentFactory
from deepresearch_agent.agents.multi_agent.executor.worker_coordinator import WorkerCoordinator
from deepresearch_agent.config.settings import ARTIFACT_ROOT, AUTO_RESUME_RUNS
from deepresearch_agent.harness.contracts import SourceMode, WorkflowMode
from deepresearch_agent.harness.event_bus import EventBus
from deepresearch_agent.harness.recovery import RecoveryManager
from deepresearch_agent.harness.runtime import HarnessRuntime
from deepresearch_agent.harness.workflow import PlanExecuteReportDriver
from deepresearch_agent.persistence import ArtifactStore, Database
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository,
    SessionRepository, MemoryRepository, AuditRepository,
    LearningReviewRepository, SkillRepository,
)
from deepresearch_agent.context import ArtifactEditContextBuilder, ContextBuilder, ContextCompactor
from deepresearch_agent.memory import MemoryExtractor, MemoryService
from deepresearch_agent.sessions import SessionSearchService
from deepresearch_agent.config import settings
from deepresearch_agent.evolution import PromotionPolicy, SkillLearningService, SkillLoader, SkillRegistry
from deepresearch_agent.models.prefix_cache import clear_run_cancelled, mark_run_cancelled, tracker as prefix_tracker
from deepresearch_agent.models.get_models import get_llm_model
from deepresearch_agent.retrieval.router import create_default_router
from deepresearch_agent.retrieval.base import TimeoutBoundProvider
from backend.app.schemas import MessageCreate, RunCreate, SessionCreate

logger = logging.getLogger("deepresearch.run_service")


class RunService:
    def __init__(
        self,
        database: Database,
        *,
        workflow_factory: Callable[[Any], Any] | None = None,
        artifact_root=ARTIFACT_ROOT,
        skills_root=settings.SKILLS_ROOT,
        queue=None,
        event_fanout=None,
    ):
        self.database = database
        self.runs = RunRepository(database)
        self.messages = MessageRepository(database)
        self.events = EventRepository(database)
        self.event_bus = EventBus(self.events, fanout=event_fanout)
        self.artifact_store = ArtifactStore(artifact_root)
        self._external_factory = workflow_factory
        self._router = None if workflow_factory else create_default_router()
        # 队列模式（外部 worker）：schedule 只投递唤醒信号，执行在 worker 进程；
        # None 时保持旧的进程内 asyncio 调度（单机 MVP 与测试路径）
        self.queue = queue
        self._tasks: dict[str, asyncio.Task] = {}
        memory_repository = MemoryRepository(database)
        memory_service = MemoryService(
            memory_repository, AuditRepository(database),
            user_max_tokens=settings.MEMORY_USER_MAX_TOKENS,
            project_max_tokens=settings.MEMORY_PROJECT_MAX_TOKENS,
            user_max_chars=settings.MEMORY_USER_MAX_CHARS,
            project_max_chars=settings.MEMORY_PROJECT_MAX_CHARS,
        )
        session_repository = SessionRepository(database)
        self.sessions = session_repository
        artifact_repository = ArtifactRepository(database)
        skill_repository = SkillRepository(database)
        self.skill_registry = SkillRegistry(skills_root, skill_repository)
        skill_loader = SkillLoader(self.skill_registry)
        self.context_builder = ContextBuilder(
            messages=self.messages, sessions=session_repository, memory_service=memory_service,
            session_search=SessionSearchService(self.messages, session_repository),
            compactor=ContextCompactor(
                session_repository, self.messages,
                threshold_tokens=min(
                    settings.CONTEXT_COMPRESSION_THRESHOLD_TOKENS,
                    max(100, int(settings.CONTEXT_MAX_TOKENS * settings.CONTEXT_COMPRESSION_THRESHOLD_RATIO)),
                ),
                protect_recent_messages=settings.CONTEXT_PROTECT_RECENT_MESSAGES,
                target_tokens=settings.CONTEXT_COMPRESSION_TARGET_TOKENS,
            ),
            skill_loader=skill_loader,
            artifact_context_builder=ArtifactEditContextBuilder(
                self.runs, artifact_repository, self.artifact_store,
            ),
            max_tokens=settings.CONTEXT_MAX_TOKENS,
            max_chars=settings.CONTEXT_MAX_CHARS,
            recent_turns=settings.CONTEXT_RECENT_TURNS,
            session_search_top_k=settings.SESSION_SEARCH_TOP_K,
            session_search_window=settings.SESSION_SEARCH_WINDOW,
        )
        memory_llm = None
        if (
            settings.MEMORY_BACKGROUND_REVIEW_ENABLED
            and workflow_factory is None
            and settings.OPENAI_API_KEY
            and settings.MEMORY_LLM_MODEL
        ):
            memory_llm = get_llm_model(
                model=settings.MEMORY_LLM_MODEL, temperature=0.0,
                max_tokens=settings.MEMORY_LLM_MAX_OUTPUT_TOKENS,
            )
        self.memory_extractor = None if memory_llm is None else MemoryExtractor(
            memory_service, self.runs, self.messages, ContractRepository(database),
            llm=memory_llm,
            min_confidence=settings.MEMORY_LLM_MIN_CONFIDENCE,
            max_candidates=settings.MEMORY_LLM_MAX_CANDIDATES,
            max_message_chars=settings.MEMORY_LLM_MAX_MESSAGE_CHARS,
        )
        learning_llms = []
        if settings.LEARNING_REVIEW_ENABLED and workflow_factory is None and settings.OPENAI_API_KEY and settings.LEARNING_REVIEW_MODEL:
            learning_llms = [
                get_llm_model(model=settings.LEARNING_REVIEW_MODEL, temperature=0.0, max_tokens=settings.LEARNING_REVIEW_MAX_OUTPUT_TOKENS),
                get_llm_model(model=settings.LEARNING_REVIEW_MODEL, temperature=0.0, max_tokens=settings.LEARNING_REVIEW_MAX_OUTPUT_TOKENS),
            ]
        self.skill_learning = SkillLearningService(
            database, LearningReviewRepository(database), skill_repository, self.skill_registry,
            proposer_llm=learning_llms[0] if learning_llms else None,
            critic_llm=learning_llms[1] if learning_llms else None,
            events=self.event_bus, enabled=settings.LEARNING_REVIEW_ENABLED,
            max_retries=settings.LEARNING_REVIEW_MAX_RETRIES,
        )
        self.skill_promotion = PromotionPolicy(
            skill_repository, self.skill_registry, AuditRepository(database),
            require_real_replay=True, staged=True,
            canary_percent=settings.SKILL_CANARY_INITIAL_PERCENT,
        )

    def schedule(self, run_id: str) -> asyncio.Task:
        if self.queue is not None:
            # 队列模式：仅投递唤醒信号；执行由 worker 进程消费（runs 表是事实源）。
            # T-003：enqueue task 不再无观测——done-callback 捕获异常并计数，
            # run 留在 queued，由租约扫描（≤15s）重建投递，不在请求路径同步重试
            task = asyncio.create_task(self.queue.enqueue(run_id), name=f"enqueue:{run_id}")
            task.add_done_callback(self._observe_enqueue)
            return task
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return existing
        clear_run_cancelled(run_id)
        task = asyncio.create_task(self._execute(run_id), name=f"run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))
        return task

    @staticmethod
    def _observe_enqueue(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        from deepresearch_agent.observability import QUEUE_ENQUEUE_FAILURES

        logger.error(
            "唤醒信号入队失败（run_id=%s，run 留在 queued，等待租约扫描重建投递）",
            task.get_name().removeprefix("enqueue:"),
            exc_info=exc,
        )
        try:
            QUEUE_ENQUEUE_FAILURES.inc()
        except Exception:
            pass

    async def execute_now(self, run_id: str) -> Any:
        """worker 进程入口：直接执行一个 Run（与 API 进程内调度共用同一执行体）。"""
        run = await self.runs.get(run_id)
        from backend.app.tenancy import set_current_tenant

        token = set_current_tenant(run.tenant_id if run is not None else "default")
        try:
            return await self._execute(run_id)
        finally:
            from backend.app.tenancy import _current_tenant

            _current_tenant.reset(token)

    async def _execute(self, run_id: str):
        agents: list[Any] = []

        def workflow_factory(context, events=None):
            if self._external_factory is not None:
                return self._external_factory(context, events)
            if context.workflow_mode is WorkflowMode.DEEP_RESEARCH:
                # T-407：独立 deep_research 引擎已退役。存量活动 Run（历史创建、
                # 非终态）显式拒绝执行并列入人工处理清单——不静默换引擎、
                # 不自动取消；新请求在 API 边界已归一化为 plan_execute_report。
                # （拒绝先于 Provider 构建——不为已拒绝的 Run 触碰外部资源。）
                from deepresearch_agent.harness.errors import AppError, ErrorCode

                raise AppError(
                    ErrorCode.LEGACY_WORKFLOW_UNSUPPORTED,
                    f"Run {context.run_id} 为旧 deep_research 工作流（活动状态）；"
                    "该引擎已退役，请人工处理（查看/取消/重建为 plan_execute_report），不自动续跑",
                )
            provider = TimeoutBoundProvider(
                self._router.for_mode(context.source_mode),
                timeout_seconds=context.budget_limits.tool_timeout_seconds,
            )
            worker = WorkerCoordinator(
                retrieval_provider=provider,
                max_parallel_workers=context.budget_limits.max_concurrency,
            )
            # T-016：角色级模型分层（Anthropic lead/subagent 模式）——
            # 优先消费建 Run 时冻结在 model_snapshot 的映射（跨进程/重启
            # recorded==actual）；缺省（旧 Run）才按当前 settings 现算。
            # T-406：角色归一化（旧 planner key → brief）；装配走唯一 coreact
            # 工厂（BriefBuilder，无 Reflector）。
            frozen = dict(context.model_snapshot or {}).get("role_models")
            if isinstance(frozen, dict) and frozen:
                role_models = frozen
            else:
                # T-406：brief 角色的历史兼容——旧 Run 的显式 planner_model 仍生效
                # （不现算覆盖历史选择，design §8）；verifier 角色为语义判定器
                # 预留（COREACT_SEMANTIC_ENABLED 开启前仅作审计冻结）。
                role_config = dict(context.config_snapshot or {})
                if not role_config.get("brief_model") and role_config.get("planner_model"):
                    role_config["brief_model"] = role_config["planner_model"]
                role_models = {
                    role: self._role_model_for_snapshot(role_config, role)
                    for role in ("brief", "reporter", "controller", "verifier")
                }
            bundle = MultiAgentFactory.create_coreact_bundle(
                retrieval_provider=provider, worker=worker, role_models=role_models,
            )
            return PlanExecuteReportDriver(context, bundle.orchestrator, events=events)

        runtime = HarnessRuntime(
            run_repository=self.runs, message_repository=self.messages,
            event_repository=self.events, checkpoint_repository=CheckpointRepository(self.database),
            evidence_repository=EvidenceRepository(self.database), contract_repository=ContractRepository(self.database),
            trajectory_repository=PlanTaskToolRepository(self.database), workflow_factory=workflow_factory,
            artifact_store=self.artifact_store, artifact_repository=ArtifactRepository(self.database),
            event_bus=self.event_bus,
            context_builder=self.context_builder, memory_extractor=self.memory_extractor, skill_distiller=None,
            prefix_tracker=prefix_tracker,
            lease_seconds=settings.RUN_LEASE_SECONDS,
        )
        try:
            return await runtime.execute_run(run_id)
        finally:
            for agent in agents:
                if hasattr(agent, "close"):
                    agent.close()
            # T-006：终态清除 per-run prefix 记账（防 worker 生命周期内无界增长）
            clear_run_cancelled(run_id)
            prefix_tracker.evict_run(run_id)
            run = await self.runs.get(run_id)
            config = json.loads(run.config_snapshot_json or "{}") if run else {}
            if not config.get("evaluation_run") and run and run.status not in {"paused", "pausing"}:
                await self.skill_learning.enqueue_for_run(run_id)
                snapshot = json.loads(run.model_snapshot_json or "{}") if run else {}
                selected = snapshot.get("skill") if isinstance(snapshot, dict) else None
                if isinstance(selected, dict) and selected.get("status") == "canary":
                    checks = await ContractRepository(self.database).list_for_run(run_id)
                    safety_violation = any(item.kind == "source_match" and item.passed != 1 for item in checks)
                    await self.skill_promotion.observe_run(
                        name=str(selected.get("name")), version=str(selected.get("version")),
                        succeeded=bool(run and run.status == "completed"), safety_violation=safety_violation,
                    )

    def _role_model_for(self, context, role: str) -> dict[str, str]:
        """T-016：按角色解析模型（四层顺序见 model_tier.resolve_role_model）。"""
        return self._role_model_for_snapshot(dict(context.config_snapshot or {}), role)

    def _role_model_for_snapshot(self, config_snapshot: dict, role: str) -> dict[str, str]:
        """T-016/T-406：按角色解析模型（快照可含历史兼容改写后的显式覆盖）。"""
        from deepresearch_agent.models.model_tier import resolve_role_model

        return resolve_role_model(config_snapshot, role=role, settings=settings)

    def _tier_model_for(self, context) -> str | None:
        """T-009：从 Run config_snapshot 解析分层模型 ID；路由关闭/未配置返回 None（旧行为）。"""
        if not settings.LLM_TIER_ROUTING_ENABLED:
            return None
        from deepresearch_agent.models.model_tier import resolve_model_for

        routing = resolve_model_for(context.config_snapshot or {})
        return routing["model"] if routing["model"] != settings.OPENAI_LLM_MODEL else None

    async def run_evaluation_case(self, *, query: str, source_mode: str, workflow_mode: str, forced_skill=None) -> dict[str, Any]:
        """Execute one isolated real Harness Run for paired Skill evaluation."""
        # T-407：评测入口与 HTTP 边界同规则——旧标识归一化（存量 eval 用例
        # 中的 deep_research 不再系统性产出诚实失败）。
        if workflow_mode == "deep_research":
            workflow_mode = "plan_execute_report"
        session = await self.sessions.create(SessionCreate(title="[Skill Eval]"))
        message, run, _ = await self.runs.create_for_user_message(
            MessageCreate(session_id=session.session_id, role="user", content=query, client_message_id=None, metadata={"evaluation": True}),
            RunCreate(
                session_id=session.session_id, trigger_message_id="assigned-atomically",
                source_mode=SourceMode(source_mode), workflow_mode=WorkflowMode(workflow_mode),
                config_snapshot={
                    "evaluation_run": True, "disable_skills": forced_skill is None,
                    "forced_skill": forced_skill, "min_evidence": 1,
                    "report_type": "brief", "deep_research_max_iterations": 1,
                    "schema_version": 1,
                },
                budget=settings.HARNESS_BUDGETS,
            ),
        )
        await self.runs.update_model_snapshot(run.run_id, {
            "llm_model": settings.OPENAI_LLM_MODEL,
            "learning_review_model": settings.LEARNING_REVIEW_MODEL,
            "evaluation_arm": "treatment" if forced_skill else "control",
        })
        await self._execute(run.run_id)
        finished = await self.runs.get(run.run_id)
        checks = await ContractRepository(self.database).list_for_run(run.run_id)
        usage = json.loads(finished.usage_json or "{}") if finished else {}
        check_map = {item.kind: bool(item.passed) for item in checks}
        limits = usage.get("limits", {}) if isinstance(usage, dict) else {}
        used = usage.get("usage", usage) if isinstance(usage, dict) else {}
        return {
            "run_id": run.run_id, "completed": bool(finished and finished.status == "completed"),
            "status": finished.status if finished else "missing",
            "citation_integrity": float(check_map.get("citation_integrity", False)),
            "claim_support": float(check_map.get("claim_support", False)),
            "tool_policy_violations": 0,
            "source_leakage": 0 if check_map.get("source_match", False) else 1,
            "safety_passed": bool(check_map.get("source_match", False)),
            "llm_tokens": int(used.get("llm_tokens", 0) or 0),
            "wall_time_seconds": float(used.get("elapsed_seconds", 0) or 0),
            "checks": check_map,
        }

    async def startup_recovery(self, *, auto_resume: bool = AUTO_RESUME_RUNS) -> list[str]:
        run_ids = await RecoveryManager(self.runs).scan(auto_resume=auto_resume)
        for run_id in run_ids:
            self.schedule(run_id)
        await self.skill_learning.recover()
        return run_ids

    async def recover_expired(self) -> list[str]:
        """P0-3b 周期扫描：把租约过期的在跑 Run 标记 interrupted 并重新投递。

        多 worker 重复投递是安全的：执行侧以条件 UPDATE 抢租约，
        抢不到的实例直接让位（RunLeaseLost 语义），租约就是互斥量。

        T-006：queue 模式（API/worker 队列传输）下返回待重投 run_ids 交由调用方
        经队列投递——worker 内不再进程内 spawn 执行（旧行为绕过 WORKER_CONCURRENCY
        信号量、且关停时被立即取消写 CANCELLED，违背 drain 语义）。
        """
        run_ids = await RecoveryManager(self.runs).scan(auto_resume=True)
        if self.queue is None:
            for run_id in run_ids:
                self.schedule(run_id)
        return run_ids

    async def cancel(self, run_id: str) -> bool:
        requested = await self.runs.request_cancel(run_id)
        if not requested:
            return False
        mark_run_cancelled(run_id)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        else:
            run = await self.runs.get(run_id)
            if run is not None:
                await self.runs.update_status(
                    run_id, status="cancelled", current_stage="cancelled",
                    usage=json.loads(run.usage_json or "{}"),
                )
                await self.event_bus.publish(run_id, "run.cancelled", stage="cancelled", payload={"status": "cancelled"})
        return True

    async def pause(self, run_id: str) -> bool:
        requested = await self.runs.request_pause(run_id)
        if not requested:
            return False
        await self.event_bus.publish(
            run_id, "run.pause_requested", stage="pausing", payload={"status": "pausing"},
        )
        task = self._tasks.get(run_id)
        if task is None or task.done():
            if await self.runs.finalize_pause(run_id):
                run = await self.runs.get(run_id)
                await self.event_bus.publish(
                    run_id, "run.paused", stage="paused",
                    payload={"status": "paused", "resume_from_status": run.current_stage if run else "queued"},
                )
        return True

    async def resume(self, run_id: str) -> bool:
        before = await self.runs.get(run_id)
        pending_pause = bool(
            before and json.loads(before.config_snapshot_json or "{}").get("pause_requested")
        )
        cancelling_pending_pause = bool(
            pending_pause and before and before.status != "paused"
        )
        resumed = await self.runs.resume(run_id)
        if resumed:
            current = await self.runs.get(run_id)
            await self.event_bus.publish(
                run_id, "run.resume_requested",
                stage=current.current_stage if current else "queued",
                payload={
                    "status": current.status if current else "queued",
                    "cancelled_pending_pause": cancelling_pending_pause,
                },
            )
            if cancelling_pending_pause:
                return True
            active = self._tasks.get(run_id)
            if active is not None and not active.done():
                loop = asyncio.get_running_loop()
                active.add_done_callback(lambda _task: loop.call_soon(self.schedule, run_id))
            else:
                self.schedule(run_id)
        return resumed

    async def submit_clarification(
        self, run_id: str, *, content: str,
        client_clarification_id: str | None = None,
        question_id: str | None = None,
    ) -> tuple[bool, str]:
        """BC-01：幂等提交澄清回答——写入会话、更新 Run 快照、唤醒继续。

        同一 client_clarification_id（或同内容重复提交）不重复写消息/历史；
        同键不同内容返回 409（IDEMPOTENCY_CONFLICT）；Run 不在等待澄清时
        只有已记录过的同一提交按幂等成功返回。
        """
        from deepresearch_agent.harness.clarifications import clarification_key, request_from_config
        from deepresearch_agent.harness.errors import AppError, ErrorCode
        from deepresearch_agent.persistence.repositories.utils import new_id

        run = await self.runs.get(run_id)
        if run is None:
            return False, "missing"
        request = request_from_config(json.loads(run.config_snapshot_json or "{}"))
        clarification_id = clarification_key(
            run_id, client_id=client_clarification_id,
            revision=request.revision if request is not None else None,
            content=content,
        )
        outcome, stored_message_id = await self.runs.record_clarification_answer(
            run_id,
            message_id=new_id("msg"),
            client_message_id=f"clarification-{clarification_id[-40:]}",
            content=content,
            clarification_id=clarification_id,
            question_id=question_id,
        )
        if outcome == "missing":
            return False, "missing"
        if outcome == "not_waiting":
            return False, run.status
        if outcome == "question_mismatch":
            raise AppError(
                ErrorCode.CONFLICT,
                "question_id 与当前澄清请求不匹配；请刷新 Run 后重试",
                retryable=False,
            )
        current = await self.runs.get(run_id)
        if outcome == "recorded":
            await self.event_bus.publish(
                run_id, "run.clarification_received",
                stage=current.current_stage if current else "queued",
                payload={
                    "status": current.status if current else "queued",
                    "question_id": request.question_id if request is not None else None,
                    "revision": request.revision if request is not None else None,
                    "message_id": stored_message_id,
                },
            )
        # duplicate 同样（重新）投递：首次提交可能已入队但进程重启后未消费，
        # schedule 在两种模式下都是幂等的唤醒（队列消费方以 runs 表为事实源）。
        self.schedule(run_id)
        return True, current.status if current is not None else "queued"

    async def shutdown(self) -> None:
        # T-006：释放 router 内 provider 资源（scholar httpx 客户端等）
        if self._router is not None:
            try:
                await self._router.aclose()
            except Exception:
                pass
        await self.skill_learning.shutdown()
        active = list(self._tasks.values())
        if not active:
            return
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
