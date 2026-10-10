from datetime import datetime
import json
import sqlite3

import pytest

from auto_coding.storage import DuplicateTaskError, TaskStatus, TaskStore


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "runtime" / "tasks.sqlite3"


@pytest.fixture
def task_data():
    return dict(
        task_id="TASK-002",
        title="SQLite storage",
        prompt="Keep the original prompt: 'quoted' text.\nUnicode: ž",
        model="assigned-model",
        task_order=2,
        acceptance_criteria=["Persist tasks", "Preserve Unicode: ž"],
    )


def test_initialization_creates_database_and_empty_schema(database_path):
    with TaskStore(database_path) as store:
        assert database_path.is_file()
        assert store.list_tasks() == []
        assert store.get_task("missing") is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_add_and_retrieve_preserves_all_fields(database_path, task_data):
    with TaskStore(database_path) as store:
        task = store.add_task(**task_data)
        assert store.get_task(task.task_id) == task
        for field, value in task_data.items():
            assert getattr(task, field) == value
        assert task.status == TaskStatus.QUEUED
        assert task.created_at == task.updated_at
        assert datetime.fromisoformat(task.created_at).utcoffset().total_seconds() == 0
    with sqlite3.connect(database_path) as connection:
        criteria = connection.execute("SELECT acceptance_criteria FROM tasks").fetchone()[0]
        assert json.loads(criteria) == task_data["acceptance_criteria"]


def test_tasks_listed_in_order(database_path, task_data):
    with TaskStore(database_path) as store:
        for order in (3, 1, 2):
            store.add_task(**(task_data | {"task_id": f"TASK-{order}", "task_order": order}))
        assert [task.task_order for task in store.list_tasks()] == [1, 2, 3]


@pytest.mark.parametrize("status", list(TaskStatus))
def test_status_update_preserves_task_data(database_path, task_data, status):
    with TaskStore(database_path) as store:
        original = store.add_task(**task_data)
        store.update_status(original.task_id, status)
        updated = store.get_task(original.task_id)
        assert updated.status == status
        assert datetime.fromisoformat(updated.updated_at) >= datetime.fromisoformat(original.updated_at)
        for field in (*task_data, "created_at"):
            assert getattr(updated, field) == getattr(original, field)


def test_persistence_after_reopening(database_path, task_data):
    with TaskStore(database_path) as store:
        store.add_task(**task_data)
        store.update_status(task_data["task_id"], TaskStatus.AWAITING_REVIEW)
        expected = store.get_task(task_data["task_id"])
    with TaskStore(database_path) as reopened:
        assert reopened.get_task(expected.task_id) == expected
        assert reopened.list_tasks() == [expected]


def test_duplicate_id_rejected_without_modifying_original(database_path, task_data):
    with TaskStore(database_path) as store:
        original = store.add_task(**task_data)
        with pytest.raises(DuplicateTaskError, match="already exists"):
            store.add_task(**(task_data | {"title": "Replacement"}))
        assert store.list_tasks() == [original]
        store.update_status(original.task_id, TaskStatus.RUNNING)
        assert store.get_task(original.task_id).status == TaskStatus.RUNNING


def test_invalid_status_rejected_on_add_and_update(database_path, task_data):
    with TaskStore(database_path) as store:
        with pytest.raises(ValueError, match="Invalid task status"):
            store.add_task(**task_data, status="unknown")
        assert store.list_tasks() == []
        original = store.add_task(**task_data)
        with pytest.raises(ValueError, match="Invalid task status"):
            store.update_status(original.task_id, "unknown")
        assert store.get_task(original.task_id) == original


def test_update_missing_task_raises_clear_error(database_path):
    with TaskStore(database_path) as store:
        with pytest.raises(KeyError, match="does not exist"):
            store.update_status("missing", TaskStatus.FAILED)


def test_acceptance_criteria_must_be_list_of_strings(database_path, task_data):
    with TaskStore(database_path) as store:
        with pytest.raises(ValueError, match="list of strings"):
            store.add_task(**(task_data | {"acceptance_criteria": "not a list"}))
        assert store.list_tasks() == []


def test_claim_only_queued_tasks_and_persist(database_path, task_data):
    with TaskStore(database_path) as store:
        store.add_task(**task_data)
        assert not store.claim_task("missing")
        assert store.claim_task(task_data["task_id"])
        assert not store.claim_task(task_data["task_id"])
    with TaskStore(database_path) as reopened:
        assert reopened.get_task(task_data["task_id"]).status == TaskStatus.RUNNING
        assert not reopened.claim_task(task_data["task_id"])


def test_legacy_schema_migration_preserves_data(database_path, task_data):
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("""CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL,
            prompt TEXT NOT NULL, model TEXT NOT NULL, task_order INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'queued', 'running', 'awaiting_review', 'approved', 'rejected', 'failed'
            )), acceptance_criteria TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        for order, status in enumerate(("queued", "running", "awaiting_review", "approved", "rejected", "failed")):
            connection.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (status, task_data["title"], task_data["prompt"], task_data["model"], order,
                                status, json.dumps(task_data["acceptance_criteria"]), "created", "updated"))
        original = connection.execute("SELECT * FROM tasks ORDER BY task_order").fetchall()
    with TaskStore(database_path) as store:
        assert len(store.list_tasks()) == 6
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT * FROM tasks ORDER BY task_order").fetchall() == original
        store.update_status("queued", TaskStatus.READY_TO_COMMIT)
    with TaskStore(database_path) as reopened:
        assert reopened.get_task("queued").status == TaskStatus.READY_TO_COMMIT
        assert len(reopened.list_tasks()) == 6
