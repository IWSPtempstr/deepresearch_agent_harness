"""Async engine factory for SQLite (local MVP) and PostgreSQL (platform mode)."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from deepresearch_agent.config.settings import APP_DATABASE_URL
from deepresearch_agent.persistence.search import PostgresTrgmSearch, SearchBackend, SqliteFtsSearch


SQLITE_PREFIX = "sqlite+aiosqlite:///"
POSTGRES_PREFIX = "postgresql+asyncpg://"


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith(SQLITE_PREFIX) or database_url.endswith(":memory:"):
        return
    raw_path = database_url[len(SQLITE_PREFIX):]
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)


class Database:
    def __init__(self, url: str = APP_DATABASE_URL, *, echo: bool = False):
        if url.startswith(SQLITE_PREFIX):
            self.dialect = "sqlite"
        elif url.startswith(POSTGRES_PREFIX):
            self.dialect = "postgresql"
        else:
            raise ValueError("仅支持 sqlite+aiosqlite 或 postgresql+asyncpg 数据库 URL")
        self.url = url
        if self.dialect == "sqlite":
            _ensure_sqlite_parent(url)
            self.engine: AsyncEngine = create_async_engine(url, echo=echo)
            self._install_sqlite_pragmas()
        else:
            # 平台模式：连接可能跨越容器重启，pre_ping 避免拿到死连接
            self.engine = create_async_engine(url, echo=echo, pool_pre_ping=True, pool_size=10, max_overflow=20)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.search: SearchBackend = SqliteFtsSearch() if self.dialect == "sqlite" else PostgresTrgmSearch()

    def _install_sqlite_pragmas(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            async with session.begin():
                yield session

    async def create_schema(self) -> None:
        """Create schema for tests/dev; deployed environments use Alembic."""
        from .models import Base

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            if self.dialect == "sqlite":
                # create_all is used by local/dev startup and does not alter old
                # SQLite tables. Keep additive compatibility with the Alembic 0002
                # migration so an existing local database can still start safely.
                await connection.run_sync(self._ensure_additive_columns)
            # 搜索后端的安装按方言分派：FTS5 虚拟表/触发器 或 pg_trgm 扩展/索引
            await self.search.install(connection)

    @staticmethod
    def _ensure_additive_columns(connection) -> None:
        required = {
            # 评审 M1：sessions 只能有一个键——dict 重复键会被静默覆盖，
            # 旧库的 memory_snapshot 自愈列会丢失
            "sessions": {
                "memory_snapshot_json": "TEXT",
                "memory_snapshot_version": "INTEGER",
                "memory_snapshot_created_at": "VARCHAR(40)",
                "tenant_id": "VARCHAR(64) DEFAULT 'default'",
            },
            "memories": {
                "target": "VARCHAR(32)",
                "archived_at": "VARCHAR(40)",
            },
            # P2-8 多租户：旧库平滑升级（create_all 不改已有表）
            "runs": {"tenant_id": "VARCHAR(64) DEFAULT 'default'"},
            "messages": {"tenant_id": "VARCHAR(64) DEFAULT 'default'"},
            "evidence": {"tenant_id": "VARCHAR(64) DEFAULT 'default'"},
        }
        for table_name, columns in required.items():
            existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()}
            for name, sql_type in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {name} {sql_type}")
        connection.exec_driver_sql("UPDATE memories SET target = scope WHERE target IS NULL AND scope IN ('user','project')")
        connection.exec_driver_sql("UPDATE memories SET status = 'archived' WHERE status = 'expired'")

    async def foreign_key_violations(self) -> list[tuple]:
        if self.dialect == "sqlite":
            async with self.engine.connect() as connection:
                result = await connection.execute(text("PRAGMA foreign_key_check"))
                return [tuple(row) for row in result.fetchall()]
        # PostgreSQL 原生强制外键约束，运行期无需自查；这里恒返回空以保持调用方契约
        return []

    async def close(self) -> None:
        await self.engine.dispose()
