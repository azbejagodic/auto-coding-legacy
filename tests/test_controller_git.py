"""Controller integration: mocked runners and temporary local bare remotes only."""

from dataclasses import replace
import sqlite3
from unittest.mock import Mock

import pytest

from auto_coding.controller import SingleTaskController
from auto_coding.integrations.codex import CodexResult
from auto_coding.integrations.git import GitError
from auto_coding.storage import PushState, TaskStatus, TaskStore
from auto_coding.test_runner import CommandResult, TestRunResult as RunResult
from auto_coding.workspace_lock import WorkspaceBusyError, WorkspaceLock
from test_git import git, repository
from test_controller import setup, execute, BRANCH, SHA, URL


@pytest.fixture
def workflow(repository, tmp_path, monkeypatch):
    repo = repository
    git(repo.path, "switch", "-c", BRANCH)
    (repo.path / "deleted.txt").write_text("tracked deletion\n")
    git(repo.path, "add", "--", "deleted.txt")
    git(repo.path, "commit", "--no-gpg-sign", "-m", "Seed deletion test")
    # Only URL formatting is adapted: all Git commit/push/remote-SHA checks are
    # real operations against the temporary local origin, never github.com.
    monkeypatch.setattr(repo, "commit_url", lambda sha: URL + sha)
    push = repo.push_branch
    monkeypatch.setattr(repo, "push_branch", lambda *args: replace(push(*args), github_commit_url=URL + args[1]))
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        for task_id in ("TASK-A", "TASK-B"):
            store.add_task(task_id=task_id, title="Implement selected task", prompt="saved prompt",
                           model="exact-model", task_order=1, acceptance_criteria=[])

        def code(**kwargs):
            (repo.path / "initial.txt").write_text("modified\n")
            (repo.path / "added.txt").write_text("added\n")
            (repo.path / "deleted.txt").unlink()
            return CodexResult(True, "exact-model", 0, "done", "", "")

        codex = Mock(side_effect=code)
        tests = Mock(return_value=RunResult(True, (CommandResult(("tool",), 0, "", "", .1, True),), None, .1))
        factory = lambda workspace: WorkspaceLock(workspace, lock_directory=tmp_path / "locks")
        controller = SingleTaskController(store, codex_runner=codex, test_runner=tests,
                                          lock_factory=factory, repository_factory=lambda _: repo)
        yield repo, store, controller, codex, tests


def run(workflow, **options):
    repo, store, controller, codex, tests = workflow
    return controller.execute("TASK-A", repo.path, [["tool"]], expected_branch=BRANCH,
                              allowed_files=["initial.txt", "added.txt", "deleted.txt"], **options)


def test_full_workflow_mixed_files_sha_persistence_and_no_next_task(workflow):
    repo, store, controller, codex, tests = workflow
    base = repo.current_commit_sha()
    result = run(workflow)
    assert result.success and result.push_verified
    assert result.final_status == TaskStatus.AWAITING_REVIEW
    assert result.model == "exact-model" and result.branch == BRANCH
    assert result.commit_sha == repo.current_commit_sha() != base
    assert result.github_commit_url == URL + result.commit_sha
    assert git(repo.path, "log", "-1", "--format=%s") == "TASK-A: Implement selected task"
    assert git(repo.path, "rev-list", "--count", f"{base}..HEAD") == "1"
    changes = git(repo.path, "show", "--format=", "--name-status", "HEAD").splitlines()
    assert sorted(changes) == ["A\tadded.txt", "D\tdeleted.txt", "M\tinitial.txt"]
    assert git(repo.path, "ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}").split()[0] == result.commit_sha
    saved = store.get_commit("TASK-A")
    assert saved.commit_sha == result.commit_sha and saved.branch == BRANCH
    assert saved.github_commit_url == result.github_commit_url and saved.push_state == PushState.VERIFIED
    assert store.get_task("TASK-A").status == TaskStatus.AWAITING_REVIEW
    assert store.get_task("TASK-B").status == TaskStatus.QUEUED
    assert store.get_commit("TASK-B") is None
    codex.assert_called_once()
    tests.assert_called_once()
    assert not run(workflow).success
    assert repo.current_commit_sha() == result.commit_sha
    codex.assert_called_once()


@pytest.mark.parametrize("state", ["wrong", "main", "dirty", "staged", "origin", "operation"])
def test_preflight_rejects_unsafe_repository_without_claim(workflow, state):
    repo, store, controller, codex, tests = workflow
    if state in ("wrong", "main"):
        git(repo.path, "switch", "main")
    elif state in ("dirty", "staged"):
        (repo.path / "initial.txt").write_text("unexpected change")
        if state == "staged":
            git(repo.path, "add", "--", "initial.txt")
    elif state == "origin":
        git(repo.path, "remote", "remove", "origin")
    else:
        (repo.path / ".git" / "MERGE_HEAD").write_text(repo.current_commit_sha())
    before = store.get_task("TASK-A")
    result = run(workflow)
    assert not result.success and store.get_task("TASK-A") == before
    codex.assert_not_called()
    tests.assert_not_called()


