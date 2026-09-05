"""Drop auto_analysis_reports.superseded_at (batch 15).

Report semantics changed to "one live report per project": compute now
DELETES every previous report instead of stamping predecessors, so the
superseded_at column (batch 13/0012) has no remaining writer or reader.
Confirmation history survives in audit_logs.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0014_drop_report_superseded"
down_revision = "0013_data_column_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("auto_analysis_reports")}
    if "superseded_at" in columns:
        op.drop_column("auto_analysis_reports", "superseded_at")


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("auto_analysis_reports")}
    if "superseded_at" not in columns:
        op.add_column("auto_analysis_reports", sa.Column("superseded_at", sa.DateTime(), nullable=True))
