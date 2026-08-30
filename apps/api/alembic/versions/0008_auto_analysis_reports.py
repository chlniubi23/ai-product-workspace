"""Project-level auto analysis reports for the upload -> report pipeline.

``auto_analysis_reports`` stores one report per generation covering the latest
version of every dataset in a project.  Deterministic aggregates are computed by
pandas before any provider call; the AI prose and the deterministic fallback are
both kept (``sections_json`` plus ``deterministic_json``) so a report stays
readable when AI is disabled, over budget, or fails later.  Confirmation is a
separate user action recorded on ``confirmed_by``/``confirmed_at`` -- a fresh
report is a draft by default.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0008_auto_analysis_reports"
down_revision = "0007_auto_pipeline_narration"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "auto_analysis_reports"):
        return
    op.create_table(
        "auto_analysis_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        # draft | succeeded | not_configured | failed | confirmed
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_markdown", sa.Text(), nullable=False, server_default=""),
        sa.Column("sections_json", sa.JSON(), nullable=False),
        sa.Column("key_findings", sa.JSON(), nullable=False),
        sa.Column("recommendations", sa.JSON(), nullable=False),
        sa.Column("limitations", sa.JSON(), nullable=False),
        sa.Column("dataset_version_ids", sa.JSON(), nullable=False),
        sa.Column("deterministic_json", sa.JSON(), nullable=False),
        sa.Column("ai_run_id", sa.String(36), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("generated_by", sa.String(36), nullable=True),
        sa.Column("confirmed_by", sa.String(36), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_auto_analysis_reports_project", "auto_analysis_reports", ["project_id"])
    op.create_index("ix_auto_analysis_reports_workspace", "auto_analysis_reports", ["workspace_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "auto_analysis_reports"):
        op.drop_table("auto_analysis_reports")
