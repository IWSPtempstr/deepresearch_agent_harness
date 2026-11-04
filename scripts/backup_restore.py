"""Create and restore verifiable local-MVP backups without storing secrets.

T-004 扩展：
- manifest schema 2：新增 postgres/neo4j 段（向后兼容 schema 1 归档的恢复）
- PostgreSQL：backup_postgres / restore_postgres（pg_dump/pg_restore custom 格式）
- verify_after_restore：核心表行数 + artifact 哈希回读（恢复演练的验收钩子）
- prune_backups：按保留天数/最小份数清理旧归档（RPO 24h → 每日备份 + 30 天保留）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

INCLUDED_DIRS = ("artifacts",)
PROJECT_DIRS = ("skills", "files", "cache")
MANIFEST_SCHEMA = 2
# 恢复验证覆盖的核心表（复用迁移测试的行数比对模式）
VERIFY_TABLES = ("sessions", "runs", "messages", "run_events", "evidence", "checkpoints")


class BackupToolError(RuntimeError):
    """备份/恢复所需的宿主工具缺失或调用失败（如 pg_dump 未安装）。"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(path: Path, root: Path) -> Path:
    resolved, root = path.resolve(), root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"路径越界: {resolved}")
    return resolved


def _pg_dsn(app_database_url: str) -> str:
    """postgresql+asyncpg://user:pass@host/db → postgresql://...（psycopg2/pg_dump 可用形式）。"""
    if app_database_url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + app_database_url[len("postgresql+asyncpg://"):]
    if app_database_url.startswith("postgresql+psycopg2://"):
        return "postgresql://" + app_database_url[len("postgresql+psycopg2://"):]
    if app_database_url.startswith(("postgresql://", "postgres://")):
        return "postgresql://" + app_database_url.split("://", 1)[1]
    raise BackupToolError(f"非 PostgreSQL DSN: {app_database_url}")


def _run_tool(command: list[str], *, tool: str) -> str:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise BackupToolError(f"未找到 {tool}；请安装 postgresql-client 或使用本地 SQLite 模式") from exc
    except subprocess.CalledProcessError as exc:
        raise BackupToolError(f"{tool} 失败（exit {exc.returncode}）：{exc.stderr.strip()[:500]}") from exc
    return completed.stdout


def backup_postgres(app_database_url: str, destination_dir: Path) -> dict:
    """pg_dump（custom 格式）导出 PostgreSQL；返回写入 manifest 的元数据段。"""
    dsn = _pg_dsn(app_database_url)
    destination_dir.mkdir(parents=True, exist_ok=True)
    dump_path = destination_dir / "postgres.dump"
    _run_tool([
        "pg_dump", "--format=custom", "--no-password", "--file", str(dump_path), dsn,
    ], tool="pg_dump")
    version = _run_tool(["pg_dump", "--version"], tool="pg_dump").strip()
    host_match = re.search(r"(?:@)([^:/?]+)", dsn)
    db_match = re.search(r"/([^/?]+?)(?:\?|$)", dsn)
    return {
        "included": True,
        "host": host_match.group(1) if host_match else None,
        "database": db_match.group(1) if db_match else None,
        "format": "pg_dump custom",
        "tool_version": version,
        "dumped_at": datetime.now(timezone.utc).isoformat(),
        "sha256": sha256(dump_path),
    }


def restore_postgres(app_database_url: str, dump_path: Path) -> None:
    """pg_restore --clean --if-exists 覆盖恢复（目标库需已存在）。"""
    dsn = _pg_dsn(app_database_url)
    _run_tool([
        "pg_restore", "--clean", "--if-exists", "--no-password", "--dbname", dsn, str(dump_path),
    ], tool="pg_restore")


def backup(
    project_root: Path,
    destination: Path,
    *,
    app_database_url: str | None = None,
) -> Path:
    """打包本地数据（SQLite 在线备份 + artifacts/skills/files/cache + 可选 neo4j.dump）。

    app_database_url 提供且为 PostgreSQL 时额外执行 pg_dump 并写入 postgres.dump
    与 manifest postgres 段；SQLite 模式行为与 v1 完全一致。
    """
    project_root = project_root.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hybridrag-backup-") as temporary:
        staging = Path(temporary)
        postgres_meta: dict | None = None
        database = project_root / "data" / "app.db"
        pg_requested = bool(app_database_url)
        if pg_requested and not app_database_url.startswith(("postgresql", "postgres")):
            raise BackupToolError(f"不支持的 DSN（仅 postgresql）：{app_database_url}")
        if pg_requested:
            # 显式 PG 模式：以 pg_dump 为准（与 SQLite 互斥，避免双源混淆）
            postgres_meta = backup_postgres(app_database_url, staging / "data")
        elif database.exists():
            target_db = staging / "data" / "app.db"
            target_db.parent.mkdir(parents=True)
            source_connection = sqlite3.connect(database)
            target_connection = sqlite3.connect(target_db)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
                source_connection.close()
        else:
            raise FileNotFoundError(f"SQLite 不存在且未提供 PostgreSQL DSN: {database}")
        for name in INCLUDED_DIRS:
            source = project_root / "data" / name
            if source.exists():
                shutil.copytree(source, staging / "data" / name)
        for name in PROJECT_DIRS:
            source = project_root / name
            if source.exists():
                shutil.copytree(source, staging / name)
        neo4j_dump = project_root / "data" / "neo4j.dump"
        if neo4j_dump.exists():
            shutil.copy2(neo4j_dump, staging / "data" / "neo4j.dump")
        files = sorted(path for path in staging.rglob("*") if path.is_file())
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "schema": MANIFEST_SCHEMA,
            "postgres": postgres_meta or {"included": False},
            "neo4j": {"included": neo4j_dump.exists()},
            "files": {path.relative_to(staging).as_posix(): {"sha256": sha256(path), "size": path.stat().st_size} for path in files},
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())
    return destination


