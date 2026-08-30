"""Persist approved cleaning operations and their version lineage."""

import sqlalchemy as sa

from alembic import op

revision = "0003_cleaning_operations"
down_revision = "0002_metric_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "cleaning_operations" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "cleaning_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_version_id", sa.String(length=36), nullable=False),
        sa.Column("result_version_id", sa.String(length=36), nullable=False),
        sa.Column("operation_type", sa.String(length=50), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("approved_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["result_version_id"], ["dataset_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_version_id"], ["dataset_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cleaning_operations_source_version_id", "cleaning_operations", ["source_version_id"], unique=False)
    op.create_index("ix_cleaning_operations_result_version_id", "cleaning_operations", ["result_version_id"], unique=False)
    op.create_index("ix_cleaning_operations_approved_by", "cleaning_operations", ["approved_by"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if "cleaning_operations" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_cleaning_operations_approved_by", table_name="cleaning_operations")
    op.drop_index("ix_cleaning_operations_result_version_id", table_name="cleaning_operations")
    op.drop_index("ix_cleaning_operations_source_version_id", table_name="cleaning_operations")
    op.drop_table("cleaning_operations")
