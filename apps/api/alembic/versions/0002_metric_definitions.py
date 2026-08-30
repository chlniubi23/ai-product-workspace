"""Add the workspace metric dictionary table.

The initial migration creates current metadata dynamically. The existence
check keeps this migration compatible with databases initialized from a newer
checkout where ``metric_definitions`` may already exist.
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_metric_definitions"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "metric_definitions" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "metric_definitions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("numerator", sa.Text(), nullable=False),
        sa.Column("denominator", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(length=80), nullable=False),
        sa.Column("aggregation_period", sa.String(length=20), nullable=False),
        sa.Column("field_mapping_json", sa.JSON(), nullable=False),
        sa.Column("display_format", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("updated_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_metric_definitions_workspace_id", "metric_definitions", ["workspace_id"], unique=False)
    op.create_index("ix_metric_definitions_deleted_at", "metric_definitions", ["deleted_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if "metric_definitions" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_metric_definitions_deleted_at", table_name="metric_definitions")
    op.drop_index("ix_metric_definitions_workspace_id", table_name="metric_definitions")
    op.drop_table("metric_definitions")
