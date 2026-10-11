"""Authoritative local Git evidence and additive storage compatibility tests."""

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from auto_coding.integrations.git import GitError, GitRepository
from auto_coding.integrations.groq import GroqReviewer
from auto_coding.plans import PlanExecutionUnavailableError, load_plan
from auto_coding.review_evidence import build_review_packet
from auto_coding.reviews import ReviewConfig, ReviewError, build_chunks
from auto_coding.storage import TaskStore
from auto_coding.test_runner import CommandResult, TestRunResult as RunResult

from test_git import git
from test_groq_reviewer import client_for, raw_reply


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "feature/test")
    git(root, "config", "--local", "user.name", "Local Test")
    git(root, "config", "--local", "user.email", "test@example.invalid")
    git(root, "config", "--local", "core.autocrlf", "false")
    for name, content in (("modify.txt", "before\n"), ("delete.txt", "remove\n"),
                          ("old.txt", "rename\n"), ("empty.txt", "")):
        (root / name).write_text(content, encoding="utf-8")
    git(root, "add", "--", "modify.txt", "delete.txt", "old.txt", "empty.txt")
    git(root, "commit", "--no-gpg-sign", "-m", "Initial")
    return GitRepository(root)


def commit_changes(repository):
    root = repository.path
    (root / "modify.txt").write_text("after\n", encoding="utf-8")
    (root / "added file.txt").write_text("added\n", encoding="utf-8")
    (root / "delete.txt").unlink()
    (root / "empty.txt").unlink()
    (root / "old.txt").rename(root / "renamed.txt")
    git(root, "add", "--", "modify.txt", "added file.txt", "delete.txt", "empty.txt", "old.txt", "renamed.txt")
    git(root, "commit", "--no-gpg-sign", "-m", "Mixed changes")
    return repository.current_commit_sha()


def passing_tests(arguments=("python", "-m", "pytest")):
    command = CommandResult(arguments, 0, "1 passed", "", 0.1, True)
    return RunResult(True, (command,), None, 0.1)


def add_task(store, sha, *, task_id="TASK-REVIEW", branch="feature/test"):
    store.add_task(task_id=task_id, title="Review", prompt="Implement approved changes",
                   model="codex-test", task_order=1, acceptance_criteria=["Correct changes"],
                   status="awaiting_review")
    store.record_commit(task_id, sha, branch, None)


def test_actual_git_add_modify_delete_rename_complete_and_readonly(repository, tmp_path):
    sha = commit_changes(repository)
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        add_task(store, sha)
        before = git(repository.path, "status", "--porcelain=v1"), git(repository.path, "rev-parse", "HEAD")
        packet = build_review_packet(store, "TASK-REVIEW", repository.path, sha,
                                     passing_tests(), tests_commit_sha=sha, safety_passed=True)
        kinds = {c.path: c.change_type for c in packet.changes}
        assert kinds == {"modify.txt": "M", "added file.txt": "A", "delete.txt": "D",
                         "empty.txt": "D", "renamed.txt": "R"}
        assert next(c for c in packet.changes if c.change_type == "R").old_path == "old.txt"
        chunks = build_chunks(packet, ReviewConfig(request_budget=15000))
        sections = [s for c in chunks for s in json.loads(c.content)["sections"]]
        assert {s["path"] for s in sections} == set(kinds)
        assert "-remove" in next(s for s in sections if s["path"] == "delete.txt")["body"]
        assert "+added" in next(s for s in sections if s["path"] == "added file.txt")["body"]
        after = git(repository.path, "status", "--porcelain=v1"), git(repository.path, "rev-parse", "HEAD")
        assert before == after


def test_initial_commit_changes_supported(repository):
    changes = repository.review_changes("feature/test", repository.current_commit_sha())
    assert len(changes) == 4 and all(change.change_type == "A" for change in changes)


@pytest.mark.parametrize("case", ["short_sha", "wrong_sha", "main", "wrong_branch", "dirty", "detached"])
def test_invalid_repository_context(repository, case):
    sha, branch = repository.current_commit_sha(), "feature/test"
    if case == "short_sha":
        sha = sha[:12]
    elif case == "wrong_sha":
        sha = "a" * 40
    elif case == "main":
        git(repository.path, "branch", "-m", "main")
        branch = "main"
    elif case == "wrong_branch":
        branch = "feature/other"
    elif case == "dirty":
        (repository.path / "modify.txt").write_text("dirty", encoding="utf-8")
    elif case == "detached":
        git(repository.path, "switch", "--detach", sha)
    with pytest.raises(GitError):
        repository.review_changes(branch, sha)


