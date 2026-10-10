from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
import logging
from pathlib import Path
import sqlite3
from threading import Barrier, Event
from unittest.mock import Mock

import pytest

from auto_coding.controller import SingleTaskController
from auto_coding.integrations.codex import CodexResult
from auto_coding.integrations.git import GitRepository, GitResult, RepositoryState
from auto_coding.storage import PushState, TaskStatus, TaskStore
from auto_coding.test_runner import CommandResult, TestRunResult as RunResult
from auto_coding.workspace_lock import WorkspaceBusyError, WorkspaceLock, WorkspaceLockError


BRANCH = "feature/task"
BASE, SHA = "a" * 40, "b" * 40
URL = "https://github.com/example/project/commit/"


def repository_factory(path):
    repo = Mock(spec=GitRepository)
    repo.path = Path(path).resolve()
    repo.validate_execution.return_value = RepositoryState(repo.path, BRANCH, BASE, False, True)
    repo.verify_origin.return_value = "https://github.com/example/project.git"
    repo.current_commit_sha.return_value = BASE
    repo.has_uncommitted_changes.return_value = False
    repo.active_branch.return_value = BRANCH
    repo.commit_url.side_effect = lambda sha: URL + sha
    repo.changed_files.return_value = ["file.txt"]
    repo.commit_files.return_value = GitResult(repo.path, BRANCH, SHA)
    repo.push_branch.return_value = GitResult(repo.path, BRANCH, SHA, URL + SHA)
    return repo


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    with TaskStore(path) as store:
        store.add_task(task_id="TASK-A", title="Selected task", prompt="Private saved prompt ž",
                       model="exact-model", task_order=1, acceptance_criteria=["Tests pass"])
        store.add_task(task_id="TASK-B", title="Next task", prompt="Never execute",
                       model="other-model", task_order=2, acceptance_criteria=[])
        codex = Mock(return_value=CodexResult(True, "exact-model", 0, "private answer", "private output", ""))
        command = CommandResult(("tool", "test"), 0, "private tests", "", 0.1, True)
        tests = Mock(return_value=RunResult(True, (command,), None, 0.1))
        factory = lambda workspace: WorkspaceLock(workspace, lock_directory=tmp_path.parent / "locks")
        controller = SingleTaskController(store, codex_runner=codex, test_runner=tests, lock_factory=factory,
                                          repository_factory=repository_factory)
        controller.execute = partial(controller.execute, expected_branch=BRANCH, allowed_files=["file.txt"])
        yield path, store, controller, codex, tests, tmp_path


def execute(setup, **kwargs):
    return setup[2].execute("TASK-A", setup[5], [["tool", "test"]], **kwargs)


def test_success_forwards_saved_prompt_model_directory_timeouts_and_persists(setup, caplog):
    path, store, controller, codex, tests, directory = setup

    def run(**kwargs):
        assert store.get_task("TASK-A").status == TaskStatus.RUNNING
        return CodexResult(True, "exact-model", 0, "private answer", "private output", "")

    codex.side_effect = run
    with caplog.at_level(logging.INFO):
        result = execute(setup, codex_timeout=12, test_timeout=8)
    assert result.success and result.final_status == TaskStatus.AWAITING_REVIEW
    assert result.model == "exact-model" and result.task_id == "TASK-A"
    assert result.error is None and not result.state_uncertain
    assert result.codex_result.final_response == "private answer"
    assert result.test_result == tests.return_value
    assert result.duration_seconds >= 0
    codex.assert_called_once_with(prompt="Private saved prompt ž", model="exact-model",
                                  working_directory=directory.resolve(), timeout=12)
    tests.assert_called_once_with(working_directory=directory.resolve(), commands=(("tool", "test"),), timeout=8)
    with TaskStore(path) as reopened:
        assert reopened.get_task("TASK-A").status == TaskStatus.AWAITING_REVIEW
        assert reopened.get_task("TASK-B").status == TaskStatus.QUEUED
    for secret in ("Private saved prompt", "private answer", "private output", "private tests"):
        assert secret not in caplog.text


def test_missing_task_does_not_execute(setup):
    result = setup[2].execute("missing", setup[5], [["tool"]])
    assert not result.success and result.final_status is None
    assert "does not exist" in result.error
    setup[3].assert_not_called()
    setup[4].assert_not_called()


