"""Private review/request history on the existing SQLite connection."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any
from uuid import uuid4

from .review_security import sanitize
from .reviews import SHA, encoded


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def initialize_review_schema(connection: sqlite3.Connection) -> None:
    # Ordinary execute preserves TaskStore's atomic migration transaction.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            review_id TEXT PRIMARY KEY NOT NULL,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            commit_sha TEXT NOT NULL CHECK(length(commit_sha) = 40),
            created_at TEXT NOT NULL,
            record_json TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS review_requests (
            review_id TEXT NOT NULL REFERENCES reviews(review_id),
            request_index INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            record_json TEXT NOT NULL,
            PRIMARY KEY(review_id, request_index)
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS reviews_task ON reviews(task_id, created_at)")


class ReviewHistory:
    """Uses TaskStore's lifecycle/thread ownership; never changes task state.

    Text is screened again at the persistence boundary. SQLite is not encrypted;
    use a private OS account/directory. No raw provider exceptions are stored.
    """

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    @staticmethod
    def _safe(record: dict) -> str:
        safe, _ = sanitize(record)
        return encoded(safe)

    def create(self, task_id: str, commit_sha: str, record: dict) -> str:
        if not SHA.fullmatch(commit_sha):
            raise ValueError("Full review commit SHA required")
        review_id, timestamp = uuid4().hex, utc_now()
        with self._connection:
            self._connection.execute("INSERT INTO reviews VALUES (?, ?, ?, ?, ?)",
                                     (review_id, task_id, commit_sha, timestamp, self._safe(record)))
        return review_id

    def finish(self, review_id: str, record: dict) -> None:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE reviews SET record_json = ? WHERE review_id = ?",
                (self._safe(record), review_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("Review not found")

    def start_request(self, review_id: str, index: int, record: dict) -> None:
        # Persist the actual intended wire arguments before I/O. A crash leaves
        # an uncertain attempt, never a fabricated response or known token count.
        with self._connection:
            self._connection.execute("INSERT INTO review_requests VALUES (?, ?, ?, ?)",
                                     (review_id, index, utc_now(), self._safe(record)))

    def finish_request(self, review_id: str, index: int, record: dict) -> None:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE review_requests SET record_json = ? WHERE review_id = ? AND request_index = ?",
                (self._safe(record), review_id, index),
            )
            if cursor.rowcount != 1:
                raise KeyError("Review request not found")

    def get(self, review_id: str) -> dict | None:
        row = self._connection.execute("SELECT * FROM reviews WHERE review_id = ?", (review_id,)).fetchone()
        if row is None:
            return None
        result = {**json.loads(row["record_json"]), "review_id": row["review_id"],
                  "task_id": row["task_id"], "commit_sha": row["commit_sha"],
                  "created_at": row["created_at"]}
        result["requests"] = [
            {**json.loads(item["record_json"]), "created_at": item["created_at"],
             "request_index": item["request_index"]}
            for item in self._connection.execute(
                "SELECT * FROM review_requests WHERE review_id = ? ORDER BY request_index", (review_id,)
            )
        ]
        return result

    def list(self, task_id: str | None = None) -> list[dict]:
        rows = self._connection.execute(
            "SELECT review_id FROM reviews" + (" WHERE task_id = ?" if task_id is not None else "") +
            " ORDER BY created_at, review_id", (task_id,) if task_id is not None else (),
        ).fetchall()
        return [self.get(row["review_id"]) for row in rows]

    def usage(self, *, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """Default UTC calendar-day estimate, not the account's actual quota.

        Supply explicit UTC ISO bounds for a known provider quota period.
        Other clients/Playground and usage omitted by Groq are unknowable here.
        """
        start = start or datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        end = end or utc_now()
        requests = self._connection.execute(
            "SELECT created_at, record_json FROM review_requests "
            "WHERE created_at >= ? AND created_at <= ? ORDER BY created_at", (start, end),
        ).fetchall()
        totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        unknown, errors, headers = 0, [], {}
        for row in requests:
            item = json.loads(row["record_json"])
            usage = item.get("usage")
            if usage is None:
                unknown += 1
            else:
                for key in totals:
                    totals[key] += usage[key]
            if item.get("error"):
                errors.append(item["error"])
            if item.get("rate_limits"):
                headers = self._limits(item, row["created_at"])
        return {**totals, "api_requests": len(requests), "unknown_usage_requests": unknown,
                "errors": errors, "provider_reported": headers, "period_start": start,
                "period_end": end, "estimate_only": True, "account_remaining_quota": None}

    def latest_limits(self, model: str) -> dict:
        rows = self._connection.execute(
            "SELECT created_at, record_json FROM review_requests ORDER BY created_at DESC"
        )
        for row in rows:
            item = json.loads(row["record_json"])
            if item.get("model") == model and item.get("rate_limits"):
                return self._limits(item, row["created_at"])
        return {}

    @staticmethod
    def _limits(item: dict, created_at: str) -> dict:
        # Preserve a response observation; never move an old deadline to 'now'.
        # Legacy records use their durable completion/creation timestamp.
        return {**item["rate_limits"], "observed_at": item.get(
            "rate_limits_observed_at", item.get("completed_at", created_at))}
