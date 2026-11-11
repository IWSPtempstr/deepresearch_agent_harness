"""持久层方言中立性验证（P0-1a）。

SQLite 路径无条件运行；PostgreSQL 路径由 TEST_POSTGRES_URL 环境变量门控
（平台模式验证），用于确认 create_schema / 子串检索 / 租约条件更新在
postgresql+asyncpg 下的行为与 SQLite 一致。
"""

import asyncio
import os

import pytest

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from deepresearch_agent.harness import SourceMode, WorkflowMode
from deepresearch_agent.harness.budgets import BudgetLimits
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import MessageRepository, RunRepository, SessionRepository


POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")


def test_escape_like_handles_metacharacters():
    """评审遗留：PG ILIKE 转义的纯函数行为（真实 ILIKE 行为需连 PG 验证）。"""
    from deepresearch_agent.persistence.search import _escape_like

    assert _escape_like("50% off") == "50\% off"
    assert _escape_like("snake_case") == "snake\_case"
    assert _escape_like("back\\slash") == "back\\\\slash"
    assert _escape_like("plain") == "plain"


async def test_rejects_unsupported_dialect(tmp_path):
    with pytest.raises(ValueError):
        Database(f"mysql+pymysql:///{(tmp_path / 'x.db').as_posix()}")


async def _exercise_search_and_lease(db: Database) -> None:
    """方言无关的行为断言：建 schema、CJK 子串检索、租约条件更新。"""
    await db.create_schema()
    sessions = SessionRepository(db)
    messages = MessageRepository(db)
    runs = RunRepository(db)

    session = await sessions.create(SessionCreate(title="研究对话"))
    await messages.append(MessageCreate(
        session_id=session.session_id, role="user",
        content="深度研究平台的租约与断点恢复机制", metadata={},
    ))
    _, run, _ = await runs.create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="lease", client_message_id=f"c-{session.session_id}", metadata={}),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
            config_snapshot={"min_evidence": 1}, budget=BudgetLimits().model_dump(),
        ),
    )

    message_hits = await messages.search("租约与断点", session_id=session.session_id)
    assert message_hits and "租约与断点恢复机制" in message_hits[0].content
    session_hits = await sessions.search("研究对话")
    assert session_hits and session_hits[0].session_id == session.session_id

    assert await runs.acquire_lease(run.run_id, "worker-1", ttl_seconds=60)
    assert not await runs.acquire_lease(run.run_id, "worker-2", ttl_seconds=60), "未过期租约不可被抢占"
    assert await runs.acquire_lease(run.run_id, "worker-1", ttl_seconds=60), "同 owner 必须可续约"
    assert await runs.release_lease(run.run_id, "worker-1")
    assert await runs.acquire_lease(run.run_id, "worker-2", ttl_seconds=1)
    await asyncio.sleep(1.2)
    assert await runs.acquire_lease(run.run_id, "worker-3", ttl_seconds=60), "过期租约必须可被抢占（跨进程接管基石）"
    assert await db.foreign_key_violations() == []


async def test_sqlite_dialect_behavior(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'dialect.db').as_posix()}")
    assert db.dialect == "sqlite"
    try:
        await _exercise_search_and_lease(db)
    finally:
        await db.close()


@pytest.mark.skipif(not POSTGRES_URL, reason="set TEST_POSTGRES_URL to verify the postgresql+asyncpg path")
async def test_postgres_dialect_behavior():
    db = Database(POSTGRES_URL)
    assert db.dialect == "postgresql"
    try:
        await _exercise_search_and_lease(db)
    finally:
        await db.close()
