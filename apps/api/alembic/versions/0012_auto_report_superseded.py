"""Superseded timestamp for auto reports (batch 13).

``auto_analysis_reports.superseded_at`` marks a report as no longer the
project's live one: compute always creates a fresh report and stamps every
still-current predecessor.  ``confirmed_at``/``confirmed_by`` are untouched --
a superseded report keeps its confirmation record, it is just history now.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0012_auto_report_superseded"
down_revision = "0011_project_archived_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("auto_analysis_reports")}
    if "superseded_at" not in columns:
        op.add_column("auto_analysis_reports", sa.Column("superseded_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("auto_analysis_reports")}
    if "superseded_at" in columns:
        op.drop_column("auto_analysis_reports", "superseded_at")
