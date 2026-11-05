"""T-004：备份恢复演练测试。

覆盖：
1. SQLite 全链路演练（backup→破坏→restore→verify→RTO 报告）；
2. manifest v2 往返（postgres/neo4j 段）与 schema 1 归档向后兼容；
3. 保留策略 prune（keep_days/keep_min）；
4. pg_dump/pg_restore 编排与 DSN 转换（stub 可执行文件，本机无需 PG 工具）；
5. restore-verify 钩子（核心表行数 + artifact 回读）。
"""

import json
import stat
from pathlib import Path

import pytest

from scripts.backup_drill import run_sqlite_drill
from scripts.backup_restore import (
    BackupToolError,
    MANIFEST_SCHEMA,
    backup,
    prune_backups,
    restore,
    verify_after_restore,
)


@pytest.fixture
def project(tmp_path):
    """构造一个最小项目树：SQLite + artifact + skills。"""
    root = tmp_path / "project"
    (root / "data" / "artifacts" / "run_1").mkdir(parents=True)
    (root / "skills" / "research").mkdir(parents=True)
    import sqlite3

    connection = sqlite3.connect(root / "data" / "app.db")
    try:
        connection.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT);"
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, status TEXT);"
            "CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT);"
            "CREATE TABLE run_events (event_id INTEGER PRIMARY KEY, run_id TEXT);"
            "CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, run_id TEXT);"
            "CREATE TABLE checkpoints (id INTEGER PRIMARY KEY, run_id TEXT);"
            "INSERT INTO sessions VALUES ('s1', 'demo');"
            "INSERT INTO runs VALUES ('r1', 'completed');"
        )
        connection.commit()
    finally:
        connection.close()
    (root / "data" / "artifacts" / "run_1" / "report.md").write_text("# report v1", encoding="utf-8")
    (root / "skills" / "research" / "SKILL.md").write_text("skill body", encoding="utf-8")
    return root


def test_sqlite_drill_full_chain(project, tmp_path):
    """演练全链路：seed→backup→破坏→restore→verify→PASS 报告。"""
    from scripts.backup_restore import VERIFY_TABLES

    report = run_sqlite_drill(project, tmp_path / "backups")
    assert report["passed"] is True
    assert report["restored_rows"] == report["seeded_rows"] == 5
    assert report["sabotaged_rows"] == 0
    assert 0 <= report["rto_seconds"] < report["rto_target_seconds"]
    assert set(report["verify_summary"]["tables"]) == set(VERIFY_TABLES)
    assert Path(report["archive"]).is_file()


def test_backup_manifest_v2_and_roundtrip(project, tmp_path):
    archive = tmp_path / "bk.zip"
    backup(project, archive)
    with pytest.raises(ValueError, match="--confirm"):
        restore(project, archive, confirm=False)
    (project / "data" / "artifacts" / "run_1" / "report.md").write_text("tampered", encoding="utf-8")
    restore(project, archive, confirm=True)
    assert (project / "data" / "artifacts" / "run_1" / "report.md").read_text(encoding="utf-8") == "# report v1"
    # manifest v2 段存在
    import zipfile

    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["postgres"] == {"included": False}
    assert manifest["neo4j"] == {"included": False}


def test_verify_after_restore_counts_and_artifacts(project):
    summary = verify_after_restore(project)
    assert summary["tables"]["sessions"] == 1
    assert summary["tables"]["runs"] == 1
    assert summary["artifacts_checked"] == 1
    assert summary["artifact_ok"] is True


def test_prune_keeps_recent_and_minimum(tmp_path):
    from datetime import datetime, timedelta, timezone

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    now = datetime.now(timezone.utc)
    # 6 份：2 份新鲜、3 份过期、1 份最老（keep_min=3 保护最近的 3 份）
    ages = [0, 1, 40, 41, 42, 43]
    names = []
    for index, age in enumerate(ages):
        stamp = (now - timedelta(days=age)).strftime("%Y%m%d-%H%M%S")  # naive str,prune 侧按 UTC 解析
        name = f"bk-{stamp}-{index}.zip"
        (backup_dir / name).write_bytes(b"x")
        names.append(name)
    removed = prune_backups(backup_dir, keep_days=30, keep_min=3)
    removed_names = {path.name for path in removed}
    assert len(removed) == 3
    assert not removed_names.intersection(names[:2]), "新鲜备份不得删除"
    assert all(not (backup_dir / name).exists() for name in removed_names)
    assert len(list(backup_dir.glob("*.zip"))) == 3


