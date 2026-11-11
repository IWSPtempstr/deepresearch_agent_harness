"""Transactional chat command service."""

import re

from sqlalchemy import select

from backend.app.schemas import MessageCreate, MessageSend, RunAccepted, RunCreate
from deepresearch_agent.config import settings
from deepresearch_agent.config.settings import HARNESS_BUDGETS, LEARNING_REVIEW_MODEL, OPENAI_LLM_MODEL
from deepresearch_agent.models.model_tier import resolve_model_for, resolve_role_model
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.repositories import MessageRepository, RunRepository, SessionRepository


def is_resume_intent(content: str) -> bool:
    """Recognize short natural-language commands that mean resume, not a new task."""
    normalized = re.sub(r"[\s，。！？、,.!?;；:：]+", "", content).lower()
    if not normalized or len(normalized) > 60:
        return False
    if any(marker in normalized for marker in ("不要", "别继续", "别恢复", "取消", "停止", "重来", "重新规划")):
        return False
    if any(marker in normalized for marker in ("改为", "换成", "另一个", "新任务", "新增", "补充要求")):
        return False
    direct = ("继续", "接着", "恢复", "续上", "往下", "下一步", "开始吧", "resume", "continue", "goon", "proceed")
    if any(marker in normalized for marker in direct):
        return True
    references = ("刚才", "之前", "原来", "上次", "当前", "这个任务", "该任务", "按计划")
    actions = ("执行", "运行", "开跑", "做下去", "完成它")
    return any(marker in normalized for marker in references) and any(action in normalized for action in actions)


