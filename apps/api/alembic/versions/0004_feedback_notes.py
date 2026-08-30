"""Add the V1.1 normalized feedback-notes table.

This migration is additive so existing V1.0 data and routes remain recoverable
while the frontend moves from feedback items/clusters to the pipeline model.
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_feedback_notes"
down_revision = "0003_cleaning_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "feedback_notes" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "feedback_notes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("dataset_version_id", sa.String(length=36), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("sentiment", sa.String(length=30), nullable=False),
        sa.Column("cluster_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dataset_version_id"], ["dataset_versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedback_notes_project_id", "feedback_notes", ["project_id"], unique=False)
    op.create_index("ix_feedback_notes_dataset_version_id", "feedback_notes", ["dataset_version_id"], unique=False)
    op.create_index("ix_feedback_notes_cluster_name", "feedback_notes", ["cluster_name"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if "feedback_notes" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_feedback_notes_cluster_name", table_name="feedback_notes")
    op.drop_index("ix_feedback_notes_dataset_version_id", table_name="feedback_notes")
    op.drop_index("ix_feedback_notes_project_id", table_name="feedback_notes")
    op.drop_table("feedback_notes")
