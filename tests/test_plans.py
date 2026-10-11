"""Pure validation, transactional storage and CLI tests; no live integrations."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sqlite3
import subprocess
from threading import Barrier
from unittest.mock import Mock

import pytest

from auto_coding.cli import main
from auto_coding.controller import SingleTaskController
from auto_coding.plans import (
    FrozenPlan, PlanConflictError, PlanExecutionUnavailableError, PlanValidationError,
    load_plan, validate_plan,
)
from auto_coding.storage import DuplicateTaskError, PushState, TaskStatus, TaskStore
from auto_coding.workspace_lock import WorkspaceLock


EXAMPLE = Path(__file__).parents[1] / "examples" / "task-plan.json"


@pytest.fixture
def definition():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def no_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Importer attempted external execution/network")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    monkeypatch.setattr("auto_coding.integrations.codex.run_codex", forbidden)
    monkeypatch.setattr("auto_coding.test_runner.run_tests", forbidden)
    monkeypatch.setattr("auto_coding.integrations.git.GitRepository", forbidden)


def test_three_task_import_reopen_preserves_every_original_value(tmp_path, definition, no_execution):
    definition["tasks"][0]["prompt"] = "  Original Āžbe 🐍\r\n" + ("full original text\n" * 10000) + "  "
    plan = validate_plan(definition)
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        assert store.import_plan(plan, approved=True) == plan
        assert store.get_plan(plan.batch_id).to_dict() == definition
        tasks = store.list_plan_tasks(plan.batch_id)
        assert [task.task_order for task in tasks] == [1, 2, 3]
        assert [task.task_id for task in tasks] == [task["task_id"] for task in definition["tasks"]]
        for stored, original in zip(tasks, definition["tasks"]):
            for key in ("task_id", "title", "prompt", "model", "task_order", "acceptance_criteria"):
                assert getattr(stored, key) == original[key]
            assert stored.status == TaskStatus.QUEUED
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        assert store.get_plan(plan.batch_id) == plan
        assert store.list_plan_tasks(plan.batch_id) == tasks
        assert store.get_plan("missing") is None
        assert store.list_plan_tasks("missing") == []


def test_frozen_snapshot_is_detached_and_object_key_order_does_not_change_hash(definition):
    original = deepcopy(definition)
    plan = validate_plan(definition)
    assert validate_plan(json.loads(json.dumps(definition, indent=4, ensure_ascii=True))) == plan
    reverse = dict(reversed(list(definition.items())))
    assert validate_plan(reverse).fingerprint == plan.fingerprint
    definition["tasks"][0]["prompt"] = "changed"
    copy = plan.to_dict()
    copy["tasks"][0]["title"] = "changed"
    assert plan.to_dict() == original
    with pytest.raises(FrozenInstanceError):
        plan.canonical_json = "{}"


def test_snapshot_precedes_validation_of_caller_owned_containers(definition, monkeypatch):
    from auto_coding import plans
    original = deepcopy(definition)
    branch_check = plans._branch
    def caller_mutation(value):
        branch_check(value)
        definition["tasks"][0]["prompt"] = "Caller changed the prompt while validation was running"
    monkeypatch.setattr(plans, "_branch", caller_mutation)
    assert validate_plan(definition).to_dict() == original


@pytest.mark.parametrize("field,value", [
    ("title", "A different title"), ("prompt", "Different full prompt"),
    ("model", "different-model"), ("reasoning_effort", "low"), ("task_order", 4),
    ("acceptance_criteria", ["New criterion"]), ("dependencies", ["EXAMPLE-001", "EXAMPLE-002"]),
    ("permitted_files", ["docs/other.md"]), ("test_commands", [["python", "-m", "unittest"]]),
    ("approved_placeholders", ["LATEST_SHA", "BRANCH"]),
    ("task_id", "EXAMPLE-OTHER"),
])
def test_each_task_field_affects_fingerprint(definition, field, value):
    before = validate_plan(definition).fingerprint
    definition["tasks"][2][field] = value
    assert validate_plan(definition).fingerprint != before


def test_batch_configuration_and_optional_presence_affect_fingerprint(definition):
    original = validate_plan(definition).fingerprint
    for key, value in (("batch_id", "DIFFERENT"), ("expected_branch", "feature/other")):
        changed = deepcopy(definition)
        changed[key] = value
        assert validate_plan(changed).fingerprint != original
    definition["tasks"][1]["approved_placeholders"] = []
    assert validate_plan(definition).fingerprint != original


@pytest.mark.parametrize("field", [
    "task_id", "title", "prompt", "model", "reasoning_effort", "task_order",
    "acceptance_criteria", "dependencies", "permitted_files", "test_commands",
])
def test_missing_task_field_rejected(definition, field):
    del definition["tasks"][1][field]
    with pytest.raises(PlanValidationError):
        validate_plan(definition)


@pytest.mark.parametrize("field", ["schema_version", "batch_id", "expected_branch", "tasks"])
def test_missing_batch_field_rejected(definition, field):
    del definition[field]
    with pytest.raises(PlanValidationError):
        validate_plan(definition)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("schema_version", "1"),
    ("batch_id", ""), ("batch_id", "../batch"), ("tasks", []), ("tasks", {}),
    ("expected_branch", "main"), ("expected_branch", "MAIN"), ("expected_branch", "master"),
    ("expected_branch", "HEAD"), ("expected_branch", "@"), ("expected_branch", "refs/heads/main"),
    ("expected_branch", "-branch"), ("expected_branch", "feature/../other"),
    ("expected_branch", "feature name"), ("expected_branch", "feature/a.lock"),
    ("expected_branch", "feature/@{1}"), ("expected_branch", "/main"),
])
def test_malformed_batch_rejected(definition, field, value):
    definition[field] = value
    with pytest.raises(PlanValidationError):
        validate_plan(definition)


@pytest.mark.parametrize("field,value", [
    ("title", ""), ("prompt", " \r\n "), ("prompt", "\ud800"), ("prompt", "NUL\x00"),
    ("model", ""), ("model", "-model"), ("reasoning_effort", "unapproved"),
    ("reasoning_effort", None), ("task_order", True), ("task_order", 0),
    ("task_order", "2"), ("task_order", 2**63), ("acceptance_criteria", []), ("acceptance_criteria", [" "]),
    ("acceptance_criteria", [7]), ("dependencies", "EXAMPLE-001"),
    ("dependencies", ["EXAMPLE-001", "EXAMPLE-001"]), ("dependencies", ["missing"]),
    ("dependencies", ["EXAMPLE-002"]), ("dependencies", ["EXAMPLE-003"]),
    ("permitted_files", []), ("permitted_files", ["src/a.py", r"SRC\a.py"]),
])
def test_malformed_task_rejected(definition, field, value):
    definition["tasks"][1][field] = value
    with pytest.raises(PlanValidationError):
        validate_plan(definition)


def test_duplicates_cycles_and_reordered_tasks_rejected(definition):
    changed = deepcopy(definition)
    changed["tasks"][1]["task_id"] = "EXAMPLE-001"
    with pytest.raises(PlanValidationError):
        validate_plan(changed)
    changed = deepcopy(definition)
    changed["tasks"][1]["task_order"] = 1
    with pytest.raises(PlanValidationError):
        validate_plan(changed)
    definition["tasks"][0]["dependencies"] = ["EXAMPLE-003"]
    with pytest.raises(PlanValidationError):
        validate_plan(definition)


@pytest.mark.parametrize("path", [
    "../outside", "src/../../outside", r"src\..\outside", "/absolute/file",
    r"C:\project\file.py", "C:relative.py", r"\\server\share\file", "//server/share/file",
    ":(glob)**", ":!secret", "src/*.py", "src/a?.py", "src/[ab].py",
    "src/:literal", "-", ".", "src/./a.py", "src//a.py", "src/", "src/a.py:stream",
    "src/a.py.", "src/a.py ", "src/NUL.txt", "src/CON", ".git/config", ".env",
    "credentials/key.txt", "config.toml", "private.pem", "src/\x00file", "{{BRANCH}}/a.py",
])
def test_unsafe_and_sensitive_paths_rejected(definition, path):
    definition["tasks"][0]["permitted_files"] = [path]
    with pytest.raises(PlanValidationError):
        validate_plan(definition)


def test_portable_unicode_and_windows_separators_preserved(definition):
    files = [r"src\Āžbe.py", "docs/file with spaces.md"]
    definition["tasks"][0]["permitted_files"] = files
    assert validate_plan(definition).to_dict()["tasks"][0]["permitted_files"] == files


@pytest.mark.parametrize("commands", [
    [], "python -m pytest", [""], [[]], [[""]], [[" "]], [[1]], [["python", None]],
    [["python", "NUL\x00"]], [["python", "{{LATEST_SHA}}"]], [{"exe": "pytest"}],
])
def test_invalid_test_commands_rejected(definition, commands):
    definition["tasks"][0]["test_commands"] = commands
    with pytest.raises(PlanValidationError):
        validate_plan(definition)


def test_arguments_preserved_not_executed_or_resolved(definition, no_execution):
    definition["tasks"][0]["test_commands"] = [["not-installed-test-tool", "", "--argument=private"]]
    assert validate_plan(definition).to_dict() == definition


@pytest.mark.parametrize("prompt,approved", [
    ("{{UNKNOWN}}", ["BRANCH"]), ("{{BRANCH}}", []), ("{{ BRANCH }}", ["BRANCH"]),
    ("{{branch}}", ["BRANCH"]), ("{{BRANCH", ["BRANCH"]), ("BRANCH}}", ["BRANCH"]),
    ("literal", ["UNKNOWN"]), ("literal", ["BRANCH", "BRANCH"]),
])
def test_unapproved_unknown_or_malformed_placeholders_rejected(definition, prompt, approved):
    definition["tasks"][0].update(prompt=prompt, approved_placeholders=approved)
    with pytest.raises(PlanValidationError):
        validate_plan(definition)


def test_placeholders_are_literal_not_substituted(definition, no_execution):
    plan = validate_plan(definition)
    assert "{{BRANCH}}" in plan.to_dict()["tasks"][0]["prompt"]
    assert "{{LATEST_SHA}}" in plan.to_dict()["tasks"][2]["prompt"]


def test_unknown_fields_rejected_without_echoing_private_values(definition):
    definition["tasks"][0]["secret-key"] = "credential-secret"
    with pytest.raises(PlanValidationError) as error:
        validate_plan(definition)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("contents", [
    '{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}', "not json", "[[[]",
])
def test_invalid_json_or_duplicate_keys_rejected(tmp_path, contents):
    source = tmp_path / "bad.json"
    source.write_text(contents, encoding="utf-8")
    with pytest.raises(PlanValidationError):
        load_plan(source)


def test_batch_and_task_conflicts_leave_all_records_unchanged(tmp_path, definition):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        frozen = store.import_plan(definition, approved=True)
        original = store.list_tasks()
        for changed in [definition, definition | {"batch_id": "other"}]:
            with pytest.raises(PlanConflictError):
                store.import_plan(changed, approved=True)
        replacement = deepcopy(definition)
        replacement["tasks"][0]["prompt"] = "replacement"
        with pytest.raises(PlanConflictError):
            store.import_plan(replacement, approved=True)
        assert store.list_tasks() == original
        assert store.get_plan("other") is None
        assert store.get_plan(frozen.batch_id) == frozen


def test_invalid_raw_and_forged_plans_cannot_bypass_validation(tmp_path, definition):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        definition["tasks"][-1]["permitted_files"] = ["../outside"]
        for invalid in (definition, FrozenPlan(json.dumps(definition))):
            with pytest.raises(PlanValidationError):
                store.import_plan(invalid, approved=True)
        assert store.list_tasks() == []
        assert store.get_plan(definition["batch_id"]) is None


def test_mid_import_database_error_rolls_back_batch_tasks_and_memberships(tmp_path, definition):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        store._connection.execute("""CREATE TRIGGER simulate_failure BEFORE INSERT ON tasks
            WHEN NEW.task_id = 'EXAMPLE-003' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="simulated"):
            store.import_plan(definition, approved=True)
        for table in ("tasks", "task_plans", "plan_tasks"):
            assert store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("sql", [
    "UPDATE task_plans SET definition_json = '{}'",
    "UPDATE task_plans SET fingerprint = 'a'",
    "DELETE FROM task_plans", "DELETE FROM plan_tasks",
    "UPDATE plan_tasks SET task_order = 99",
    "UPDATE tasks SET prompt = 'replacement'",
    "UPDATE tasks SET model = 'different'",
    "UPDATE tasks SET task_order = 99",
    "UPDATE tasks SET acceptance_criteria = '[]'",
    "DELETE FROM tasks",
    "INSERT OR REPLACE INTO task_plans SELECT * FROM task_plans",
    "INSERT OR REPLACE INTO plan_tasks SELECT * FROM plan_tasks",
    "INSERT OR REPLACE INTO tasks SELECT * FROM tasks",
])
def test_frozen_definitions_protected_even_from_direct_sql(tmp_path, definition, sql):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        plan = store.import_plan(definition, approved=True)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with store._connection:
                store._connection.execute(sql)
        assert store.get_plan(plan.batch_id) == plan
        assert len(store.list_plan_tasks(plan.batch_id)) == 3