@pytest.mark.parametrize("status", [status for status in TaskStatus if status != TaskStatus.QUEUED])
def test_only_queued_task_may_start(setup, status):
    setup[1].update_status("TASK-A", status)
    result = execute(setup)
    assert not result.success and result.final_status == status
    assert setup[1].get_task("TASK-A").status == status
    setup[3].assert_not_called()
    setup[4].assert_not_called()


@pytest.mark.parametrize("commit_visible_to_loser", [False, True])
def test_concurrent_controllers_cannot_both_execute(setup, commit_visible_to_loser):
    path = setup[0]
    barrier = Barrier(2)
    winner_finished = Event()
    codex, tests = setup[3:5]

    def worker(index):
        directory = setup[5] / str(index)
        directory.mkdir()
        with TaskStore(path) as store:
            repository = repository_factory(directory)
            updates = Mock(wraps=store.update_status)
            store.update_status = updates
            original = store.get_task
            first = True

            def retrieve(task_id):
                nonlocal first
                task = original(task_id)
                if first:
                    first = False
                    barrier.wait(timeout=5)  # Both observed queued before either claims.
                    if commit_visible_to_loser and index == 1:
                        assert winner_finished.wait(timeout=5)
                return task

            store.get_task = retrieve
            result = SingleTaskController(store, codex_runner=codex, test_runner=tests,
                                          lock_factory=setup[2].lock_factory,
                                          repository_factory=lambda _: repository).execute(
                "TASK-A", directory, [["tool", "test"]],
                expected_branch=BRANCH, allowed_files=["file.txt"],
            )
            if index == 0:
                winner_finished.set()
            return result, repository, updates

    with ThreadPoolExecutor(max_workers=2) as pool:
        executions = list(pool.map(worker, range(2)))
    assert sum(result.success for result, _, _ in executions) == 1
    winner, winning_repo, _ = next(item for item in executions if item[0].success)
    loser, losing_repo, losing_updates = next(item for item in executions if not item[0].success)
    claimed_error = "Task is no longer queued or another execution already claimed it"
    committed_error = "Task already produced a commit; manual recovery/review is required"
    # get_task and get_commit are separate reads: the winner can commit after
    # both queued snapshots, but before the loser's commit lookup or claim.
    assert loser.error in {claimed_error, committed_error}, loser
    assert not loser.state_uncertain and not loser.workspace_busy
    assert loser.codex_result is None and loser.test_result is None
    if loser.error == committed_error:
        assert loser.final_status == TaskStatus.QUEUED  # Its earlier read, not persisted state.
        assert loser.commit_sha == winner.commit_sha == SHA
        assert loser.branch == BRANCH
    else:
        assert loser.final_status in {TaskStatus.RUNNING, TaskStatus.READY_TO_COMMIT, TaskStatus.AWAITING_REVIEW}
        assert loser.commit_sha is None and not loser.push_verified
    if commit_visible_to_loser:
        assert loser.error == committed_error and loser.push_verified
    codex.assert_called_once()
    tests.assert_called_once()
    winning_repo.commit_files.assert_called_once()
    winning_repo.push_branch.assert_called_once()
    losing_repo.commit_files.assert_not_called()
    losing_repo.push_branch.assert_not_called()
    losing_updates.assert_not_called()
    assert winner.error is None and not winner.state_uncertain and winner.push_verified
    assert setup[1].get_task("TASK-A").status == TaskStatus.AWAITING_REVIEW
    saved_commit = setup[1].get_commit("TASK-A")
    assert (saved_commit.commit_sha, saved_commit.branch, saved_commit.github_commit_url, saved_commit.push_state) == (
        SHA, BRANCH, URL + SHA, PushState.VERIFIED)


@pytest.mark.parametrize("timed_out", [False, True])
def test_codex_failure_or_timeout_stops_tests_and_does_not_retry(setup, timed_out):
    setup[3].return_value = replace(setup[3].return_value, success=False, exit_code=None if timed_out else 1,
                                   error="Codex unavailable or timed out", timed_out=timed_out)
    result = execute(setup)
    assert not result.success and result.final_status == TaskStatus.FAILED
    assert result.codex_result.timed_out == timed_out
    assert result.test_result is None
    assert setup[1].get_task("TASK-A").status == TaskStatus.FAILED
    setup[3].assert_called_once()
    setup[4].assert_not_called()
    execute(setup)  # No automatic rerun of failed tasks.
    setup[3].assert_called_once()


