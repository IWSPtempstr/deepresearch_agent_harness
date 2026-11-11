"""T-407：历史兼容与旧快照适配测试（历史读取/转换/安全拒绝）。"""
import json
import sqlite3
import sys

import pytest

from deepresearch_agent.harness.coreact_state import (
    CoreActLoopState,
    PendingAction,
    loop_state_from_workflow_state,
)
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository
from deepresearch_agent.harness.contracts import RunStatus, SourceMode, WorkflowMode


@pytest.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'legacy-adapter.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


async def create_completed_legacy_run(database) -> str:
    from backend.app.schemas import MessageCreate, RunCreate, SessionCreate

    session = await SessionRepository(database).create(SessionCreate(title="历史会话"))
    _, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="历史问题", client_message_id="hist-1"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="assigned-atomically",
            source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.DEEP_RESEARCH,
            config_snapshot={"min_evidence": 1},
        ),
    )
    # 直接落一条"存量"历史完成行（repo 的 completed 守卫是给新写入用的；
    # 存量数据以原始 SQL 形态存在，本测试验证的是读取兼容）
    from sqlalchemy import text as _sql

    async with database.sessions() as session:
        await session.execute(_sql(
            "update runs set status='completed', current_stage='completed' where run_id = :run_id"
        ), {"run_id": run.run_id})
        await session.commit()
    return run.run_id


# ---------- 历史完成 Run 可读 ----------

async def test_completed_legacy_run_remains_readable(database):
    """历史 deep_research 完成报告可读：workflow_mode/报告/证据不改写。"""

    run_id = await create_completed_legacy_run(database)
    runs = RunRepository(database)
    row = await runs.get(run_id)
    assert row is not None
    assert row.workflow_mode == "deep_research"  # 历史标识原样可读
    assert row.status == "completed"


# ---------- 旧 coreact 快照的恢复适配（in-code，无需转换行） ----------

def test_legacy_single_pending_coreact_snapshot_restores():
    """T-016 单动作 pending 快照（无 pending_batch）经恢复适配器可读——
    不需要转换行即可恢复（旧格式=新格式的单项批次）。"""
    rubric = {"rubric_id": "r1", "task_id": "t1", "criteria": [
        {"criterion_id": "c", "kind": "evidence_presence", "description": "d"}]}
    snapshot = CoreActLoopState(
        phase="act",
        pending_action=PendingAction(
            task={"task_id": "t1", "task_type": "web_search", "description": "d", "source_mode": "web"},
            rubric=rubric, attempt=1,
        ),
    ).model_dump(mode="json")
    restored = loop_state_from_workflow_state({"coreact": snapshot})
    assert restored is not None
    assert [item.attempt for item in restored.inflight_items()] == [1]
    assert restored.next_operation == "execute"


def test_future_coreact_cursor_version_is_rejected_by_conversion_tool():
    """高于实现的 coreact 游标版本 → 转换工具分类为待人工处理（不清空重跑）。"""
    from scripts.convert_legacy_checkpoints import classify

    snapshot = CoreActLoopState().model_dump(mode="json")
    snapshot["version"] = 99
    payload = json.dumps({"workflow_mode": "plan_execute_report", "workflow_state": {"coreact": snapshot}})
    category, target = classify(payload)
    assert category == "manual_coreact_future_version" and target is None


def test_conversion_tool_classifies_all_snapshot_kinds(tmp_path):
    """转换工具 dry-run 分类：coreact 可映射 / staged 待人工 / deep_research 不转换。"""
    import sys

    sys.path.insert(0, "scripts")
    from convert_legacy_checkpoints import classify

    coreact = json.dumps({"workflow_mode": "plan_execute_report", "workflow_state": {
        "orchestration": "coreact_v1",
        "coreact": CoreActLoopState(phase="decide").model_dump(mode="json"),
    }})
    staged = json.dumps({"workflow_mode": "plan_execute_report", "workflow_state": {
        "orchestration": "staged_v1", "state": {}, "planner_result": None,
    }})
    deep = json.dumps({"workflow_mode": "deep_research", "workflow_state": {"answer": "历史"}})
    empty = json.dumps({"workflow_mode": "plan_execute_report", "workflow_state": {}})

    assert classify(coreact) == ("mappable_coreact", "normalized_loop_cursor")
    assert classify(staged) == ("manual_staged", None)
    assert classify(deep) == ("not_convertible_deep_research", None)
    assert classify(empty) == ("empty", None)
    assert classify("not json") == ("unparsable", None)


