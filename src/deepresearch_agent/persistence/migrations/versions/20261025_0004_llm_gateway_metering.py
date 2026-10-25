"""LLM gateway metering table.

Revision ID: 20261025_0004
Revises: 20260828_0003

注意：0001 使用 Base.metadata.create_all（随元数据演进会建出后续迁移的表），
因此本迁移全部操作按存在性幂等。
"""

from alembic import op
import sqlalchemy as sa

revision = "20261025_0004"
down_revision = "20260828_0003"
branch_labels = None
depends_on = None


def _table_exists(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _index_exists(bind, name: str) -> bool:
    return name in {index["name"] for index in sa.inspect(bind).get_indexes("llm_calls")}


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "llm_calls"):
        op.create_table(
            "llm_calls",
            sa.Column("call_id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
            sa.Column("run_id", sa.String(length=64), nullable=True),
            sa.Column("provider", sa.String(length=160), nullable=False),
            sa.Column("model", sa.String(length=160), nullable=False),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
            sa.Column("error_code", sa.String(length=80), nullable=True),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.PrimaryKeyConstraint("call_id"),
        )
    if not _index_exists(bind, "ix_llm_calls_provider_created"):
        op.create_index("ix_llm_calls_provider_created", "llm_calls", ["provider", "created_at"])
    if not _index_exists(bind, "ix_llm_calls_tenant_created"):
        op.create_index("ix_llm_calls_tenant_created", "llm_calls", ["tenant_id", "created_at"])
    if not _index_exists(bind, "ix_llm_calls_run"):
        op.create_index("ix_llm_calls_run", "llm_calls", ["run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "llm_calls"):
        for name in ("ix_llm_calls_run", "ix_llm_calls_tenant_created", "ix_llm_calls_provider_created"):
            if _index_exists(bind, name):
                op.drop_index(name, table_name="llm_calls")
        op.drop_table("llm_calls")
