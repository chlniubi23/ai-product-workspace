"""Interview questions for the AI-interview insight collection flow.

``interview_questions`` stores one row per asked (or manually supplemented)
question.  AI rounds count up from ``round_number`` 1; manual supplements are
round 0.  Answers stay draft material: stage 7 distills them into insight
drafts, and only the usual human adjudication can confirm anything.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0009_interview_questions"
down_revision = "0008_auto_analysis_reports"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "interview_questions"):
        return
    op.create_table(
        "interview_questions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("topic", sa.String(120), nullable=False, server_default=""),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        # pending | answered | skipped
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("answer_text", sa.Text(), nullable=False, server_default=""),
        # ai | manual
        sa.Column("source", sa.String(20), nullable=False, server_default="ai"),
        sa.Column("ai_run_id", sa.String(36), nullable=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("answered_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_interview_questions_workspace_id", "interview_questions", ["workspace_id"])
    op.create_index("ix_interview_questions_project_id", "interview_questions", ["project_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "interview_questions"):
        return
    op.drop_index("ix_interview_questions_project_id", table_name="interview_questions")
    op.drop_index("ix_interview_questions_workspace_id", table_name="interview_questions")
    op.drop_table("interview_questions")