def test_status_and_commit_metadata_remain_mutable_but_definition_does_not(tmp_path, definition):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        plan = store.import_plan(definition, approved=True)
        task_id = definition["tasks"][0]["task_id"]
        store.update_status(task_id, TaskStatus.READY_TO_COMMIT)
        store.record_commit(task_id, "a" * 40, definition["expected_branch"], None)
        store.mark_push_attempted(task_id)
        store.complete_push(task_id, "a" * 40, definition["expected_branch"],
                            "https://github.com/example/project/commit/" + "a" * 40)
        assert store.get_commit(task_id).push_state == PushState.VERIFIED
        assert store.get_task(task_id).status == TaskStatus.AWAITING_REVIEW
        assert store.get_plan(plan.batch_id) == plan
        original = definition["tasks"][0]
        with pytest.raises(DuplicateTaskError):
            store.add_task(**{key: original[key] for key in (
                "task_id", "title", "prompt", "model", "task_order", "acceptance_criteria")})


def test_migration_preserves_existing_tasks_and_commits(tmp_path, definition):
    path = tmp_path / "legacy.sqlite3"
    with TaskStore(path) as store:
        task = store.add_task(task_id="LEGACY", title="Legacy", prompt="Saved", model="original",
                              task_order=9, acceptance_criteria=["Keep"])
        store.record_commit("LEGACY", "b" * 40, "feature/legacy", None)
        commit = store.get_commit("LEGACY")
    # Model a pre-TASK-009 database: remove ONLY the new empty schema.
    with sqlite3.connect(path) as connection:
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            connection.execute(f'DROP TRIGGER "{row[0]}"')
        connection.execute("DROP TABLE plan_tasks")
        connection.execute("DROP TABLE task_plans")
    with TaskStore(path) as store:
        assert store.get_task("LEGACY") == task
        assert store.get_commit("LEGACY") == commit
        store.import_plan(definition, approved=True)
        assert store.get_task("LEGACY") == task
        assert store.get_commit("LEGACY") == commit
        with pytest.raises(PlanConflictError):
            changed = deepcopy(definition)
            changed["batch_id"] = "conflict"
            changed["tasks"][2]["task_id"] = "LEGACY"
            store.import_plan(changed, approved=True)
        assert store.get_plan("conflict") is None


