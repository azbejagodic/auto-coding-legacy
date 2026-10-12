"""Persistent task storage, independent of workflow decisions and integrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
from pathlib import Path
import re
import sqlite3
from types import TracebackType
from uuid import uuid4

from .plans import (
    FrozenPlan, PlanConflictError, PlanExecutionUnavailableError, validate_plan,
)
from .review_history import ReviewHistory, initialize_review_schema
from .batch_history import BatchHistory, initialize_batch_schema


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


class PushState(StrEnum):
    LOCAL = "local"
    UNCERTAIN = "push_uncertain"
    VERIFIED = "verified"


@dataclass(frozen=True)
class TaskCommit:
    task_id: str
    commit_sha: str
    branch: str
    github_commit_url: str | None
    push_state: PushState


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

    def __init__(self, database_path: str | Path, *, read_only: bool = False) -> None:
        path = Path(database_path)
        if read_only:
            self._connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            self._connection.row_factory = sqlite3.Row
            self.reviews = ReviewHistory(self._connection)
            self.executions = BatchHistory(self._connection)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
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
                self._connection.execute("""
                    CREATE TABLE IF NOT EXISTS task_commits (
                        task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(task_id),
                        commit_sha TEXT NOT NULL,
                        branch TEXT NOT NULL,
                        github_commit_url TEXT,
                        push_state TEXT NOT NULL CHECK(push_state IN ('local', 'push_uncertain', 'verified'))
                    )
                """)
                _initialize_plan_schema(self._connection)
                initialize_review_schema(self._connection)
                initialize_batch_schema(self._connection)
        except Exception:
            self._connection.close()
            raise
        self.reviews = ReviewHistory(self._connection)
        self.executions = BatchHistory(self._connection)

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
            if (exc.sqlite_errorname == "SQLITE_CONSTRAINT_PRIMARYKEY"
                    or str(exc) == "Frozen task definition is immutable"):
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
        if self._connection.execute(
            "SELECT 1 FROM plan_tasks WHERE task_id = ?", (task_id,)
        ).fetchone():
            raise PlanExecutionUnavailableError(
                "Frozen plan tasks require a plan-aware controller; no execution was claimed"
            )
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status = ?
                   AND NOT EXISTS (SELECT 1 FROM task_commits WHERE task_commits.task_id = tasks.task_id)""",
                (TaskStatus.RUNNING, _timestamp(), task_id, TaskStatus.QUEUED),
            )
        return cursor.rowcount == 1

    def import_plan(self, plan: FrozenPlan | dict, *, approved: bool = False) -> FrozenPlan:
        """Revalidate and freeze a whole approved batch in one transaction."""
        if approved is not True:
            raise ValueError("Explicit plan approval is required")
        frozen = validate_plan(plan.to_dict() if isinstance(plan, FrozenPlan) else plan)
        definition = frozen.to_dict()
        timestamp = _timestamp()
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if self._connection.execute(
                "SELECT 1 FROM task_plans WHERE batch_id = ?", (definition["batch_id"],)
            ).fetchone():
                raise PlanConflictError("Batch ID already exists; frozen plans cannot be replaced")
            if any(self._connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task["task_id"],)
            ).fetchone() for task in definition["tasks"]):
                raise PlanConflictError("A task ID already exists; the entire import was rejected")
            self._connection.execute(
                "INSERT INTO task_plans VALUES (?, ?, ?, ?, ?, ?, ?)",
                (definition["batch_id"], definition["schema_version"], definition["expected_branch"],
                 frozen.task_count, frozen.fingerprint, frozen.canonical_json, timestamp),
            )
            for task in definition["tasks"]:
                self._connection.execute(
                    """INSERT INTO tasks (task_id, title, prompt, model, task_order, status,
                       acceptance_criteria, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task["task_id"], task["title"], task["prompt"], task["model"], task["task_order"],
                     TaskStatus.QUEUED, json.dumps(task["acceptance_criteria"], ensure_ascii=False),
                     timestamp, timestamp),
                )
                self._connection.execute(
                    "INSERT INTO plan_tasks VALUES (?, ?, ?)",
                    (task["task_id"], definition["batch_id"], task["task_order"]),
                )
        return frozen

    def get_plan(self, batch_id: str) -> FrozenPlan | None:
        row = self._connection.execute(
            "SELECT * FROM task_plans WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            return None
        plan = validate_plan(FrozenPlan(row["definition_json"]).to_dict())
        definition = plan.to_dict()
        if (plan.fingerprint != row["fingerprint"] or plan.canonical_json != row["definition_json"]
                or definition["batch_id"] != batch_id or definition["expected_branch"] != row["expected_branch"]
                or definition["schema_version"] != row["schema_version"] or plan.task_count != row["task_count"]):
            raise ValueError("Stored frozen plan integrity check failed")
        return plan

    def list_plans(self) -> list[FrozenPlan]:
        return [self.get_plan(row["batch_id"]) for row in self._connection.execute(
            "SELECT batch_id FROM task_plans ORDER BY imported_at, batch_id"
        ).fetchall()]

    def validate_plan_tasks(self, plan: FrozenPlan) -> None:
        """Check materialized tasks AND membership, not merely canonical JSON."""
        definitions = plan.to_dict()["tasks"]
        tasks = self.list_plan_tasks(plan.batch_id)
        if len(tasks) != len(definitions):
            raise ValueError("Frozen task membership differs from plan")
        for task, definition in zip(tasks, definitions):
            if any(getattr(task, key) != definition[key] for key in (
                "task_id", "title", "prompt", "model", "task_order", "acceptance_criteria"
            )):
                raise ValueError("Stored task differs from frozen definition")
            membership = self._connection.execute(
                "SELECT task_order FROM plan_tasks WHERE task_id = ?", (task.task_id,)
            ).fetchone()
            if membership["task_order"] != definition["task_order"]:
                raise ValueError("Stored task order differs from frozen definition")

    def bind_batch(self, plan: FrozenPlan, workspace: str, origin: str, initial_sha: str) -> dict:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if self.get_plan(plan.batch_id) != plan:
                raise ValueError("Frozen batch changed")
            binding = self.executions.binding(plan.batch_id)
            if binding:
                if any(binding[key] != value for key, value in (
                    ("fingerprint", plan.fingerprint), ("workspace", workspace), ("origin", origin)
                )):
                    raise ValueError("Batch is bound to a different workspace or origin")
            else:
                self._connection.execute("INSERT INTO batch_workspaces VALUES (?, ?, ?, ?, ?)",
                                         (plan.batch_id, plan.fingerprint, workspace, origin, initial_sha))
        return self.executions.binding(plan.batch_id)

    def claim_plan_task(self, plan: FrozenPlan, task_id: str, baseline_sha: str) -> bool:
        """Atomic frozen-plan claim + durable intent; legacy claims stay blocked."""
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            binding = self.executions.binding(plan.batch_id)
            if (self.get_plan(plan.batch_id) != plan or not binding
                    or binding["fingerprint"] != plan.fingerprint):
                raise ValueError("Approved batch identity or workspace binding is missing")
            self.validate_plan_tasks(plan)
            definitions = plan.to_dict()["tasks"]
            index = next(i for i, task in enumerate(definitions) if task["task_id"] == task_id)
            definition = definitions[index]
            if "execution_limits" not in definition:
                raise ValueError("Frozen execution limits are required")
            expected_sha = binding["initial_sha"]
            # Strict sequence is stronger than just the explicitly listed dependencies.
            for earlier in definitions[:index]:
                execution = self.executions.get(earlier["task_id"])
                commit = self.get_commit(earlier["task_id"])
                if (self.get_task(earlier["task_id"]).status != TaskStatus.APPROVED
                        or not execution or execution["stage"] != "finalized"
                        or not commit or commit.push_state != PushState.VERIFIED
                        or execution["baseline_sha"] != expected_sha
                        or execution.get("remote_sha") != commit.commit_sha
                        or execution.get("local_sha") != commit.commit_sha):
                    raise ValueError("Earlier tasks must be durably approved")
                expected_sha = commit.commit_sha
            if baseline_sha != expected_sha or not re.fullmatch(r"[0-9a-f]{40}", baseline_sha):
                raise ValueError("Task baseline differs from the approved execution history")
            if self.executions.get(task_id) or self.get_commit(task_id):
                return False
            cursor = self._connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status = ?",
                (TaskStatus.RUNNING, _timestamp(), task_id, TaskStatus.QUEUED),
            )
            if cursor.rowcount != 1:
                return False
            timestamp = _timestamp()
            record = {
                "batch_id": plan.batch_id, "task_id": task_id, "fingerprint": plan.fingerprint,
                "model": definition["model"], "reasoning_effort": definition["reasoning_effort"],
                "execution_limits": definition["execution_limits"], "baseline_sha": baseline_sha,
                "stage": "claimed", "started_at": timestamp, "updated_at": timestamp,
                "recovery_required": True,
                "checkpoints": [{"stage": "claimed", "at": timestamp}],
            }
            self._connection.execute("INSERT INTO task_executions VALUES (?, ?, ?)",
                                     (task_id, plan.batch_id, json.dumps(record)))
        return True

    def finalize_plan_task(self, task_id: str, review_id: str, *, approved: bool) -> None:
        """Caller verifies authoritative review/Git evidence before finalization."""
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._finalize_plan_task(task_id, review_id, approved=approved)

    def _finalize_plan_task(self, task_id: str, review_id: str, *, approved: bool) -> None:
        """Shared finalization inside the caller's transaction."""
        execution = self.executions.get(task_id)
        commit = self.get_commit(task_id)
        frozen = self.get_task_plan(task_id)
        if (not execution or execution["stage"] != "review_finished"
                or execution.get("review_id") != review_id
                or not commit or commit.push_state != PushState.VERIFIED
                or execution.get("remote_sha") != commit.commit_sha
                or execution.get("local_sha") != commit.commit_sha
                or not frozen or execution["fingerprint"] != frozen[0].fingerprint):
            raise ValueError("Execution is not ready for finalization")
        self.validate_plan_tasks(frozen[0])
        review = self.reviews.get(review_id)
        expected = "APPROVED" if approved else "REJECTED"
        if (not review or review["task_id"] != task_id or review["commit_sha"] != commit.commit_sha
                or review["decision"] != expected
                or (approved and (review.get("error") or review.get("coverage_complete") is not True))):
            raise ValueError("Durable review differs from finalization")
        cursor = self._connection.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status = ?",
            (TaskStatus.APPROVED if approved else TaskStatus.REJECTED, _timestamp(),
             task_id, TaskStatus.AWAITING_REVIEW),
        )
        if cursor.rowcount != 1:
            raise ValueError("Task state changed during review")
        self.executions._checkpoint(task_id, "finalized" if approved else "stopped", {
            "ended_at": _timestamp(), "failure_stage": None if approved else "review",
            "error": None if approved else "Groq rejected the commit",
            "recovery_required": not approved,
        })

    def claim_review_recovery(self, plan: FrozenPlan, task: Task, execution: dict,
                              original_review: dict, config: dict, binding: dict, *,
                              previous_recovery: dict | None = None,
                              continuation: dict | None = None) -> dict:
        """Durable one-attempt intent. Interrupted attempts are never replayed."""
        from dataclasses import asdict
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            previous_reviews = ([previous_recovery["original_review"], original_review]
                                if previous_recovery is not None else [original_review])
            if continuation is not None:
                previous_reviews = continuation["previous_reviews"]
                audit_matches = self.executions.signatures(self.executions.recoveries(task.task_id)) == continuation["prior_recoveries"]
            else:
                audit_matches = (self.executions.recovery(task.task_id) == previous_recovery
                                 and self.executions.recovery(task.task_id, compatibility=True) is None
                                 and not self.executions.continuations(task.task_id))
            if (self.get_plan(plan.batch_id) != plan or self.get_task(task.task_id) != task
                    or task.status != TaskStatus.AWAITING_REVIEW
                    or self.executions.get(task.task_id) != execution
                    or self.executions.binding(plan.batch_id) != binding
                    or self.reviews.list(task.task_id) != previous_reviews
                    or not audit_matches):
                raise ValueError("Recovery state changed or was already claimed")
            self.validate_plan_tasks(plan)
            commit = self.get_commit(task.task_id)
            if (not commit or commit.push_state != PushState.VERIFIED
                    or commit.commit_sha != execution.get("local_sha")
                    or commit.commit_sha != original_review["commit_sha"]):
                raise ValueError("Recovery commit state changed")
            record = {
                "recovery_id": uuid4().hex, "task_id": task.task_id, "batch_id": plan.batch_id,
                "commit_sha": commit.commit_sha, "original_review_id": original_review["review_id"],
                "started_at": _timestamp(), "stage": "claimed", "review_id": None,
                "original_execution": execution, "original_review": original_review,
                "original_task": asdict(task), "original_commit": asdict(commit), "config": config,
                "original_binding": binding,
            }
            table = "review_recoveries"
            if previous_recovery is not None:
                if (previous_recovery.get("stage") != "finished"
                        or previous_recovery.get("decision") != "UNCERTAIN"
                        or previous_recovery.get("review_id") != original_review["review_id"]):
                    raise ValueError("Previous recovery is not a completed technical failure")
                table = "review_compatibility_recoveries"
                record.update(compatibility_retry=True, previous_recovery=previous_recovery,
                              previous_reviews=previous_reviews)
            if continuation is not None:
                if previous_reviews[-1] != original_review:
                    raise ValueError("Continuation parent differs from the inspected review")
                record.update(technical_continuation=True, **continuation)
                self._connection.execute("INSERT INTO review_continuations VALUES (?, ?, ?, ?)",
                    (task.task_id, record["recovery_id"], original_review["review_id"],
                     json.dumps(record, ensure_ascii=False, allow_nan=False)))
            else:
                self._connection.execute(
                    f"INSERT INTO {table} VALUES (?, ?, ?)",
                    (task.task_id, record["recovery_id"], json.dumps(record, ensure_ascii=False, allow_nan=False)),
                )
        return record

    def attach_recovery_review(self, task_id: str, recovery_id: str, review_id: str) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self.executions.recovery_by_id(task_id, recovery_id)
            review = self.reviews.get(review_id)
            if (not record or record["recovery_id"] != recovery_id or record["stage"] != "claimed"
                    or not review or review["task_id"] != task_id
                    or review["commit_sha"] != record["commit_sha"]
                    or review.get("recovery_id") != recovery_id):
                raise ValueError("Recovery review attachment differs from the claimed attempt")
            record.update(stage="reviewing", review_id=review_id)
            self.executions._save_recovery(record)

    def finish_review_recovery(self, task_id: str, recovery_id: str, review_id: str,
                               expected_review: dict) -> None:
        """Atomic review linkage, task finalization and recovery-event completion."""
        from dataclasses import asdict
        from .batch_controller import _approval
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self.executions.recovery_by_id(task_id, recovery_id)
            if (not record or record["recovery_id"] != recovery_id
                    or record["stage"] != "reviewing" or record["review_id"] != review_id):
                raise ValueError("Recovery is missing, interrupted or already completed")
            plan = self.get_plan(record["batch_id"])
            task, commit = self.get_task(task_id), self.get_commit(task_id)
            review = self.reviews.get(review_id)
            if (not plan or plan.fingerprint != record["original_execution"]["fingerprint"]
                    or not task or asdict(task) != record["original_task"]
                    or not commit or asdict(commit) != record["original_commit"]
                    or self.executions.get(task_id) != record["original_execution"]
                    or self.executions.binding(record["batch_id"]) != record["original_binding"]
                    or self.reviews.get(record["original_review_id"]) != record["original_review"]
                    or review != expected_review
                    or not review or review.get("recovery_id") != recovery_id
                    or review["task_id"] != task_id or review["commit_sha"] != commit.commit_sha):
                raise ValueError("Recovery evidence changed before finalization")
            histories = self.reviews.list(task_id)
            previous = record.get("previous_reviews", [record["original_review"]])
            if (len(histories) != len(previous) + 1
                    or {item["review_id"] for item in histories} != {
                        *(item["review_id"] for item in previous), review_id}
                    or any(self.reviews.get(item["review_id"]) != item for item in previous)
                    or (record.get("compatibility_retry") and
                        self.executions.recovery(task_id) != record["previous_recovery"])
                    or (record.get("technical_continuation") and
                        self.executions.signatures([r for r in self.executions.recoveries(task_id) if r["recovery_id"] != recovery_id])
                        != record["prior_recoveries"])):
                raise ValueError("Conflicting review history appeared before finalization")
            self.validate_plan_tasks(plan)
            decision = review["decision"]
            if decision == "APPROVED" and not _approval(
                review, task_id, commit.commit_sha, task.acceptance_criteria
            ):
                raise ValueError("Recovery approval lacks durable complete coverage")
            if decision == "REJECTED" and not review.get("blocking_issues"):
                raise ValueError("Recovery rejection lacks a blocking issue")
            if decision not in ("APPROVED", "REJECTED", "UNCERTAIN"):
                raise ValueError("Invalid recovery decision")
            details = {"review_id": review_id, "review_status": decision, "recovery_id": recovery_id}
            if decision in ("APPROVED", "REJECTED"):
                self.executions._checkpoint(task_id, "review_finished", details)
                self._finalize_plan_task(task_id, review_id, approved=decision == "APPROVED")
            else:
                self.executions._checkpoint(task_id, "stopped", {
                    **details, "failure_stage": "review_recovery", "recovery_required": True,
                    "error": "Recovery review did not provide complete approval", "ended_at": _timestamp(),
                })
            record.update(stage="finished", decision=decision, finished_at=_timestamp())
            self.executions._save_recovery(record)

    def block_review_recovery(self, task_id: str, recovery_id: str) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self.executions.recovery_by_id(task_id, recovery_id)
            if record and record["recovery_id"] == recovery_id and record["stage"] != "finished":
                record.update(stage="blocked", error="Recovery interrupted or safety/persistence verification failed",
                              finished_at=_timestamp())
                self.executions._save_recovery(record)

    def get_task_plan(self, task_id: str) -> tuple[FrozenPlan, dict] | None:
        """Return the integrity-checked frozen definition, if this is a plan task."""
        row = self._connection.execute(
            "SELECT batch_id FROM plan_tasks WHERE task_id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return None
        plan = self.get_plan(row["batch_id"])
        if plan is None:
            raise ValueError("Frozen plan membership is inconsistent")
        definition = next(task for task in plan.to_dict()["tasks"] if task["task_id"] == task_id)
        return plan, definition

    def list_plan_tasks(self, batch_id: str) -> list[Task]:
        rows = self._connection.execute(
            """SELECT tasks.* FROM tasks JOIN plan_tasks USING(task_id)
               WHERE plan_tasks.batch_id = ? ORDER BY plan_tasks.task_order""", (batch_id,)
        ).fetchall()
        return [_task_from_row(row) for row in rows]

    def get_commit(self, task_id: str) -> TaskCommit | None:
        row = self._connection.execute("SELECT * FROM task_commits WHERE task_id = ?", (task_id,)).fetchone()
        return (TaskCommit(row["task_id"], row["commit_sha"], row["branch"], row["github_commit_url"],
                           PushState(row["push_state"])) if row is not None else None)

    def record_commit(self, task_id: str, commit_sha: str, branch: str,
                      github_commit_url: str | None) -> None:
        """Save the local commit before any push; never overwrite a previous commit."""
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit_sha):
            raise ValueError("A full commit SHA is required")
        if not branch or branch.casefold() == "main":
            raise ValueError("A development branch is required")
        with self._connection:
            self._connection.execute(
                "INSERT INTO task_commits VALUES (?, ?, ?, ?, ?)",
                (task_id, commit_sha, branch, github_commit_url, PushState.LOCAL),
            )

    def mark_push_attempted(self, task_id: str, github_commit_url: str | None = None) -> None:
        """Persist uncertainty before network I/O, including crash recovery."""
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE task_commits SET push_state = ?, github_commit_url = COALESCE(?, github_commit_url)
                   WHERE task_id = ? AND push_state = ?""",
                (PushState.UNCERTAIN, github_commit_url, task_id, PushState.LOCAL),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task has no unpushed local commit")

    def complete_push(self, task_id: str, commit_sha: str, branch: str, github_commit_url: str) -> None:
        """Atomically persist verified metadata and awaiting_review, or neither.

        The caller must independently verify the remote before calling this.
        """
        if not github_commit_url:
            raise ValueError("A GitHub commit URL is required")
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE task_commits SET github_commit_url = ?, push_state = ?
                   WHERE task_id = ? AND commit_sha = ? AND branch = ? AND push_state = ?""",
                (github_commit_url, PushState.VERIFIED, task_id, commit_sha, branch, PushState.UNCERTAIN),
            )
            if cursor.rowcount != 1:
                raise ValueError("Commit metadata differs from the verified push")
            cursor = self._connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status = ?",
                (TaskStatus.AWAITING_REVIEW, _timestamp(), task_id, TaskStatus.READY_TO_COMMIT),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task is not ready to commit")


