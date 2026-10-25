"""Multi-tenant skeleton: tenants, api_keys, tenant_id on core tables.

Revision ID: 20261025_0005
Revises: 20261025_0004

注意：0001 使用 Base.metadata.create_all（随元数据演进会建出后续迁移的表），
因此本迁移全部操作按存在性幂等。
"""

from alembic import op
import sqlalchemy as sa

revision = "20261025_0005"
down_revision = "20261025_0004"
branch_labels = None
depends_on = None

TENANT_TABLES = ("sessions", "runs", "messages", "evidence")


def _table_exists(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _index_exists(bind, table: str, name: str) -> bool:
    return name in {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def _column_exists(bind, table: str, name: str) -> bool:
    return name in {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "tenants"):
        op.create_table(
            "tenants",
            sa.Column("tenant_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("budget_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.PrimaryKeyConstraint("tenant_id"),
        )
    if not _table_exists(bind, "api_keys"):
        op.create_table(
            "api_keys",
            sa.Column("key_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.tenant_id"), nullable=False),
            sa.Column("key_hash", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.Column("revoked_at", sa.String(length=40), nullable=True),
            sa.PrimaryKeyConstraint("key_id"),
        )
        op.create_index("uq_api_keys_hash", "api_keys", ["key_hash"], unique=True)
    if not _table_exists(bind, "tenants") or not list(bind.execute(sa.text("SELECT 1 FROM tenants WHERE tenant_id = 'default'")).fetchall()):
        bind.execute(sa.text("INSERT INTO tenants (tenant_id, name, budget_json, created_at) VALUES ('default', 'default', '{}', '1970-01-01T00:00:00Z')"))
    for table in TENANT_TABLES:
        if not _column_exists(bind, table, "tenant_id"):
            op.add_column(table, sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"))
            bind.execute(sa.text(f"UPDATE {table} SET tenant_id = 'default' WHERE tenant_id IS NULL OR tenant_id = ''"))
        if not _index_exists(bind, table, f"ix_{table}_tenant"):
            op.create_index(f"ix_{table}_tenant", table, ["tenant_id"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(TENANT_TABLES):
        if _table_exists(bind, table) and _column_exists(bind, table, "tenant_id"):
            # SQLite 删列前必须先删引用该列的全部索引（含变体命名）
            for index in sa.inspect(bind).get_indexes(table):
                if "tenant_id" in index["column_names"]:
                    op.drop_index(index["name"], table_name=table)
            op.drop_column(table, "tenant_id")
    if _table_exists(bind, "api_keys"):
        if _index_exists(bind, "api_keys", "uq_api_keys_hash"):
            op.drop_index("uq_api_keys_hash", table_name="api_keys")
        op.drop_table("api_keys")
    if _table_exists(bind, "tenants"):
        op.drop_table("tenants")
