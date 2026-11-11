"""Stage-3 application entry point wiring the real workflows into Harness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from deepresearch_agent.agents.multi_agent.integration.multi_agent_factory import MultiAgentFactory
from deepresearch_agent.config.settings import APP_DATABASE_URL, ARTIFACT_ROOT, HARNESS_BUDGETS
from deepresearch_agent.persistence import ArtifactStore, Database
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository, SessionRepository,
)
from deepresearch_agent.retrieval.router import create_default_router

from .contracts import SourceMode, WorkflowMode
from .runtime import HarnessRuntime
from .workflow import PlanExecuteReportDriver


@dataclass
class HarnessRunResult:
    run_id: str
    session_id: str
    status: str
    report: str | None


async def run_persistent_query(
    query: str,
    *,
    source_mode: SourceMode | str = SourceMode.GRAPHRAG,
    # T-407：唯一生产入口——CLI 默认与 HTTP 边界同规则；旧标识归一化而非
    # 创建后拒绝。
    workflow_mode: WorkflowMode | str = WorkflowMode.PLAN_EXECUTE_REPORT,
    database_url: str = APP_DATABASE_URL,
    client_message_id: str | None = None,
    config_snapshot: dict[str, Any] | None = None,
) -> HarnessRunResult:
    """Create Session/Message/Run and execute the real selected workflow once."""
    source = SourceMode(source_mode)
    if isinstance(workflow_mode, str) and workflow_mode == WorkflowMode.DEEP_RESEARCH.value:
        workflow_mode = WorkflowMode.PLAN_EXECUTE_REPORT  # 旧标识边界归一化
    workflow = WorkflowMode(workflow_mode)
    database = Database(database_url)
    agents: list[Any] = []
    try:
        # CLI/local stage-3 entry remains self-contained; deployed startup uses Alembic.
        await database.create_schema()
        # T-406：CLI/本地入口与 HTTP 同规则写入编排戳记（缺省=staged_v1 的旧
        # 语义会让 plan_execute_report 落入整批外壳而非 coreact 循环）。
        config_payload = dict(config_snapshot or {"min_evidence": 1})
        if workflow is WorkflowMode.PLAN_EXECUTE_REPORT:
            from deepresearch_agent.config.settings import RESEARCH_ORCHESTRATION

            config_payload.setdefault("orchestration", RESEARCH_ORCHESTRATION)
        session = await SessionRepository(database).create(SessionCreate(title=query[:80] or "新对话"))
        _, run, _ = await RunRepository(database).create_for_user_message(
            MessageCreate(session_id=session.session_id, role="user", content=query, client_message_id=client_message_id or f"cli-{session.session_id}"),
            RunCreate(session_id=session.session_id, trigger_message_id="assigned-atomically", source_mode=source, workflow_mode=workflow, config_snapshot=config_payload, budget=HARNESS_BUDGETS),
        )
        provider = create_default_router().for_mode(source)

        def workflow_factory(context, events=None):
            if workflow is WorkflowMode.DEEP_RESEARCH:
                # T-407：独立 deep_research 引擎已退役——显式拒绝（人工处理），
                # 不静默换引擎。
                from deepresearch_agent.harness.errors import AppError, ErrorCode

                raise AppError(
                    ErrorCode.LEGACY_WORKFLOW_UNSUPPORTED,
                    f"Run {context.run_id} 为旧 deep_research 工作流；该引擎已退役，请改用 plan_execute_report",
                )
            # T-406：唯一 coreact 装配（BriefBuilder，无 Reflector）
            bundle = MultiAgentFactory.create_coreact_bundle(retrieval_provider=provider)
            return PlanExecuteReportDriver(context, bundle.orchestrator, events=events)

        runtime = HarnessRuntime(
            run_repository=RunRepository(database), message_repository=MessageRepository(database),
            event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
            evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
            trajectory_repository=PlanTaskToolRepository(database), workflow_factory=workflow_factory,
            artifact_store=ArtifactStore(ARTIFACT_ROOT), artifact_repository=ArtifactRepository(database),
        )
        context = await runtime.execute_run(run.run_id)
        return HarnessRunResult(run_id=run.run_id, session_id=session.session_id, status=context.status.value, report=context.report)
    finally:
        for agent in agents:
            if hasattr(agent, "close"):
                agent.close()
        await database.close()

