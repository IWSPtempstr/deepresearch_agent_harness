"""Create the complete local MVP persistence schema.

Revision ID: 20260815_0001
Revises: None

方言分派：SQLite 建 FTS5 trigram 虚拟表与同步触发器；PostgreSQL 建
pg_trgm 扩展与 GIN 索引（与 persistence/search.py 的运行期实现共用 SQL）。
"""

from alembic import op

from deepresearch_agent.persistence.fts import FTS_STATEMENTS
from deepresearch_agent.persistence.models import Base
from deepresearch_agent.persistence.search import PG_TRGM_DROP_STATEMENTS, PG_TRGM_STATEMENTS

revision = "20260815_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "sqlite":
        for statement in FTS_STATEMENTS:
            bind.exec_driver_sql(statement)
    elif bind.dialect.name == "postgresql":
        for statement in PG_TRGM_STATEMENTS:
            bind.exec_driver_sql(statement)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for trigger in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au", "sessions_fts_ai", "sessions_fts_ad", "sessions_fts_au", "memories_fts_ai", "memories_fts_ad", "memories_fts_au"):
            bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
        for table in ("messages_fts", "sessions_fts", "memories_fts"):
            bind.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
    elif bind.dialect.name == "postgresql":
        for statement in PG_TRGM_DROP_STATEMENTS:
            bind.exec_driver_sql(statement)
    Base.metadata.drop_all(bind=bind)
