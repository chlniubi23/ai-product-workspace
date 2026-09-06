"""Semantic label/description columns for data_columns (batch 21).

``data_columns.semantic_label`` (short business tag, e.g. "参会人数") and
``data_columns.semantic_description`` (one-line business reading) are written
by the optional field-semantics AI pass in the parse pipeline.  NULL means
"not interpreted / provider unavailable" -- the parse itself never depends on
them.  The dataset-level label lives in ``dataset_versions.schema_json`` and
needs no migration.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0017_field_semantics"
down_revision = "0016_interview_summaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("data_columns")}
    if "semantic_label" not in columns:
        op.add_column("data_columns", sa.Column("semantic_label", sa.String(length=120), nullable=True))
    if "semantic_description" not in columns:
        op.add_column("data_columns", sa.Column("semantic_description", sa.String(length=600), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("data_columns")}
    if "semantic_description" in columns:
        op.drop_column("data_columns", "semantic_description")
    if "semantic_label" in columns:
        op.drop_column("data_columns", "semantic_label")
