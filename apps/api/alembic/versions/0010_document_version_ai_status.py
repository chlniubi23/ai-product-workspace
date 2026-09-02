"""AI status columns for document versions (degradation visibility).

``ai_status`` records how a version was produced -- NULL for legacy rows,
``succeeded`` when the AI wrote it, ``fallback`` when the deterministic
template had to stand in.  ``ai_error_code`` carries the reason (provider
error, budget valve, truncation...) so the delivery page can tell the user
what happened instead of silently presenting a template.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0010_document_version_ai_status"
down_revision = "0009_interview_questions"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("ai_status", "ai_status", sa.String(32)),
    ("ai_error_code", "ai_error_code", sa.String(80)),
)


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("document_versions")}
    for column_name, _backup, column_type in _COLUMNS:
        if column_name not in columns:
            op.add_column("document_versions", sa.Column(column_name, column_type, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("document_versions")}
    for column_name, _backup, _column_type in reversed(_COLUMNS):
        if column_name in columns:
            op.drop_column("document_versions", column_name)
