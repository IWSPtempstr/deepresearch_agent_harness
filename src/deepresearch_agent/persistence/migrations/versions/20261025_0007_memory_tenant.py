"""Tenant-scoped memories (review M3).

Revision ID: 20261025_0007
Revises: 20261025_0006

注意：模型列上的 index=True 会让 0001 的 create_all 生成自动命名索引
（ix_memories_tenant_id），而本迁移加列路径自建的索引名是 ix_memories_tenant——
存在性判断必须按「列上的全部索引」而不是具体名字，否则 fresh 库会重复建索引、
downgrade 会因未删净索引而失败。
"""

from alembic import op
import sqlalchemy as sa

revision = "20261025_0007"
down_revision = "20261025_0006"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, name: str) -> bool:
    return name in {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _tenant_indexes(bind) -> list[str]:
    return [index["name"] for index in sa.inspect(bind).get_indexes("memories") if "tenant_id" in index["column_names"]]


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "memories", "tenant_id"):
        op.add_column("memories", sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"))
        bind.execute(sa.text("UPDATE memories SET tenant_id = 'default' WHERE tenant_id IS NULL OR tenant_id = ''"))
        # 仅加列路径需要自建索引；create_all 路径已有自动命名索引
        if not _tenant_indexes(bind):
            op.create_index("ix_memories_tenant", "memories", ["tenant_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "memories", "tenant_id"):
        for index_name in _tenant_indexes(bind):
            op.drop_index(index_name, table_name="memories")
        op.drop_column("memories", "tenant_id")
