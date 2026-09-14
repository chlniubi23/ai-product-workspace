"""Locks the two properties that keep test runs out of the Windows Recycle Bin.

On this machine a plain ``Path.unlink()`` is intercepted by a filesystem-level
agent that turns the delete into a Recycle Bin item for every directory except
the system temp tree (measured: ``+1`` item per delete under the OneDrive-synced
checkout, ``AppData\\Roaming`` and the user home root; ``+0`` under ``%TEMP%``).
SQLite recreating and deleting ``api-test.db-journal`` on every write
transaction therefore used to recycle roughly ten thousand files per suite run.

Two invariants stop that from coming back:

1. the throwaway runtime lives under the system temp directory, and
2. the disposable SQLite database keeps its rollback journal in memory.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from conftest import TEST_ROOT
from sqlalchemy import text

from app import db as database


def test_test_runtime_lives_under_the_system_temp_directory():
    resolved = TEST_ROOT.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    assert resolved.is_relative_to(temp_root), (
        f"test runtime {resolved} is outside {temp_root}; everywhere else on this machine "
        "a delete becomes a Recycle Bin entry, so test artifacts would pile up there. "
        "Point APW_TEST_ROOT at a directory under the system temp dir."
    )


def test_disposable_database_keeps_its_rollback_journal_in_memory():
    with database.engine.connect() as connection:
        mode = connection.execute(text("PRAGMA journal_mode")).scalar()
    assert str(mode).lower() == "memory", (
        "journal_mode=DELETE recreates and deletes api-test.db-journal on every write transaction, "
        "which turns each commit into a Recycle Bin entry outside the temp tree"
    )
