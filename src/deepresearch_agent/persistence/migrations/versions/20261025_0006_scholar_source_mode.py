"""Widen source_mode check constraints to include 'scholar'.

Revision ID: 20261025_0006
Revises: 20261025_0005
"""

from alembic import op

revision = "20261025_0006"
down_revision = "20261025_0005"
branch_labels = None
depends_on = None

# (table, constraint_name)
CONSTRAINTS = (
    ("runs", "ck_runs_source_mode"),
    ("tasks", "ck_tasks_source_mode"),
    ("tool_calls", "ck_tool_calls_source_mode"),
    ("evidence", "ck_evidence_source_mode"),
)

NEW_SQL = "source_mode IN ('graphrag', 'web', 'scholar')"
OLD_SQL = "source_mode IN ('graphrag', 'web')"


def upgrade() -> None:
    for table, name in CONSTRAINTS:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, NEW_SQL)


def downgrade() -> None:
    # 回退前清掉 scholar 数据，避免重建旧约束失败
    for table, _name in CONSTRAINTS:
        op.execute(f"DELETE FROM {table} WHERE source_mode = 'scholar'")
    for table, name in CONSTRAINTS:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, OLD_SQL)