@pytest.mark.parametrize("unexpected", ["unrelated.txt", ".env", "secrets/token.txt", "bad[1].txt"])
def test_unexpected_and_sensitive_changes_are_not_staged(workflow, unexpected):
    repo, store, controller, codex, tests = workflow
    original = codex.side_effect
    base = repo.current_commit_sha()

    def code(**kwargs):
        result = original(**kwargs)
        path = repo.path / unexpected
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must not commit")
        return result

    codex.side_effect = code
    result = run(workflow)
    assert not result.success and result.final_status == TaskStatus.FAILED
    assert repo.current_commit_sha() == base and store.get_commit("TASK-A") is None
    assert git(repo.path, "diff", "--cached", "--name-only") == ""
    assert (repo.path / unexpected).read_text() == "must not commit"


@pytest.mark.parametrize("mutation", ["none", "stage", "commit", "branch", "origin"])
def test_post_execution_git_drift_stops_without_controller_commit(workflow, mutation):
    repo, store, controller, codex, tests = workflow
    base = repo.current_commit_sha()

    def code(**kwargs):
        if mutation != "none":
            (repo.path / "initial.txt").write_text("modified")
        if mutation in ("stage", "commit"):
            git(repo.path, "add", "--", "initial.txt")
        if mutation == "commit":
            git(repo.path, "commit", "--no-gpg-sign", "-m", "Unexpected Codex commit")
        if mutation == "branch":
            git(repo.path, "switch", "-c", "unexpected")
        if mutation == "origin":
            git(repo.path, "remote", "set-url", "origin", "https://github.com/example/other.git")
        return CodexResult(True, "exact-model", 0, "done", "", "")

    codex.side_effect = code
    result = run(workflow)
    assert not result.success and not result.push_verified
    assert result.final_status == TaskStatus.FAILED
    if mutation == "commit":
        assert result.commit_sha == repo.current_commit_sha() != base
        assert store.get_commit("TASK-A").commit_sha == result.commit_sha
        assert result.state_uncertain
    else:
        assert repo.current_commit_sha() == base


def test_validation_repeated_after_lock_before_claim(workflow, monkeypatch):
    repo, store, controller, codex, tests = workflow
    original_factory = controller.lock_factory

    def factory(workspace):
        (repo.path / "initial.txt").write_text("changed between validation and lock")
        return original_factory(workspace)

    controller.lock_factory = factory
    before = store.get_task("TASK-A")
    result = run(workflow)
    assert not result.success and store.get_task("TASK-A") == before
    codex.assert_not_called()


def test_lock_held_through_git_and_metadata_persistence(workflow, monkeypatch):
    repo, store, controller, codex, tests = workflow

    def wrap(owner, name):
        original = getattr(owner, name)

        def check(*args, **kwargs):
            with pytest.raises(WorkspaceBusyError):
                with controller.lock_factory(repo.path):
                    pass
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, check)

    for name in ("changed_files", "commit_files", "push_branch"):
        wrap(repo, name)
    for name in ("claim_task", "record_commit", "mark_push_attempted", "complete_push"):
        wrap(store, name)
    assert run(workflow).success
    with controller.lock_factory(repo.path):
        pass


@pytest.fixture
def mocked_repo(setup):
    repo = setup[2].repository_factory(setup[5])
    setup[2].repository_factory = lambda _: repo
    return repo


@pytest.mark.parametrize("runner", [3, 4])
def test_failed_runner_prevents_git_commits(setup, mocked_repo, runner):
    setup[runner].return_value = replace(setup[runner].return_value, success=False)
    assert not execute(setup).success
    mocked_repo.commit_files.assert_not_called()
    mocked_repo.push_branch.assert_not_called()


@pytest.mark.parametrize("failure", ["commit", "push", "mismatch", "record", "intent", "complete"])
def test_partial_operations_preserve_commit_and_never_retry(setup, mocked_repo, monkeypatch, failure):
    repo, store = mocked_repo, setup[1]
    if failure == "commit":
        repo.commit_files.side_effect = GitError("commit", "staging failed")
    elif failure == "push":
        repo.push_branch.side_effect = GitError("push", "network failed")
    elif failure == "mismatch":
        repo.push_branch.return_value = replace(repo.push_branch.return_value, commit_sha="c" * 40)
    else:
        method = {"record": "record_commit", "intent": "mark_push_attempted", "complete": "complete_push"}[failure]
        monkeypatch.setattr(store, method, Mock(side_effect=sqlite3.OperationalError("private database text")))
    result = execute(setup)
    assert not result.success and result.final_status == TaskStatus.FAILED
    assert "private database text" not in result.error
    if failure == "commit":
        assert result.commit_sha is None
    else:
        assert result.commit_sha == SHA and result.state_uncertain
        saved = store.get_commit("TASK-A")
        if failure == "record":
            assert saved is None
        else:
            assert saved.commit_sha == SHA
            assert saved.push_state == (PushState.LOCAL if failure == "intent" else PushState.UNCERTAIN)
            if failure in ("push", "mismatch", "complete"):
                assert saved.github_commit_url == URL + SHA
    if failure in ("commit", "record", "intent"):
        repo.push_branch.assert_not_called()
    assert result.push_verified == (failure == "complete")
    assert not execute(setup).success
    repo.commit_files.assert_called_once()
    setup[3].assert_called_once()
    assert store.get_task("TASK-B").status == TaskStatus.QUEUED


