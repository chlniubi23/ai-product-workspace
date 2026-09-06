"""MEDIUMTEXT for deep-generated document content (batch 17 hotfix).

``document_versions.content_markdown`` and
``auto_analysis_reports.content_markdown`` were plain TEXT (64KB bytes ≈ 21k
Chinese chars); a two-pass generated PRD exceeds that and the final write
fails with MySQL error 1406.  TEXT→MEDIUMTEXT is an in-place metadata change
on MySQL -- existing rows are preserved.  Other dialects (SQLite tests) keep
TEXT, which has no practical length limit there.
"""

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0015_content_markdown_mediumtext"
down_revision = "0014_drop_report_superseded"
branch_labels = None
depends_on = None

_TARGETS = ("document_versions", "auto_analysis_reports")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    for table in _TARGETS:
        columns = {info["name"] for info in inspect(bind).get_columns(table)}
        if "content_markdown" in columns:
            op.alter_column(
                table,
                "content_markdown",
                existing_type=sa.Text(),
                type_=mysql.MEDIUMTEXT(),
                existing_nullable=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    for table in _TARGETS:
        columns = {info["name"] for info in inspect(bind).get_columns(table)}
        if "content_markdown" in columns:
            op.alter_column(
                table,
                "content_markdown",
                existing_type=mysql.MEDIUMTEXT(),
                type_=sa.Text(),
                existing_nullable=False,
            )