def test_conversion_tool_apply_writes_restorable_row(tmp_path):
    """--apply 对可映射游标**追加**新 checkpoint 行（原行与原 hash 保留）；
    新行 state_json 保持完整 RunContext 形状、可被恢复层 model_validate。"""
    import sys

    sys.path.insert(0, "scripts")
    from convert_legacy_checkpoints import inspect as convert_inspect
    from deepresearch_agent.harness.run_context import RunContext

    db_path = tmp_path / "conv.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, workflow_mode TEXT, status TEXT);
        CREATE TABLE checkpoints (checkpoint_id TEXT PRIMARY KEY, run_id TEXT, version INTEGER,
            stage TEXT, state_json TEXT, state_hash TEXT, schema_version INTEGER, created_at TEXT);
    """)
    # 真实形状：完整 RunContext dump（CheckpointManager.save 的实际写入布局）
    context = RunContext(
        run_id="run_conv", session_id="sess_conv", trigger_message_id="msg_conv",
        source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.PLAN_EXECUTE_REPORT,
        status=RunStatus.EXECUTING, original_query="调研问题",
        config_snapshot={"orchestration": "coreact_v1", "min_evidence": 1},
    )
    context.workflow_state = {
        "orchestration": "coreact_v1",
        "coreact": CoreActLoopState(phase="act", pending_action=PendingAction(
            task={"task_id": "t1", "task_type": "web_search", "description": "d", "source_mode": "web"},
            rubric={"rubric_id": "r1", "task_id": "t1", "criteria": [
                {"criterion_id": "c", "kind": "evidence_presence", "description": "d"}]},
            attempt=1,
        )),
    }
    state = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)
    conn.execute("insert into runs values ('run_conv', 'plan_execute_report', 'interrupted')")
    conn.execute(
        "insert into checkpoints values ('ckpt_1', 'run_conv', 3, 'executing', ?, 'hash_orig', 1, '2026-01-01')",
        (state,),
    )
    conn.commit()
    conn.close()

    report = convert_inspect(db_path, apply=True)
    assert report["categories"].get("mappable_coreact") == 1
    applied = [item for item in report["items"] if item["applied"]]
    assert len(applied) == 1 and applied[0]["new_checkpoint_version"] == 4

    # 原行保留、hash 不变；新行可被恢复层还原（RunContext.model_validate）
    conn = sqlite3.connect(db_path)
    rows = conn.execute("select version, state_hash, state_json from checkpoints where run_id='run_conv' order by version").fetchall()
    conn.close()
    assert [row[0] for row in rows] == [3, 4]
    assert rows[0][1] == "hash_orig"  # 原行不破坏
    restored = RunContext.model_validate(json.loads(rows[1][2]))
    cursor = restored.workflow_state["coreact"]
    assert cursor["next_operation"] == "execute"
    # 旧单动作 pending 原样保留（恢复侧 inflight_items() 回退兼容，不虚构批次）
    assert cursor["pending_action"]["attempt"] == 1
    assert cursor["pending_batch"] == []


def test_conversion_tool_dry_run_writes_nothing(tmp_path):
    sys.path.insert(0, "scripts")
    from convert_legacy_checkpoints import inspect as convert_inspect

    db_path = tmp_path / "conv2.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, workflow_mode TEXT, status TEXT);
        CREATE TABLE checkpoints (checkpoint_id TEXT PRIMARY KEY, run_id TEXT, version INTEGER,
            stage TEXT, state_json TEXT, state_hash TEXT, schema_version INTEGER, created_at TEXT);
    """)
    state = json.dumps({"workflow_mode": "plan_execute_report", "workflow_state": {
        "orchestration": "coreact_v1",
        "coreact": CoreActLoopState(phase="decide").model_dump(mode="json"),
    }})
    conn.execute("insert into runs values ('run_dry', 'plan_execute_report', 'interrupted')")
    conn.execute("insert into checkpoints values ('ckpt_1', 'run_dry', 1, 'executing', ?, 'h', 1, '2026-01-01')", (state,))
    conn.commit()
    conn.close()

    report = convert_inspect(db_path, apply=False)
    assert report["mode"] == "dry-run"
    assert all(not item["applied"] for item in report["items"])
    conn = sqlite3.connect(db_path)
    count = conn.execute("select count(*) from checkpoints").fetchone()[0]
    conn.close()
    assert count == 1  # dry-run 无写入


# ---------- chat_service 冻结快照角色（T-406 遗留项落地） ----------

def test_chat_service_stamps_brief_and_verifier_roles():
    """新 Run 的冻结 role_models 含 brief/verifier（旧 planner 键经映射兼容）。"""
    from pathlib import Path

    source = Path("backend/app/services/chat_service.py").read_text(encoding="utf-8")
    assert 'for role in ("brief", "controller", "reporter", "verifier")' in source
    assert 'for role in ("planner", "reporter", "controller")' not in source
