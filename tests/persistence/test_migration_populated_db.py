"""有数据的存量库跑迁移链（评审 H1 回归测试）。

复现并守护：SQLite batch 模式重建表时，若子表有行且 foreign_keys=ON，
DROP TABLE 会报 FOREIGN KEY constraint failed。env.py 已改为迁移期
foreign_keys=OFF；本测试以真实 alembic 子进程 + 已播种数据的库验证
upgrade(0005 → head) 保全数据且放宽 source_mode 约束。
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _alembic(database_url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(ROOT), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "APP_DATABASE_URL": database_url, "PYTHONPATH": f"{ROOT / 'src'}{Pathsep}{ROOT}"},
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} 失败:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"


Pathsep = ":"


def seed_rows(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        # 0005 之后的 schema：sessions/runs 已含 tenant_id
        conn.execute("INSERT INTO sessions (session_id, tenant_id, title, status, created_at, updated_at) VALUES ('ses_seed', 'default', 'seed', 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO runs (run_id, tenant_id, session_id, trigger_message_id, source_mode, workflow_mode, status, config_snapshot_json, model_snapshot_json, budget_json, usage_json, cancellation_requested, created_at, updated_at) "
            "VALUES ('run_seed', 'default', 'ses_seed', 'msg_seed', 'graphrag', 'deep_research', 'completed', '{}', '{}', '{}', '{}', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute("INSERT INTO messages (message_id, tenant_id, session_id, role, content, metadata_json, created_at) VALUES ('msg_seed', 'default', 'ses_seed', 'user', 'seed', '{}', '2026-01-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO run_events (run_id, event_type, stage, payload_json, schema_version, created_at) "
            "VALUES ('run_seed', 'run.queued', 'queued', '{}', 1, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO evidence (evidence_id, tenant_id, run_id, source_mode, provider, source_id, summary, metadata_json, content_hash, score, created_at) "
            "VALUES ('ev_seed', 'default', 'run_seed', 'graphrag', 'local_search', 'doc-1', 'seed evidence', '{}', '" + "a" * 64 + "', 0.9, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO checkpoints (checkpoint_id, run_id, version, stage, state_json, state_hash, schema_version, created_at) "
            "VALUES ('ckpt_seed', 'run_seed', 1, 'completed', '{}', '" + "b" * 64 + "', 1, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()


def row_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("sessions", "runs", "messages", "run_events", "evidence", "checkpoints")}
    finally:
        conn.close()


@pytest.fixture
def populated_db(tmp_path):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'seeded-migration.db').as_posix()}"
    _alembic(database_url, "upgrade", "20261025_0005")
    seed_rows(tmp_path / "seeded-migration.db")
    return database_url, tmp_path / "seeded-migration.db"


def test_upgrade_head_preserves_data_and_widens_source_mode(populated_db):
    database_url, db_path = populated_db
    before = row_counts(db_path)
    assert before["runs"] == 1 and before["evidence"] == 1

    _alembic(database_url, "upgrade", "head")

    after = row_counts(db_path)
    assert after == before, "迁移必须保全存量数据"

    # 约束已放宽：scholar 模式可写入（旧约束下会报 CHECK 失败）
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO runs (run_id, tenant_id, session_id, trigger_message_id, source_mode, workflow_mode, status, config_snapshot_json, model_snapshot_json, budget_json, usage_json, cancellation_requested, created_at, updated_at) "
            "VALUES ('run_scholar', 'default', 'ses_seed', 'msg_seed2', 'scholar', 'deep_research', 'queued', '{}', '{}', '{}', '{}', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()


def test_downgrade_to_base_roundtrip(populated_db):
    database_url, db_path = populated_db
    _alembic(database_url, "downgrade", "base")
    _alembic(database_url, "upgrade", "head")
