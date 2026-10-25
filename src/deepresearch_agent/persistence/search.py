"""Dialect-neutral substring/full-text search over messages, sessions and memories.

SQLite 沿用 FTS5 trigram（对中文是子串匹配，现有行为不变）；PostgreSQL 用
pg_trgm GIN 索引 + ILIKE 子串匹配——这是对 CJK 文本语义最接近的等价物
（tsvector 需要额外的中文分词扩展，镜像与部署成本高）。

已知语义差异（有意接受，见 docs/）：SQLite FTS5 按内部相关性返回，
PostgreSQL 按 created_at/updated_at 倒序返回；两者的命中集合一致，
仅排序策略不同，上游消费方都按 id 集合二次过滤。
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

from deepresearch_agent.persistence.fts import FTS_STATEMENTS


class SearchBackend:
    """按方言实现的搜索后端；所有方法在调用方持有的 AsyncSession 上执行。"""

    async def install(self, connection: Any) -> None:
        raise NotImplementedError

    async def search_message_ids(self, session: Any, query: str, *, session_id: Optional[str] = None, limit: int = 20) -> list[str]:
        raise NotImplementedError

    async def search_session_ids(self, session: Any, query: str, *, limit: int = 20) -> list[str]:
        raise NotImplementedError

    async def search_memory_ids(self, session: Any, query: str, *, limit: int = 20) -> list[str]:
        raise NotImplementedError


class SqliteFtsSearch(SearchBackend):
    """FTS5 trigram 虚拟表 + 同步触发器（见 persistence/fts.py）。"""

    async def install(self, connection: Any) -> None:
        for statement in FTS_STATEMENTS:
            await connection.execute(text(statement))

    async def search_message_ids(self, session: Any, query: str, *, session_id: Optional[str] = None, limit: int = 20) -> list[str]:
        clause = "messages_fts MATCH :query"
        params: dict[str, Any] = {"query": query, "limit": limit}
        if session_id:
            clause += " AND session_id = :session_id"
            params["session_id"] = session_id
        return list((await session.execute(text(f"SELECT message_id FROM messages_fts WHERE {clause} LIMIT :limit"), params)).scalars().all())

    async def search_session_ids(self, session: Any, query: str, *, limit: int = 20) -> list[str]:
        return list((await session.execute(text("SELECT session_id FROM sessions_fts WHERE sessions_fts MATCH :query LIMIT :limit"), {"query": query, "limit": limit})).scalars().all())

    async def search_memory_ids(self, session: Any, query: str, *, limit: int = 20) -> list[str]:
        return list((await session.execute(text("SELECT memory_id FROM memories_fts WHERE memories_fts MATCH :query LIMIT :limit"), {"query": query, "limit": limit})).scalars().all())


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PostgresTrgmSearch(SearchBackend):
    """pg_trgm GIN 索引 + ILIKE 子串匹配，语义对齐 SQLite 的 trigram 行为。"""

    async def install(self, connection: Any) -> None:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_messages_content_trgm ON messages USING gin (content gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS ix_sessions_title_trgm ON sessions USING gin (title gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS ix_sessions_summary_trgm ON sessions USING gin (summary_json gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS ix_memories_content_trgm ON memories USING gin (content gin_trgm_ops)",
        ):
            await connection.execute(text(statement))

    async def search_message_ids(self, session: Any, query: str, *, session_id: Optional[str] = None, limit: int = 20) -> list[str]:
        statement = "SELECT message_id FROM messages WHERE content ILIKE :pattern ESCAPE '\\'"
        params: dict[str, Any] = {"pattern": f"%{_escape_like(query)}%", "limit": limit}
        if session_id:
            statement += " AND session_id = :session_id"
            params["session_id"] = session_id
        statement += " ORDER BY created_at DESC, message_id LIMIT :limit"
        return list((await session.execute(text(statement), params)).scalars().all())

    async def search_session_ids(self, session: Any, query: str, *, limit: int = 20) -> list[str]:
        statement = "SELECT session_id FROM sessions WHERE title ILIKE :pattern ESCAPE '\\' OR summary_json ILIKE :pattern ESCAPE '\\' ORDER BY updated_at DESC, session_id LIMIT :limit"
        return list((await session.execute(text(statement), {"pattern": f"%{_escape_like(query)}%", "limit": limit})).scalars().all())

    async def search_memory_ids(self, session: Any, query: str, *, limit: int = 20) -> list[str]:
        statement = "SELECT memory_id FROM memories WHERE content ILIKE :pattern ESCAPE '\\' ORDER BY created_at DESC, memory_id LIMIT :limit"
        return list((await session.execute(text(statement), {"pattern": f"%{_escape_like(query)}%", "limit": limit})).scalars().all())
