"""Source column for data_columns (batch 14).

``data_columns.source`` marks whether a column came from the uploaded file
("original") or was derived by the text-metric extraction engine
("extracted", named ``{source}__{metric}``).  The original file is never
modified -- derived columns are first-class metadata only.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0013_data_column_source"
down_revision = "0012_auto_report_superseded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("data_columns")}
    if "source" not in columns:
        op.add_column(
            "data_columns",
            sa.Column("source", sa.String(length=20), nullable=False, server_default="original"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("data_columns")}
    if "source" in columns:
        op.drop_column("data_columns", "source")