@pytest.mark.parametrize("name,content", [
    (".env", "plain"), ("config.toml", "plain"), ("secrets/key.txt", "plain"),
])
def test_sensitive_changed_file_never_collected(repository, name, content):
    path = repository.path / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repository.path, "add", "--", name)
    git(repository.path, "commit", "--no-gpg-sign", "-m", "Sensitive path")
    with pytest.raises(ValueError):
        repository.review_changes("feature/test", repository.current_commit_sha())


def test_binary_commit_is_not_approved(repository, tmp_path):
    (repository.path / "image.bin").write_bytes(b"\x00\x01\x02")
    git(repository.path, "add", "--", "image.bin")
    git(repository.path, "commit", "--no-gpg-sign", "-m", "Binary")
    sha = repository.current_commit_sha()
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        add_task(store, sha)
        with pytest.raises(ReviewError, match="Unsupported"):
            build_review_packet(store, "TASK-REVIEW", repository.path, sha, passing_tests(),
                                tests_commit_sha=sha, safety_passed=True)


def test_limits_and_merge_fail_closed(repository):
    sha = repository.current_commit_sha()
    with pytest.raises(GitError, match="excessive"):
        repository.review_changes("feature/test", sha, max_files=1)
    with pytest.raises(GitError, match="limits"):
        repository.review_changes("feature/test", sha, max_patch_bytes=1)
    git(repository.path, "switch", "-c", "feature/side")
    (repository.path / "side.txt").write_text("side", encoding="utf-8")
    git(repository.path, "add", "--", "side.txt")
    git(repository.path, "commit", "--no-gpg-sign", "-m", "Side")
    git(repository.path, "switch", "feature/test")
    git(repository.path, "merge", "--no-ff", "--no-gpg-sign", "-m", "Merge", "feature/side")
    with pytest.raises(GitError, match="merge"):
        repository.review_changes("feature/test", repository.current_commit_sha())


@pytest.mark.parametrize("case", ["missing_task", "no_commit", "wrong_sha", "wrong_state",
                                  "missing_tests", "stale_tests", "failed_tests", "unsafe"])
def test_incomplete_evidence(repository, tmp_path, case):
    sha = commit_changes(repository)
    tests = passing_tests()
    tests_sha, safety = sha, True
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        if case != "missing_task":
            add_task(store, sha)
        if case == "no_commit":
            store._connection.execute("DELETE FROM task_commits")
            store._connection.commit()
        if case == "wrong_sha":
            sha = "f" * 40
        if case == "wrong_state":
            store.update_status("TASK-REVIEW", "failed")
        if case == "missing_tests":
            tests = None
        if case == "stale_tests":
            tests_sha = "a" * 40
        if case == "failed_tests":
            tests = replace(tests, success=False)
        if case == "unsafe":
            safety = False
        with pytest.raises(ReviewError):
            build_review_packet(store, "TASK-REVIEW", repository.path, sha, tests,
                                tests_commit_sha=tests_sha, safety_passed=safety)


def test_frozen_plan_packet_and_review_preserve_approval(repository, tmp_path):
    definition = load_plan(Path(__file__).parents[1] / "examples/task-plan.json").to_dict()
    definition["expected_branch"] = "feature/test"
    task = definition["tasks"][0]
    task["permitted_files"] = ["modify.txt"]
    (repository.path / "modify.txt").write_text("after\n", encoding="utf-8")
    git(repository.path, "add", "--", "modify.txt")
    git(repository.path, "commit", "--no-gpg-sign", "-m", "Task change")
    sha = repository.current_commit_sha()
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        plan = store.import_plan(definition, approved=True)
        task_id = task["task_id"]
        store.record_commit(task_id, sha, "feature/test", None)
        store.update_status(task_id, "awaiting_review")
        tests = passing_tests(tuple(task["test_commands"][0]))
        packet = build_review_packet(store, task_id, repository.path, sha, tests,
                                     tests_commit_sha=sha, safety_passed=True)
        assert packet.plan_fingerprint == plan.fingerprint
        assert packet.requirements == task["prompt"]
        result = GroqReviewer(store, ReviewConfig(request_budget=12000),
                              client=client_for(raw_reply(packet))).review(packet)
        assert result.decision == "APPROVED"
        assert store.get_plan(plan.batch_id) == plan
        assert store.get_task(task_id).status == "awaiting_review"
        assert len(store.list_tasks()) == 3
        with pytest.raises(PlanExecutionUnavailableError):
            store.claim_task(task_id)
        with pytest.raises(ReviewError, match="approved commands"):
            build_review_packet(store, task_id, repository.path, sha, passing_tests(),
                                tests_commit_sha=sha, safety_passed=True)