def test_import_requires_approval_at_storage_boundary(tmp_path, definition):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        with pytest.raises(ValueError, match="approval"):
            store.import_plan(definition)
        assert store.list_tasks() == []


def test_imported_task_cannot_run_through_legacy_controller(tmp_path, definition):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        plan = store.import_plan(definition, approved=True)
        identity = definition["tasks"][0]["task_id"]
        with pytest.raises(PlanExecutionUnavailableError):
            store.claim_task(identity)
        from auto_coding.integrations.git import GitRepository, RepositoryState
        repo = Mock(spec=GitRepository)
        repo.path = tmp_path.resolve()
        repo.validate_execution.return_value = RepositoryState(repo.path, "feature/001-foundation", "a" * 40, False, True)
        codex, tests = Mock(), Mock()
        controller = SingleTaskController(
            store, codex_runner=codex, test_runner=tests, repository_factory=lambda path: repo,
            lock_factory=lambda path: WorkspaceLock(path, lock_directory=tmp_path.parent / "locks"),
        )
        result = controller.execute(identity, tmp_path, [["python", "-m", "pytest"]],
                                    expected_branch="feature/001-foundation", allowed_files=["docs/example.md"])
        assert not result.success and "PlanExecutionUnavailableError" in result.error
        codex.assert_not_called()
        tests.assert_not_called()
        repo.commit_files.assert_not_called()
        repo.push_branch.assert_not_called()
        assert store.get_task(identity).status == TaskStatus.QUEUED
        assert store.get_plan(plan.batch_id) == plan


