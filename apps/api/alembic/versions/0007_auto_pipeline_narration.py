"""Automatic stage 1-5 pipeline: auto-accepted schema + AI report narration.

Two additions, both additive so a downgrade loses only the automation metadata:

``dataset_versions.schema_auto_accepted_at`` records that the *system* accepted
the inferred column roles, as distinct from ``schema_reviewed_at`` which records
that a *person* did.  Both satisfy the stage-2 gate, but the UI must be able to
tell the two apart -- an auto-accepted schema is a guess the user has not seen
yet, and every report built on one carries that disclosure.

``analysis_report_narrations`` stores the AI-written reading of a finished set of
analysis runs.  It is deliberately a separate table rather than a column on
``analysis_runs``: the runs are deterministic pandas output and must stay usable
when narration is absent, disabled, or over budget.  ``status`` starts at
``draft`` and is never auto-confirmed, so a narration cannot reach the stage 6+
decision chain without a human acting on it.

Existing rows are NOT backfilled.  A version parsed before this migration was
reviewed by a person or not at all; inventing an auto-acceptance timestamp would
silently relabel real human review as a machine guess.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0007_auto_pipeline_narration"
down_revision = "0006_schema_reviewed_at"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    inspector = inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def _has_table(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "dataset_versions", "schema_auto_accepted_at"):
        op.add_column("dataset_versions", sa.Column("schema_auto_accepted_at", sa.DateTime(), nullable=True))

    if not _has_table(bind, "analysis_report_narrations"):
        op.create_table(
            "analysis_report_narrations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "dataset_version_id",
                sa.String(36),
                sa.ForeignKey("dataset_versions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ai_run_id", sa.String(36), nullable=True),
            # draft | not_configured | budget_exceeded | failed | confirmed
            sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=True),
            sa.Column("analysis_run_ids", sa.JSON(), nullable=True),
            sa.Column("generated_by", sa.String(36), nullable=True),
            sa.Column("confirmed_by", sa.String(36), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index(
            "ix_report_narrations_version",
            "analysis_report_narrations",
            ["dataset_version_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "analysis_report_narrations"):
        # Dropping the table drops its indexes; an explicit drop_index first
        # fails on SQLite, where the index is owned by the table definition.
        op.drop_table("analysis_report_narrations")
    if _has_column(bind, "dataset_versions", "schema_auto_accepted_at"):
        op.drop_column("dataset_versions", "schema_auto_accepted_at")
