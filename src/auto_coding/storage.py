"""Persistent task storage, independent of workflow decisions and integrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
from pathlib import Path
import sqlite3
from types import TracebackType


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    READY_TO_COMMIT = "ready_to_commit"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class DuplicateTaskError(ValueError):
    """A task with the supplied ID already exists."""


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    prompt: str
    model: str
    task_order: int
    status: TaskStatus
    acceptance_criteria: list[str]
    created_at: str
    updated_at: str


def _validate_status(status: str) -> TaskStatus:
    try:
        return TaskStatus(status)
    except ValueError:
        raise ValueError(f"Invalid task status: {status!r}") from None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        title=row["title"],
        prompt=row["prompt"],
        model=row["model"],
        task_order=row["task_order"],
        status=TaskStatus(row["status"]),
        acceptance_criteria=json.loads(row["acceptance_criteria"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class TaskStore:
    """Open a local SQLite database and create its schema if needed.

    Paths are relative to the working directory unless absolute. Use as a
    context manager, or call close() explicitly when finished. Timestamps are
    UTC ISO 8601 strings. Missing lookups return None; missing updates raise
    KeyError. Equal task orders are listed deterministically by task ID.
    """

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        try:
            with self._connection:
                # Serialize schema initialization/migration across connections.
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
                ).fetchone()
                if existing and "'ready_to_commit'" not in existing["sql"]:
                    self._connection.execute("ALTER TABLE tasks RENAME TO tasks_legacy")
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY NOT NULL,
                        title TEXT NOT NULL,
                        prompt TEXT NOT NULL,
                        model TEXT NOT NULL,
                        task_order INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK (status IN (
                            'queued', 'running', 'awaiting_review',
                            'approved', 'rejected', 'failed', 'ready_to_commit'
                        )),
                        acceptance_criteria TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                if existing and "'ready_to_commit'" not in existing["sql"]:
                    self._connection.execute("""
                        INSERT INTO tasks (task_id, title, prompt, model, task_order,
                                           status, acceptance_criteria, created_at, updated_at)
                        SELECT task_id, title, prompt, model, task_order,
                               status, acceptance_criteria, created_at, updated_at
                        FROM tasks_legacy
                    """)
                    self._connection.execute("DROP TABLE tasks_legacy")
        except Exception:
            self._connection.close()
            raise

    def __enter__(self) -> TaskStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def add_task(
        self,
        *,
        task_id: str,
        title: str,
        prompt: str,
        model: str,
        task_order: int,
        acceptance_criteria: list[str],
        status: str = TaskStatus.QUEUED,
    ) -> Task:
        status = _validate_status(status)
        if not isinstance(acceptance_criteria, list) or not all(
            isinstance(item, str) for item in acceptance_criteria
        ):
            raise ValueError("Acceptance criteria must be a list of strings")
        timestamp = _timestamp()
        criteria_json = json.dumps(acceptance_criteria, ensure_ascii=False)
        try:
            with self._connection:
                self._connection.execute(
                    """INSERT INTO tasks (
                        task_id, title, prompt, model, task_order, status,
                        acceptance_criteria, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task_id, title, prompt, model, task_order, status,
                     criteria_json, timestamp, timestamp),
                )
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorname == "SQLITE_CONSTRAINT_PRIMARYKEY":
                raise DuplicateTaskError(f"Task {task_id!r} already exists") from exc
            raise
        return Task(task_id, title, prompt, model, task_order, TaskStatus(status),
                    json.loads(criteria_json), timestamp, timestamp)

    def get_task(self, task_id: str) -> Task | None:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return _task_from_row(row) if row is not None else None

    def list_tasks(self) -> list[Task]:
        rows = self._connection.execute(
            "SELECT * FROM tasks ORDER BY task_order, task_id"
        ).fetchall()
        return [_task_from_row(row) for row in rows]

    def update_status(self, task_id: str, status: str) -> None:
        status = _validate_status(status)
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, _timestamp(), task_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Task {task_id!r} does not exist")

    def claim_task(self, task_id: str) -> bool:
        """Atomically claim a queued task; False means missing or already claimed.

        The conditional write and commit happen before returning. Each controller
        invocation should use its own connection (SQLite's normal thread rules).
        """
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status = ?",
                (TaskStatus.RUNNING, _timestamp(), task_id, TaskStatus.QUEUED),
            )
        return cursor.rowcount == 1