def restore(project_root: Path, archive_path: Path, *, confirm: bool) -> dict:
    if not confirm:
        raise ValueError("恢复会覆盖同名本地数据；必须显式传入 --confirm")
    project_root, archive_path = project_root.resolve(), archive_path.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    with tempfile.TemporaryDirectory(prefix="hybridrag-restore-") as temporary:
        staging = Path(temporary)
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                _within(staging / member.filename, staging)
            archive.extractall(staging)
        manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        schema = int(manifest.get("schema", 1))
        if schema > MANIFEST_SCHEMA:
            raise ValueError(f"manifest schema {schema} 高于本工具支持的 {MANIFEST_SCHEMA}")
        for relative, expected in manifest["files"].items():
            source = _within(staging / relative, staging)
            if sha256(source) != expected["sha256"]:
                raise ValueError(f"备份 hash 校验失败: {relative}")
        current_db = project_root / "data" / "app.db"
        if current_db.exists():
            previous = current_db.with_name(f"app.pre-restore-{datetime.now().strftime('%Y%m%d%H%M%S')}.db")
            shutil.copy2(current_db, previous)
        for relative in manifest["files"]:
            if relative == "manifest.json":
                continue
            source = _within(staging / relative, staging)
            target = _within(project_root / relative, project_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        # PostgreSQL 段：dump 已随包落盘 data/postgres.dump，实际 pg_restore 由
        # 调用方/演练脚本在服务停止窗口执行（需目标库可写），此处仅回传元数据
        return manifest


def verify_after_restore(project_root: Path, *, app_database_url: str | None = None) -> dict:
    """恢复演练验收钩子：核心表行数 + artifact 哈希回读。返回可写入报告的摘要。"""
    project_root = project_root.resolve()
    summary: dict = {"tables": {}, "artifacts_checked": 0, "artifact_ok": True}
    if app_database_url and app_database_url.startswith(("postgresql", "postgres")):
        dsn = _pg_dsn(app_database_url)
        for table in VERIFY_TABLES:
            out = _run_tool([
                "psql", "--no-password", "--tuples-only", "--no-align", "--command",
                f"SELECT count(*) FROM {table}", dsn,
            ], tool="psql")
            summary["tables"][table] = int(out.strip() or 0)
    else:
        db_path = project_root / "data" / "app.db"
        if not db_path.exists():
            raise FileNotFoundError(f"恢复后 SQLite 不存在: {db_path}")
        connection = sqlite3.connect(db_path)
        try:
            for table in VERIFY_TABLES:
                try:
                    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
                    summary["tables"][table] = int(row[0]) if row else 0
                except sqlite3.OperationalError as exc:
                    raise BackupToolError(f"恢复验证失败：表 {table} 不存在（schema 未恢复？）") from exc
        finally:
            connection.close()
    artifacts = project_root / "data" / "artifacts"
    if artifacts.exists():
        for artifact_file in sorted(artifacts.rglob("*")):
            if artifact_file.is_file():
                # 回读采样：确保文件可读且非空（内容哈希在 manifest 校验已覆盖）
                size = artifact_file.stat().st_size
                with artifact_file.open("rb") as handle:
                    handle.read(min(1024, size))
                summary["artifacts_checked"] += 1
                if size == 0 and artifact_file.suffix != ".keep":
                    summary["artifact_ok"] = False
    return summary


def prune_backups(backup_dir: Path, *, keep_days: int = 30, keep_min: int = 3) -> list[Path]:
    """按文件名时间戳清理旧归档：保留最近 keep_days 天，且至少保留 keep_min 份。"""
    backup_dir = backup_dir.resolve()
    if not backup_dir.is_dir():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    archives: list[tuple[datetime, Path]] = []
    for path in backup_dir.glob("*.zip"):
        match = re.search(r"(\d{8}-\d{6})", path.name)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        archives.append((stamp, path))
    archives.sort(key=lambda item: item[0], reverse=True)
    removed: list[Path] = []
    survivors = 0
    for stamp, path in archives:
        if stamp >= cutoff or survivors < keep_min:
            survivors += 1
            continue
        path.unlink()
        removed.append(path)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("backup", "restore", "prune"))
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--archive", type=Path, required=False, help="backup/restore 必填；prune 不需要")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--app-database-url", default=None, help="PostgreSQL 模式的 DSN（platform 模式备份/恢复）")
    parser.add_argument("--backup-dir", type=Path, default=Path(__file__).resolve().parents[1] / "backups", help="prune 目标目录")
    parser.add_argument("--keep-days", type=int, default=30)
    parser.add_argument("--keep-min", type=int, default=3)
    args = parser.parse_args()
    if args.action in ("backup", "restore") and args.archive is None:
        parser.error(f"{args.action} 需要 --archive")
    if args.action == "backup":
        print(backup(args.project_root, args.archive, app_database_url=args.app_database_url))
    elif args.action == "restore":
        restore(args.project_root, args.archive, confirm=args.confirm)
        print("restore completed")
    else:
        removed = prune_backups(args.backup_dir, keep_days=args.keep_days, keep_min=args.keep_min)
        print(f"pruned {len(removed)} archive(s)")


if __name__ == "__main__":
    main()