def test_commit_error_after_head_advanced_is_retained(setup, mocked_repo):
    mocked_repo.commit_files.side_effect = GitError("commit", "post-commit verification failed")
    mocked_repo.current_commit_sha.return_value = SHA
    result = execute(setup)
    assert not result.success and result.commit_sha == SHA and result.state_uncertain
    assert setup[1].get_commit("TASK-A").commit_sha == SHA
    mocked_repo.push_branch.assert_not_called()


@pytest.mark.parametrize("allowed", [["../outside.txt"], [".env"], [":(all)"], ["*.py"], []])
def test_unsafe_approved_file_list_is_rejected_before_claim(workflow, allowed):
    repo, store, controller, codex, tests = workflow
    before = store.get_task("TASK-A")
    result = controller.execute("TASK-A", repo.path, [["tool"]], expected_branch=BRANCH, allowed_files=allowed)
    assert not result.success and store.get_task("TASK-A") == before
    codex.assert_not_called()


def test_missing_expected_branch_or_files_is_a_read_only_failure(workflow):
    repo, store, controller, codex, tests = workflow
    before = store.get_task("TASK-A")
    result = controller.execute("TASK-A", repo.path, [["tool"]])
    assert not result.success and "explicit" in result.error
    assert store.get_task("TASK-A") == before
    codex.assert_not_called()


def test_changed_repository_root_is_rejected(workflow):
    repo, store, controller, codex, tests = workflow

    def code(**kwargs):
        other = repo.path.parent / "outside"
        other.mkdir()
        git(repo.path, "config", "--local", "core.worktree", str(other))
        return CodexResult(True, "exact-model", 0, "done", "", "")

    codex.side_effect = code
    result = run(workflow)
    assert not result.success and "root changed" in result.error
    assert store.get_commit("TASK-A") is None


def test_late_baseline_drift_prevents_commit(workflow, monkeypatch):
    repo, store, controller, codex, tests = workflow
    original = repo.changed_files

    def discover(*args):
        files = original(*args)
        git(repo.path, "commit", "--no-gpg-sign", "--allow-empty", "-m", "Unexpected external commit")
        return files

    monkeypatch.setattr(repo, "changed_files", discover)
    result = run(workflow)
    assert not result.success and "tested task baseline" in result.error
    assert store.get_commit("TASK-A").commit_sha == repo.current_commit_sha()
    assert git(repo.path, "log", "-1", "--format=%s") == "Unexpected external commit"


def test_failure_to_persist_failed_status_retains_commit_and_reports_uncertainty(setup, mocked_repo, monkeypatch):
    mocked_repo.push_branch.side_effect = GitError("push", "network failure")
    original = setup[1].update_status

    def update(task_id, status):
        if status == TaskStatus.FAILED:
            raise sqlite3.OperationalError("db failed")
        original(task_id, status)

    monkeypatch.setattr(setup[1], "update_status", update)
    result = execute(setup)
    assert not result.success and result.state_uncertain and result.final_status is None
    assert result.commit_sha == SHA and setup[1].get_commit("TASK-A").commit_sha == SHA


def test_saved_commit_blocks_rerun_even_after_manual_requeue(setup, mocked_repo):
    assert execute(setup).success
    setup[1].update_status("TASK-A", TaskStatus.QUEUED)
    result = execute(setup)
    assert not result.success and "already produced a commit" in result.error
    assert result.commit_sha == SHA and result.push_verified
    mocked_repo.commit_files.assert_called_once()
    mocked_repo.push_branch.assert_called_once()
    setup[3].assert_called_once()


def test_absolute_approved_paths_and_ignored_test_artifacts(workflow):
    repo, store, controller, codex, tests = workflow
    (repo.path / ".gitignore").write_text("cache/\n")
    git(repo.path, "add", "--", ".gitignore")
    git(repo.path, "commit", "--no-gpg-sign", "-m", "Ignore test cache")

    def test(**kwargs):
        cache = repo.path / "cache"
        cache.mkdir()
        (cache / "artifact.txt").write_text("ignored runtime artifact")
        return tests.return_value

    tests.side_effect = test
    result = controller.execute("TASK-A", repo.path, [["tool"]], expected_branch=BRANCH,
                                 allowed_files=[repo.path / name for name in
                                                ("initial.txt", "added.txt", "deleted.txt")])
    assert result.success
    assert "cache/artifact.txt" not in git(repo.path, "ls-tree", "-r", "--name-only", "HEAD")
