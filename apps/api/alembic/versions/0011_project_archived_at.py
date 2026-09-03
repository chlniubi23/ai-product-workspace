"""Project archival timestamp for the one-project-one-workflow model.

``projects.archived_at`` is set when a finished workflow is archived (status
flips to ``archived``) and cleared on restore.  Archived projects are
read-only history: ``project_for`` refuses editor+ access to them.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0011_project_archived_at"
down_revision = "0010_document_version_ai_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("projects")}
    if "archived_at" not in columns:
        op.add_column("projects", sa.Column("archived_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("projects")}
    if "archived_at" in columns:
        op.drop_column("projects", "archived_at")