def test_frozen_scope_enforced(repository, tmp_path):
    definition = load_plan(Path(__file__).parents[1] / "examples/task-plan.json").to_dict()
    definition["expected_branch"] = "feature/test"
    task = definition["tasks"][0]
    task["permitted_files"] = ["not-changed.txt"]
    sha = repository.current_commit_sha()
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        store.import_plan(definition, approved=True)
        store.record_commit(task["task_id"], sha, "feature/test", None)
        store.update_status(task["task_id"], "awaiting_review")
        with pytest.raises(ReviewError, match="permitted"):
            build_review_packet(store, task["task_id"], repository.path, sha,
                                passing_tests(tuple(task["test_commands"][0])),
                                tests_commit_sha=sha, safety_passed=True)


def test_review_task_missing_evidence_records_uncertainty_no_api(repository, tmp_path):
    sha = repository.current_commit_sha()
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        add_task(store, sha)
        client = Mock()
        result = GroqReviewer(store, client=client).review_task(
            "TASK-REVIEW", repository.path, sha, None,
            tests_commit_sha=None, safety_passed=False,
        )
        assert result.decision == "UNCERTAIN" and not result.coverage_complete
        client.with_options.assert_not_called()
        assert store.reviews.get(result.review_id)["missing_evidence"]


def test_additive_migration_preserves_task_commit_and_plan(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    with TaskStore(path) as store:
        plan = store.import_plan(load_plan(
            Path(__file__).parents[1] / "examples/task-plan.json"), approved=True)
        task_id = store.list_tasks()[0].task_id
        store.record_commit(task_id, "a" * 40, "feature/test", None)
        before = (store.list_tasks(), store.get_commit(task_id), store.get_plan(plan.batch_id))
        store._connection.execute("DROP TABLE review_requests")
        store._connection.execute("DROP TABLE reviews")
        store._connection.commit()
    with TaskStore(path) as store:
        assert (store.list_tasks(), store.get_commit(task_id), store.get_plan(plan.batch_id)) == before
        assert store.reviews.list() == []


def test_git_patch_preserves_line_ending_changes(repository):
    root = repository.path
    git(root, "config", "--local", "core.autocrlf", "false")
    (root / "modify.txt").write_bytes(b"same\r\n")
    git(root, "add", "--", "modify.txt")
    git(root, "commit", "--no-gpg-sign", "-m", "CRLF")
    (root / "modify.txt").write_bytes(b"same\n")
    git(root, "add", "--", "modify.txt")
    git(root, "commit", "--no-gpg-sign", "-m", "LF")
    assert git(root, "status", "--porcelain=v1") == ""
    changes = repository.review_changes("feature/test", repository.current_commit_sha())
    assert len(changes) == 1
    assert "-same\r\n" in changes[0].patch
    assert "+same\n" in changes[0].patch


def test_git_replacement_objects_cannot_fake_clean_evidence(repository):
    root = repository.path
    original = repository.current_commit_sha()
    git(root, "switch", "-c", "feature/replacement")
    (root / "modify.txt").write_text("replacement\n", encoding="utf-8")
    git(root, "add", "--", "modify.txt")
    git(root, "commit", "--no-gpg-sign", "-m", "Replacement")
    replacement = repository.current_commit_sha()
    git(root, "switch", "feature/test")
    git(root, "replace", original, replacement)
    (root / "modify.txt").write_text("replacement\n", encoding="utf-8")
    git(root, "add", "--", "modify.txt")
    assert git(root, "status", "--porcelain=v1") == ""
    with pytest.raises(GitError, match="clean"):
        repository.review_changes("feature/test", original)
