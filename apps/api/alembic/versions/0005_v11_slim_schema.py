"""Converge the storage schema on the V1.1 pipeline.

The application keeps a short compatibility window for V1.0 clients.  A
production operator can set ``V11_DROP_LEGACY_TABLES=true`` while upgrading to
remove the old collaboration/task/approval tables in one explicit step.  The
default is additive so a rolling deployment does not destroy data before all
clients have moved to ``feedback_notes`` and status-based confirmation.
"""

import os

from sqlalchemy import inspect

from alembic import op

revision = "0005_v11_slim_schema"
down_revision = "0004_feedback_notes"
branch_labels = None
depends_on = None


LEGACY_TABLES = (
    "feedback_cluster_items",
    "feedback_clusters",
    "feedback_items",
    "decision_proposals",
    "approval_requests",
    "task_links",
    "tasks",
    "copilot_messages",
    "copilot_sessions",
    "jobs",
    "audit_logs",
    "workspace_members",
    "workspaces",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # ``0004`` creates this table for fresh databases.  Do not silently create
    # a partial table if an installation has skipped an earlier migration.
    if "feedback_notes" not in tables:
        raise RuntimeError("feedback_notes is missing; apply 0004_feedback_notes first")

    if os.getenv("V11_DROP_LEGACY_TABLES", "").lower() not in {"1", "true", "yes"}:
        return

    # Drop children before parents so MySQL foreign-key checks remain valid.
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for table_name in LEGACY_TABLES:
        if table_name in tables:
            op.drop_table(table_name)


def downgrade() -> None:
    # Legacy tables are intentionally not recreated automatically.  Restoring
    # them would require recovering data that this migration may have removed.
    pass
