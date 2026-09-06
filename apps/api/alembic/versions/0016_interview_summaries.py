"""End-of-interview digest table (batch 18).

``interview_summaries`` holds one adaptive-interview digest per project
(unique project_id; re-running completion overwrites).  ``summary`` stores the
JSON-serialized {collected, gaps, ready_for} payload.
"""

import sqlalchemy as sa

from alembic import op

revision = "0016_interview_summaries"
down_revision = "0015_content_markdown_mediumtext"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_summaries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("ai_run_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_interview_summaries_workspace_id", "interview_summaries", ["workspace_id"])
    op.create_index("ix_interview_summaries_project_id", "interview_summaries", ["project_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_interview_summaries_project_id", table_name="interview_summaries")
    op.drop_index("ix_interview_summaries_workspace_id", table_name="interview_summaries")
    op.drop_table("interview_summaries")
