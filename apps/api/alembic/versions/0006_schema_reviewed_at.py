"""Add ``dataset_versions.schema_reviewed_at`` for the advisory stage-2 gate.

V1.1 required the ``user_id``/``event_time``/``event_name`` roles to be confirmed
before the pipeline could advance.  That assumes an event stream; a cleaned
business table (aggregate wide table, metric snapshot) has no such columns and
would block forever.  Stage 2 now completes when the user has *reviewed* the
inferred roles, so this column records that moment.

Existing rows are backfilled: a version that already has every suggested role
mapped was effectively reviewed under the old rules, so it keeps its progress
instead of regressing to "not reviewed" after the upgrade.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0006_schema_reviewed_at"
down_revision = "0005_v11_slim_schema"
branch_labels = None
depends_on = None

SUGGESTED_ROLES = ("user_id", "event_time", "event_name")


def _has_column(bind, table: str, column: str) -> bool:
    inspector = inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "dataset_versions", "schema_reviewed_at"):
        return
    op.add_column("dataset_versions", sa.Column("schema_reviewed_at", sa.DateTime(), nullable=True))

    # Backfill: versions that already satisfied the old hard gate (all three
    # suggested roles mapped) are treated as reviewed so upgrading does not
    # push a project backwards in the pipeline.
    bind.execute(
        sa.text(
            """
            UPDATE dataset_versions
               SET schema_reviewed_at = created_at
             WHERE id IN (
                   SELECT dataset_version_id
                     FROM data_columns
                    WHERE mapping_role IN :roles
                 GROUP BY dataset_version_id
                   HAVING COUNT(DISTINCT mapping_role) = :role_count
             )
            """
        ).bindparams(sa.bindparam("roles", value=SUGGESTED_ROLES, expanding=True), role_count=len(SUGGESTED_ROLES))
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "dataset_versions", "schema_reviewed_at"):
        op.drop_column("dataset_versions", "schema_reviewed_at")
