"""T-407：唯一 coreact 入口切换测试（HTTP/API/worker/CLI/恢复路径）。"""
import pytest
from pydantic import ValidationError
from deepresearch_agent.persistence import Database

from backend.app.schemas.api import MessageSend
from deepresearch_agent.agents.multi_agent.integration.multi_agent_factory import MultiAgentFactory
from deepresearch_agent.agents.multi_agent.planner.brief_builder import BriefBuilder
from deepresearch_agent.harness.contracts import SourceMode, WorkflowMode


# ---------- API 边界归一化 ----------

def test_message_send_normalizes_legacy_workflow_identifier():
    """旧标识 deep_research 在边界归一化为 plan_execute_report（新写入唯一入口）。"""
    request = MessageSend.model_validate({
        "client_message_id": "c-1", "content": "调研问题",
        "source_mode": "web", "workflow_mode": "deep_research",
    })
    assert request.workflow_mode is WorkflowMode.PLAN_EXECUTE_REPORT


def test_message_send_defaults_to_coreact_entry():
    request = MessageSend.model_validate({
        "client_message_id": "c-2", "content": "调研问题", "source_mode": "graphrag",
    })
    assert request.workflow_mode is WorkflowMode.PLAN_EXECUTE_REPORT


def test_message_send_rejects_unknown_workflow():
    with pytest.raises(ValidationError):
        MessageSend.model_validate({
            "client_message_id": "c-3", "content": "调研问题",
            "source_mode": "web", "workflow_mode": "not_a_mode",
        })


def test_capabilities_advertise_single_workflow():
    """capabilities 只广告唯一生产入口（历史标识不外发为可选工作流）。"""
    import asyncio

    from backend.app.api.v1 import health

    payload = asyncio.run(health.capabilities())
    assert payload["workflows"] == ["plan_execute_report"]


# ---------- worker/CLI 工厂：唯一 coreact，无 else 落入旧引擎 ----------

def test_factory_path_always_coreact_for_plan_execute_report():
    bundle = MultiAgentFactory.create_coreact_bundle()

    assert isinstance(bundle.planner, BriefBuilder)
    # T-408：ReflectionExecutor 类已删除——结构上不存在于任何执行器栈
    assert all(type(executor).__name__ == "RetrievalExecutor" for executor in bundle.worker.executors)


def test_deep_research_context_explicitly_rejected_by_runtime_factory(tmp_path):
    """存量活动 deep_research Run：工厂显式拒绝（人工处理），不静默换引擎。"""

    # 直接以 run_service 的工厂闭包语义构造同型调用：workflow_mode=DEEP_RESEARCH
    # → AppError(LEGACY_WORKFLOW_UNSUPPORTED)。这里经由 bootstrap/run_service
    # 共同引用的判定（工厂内分支）验证拒绝语义。
    from backend.app.services import run_service as run_service_module
    from backend.app.schemas import RunCreate, MessageCreate
    from deepresearch_agent.persistence import Database
    from deepresearch_agent.persistence.repositories import SessionRepository
    from backend.app.schemas import SessionCreate

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'entry.db').as_posix()}")
        await database.create_schema()
        try:
            service = run_service_module.RunService(database)
            session = await SessionRepository(database).create(SessionCreate(title="legacy"))
            message, run, _ = await service.runs.create_for_user_message(
                MessageCreate(session_id=session.session_id, role="user", content="旧问题", client_message_id="legacy-1"),
                RunCreate(
                    session_id=session.session_id, trigger_message_id="assigned-atomically",
                    source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.DEEP_RESEARCH,
                    config_snapshot={"min_evidence": 1},
                ),
            )
            # Runtime 将显式拒绝落为诚实失败（不自动取消、不静默换引擎），
            # 业务错误码可审计（人工处理清单按错误码筛选）
            await service.execute_now(run.run_id)
            row = await service.runs.get(run.run_id)
            assert row.status == "failed"
            assert row.error_code == "LEGACY_WORKFLOW_UNSUPPORTED"
            assert "人工处理" in (row.error_message or "")
        finally:
            await database.close()

    asyncio_run(scenario())


def asyncio_run(coro):
    import asyncio

    asyncio.run(coro)


@pytest.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'coreact-entry.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


# ---------- 恢复路径：旧引擎活动 Run 不自动续跑 ----------

async def test_recovery_scan_excludes_legacy_runs(database, tmp_path):
    """RecoveryManager：deep_research 活动 Run 不进入自动恢复（保持原状）。"""
    from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
    from deepresearch_agent.harness.recovery import RecoveryManager
    from deepresearch_agent.persistence.repositories import (
        RunRepository, SessionRepository,
    )

    session = await SessionRepository(database).create(SessionCreate(title="legacy-recovery"))
    _, legacy_run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="旧问题", client_message_id="legacy-2"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="assigned-atomically",
            source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.DEEP_RESEARCH,
            config_snapshot={"min_evidence": 1},
        ),
    )
    _, coreact_run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="新问题", client_message_id="coreact-2"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="assigned-atomically",
            source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
            config_snapshot={"min_evidence": 1},
        ),
    )
    await RunRepository(database).update_status(legacy_run.run_id, status="interrupted", current_stage="interrupted")
    await RunRepository(database).update_status(coreact_run.run_id, status="interrupted", current_stage="interrupted")

    resumable = await RecoveryManager(RunRepository(database)).scan(auto_resume=True)
    assert coreact_run.run_id in resumable
    assert legacy_run.run_id not in resumable
    # 保持原状：legacy Run 状态未被自动取消/迁移
    row = await RunRepository(database).get(legacy_run.run_id)
    assert row.status == "interrupted" and row.workflow_mode == "deep_research"


# ---------- OpenAPI/前端一致性 ----------

def test_openapi_request_schema_has_single_default():
    """OpenAPI 请求模型默认值与归一化行为一致（前端类型对齐由 npm build 门禁保证）。"""
    schema = MessageSend.model_json_schema()
    assert schema["properties"]["workflow_mode"]["default"] == "plan_execute_report"
