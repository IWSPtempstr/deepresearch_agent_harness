"""T-005：迁移生命周期——启动期 DDL 开关、create_schema 幂等、独立迁移入口。

覆盖：
1. SCHEMA_BOOTSTRAP_ON_STARTUP=false 时 API lifespan 不执行 create_schema（生产语义）；
2. true（默认）时行为不变（本地零配置）；
3. create_schema 双调用幂等（此前仅隐式覆盖）；
4. scripts/migrate.py 成功路径（真实 alembic 子进程，sqlite tmp）。
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import deepresearch_agent.config.settings as settings_mod
from backend.app.main import create_app
from deepresearch_agent.persistence.database import Database


@pytest.fixture
def tmp_db(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'mig.db').as_posix()}"


def _build_app(database_url: str, bootstrap: bool, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings_mod, "SCHEMA_BOOTSTRAP_ON_STARTUP", bootstrap)
    return create_app(database_url=database_url)


def _table_exists(db_path: Path, table: str) -> bool:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return bool(row[0])
    finally:
        connection.close()


async def test_lifespan_bootstrap_false_skips_ddl(tmp_db, tmp_path, monkeypatch):
    """生产语义：开关关闭时 lifespan 不执行 DDL（表不被创建）；跳过迁移就启动
    会在 startup_recovery 处 fail-fast（诚实失败，不静默带病运行）。

    注意：不走 TestClient portal——lifespan 抛异常会污染其内部事件循环，
    导致后续测试 "Event loop is closed"；这里直接在测试自身事件循环里驱动
    lifespan 上下文。
    """
    from sqlalchemy.exc import OperationalError

    app = _build_app(tmp_db, bootstrap=False, monkeypatch=monkeypatch)
    db_path = Path(tmp_db.removeprefix("sqlite+aiosqlite:///"))
    with pytest.raises(OperationalError):
        async with app.router.lifespan_context(app):
            pass
    assert not _table_exists(db_path, "sessions"), \
        "SCHEMA_BOOTSTRAP_ON_STARTUP=false 时启动不得建表"


def test_lifespan_bootstrap_true_keeps_local_behavior(tmp_db, tmp_path, monkeypatch):
    app = _build_app(tmp_db, bootstrap=True, monkeypatch=monkeypatch)
    with TestClient(app):
        pass
    assert _table_exists(Path(tmp_db.removeprefix("sqlite+aiosqlite:///")), "sessions")


async def test_create_schema_is_idempotent(tmp_db):
    """双调用幂等（显式断言，此前仅隐式覆盖）。"""
    database = Database(tmp_db)
    await database.create_schema()
    await database.create_schema()  # 第二次不得抛错/破坏数据
    from deepresearch_agent.persistence.repositories import SessionRepository
    from backend.app.schemas import SessionCreate

    repository = SessionRepository(database)
    created = await repository.create(SessionCreate(title="idem"))
    await database.create_schema()  # 有数据后再次调用
    loaded = await repository.get(created.session_id)
    assert loaded is not None and loaded.title == "idem"
    await database.close()


def test_migrate_script_success_path(tmp_db, tmp_path, monkeypatch):
    """scripts/migrate.py：真实 alembic 子进程在空库上 upgrade head。"""
    import subprocess
    import sys

    monkeypatch.setenv("APP_DATABASE_URL", tmp_db)
    monkeypatch.setenv("PYTHONPATH", f"src{__import__('os').pathsep}.")
    completed = subprocess.run(
        [sys.executable, "scripts/migrate.py", "--retry", "2"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2], timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert _table_exists(Path(tmp_db.removeprefix("sqlite+aiosqlite:///")), "sessions")
    assert _table_exists(Path(tmp_db.removeprefix("sqlite+aiosqlite:///")), "runs")