def test_cli_validate_read_only_and_import_without_external_execution(tmp_path, monkeypatch, capsys, no_execution):
    monkeypatch.chdir(tmp_path)
    assert main(["plan", "validate", str(EXAMPLE)]) == 0
    plan = load_plan(EXAMPLE)
    output = capsys.readouterr().out
    assert "3 tasks" in output and plan.fingerprint in output
    assert list(tmp_path.iterdir()) == []
    assert main(["plan", "import", str(EXAMPLE), "--db", "runtime/tasks.sqlite3"]) == 1
    assert list(tmp_path.iterdir()) == []
    assert main(["plan", "import", str(EXAMPLE), "--db", "runtime/tasks.sqlite3", "--approve"]) == 0
    output = capsys.readouterr().out
    assert plan.fingerprint in output and definition_prompt() not in output
    assert not (tmp_path / "logs").exists()
    with TaskStore(tmp_path / "runtime/tasks.sqlite3") as store:
        assert store.get_plan(plan.batch_id) == plan
    assert main(["plan", "import", str(EXAMPLE), "--db", "runtime/tasks.sqlite3", "--approve"]) == 1


def definition_prompt():
    return load_plan(EXAMPLE).to_dict()["tasks"][0]["prompt"]


def test_invalid_cli_input_creates_no_database_or_logs(tmp_path, monkeypatch, definition, capsys, no_execution):
    definition["tasks"][-1]["prompt"] = "credential-secret {{UNKNOWN}}"
    source = tmp_path / "bad.json"
    source.write_text(json.dumps(definition), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for command in (["plan", "validate", str(source)],
                    ["plan", "import", str(source), "--db", "new/tasks.sqlite3", "--approve"]):
        assert main(command) == 1
        output = capsys.readouterr()
        assert "credential-secret" not in output.out + output.err
    assert not (tmp_path / "new").exists()
    assert not (tmp_path / "logs").exists()


def test_cli_missing_file_and_database_errors_are_safe(tmp_path, monkeypatch, capsys):
    assert main(["plan", "validate", str(tmp_path / "missing.json")]) == 1
    monkeypatch.setattr("auto_coding.cli.TaskStore", Mock(side_effect=sqlite3.OperationalError("secret detail")))
    assert main(["plan", "import", str(EXAMPLE), "--db", str(tmp_path / "db"), "--approve"]) == 1
    assert "secret detail" not in capsys.readouterr().err


def test_concurrent_importers_cannot_duplicate_a_batch(tmp_path, definition):
    path = tmp_path / "tasks.sqlite3"
    with TaskStore(path):
        pass
    ready = Barrier(2)
    def importing():
        with TaskStore(path) as store:
            ready.wait(timeout=10)
            try:
                store.import_plan(definition, approved=True)
                return "imported"
            except PlanConflictError:
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: importing(), range(2)))
    assert sorted(results) == ["conflict", "imported"]
    with TaskStore(path) as store:
        assert len(store.list_tasks()) == 3
        assert store.get_plan(definition["batch_id"]) == validate_plan(definition)
        assert store._connection.execute("SELECT count(*) FROM task_plans").fetchone()[0] == 1


