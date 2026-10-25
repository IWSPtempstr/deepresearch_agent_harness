"""LLM gateway metering table.

Revision ID: 20261025_0004
Revises: 20260828_0003
"""

from alembic import op
import sqlalchemy as sa

revision = "20261025_0004"
down_revision = "20260828_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    op.create_index("ix_llm_calls_provider_created", "llm_calls", ["provider", "created_at"])
    op.create_index("ix_llm_calls_tenant_created", "llm_calls", ["tenant_id", "created_at"])
    op.create_index("ix_llm_calls_run", "llm_calls", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_run", table_name="llm_calls")
    op.drop_index("ix_llm_calls_tenant_created", table_name="llm_calls")
    op.drop_index("ix_llm_calls_provider_created", table_name="llm_calls")
    op.drop_table("llm_calls")