def _initialize_plan_schema(connection: sqlite3.Connection) -> None:
    """Additive migration; called inside the existing initialization transaction."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS task_plans (
            batch_id TEXT PRIMARY KEY NOT NULL,
            schema_version INTEGER NOT NULL CHECK(schema_version = 1),
            expected_branch TEXT NOT NULL,
            task_count INTEGER NOT NULL CHECK(task_count > 0),
            fingerprint TEXT NOT NULL CHECK(length(fingerprint) = 64),
            definition_json TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS plan_tasks (
            task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(task_id),
            batch_id TEXT NOT NULL REFERENCES task_plans(batch_id),
            task_order INTEGER NOT NULL,
            UNIQUE(batch_id, task_order)
        )
    """)
    # Do not use executescript: it can implicitly commit the outer migration.
    for table in ("task_plans", "plan_tasks"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(f"""
                CREATE TRIGGER IF NOT EXISTS frozen_{table}_{action.lower()}
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT, 'Frozen plan definition is immutable'); END
            """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS frozen_batch_replace BEFORE INSERT ON task_plans
        WHEN EXISTS (SELECT 1 FROM task_plans WHERE batch_id = NEW.batch_id)
        BEGIN SELECT RAISE(ABORT, 'Frozen plan definition is immutable'); END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS frozen_membership_replace BEFORE INSERT ON plan_tasks
        WHEN EXISTS (SELECT 1 FROM plan_tasks WHERE task_id = NEW.task_id)
        BEGIN SELECT RAISE(ABORT, 'Frozen plan definition is immutable'); END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS frozen_task_definition
        BEFORE UPDATE OF task_id, title, prompt, model, task_order, acceptance_criteria, created_at ON tasks
        WHEN EXISTS (SELECT 1 FROM plan_tasks WHERE task_id = OLD.task_id)
        BEGIN SELECT RAISE(ABORT, 'Frozen task definition is immutable'); END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS frozen_task_delete BEFORE DELETE ON tasks
        WHEN EXISTS (SELECT 1 FROM plan_tasks WHERE task_id = OLD.task_id)
        BEGIN SELECT RAISE(ABORT, 'Frozen task definition is immutable'); END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS frozen_task_replace BEFORE INSERT ON tasks
        WHEN EXISTS (SELECT 1 FROM plan_tasks WHERE task_id = NEW.task_id)
        BEGIN SELECT RAISE(ABORT, 'Frozen task definition is immutable'); END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS frozen_plan_membership BEFORE INSERT ON plan_tasks
        WHEN (SELECT count(*) FROM plan_tasks WHERE batch_id = NEW.batch_id) >=
             (SELECT task_count FROM task_plans WHERE batch_id = NEW.batch_id)
        BEGIN SELECT RAISE(ABORT, 'Frozen plan membership is immutable'); END
    """)
