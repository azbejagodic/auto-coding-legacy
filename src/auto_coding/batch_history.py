"""Private durable execution checkpoints; no external I/O or task decisions."""

from __future__ import annotations

import json
import sqlite3

from .review_history import utc_now
from .review_security import sanitize


def initialize_batch_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS batch_workspaces (
            batch_id TEXT PRIMARY KEY REFERENCES task_plans(batch_id),
            fingerprint TEXT NOT NULL, workspace TEXT NOT NULL,
            origin TEXT NOT NULL, initial_sha TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS task_executions (
            task_id TEXT PRIMARY KEY REFERENCES plan_tasks(task_id),
            batch_id TEXT NOT NULL REFERENCES task_plans(batch_id),
            record_json TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS review_recoveries (
            task_id TEXT PRIMARY KEY REFERENCES task_executions(task_id),
            recovery_id TEXT UNIQUE NOT NULL,
            record_json TEXT NOT NULL
        )
    """)


class BatchHistory:
    """Connection/thread lifetime belongs to TaskStore, like ReviewHistory."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def binding(self, batch_id: str) -> dict | None:
        if not self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'batch_workspaces'"
        ).fetchone():
            return None  # Read-only inspection of a pre-TASK-011 database.
        row = self._connection.execute(
            "SELECT * FROM batch_workspaces WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return dict(row) if row else None

    def get(self, task_id: str) -> dict | None:
        if not self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'task_executions'"
        ).fetchone():
            return None
        row = self._connection.execute(
            "SELECT record_json FROM task_executions WHERE task_id = ?", (task_id,)
        ).fetchone()
        return json.loads(row["record_json"]) if row else None

    def checkpoint(self, task_id: str, stage: str, details: dict | None = None) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._checkpoint(task_id, stage, details or {})

    def recovery(self, task_id: str) -> dict | None:
        if not self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'review_recoveries'"
        ).fetchone():
            return None
        row = self._connection.execute(
            "SELECT task_id, recovery_id, record_json FROM review_recoveries WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        if record.get("task_id") != row["task_id"] or record.get("recovery_id") != row["recovery_id"]:
            raise ValueError("Recovery audit identity is inconsistent")
        return record

    def _save_recovery(self, record: dict) -> None:
        safe, _ = sanitize(record)
        cursor = self._connection.execute(
            "UPDATE review_recoveries SET record_json = ? WHERE task_id = ? AND recovery_id = ?",
            (json.dumps(safe, ensure_ascii=False, allow_nan=False),
             record["task_id"], record["recovery_id"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("Recovery audit record disappeared")

    def _checkpoint(self, task_id: str, stage: str, details: dict) -> None:
        """Called inside a transaction, also for atomic task finalization."""
        record = self.get(task_id)
        if record is None or record["stage"] == "finalized":
            raise ValueError("Missing or already finalized execution")
        safe, _ = sanitize(details)
        timestamp = utc_now()
        record.update(safe)
        record.update(stage=stage, updated_at=timestamp)
        record["checkpoints"].append({"stage": stage, "at": timestamp})
        self._connection.execute(
            "UPDATE task_executions SET record_json = ? WHERE task_id = ?",
            (json.dumps(record, ensure_ascii=False, allow_nan=False), task_id),
        )