def _write_stub_executable(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_postgres_backup_restore_orchestration(project, tmp_path, monkeypatch):
    """stub pg_dump/pg_restore（bash）：编排参数与 DSN 转换正确，dump 入档。"""
    calls_log = tmp_path / "pg_calls.log"

    _write_stub_executable(tmp_path, "pg_dump", f"""#!/bin/bash
echo "pg_dump $@" >> {calls_log}
prev=""
for arg in "$@"; do
  if [ "$prev" = "--file" ]; then out="$arg"; fi
  prev="$arg"
done
printf 'PGDMP' > "$out"
exit 0
""")
    _write_stub_executable(tmp_path, "pg_restore", f"""#!/bin/bash
echo "pg_restore $@" >> {calls_log}
exit 0
""")

    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    dsn = "postgresql+asyncpg://dra:secret@dbhost:5432/dra"
    archive = tmp_path / "pg.zip"
    backup(project, archive, app_database_url=dsn)

    log = calls_log.read_text()
    assert "pg_dump --format=custom" in log
    assert "postgresql://dra:secret@dbhost:5432/dra" in log, "DSN 必须转换为裸 postgresql://"
    import zipfile

    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["postgres"]["included"] is True
        assert manifest["postgres"]["host"] == "dbhost"
        assert manifest["postgres"]["database"] == "dra"
        assert manifest["files"]["data/postgres.dump"]["sha256"]
        assert zf.read("data/postgres.dump") == b"PGDMP"

    # 恢复编排：dump 落盘后调 stub pg_restore
    restore(project, archive, confirm=True)
    from scripts.backup_restore import restore_postgres

    restore_postgres(dsn, project / "data" / "postgres.dump")
    assert "pg_restore --clean --if-exists" in calls_log.read_text()


def test_backup_tool_missing_raises_guidance(project, tmp_path, monkeypatch):
    """pg_dump 不存在时给出安装指引而不是裸 FileNotFoundError 堆栈。"""
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()
    with pytest.raises(BackupToolError, match="postgresql-client"):
        backup(project, tmp_path / "x.zip", app_database_url="postgresql+asyncpg://u:p@h:5432/db")


def test_manifest_schema_future_rejected(project, tmp_path):
    archive = tmp_path / "future.zip"
    backup(project, archive)
    import zipfile

    rebuilt = tmp_path / "future2.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(rebuilt, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["schema"] = MANIFEST_SCHEMA + 1
                data = json.dumps(manifest).encode()
            dst.writestr(info.filename, data)
    with pytest.raises(ValueError, match="schema"):
        restore(project, rebuilt, confirm=True)


def test_postgres_drill_real_restore(tmp_path, monkeypatch):
    """run_postgres_drill 全链路(stub 工具)：dump→建 scratch 库→pg_restore→对恢复后库 verify。"""
    from scripts.backup_drill import run_postgres_drill

    calls_log = tmp_path / "pg_calls.log"
    project = tmp_path / "proj"
    project.mkdir()
    scratch_count = {"n": 0}

    _write_stub_executable(tmp_path, "pg_dump", f"""#!/bin/bash
echo "pg_dump $@" >> {calls_log}
prev=""
for arg in "$@"; do
  if [ "$prev" = "--file" ]; then out="$arg"; fi
  prev="$arg"
done
printf 'PGDMP' > "$out"
exit 0
""")
    _write_stub_executable(tmp_path, "pg_restore", f"""#!/bin/bash
echo "pg_restore $@" >> {calls_log}
scratch_count=$((scratch_count+1))
exit 0
""")
    # psql：count(*) 返回固定行数；pg_database 探测返回 0（触发 CREATE DATABASE）
    _write_stub_executable(tmp_path, "psql", f"""#!/bin/bash
echo "psql $@" >> {calls_log}
if echo "$@" | grep -q "pg_database"; then echo 0; else echo 3; fi
exit 0
""")

    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    dsn = "postgresql+asyncpg://dra:secret@dbhost:5432/dra"
    report = run_postgres_drill(project, tmp_path / "backups", dsn)
    assert report["passed"] is True
    assert report["pg_restore_exercised"] is True
    assert report["restored_dsn_database"] == "dra_restore"
    log = calls_log.read_text()
    assert "pg_restore --clean --if-exists" in log, "恢复路径必须真实执行"
    assert 'CREATE DATABASE "dra_restore"' in log
    assert report["verify_summary"]["tables"]["runs"] == 3
