"""T-004 备份恢复演练：backup → 破坏 → restore → verify → RTO 计时报告。

单命令演练，输出 PASS/FAIL JSON 报告（RTO = restore+verify 墙钟）。
支持 --target sqlite（本地干跑，零依赖）与 --target postgres（CI services /
本机容器，需 pg_dump/pg_restore/psql 可用）。

用法：
    python scripts/backup_drill.py --target sqlite
    python scripts/backup_drill.py --target postgres \
        --dsn postgresql+asyncpg://dra:dra@127.0.0.1:5432/dra
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.backup_restore import (
    backup,
    restore,
    verify_after_restore,
)

DRILL_TABLE = "drill_notes"


def _sqlite_seed_and_sabotage(db_path: Path) -> tuple[int, int]:
    """向 SQLite 写入演练数据，返回 (备份后行数, 破坏后行数)。"""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"CREATE TABLE IF NOT EXISTS {DRILL_TABLE} (id INTEGER PRIMARY KEY, note TEXT)")
        connection.executemany(f"INSERT INTO {DRILL_TABLE}(note) VALUES (?)", [(f"note-{i}",) for i in range(5)])
        connection.commit()
        before = connection.execute(f"SELECT count(*) FROM {DRILL_TABLE}").fetchone()[0]
    finally:
        connection.close()
    return int(before), 0


def _sqlite_sabotage(db_path: Path) -> int:
    """破坏性变更：清空演练表（模拟数据丢失事故）。"""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"DELETE FROM {DRILL_TABLE}")
        connection.commit()
        after = connection.execute(f"SELECT count(*) FROM {DRILL_TABLE}").fetchone()[0]
    finally:
        connection.close()
    return int(after)


def run_sqlite_drill(project_root: Path, backup_dir: Path) -> dict:
    project_root = project_root.resolve()
    db_path = project_root / "data" / "app.db"
    if not db_path.exists():
        raise FileNotFoundError(f"演练需要本地 SQLite: {db_path}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = backup_dir / f"drill-{stamp}.zip"

    seeded, _ = _sqlite_seed_and_sabotage(db_path)
    backup(project_root, archive)
    sabotaged = _sqlite_sabotage(db_path)

    restore_started = time.monotonic()
    restore(project_root, archive, confirm=True)
    summary = verify_after_restore(project_root)
    rto_seconds = round(time.monotonic() - restore_started, 2)

    connection = sqlite3.connect(db_path)
    try:
        restored = connection.execute(f"SELECT count(*) FROM {DRILL_TABLE}").fetchone()[0]
    finally:
        connection.close()
    passed = restored == seeded and sabotaged == 0
    return {
        "target": "sqlite",
        "passed": passed,
        "seeded_rows": seeded,
        "sabotaged_rows": sabotaged,
        "restored_rows": restored,
        "rto_seconds": rto_seconds,
        "rto_target_seconds": 3600,
        "rpo_note": "manifest.created_at 与破坏点时间差即演练内数据丢失窗口（真实系统 = 备份周期 24h）",
        "verify_summary": summary,
        "archive": str(archive),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }


def run_postgres_drill(project_root: Path, backup_dir: Path, dsn: str, restore_dsn: str | None = None) -> dict:
    """PG 演练：dump → pg_restore 到 scratch 库 → 对**恢复后的库** verify。

    restore_dsn 缺省时从 dsn 派生 `<db>_restore`（CI/本机均可执行真实恢复路径，
    不碰源库）。RTO = pg_restore + verify 墙钟（与 sqlite 演练口径一致）。
    """
    from scripts.backup_restore import restore_postgres

    project_root = project_root.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = backup_dir / f"drill-{stamp}.zip"

    backup(project_root, archive, app_database_url=dsn)
    restore_started = time.monotonic()
    # 先解包落地（restore 校验哈希并把 dump 写回 data/），再做 pg_restore
    restore(project_root, archive, confirm=True)
    dump_path = project_root / "data" / "postgres.dump"
    if not dump_path.exists():
        raise FileNotFoundError(f"备份解包后缺少 dump: {dump_path}")
    target_dsn = restore_dsn or _derive_restore_dsn(dsn)
    _ensure_postgres_db(target_dsn, dsn)
    restore_postgres(target_dsn, dump_path)
    summary = verify_after_restore(project_root, app_database_url=target_dsn)
    rto_seconds = round(time.monotonic() - restore_started, 2)
    passed = all(isinstance(count, int) for count in summary["tables"].values())
    return {
        "target": "postgres",
        "passed": passed,
        "pg_restore_exercised": True,
        "source_dsn_database": _dsn_database(dsn),
        "restored_dsn_database": _dsn_database(target_dsn),
        "rto_seconds": rto_seconds,
        "rto_target_seconds": 3600,
        "rpo_note": "同 sqlite；真实 RPO = 生产备份调度周期（24h）",
        "verify_summary": summary,
        "archive": str(archive),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }


def _dsn_database(dsn: str) -> str | None:
    from scripts.backup_restore import _pg_dsn

    plain = _pg_dsn(dsn)
    match = re.search(r"/([^/?]+?)(?:\?|$)", plain)
    return match.group(1) if match else None


def _derive_restore_dsn(dsn: str) -> str:
    """postgresql+asyncpg://u:p@host:port/db → .../db_restore"""
    match = re.search(r"/([^/?]+?)(?:\?|$)", dsn)
    if not match:
        raise ValueError(f"无法从 DSN 解析数据库名: {dsn}")
    return dsn[: match.start(1)] + match.group(1) + "_restore" + dsn[match.end(1):]


def _ensure_postgres_db(target_dsn: str, admin_dsn: str) -> None:
    """scratch 库不存在时创建（pg_restore 要求目标库已存在）。"""
    from scripts.backup_restore import _pg_dsn, _run_tool

    target_db = _dsn_database(target_dsn)
    admin = _pg_dsn(admin_dsn)
    exists = _run_tool([
        "psql", "--no-password", "--tuples-only", "--no-align", "--command",
        f"SELECT 1 FROM pg_database WHERE datname='{target_db}'", admin,
    ], tool="psql").strip()
    if exists == "1":
        return
    _run_tool([
        "psql", "--no-password", "--command", f'CREATE DATABASE "{target_db}"', admin,
    ], tool="psql")


def main() -> None:
    parser = argparse.ArgumentParser(description="备份恢复演练（T-004）")
    parser.add_argument("--target", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--backup-dir", type=Path, default=Path(__file__).resolve().parents[1] / "backups")
    parser.add_argument("--dsn", default=None, help="postgres 源 DSN（postgresql+asyncpg://...）")
    parser.add_argument("--restore-dsn", default=None, help="恢复目标 scratch 库 DSN；缺省为 <db>_restore")
    parser.add_argument("--output", type=Path, default=None, help="报告 JSON 输出路径")
    args = parser.parse_args()
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    if args.target == "postgres":
        if not args.dsn:
            parser.error("--target postgres 需要 --dsn")
        report = run_postgres_drill(args.project_root, args.backup_dir, args.dsn, restore_dsn=args.restore_dsn)
    else:
        report = run_sqlite_drill(args.project_root, args.backup_dir)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