@pytest.mark.parametrize("timed_out", [False, True])
def test_test_failure_or_timeout_persists_failed(setup, timed_out):
    command = replace(setup[4].return_value.commands[0], success=False, exit_code=1,
                      timed_out=timed_out, error="required test failed")
    setup[4].return_value = RunResult(False, (command,), command, 0.1, "required tests failed")
    result = execute(setup)
    assert not result.success and result.final_status == TaskStatus.FAILED
    assert result.test_result.failed_command.timed_out == timed_out
    assert setup[1].get_task("TASK-A").status == TaskStatus.FAILED
    setup[3].assert_called_once()
    setup[4].assert_called_once()


@pytest.mark.parametrize("commands", [[], "tool test", [[]], [["tool"], ["bad\x00arg"]]])
def test_invalid_test_configuration_rejected_before_codex(setup, commands):
    result = setup[2].execute("TASK-A", setup[5], commands)
    assert not result.success and result.final_status == TaskStatus.FAILED
    assert "Invalid test configuration" in result.error
    setup[3].assert_not_called()
    setup[4].assert_not_called()


def test_empty_test_result_cannot_pass(setup):
    setup[4].return_value = RunResult(True, (), None, 0.1)
    assert execute(setup).final_status == TaskStatus.FAILED


def test_incomplete_test_plan_cannot_pass(setup):
    result = setup[2].execute("TASK-A", setup[5], [["tool", "test"], ["other", "test"]])
    assert not result.success and result.final_status == TaskStatus.FAILED


def test_inconsistent_test_success_with_failed_command_cannot_pass(setup):
    tests = setup[4].return_value
    setup[4].return_value = replace(tests, commands=(replace(tests.commands[0], exit_code=1),))
    assert execute(setup).final_status == TaskStatus.FAILED


def test_database_retrieval_and_claim_errors_stop_execution(setup, monkeypatch):
    original = setup[1].get_task
    monkeypatch.setattr(setup[1], "get_task", Mock(side_effect=sqlite3.OperationalError("private db data")))
    result = execute(setup)
    assert not result.success and result.state_uncertain
    monkeypatch.setattr(setup[1], "get_task", original)
    monkeypatch.setattr(setup[1], "claim_task", Mock(side_effect=sqlite3.OperationalError("private db data")))
    result = execute(setup)
    assert not result.success and result.state_uncertain and result.final_status is None
    assert "private db data" not in result.error
    setup[3].assert_not_called()
    setup[4].assert_not_called()


def test_failure_persistence_error_reports_uncertain_state(setup, monkeypatch):
    setup[3].return_value = replace(setup[3].return_value, success=False, error="execution failure")
    monkeypatch.setattr(setup[1], "update_status", Mock(side_effect=sqlite3.OperationalError("db locked")))
    result = execute(setup)
    assert not result.success and result.state_uncertain
    assert result.final_status is None
    assert "state is uncertain" in result.error
    assert setup[1].get_task("TASK-A").status == TaskStatus.RUNNING


def test_ready_status_persistence_error_attempts_failed(setup, monkeypatch):
    original = setup[1].update_status

    def update(task_id, status):
        if status == TaskStatus.READY_TO_COMMIT:
            raise sqlite3.OperationalError("db error")
        original(task_id, status)

    monkeypatch.setattr(setup[1], "update_status", update)
    result = execute(setup)
    assert not result.success and not result.state_uncertain
    assert result.final_status == TaskStatus.FAILED
    assert "Could not persist ready_to_commit" in result.error


@pytest.mark.parametrize("runner_index", [3, 4])
def test_unexpected_runner_exception_records_failure_without_private_logs(setup, runner_index, caplog):
    setup[runner_index].side_effect = RuntimeError("private prompt token")
    result = execute(setup)
    assert not result.success and result.final_status == TaskStatus.FAILED
    assert "RuntimeError" in result.error
    assert "private prompt token" not in result.error + caplog.text