class ChatService:
    def __init__(self, sessions: SessionRepository, runs: RunRepository, messages: MessageRepository, run_service):
        self.sessions = sessions
        self.runs = runs
        self.messages = messages
        self.run_service = run_service
    async def _tokens_spent_today(self) -> int:
        """当日 llm_calls output_tokens 总和（T-007 成本天花板口径）。"""
        from datetime import datetime, timezone

        from sqlalchemy import func, select

        from deepresearch_agent.persistence.models import LlmCallModel

        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            async with self.runs.database.engine.connect() as connection:
                row = await connection.execute(
                    select(func.coalesce(func.sum(LlmCallModel.output_tokens), 0)).where(
                        LlmCallModel.created_at >= midnight.isoformat()
                    )
                )
                return int(row.scalar() or 0)
        except Exception:
            return 0  # 计量不可读按未超限（readiness 会另行暴露）


    async def send(self, session_id: str, request: MessageSend) -> RunAccepted:
        session = await self.sessions.get(session_id)
        from backend.app.tenancy import DEFAULT_TENANT_ID, current_tenant_id

        if session is not None and current_tenant_id() != DEFAULT_TENANT_ID and session.tenant_id != current_tenant_id():
            raise AppError(ErrorCode.NOT_FOUND, "Session 不存在")
        if session is None:
            raise AppError(ErrorCode.NOT_FOUND, "Session 不存在")
        if session.status != "active":
            raise AppError(ErrorCode.CONFLICT, "已归档 Session 不能发送消息")
        # T-007：平台级 LLM 成本天花板（当日 output_tokens；0=不启用）。
        # 超限 = 诚实失败（明确 429 语义），不悄悄启动注定无预算的任务
        ceiling = settings.LLM_DAILY_TOKEN_CEILING
        if ceiling > 0:
            spent = await self._tokens_spent_today()
            if spent >= ceiling:
                raise AppError(
                    ErrorCode.LLM_RATE_LIMITED,
                    f"平台 LLM 日预算已耗尽（{spent}/{ceiling} tokens）；明日重置或由管理员调整 LLM_DAILY_TOKEN_CEILING",
                    retryable=True,
                )

        existing = await self.messages.get_by_client_id(session_id, request.client_message_id)
        if existing is not None and existing.run_id:
            existing_run = await self.runs.get(existing.run_id)
            if existing_run is not None:
                return RunAccepted(
                    message_id=existing.message_id, run_id=existing_run.run_id,
                    status=existing_run.status, events_url=f"/api/v1/runs/{existing_run.run_id}/events",
                    created=False,
                )

        paused = await self.runs.latest_paused_for_session(session_id)
        if paused is not None and is_resume_intent(request.content):
            message, _ = await self.messages.append(MessageCreate(
                session_id=session_id, run_id=paused.run_id, role="user",
                content=request.content, client_message_id=request.client_message_id,
                metadata={"control": "resume", "resume_run_id": paused.run_id},
            ))
            resumed = await self.run_service.resume(paused.run_id)
            current = await self.runs.get(paused.run_id)
            if not resumed and (current is None or current.status == "paused"):
                raise AppError(ErrorCode.CONFLICT, "暂停的 Run 当前不能恢复")
            return RunAccepted(
                message_id=message.message_id, run_id=paused.run_id,
                status="resuming" if resumed else current.status,
                events_url=f"/api/v1/runs/{paused.run_id}/events", created=False,
            )

        detailed_request = request.report_type == "long_document" or any(
            marker in request.content for marker in ("详细", "深入", "全面", "长篇")
        )
        # P2-8：租户预算覆盖默认预算（部分键覆盖，其余沿用 HARNESS_BUDGETS）
        budget = dict(HARNESS_BUDGETS)
        from backend.app.tenancy import current_tenant_id

        tenant_row = None
        if current_tenant_id() != "default":

            async with self.runs.database.sessions() as db_session:
                from deepresearch_agent.persistence.models import TenantModel

                tenant_row = (await db_session.execute(
                    select(TenantModel).where(TenantModel.tenant_id == current_tenant_id())
                )).scalar_one_or_none()
        if tenant_row is not None:
            import json as _json

            budget = {**budget, **(_json.loads(tenant_row.budget_json or "{}") or {})}
        # T-009：按任务难度解析模型层级（纯函数、可审计；留痕进 model_snapshot）
        snapshot = {
            # A single weakly-related chunk may be enough for a concise
            # answer, but must never unlock a user-requested detailed report.
            "min_evidence": 3 if detailed_request else 1,
            "report_type": "long_document" if detailed_request else request.report_type,
            "deep_research_max_iterations": 1 if request.source_mode.value == "graphrag" else 2,
            "schema_version": 1,
        }
        routing = (
            resolve_model_for(snapshot, content=request.content, settings=settings)
            if settings.LLM_TIER_ROUTING_ENABLED
            else {"model": OPENAI_LLM_MODEL, "tier": "standard", "reason": "routing disabled"}
        )
        snapshot["model_tier"] = routing["tier"]
        # T-015：plan_execute_report 编排实现版本——驱动按此分派 coreact/staged，
        # 缺失时按 staged_v1 处理，保证存量 Run 的快照恢复兼容。
        if request.workflow_mode.value == "plan_execute_report":
            snapshot["orchestration"] = settings.RESEARCH_ORCHESTRATION
        message, run, created = await self.runs.create_for_user_message(
            MessageCreate(
                session_id=session_id, role="user", content=request.content,
                client_message_id=request.client_message_id,
                metadata={"source_mode": request.source_mode.value, "workflow_mode": request.workflow_mode.value},
            ),
            RunCreate(
                session_id=session_id, trigger_message_id="assigned-atomically",
                source_mode=request.source_mode, workflow_mode=request.workflow_mode,
                config_snapshot=snapshot,
                budget=budget,
            ),
        )
        if created:
            model_snapshot = {
                "llm_model": routing["model"],
                "llm_model_tier": routing["tier"],
                "llm_model_routing_reason": routing["reason"],
                "learning_review_model": LEARNING_REVIEW_MODEL,
            }
            # T-016：角色→模型审计映射写入 model_snapshot（与 tier 同处、可审计）。
            # factory 执行时优先消费此处冻结的映射，保证跨进程/重启 recorded==actual。
            if request.workflow_mode.value == "plan_execute_report":
                # T-406/T-407：冻结 brief/controller/reporter/verifier 实际身份
                #（旧 planner key 经 canonical 映射兼容历史快照）。
                model_snapshot["role_models"] = {
                    role: resolve_role_model(snapshot, role=role, settings=settings)
                    for role in ("brief", "controller", "reporter", "verifier")
                }
            await self.runs.update_model_snapshot(run.run_id, model_snapshot)
            if session.title.strip() in {"新对话", "New conversation", "Untitled"}:
                title = " ".join(request.content.strip().split())[:36]
                if title:
                    await self.sessions.rename(session_id, title)
            self.run_service.schedule(run.run_id)
        return RunAccepted(
            message_id=message.message_id, run_id=run.run_id, status=run.status,
            events_url=f"/api/v1/runs/{run.run_id}/events", created=created,
        )