def test_independent_batches_have_ordered_membership_without_touching_legacy(tmp_path, definition):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        first = store.import_plan(definition, approved=True)
        second = deepcopy(definition)
        second["batch_id"] = "SECOND"
        for task in second["tasks"]:
            task["task_id"] = task["task_id"].replace("EXAMPLE", "SECOND")
            task["dependencies"] = [dep.replace("EXAMPLE", "SECOND") for dep in task["dependencies"]]
        later = store.import_plan(second, approved=True)
        assert store.get_plan(first.batch_id) == first
        assert store.get_plan(later.batch_id) == later
        assert [task.task_order for task in store.list_plan_tasks(later.batch_id)] == [1, 2, 3]
        assert len(store.list_tasks()) == 6


def test_invalid_and_validation_commands_leave_existing_database_byte_for_byte_unchanged(
    tmp_path, definition, monkeypatch, capsys,
):
    path = tmp_path / "tasks.sqlite3"
    with TaskStore(path) as store:
        store.import_plan(definition, approved=True)
    original = path.read_bytes()
    monkeypatch.chdir(tmp_path)
    assert main(["plan", "validate", str(EXAMPLE)]) == 0
    bad = tmp_path / "bad.json"
    definition["tasks"][2]["dependencies"] = ["MISSING"]
    bad.write_text(json.dumps(definition), encoding="utf-8")
    assert main(["plan", "import", str(bad), "--db", str(path), "--approve"]) == 1
    capsys.readouterr()
    assert path.read_bytes() == original
    assert not (tmp_path / "logs").exists()


def test_permitted_path_addition_to_frozen_batch_rejected(tmp_path, definition):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        store.import_plan(definition, approved=True)
        store.add_task(task_id="LEGACY", title="Legacy", prompt="Legacy", model="saved-model",
                       task_order=1, acceptance_criteria=["Keep"])
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with store._connection:
                store._connection.execute("INSERT INTO plan_tasks VALUES (?, ?, ?)",
                                          ("LEGACY", definition["batch_id"], 4))
        assert len(store.list_plan_tasks(definition["batch_id"])) == 3
        assert store.claim_task("LEGACY")


def test_read_back_detects_corruption_instead_of_returning_success(tmp_path, definition):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        plan = store.import_plan(definition, approved=True)
        # A database owner can bypass SQLite protection; a hash is not a signature.
        store._connection.execute("DROP TRIGGER frozen_task_plans_update")
        with store._connection:
            store._connection.execute("UPDATE task_plans SET fingerprint = ?", ("0" * 64,))
        with pytest.raises(ValueError, match="integrity"):
            store.get_plan(plan.batch_id)