def test_interrupted_task_stays_running_and_cannot_restart(setup):
    setup[3].side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        execute(setup)
    with TaskStore(setup[0]) as reopened:
        assert reopened.get_task("TASK-A").status == TaskStatus.RUNNING
        result = SingleTaskController(reopened, codex_runner=setup[3], test_runner=setup[4],
                                      lock_factory=setup[2].lock_factory,
                                      repository_factory=repository_factory).execute(
            "TASK-A", setup[5], [["tool"]],
            expected_branch=BRANCH, allowed_files=["file.txt"],
        )
        assert not result.success and result.final_status == TaskStatus.RUNNING
    setup[3].assert_called_once()
    setup[4].assert_not_called()


def test_busy_workspace_leaves_both_tasks_unchanged(setup, monkeypatch):
    store, controller = setup[1:3]
    before = store.list_tasks()
    claim = Mock(wraps=store.claim_task)
    update = Mock(wraps=store.update_status)
    monkeypatch.setattr(store, "claim_task", claim)
    monkeypatch.setattr(store, "update_status", update)
    with controller.lock_factory(setup[5]):
        for task_id in ("TASK-A", "TASK-B"):
            result = controller.execute(task_id, setup[5], [["tool", "test"]])
            assert result.workspace_busy and not result.success
            assert "busy" in result.error and not result.state_uncertain
    assert store.list_tasks() == before
    claim.assert_not_called()
    update.assert_not_called()
    setup[3].assert_not_called()
    setup[4].assert_not_called()


def test_lock_covers_claim_runners_and_final_persistence(setup, monkeypatch):
    store, controller, codex, tests = setup[1:5]

    def assert_locked():
        with pytest.raises(WorkspaceBusyError):
            with controller.lock_factory(setup[5]):
                pass

    original_claim, original_update = store.claim_task, store.update_status

    def claim(*args):
        assert_locked()
        return original_claim(*args)

    def update(*args):
        assert_locked()
        return original_update(*args)

    monkeypatch.setattr(store, "claim_task", claim)
    monkeypatch.setattr(store, "update_status", update)
    codex.side_effect = lambda **kwargs: (assert_locked(), codex.return_value)[1]
    tests.side_effect = lambda **kwargs: (assert_locked(), tests.return_value)[1]
    assert execute(setup).success
    with controller.lock_factory(setup[5]):
        pass


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_lock_released_after_runner_failure_or_interruption(setup, failure):
    setup[3].side_effect = failure
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            execute(setup)
    else:
        assert not execute(setup).success
    with setup[2].lock_factory(setup[5]):
        pass


def test_invalid_workspace_does_not_claim_task(setup):
    before = setup[1].get_task("TASK-A")
    result = setup[2].execute("TASK-A", setup[5] / "missing", [["tool"]])
    assert not result.success and not result.workspace_busy
    assert setup[1].get_task("TASK-A") == before
    setup[3].assert_not_called()
    setup[4].assert_not_called()


def test_release_failure_reports_error_without_losing_persisted_status(setup, monkeypatch):
    lock = setup[2].lock_factory(setup[5])
    release = lock.release

    def fail_release():
        release()
        raise WorkspaceLockError("Workspace lock unlock failed")

    monkeypatch.setattr(lock, "release", fail_release)
    setup[2].lock_factory = lambda workspace: lock
    result = execute(setup)
    assert not result.success and "unlock failed" in result.error
    assert result.final_status == TaskStatus.AWAITING_REVIEW
    assert setup[1].get_task("TASK-A").status == TaskStatus.AWAITING_REVIEW


@pytest.mark.parametrize("error", ["Windows containment setup failed", "Windows cleanup could not be confirmed",
                                   "Could not launch Codex CLI", "Codex execution timed out"])
def test_process_safety_errors_stop_tests_persist_failure_and_release_lock(setup, error):
    setup[3].return_value = replace(setup[3].return_value, success=False, error=error,
                                   timed_out="timed out" in error)
    result = execute(setup)
    assert not result.success and result.final_status == TaskStatus.FAILED
    assert error in result.error
    setup[3].assert_called_once()
    setup[4].assert_not_called()
    with setup[2].lock_factory(setup[5]):
        pass
