"""Offline recovery: real temporary Git, mocked code/test/provider execution."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
from threading import Barrier, Event
from unittest.mock import Mock

import pytest

from auto_coding.batch_controller import BatchController
from auto_coding.cli import main
from auto_coding.controller import SingleTaskController
from auto_coding.integrations.groq import GroqReviewer
from auto_coding.reviews import ReviewConfig
from auto_coding.storage import TaskStore, TaskStatus

from test_batch_controller import BRANCH, offline, workflow, run
from test_git import git, repository
from test_groq_reviewer import raw_reply


@pytest.fixture
def stopped(workflow, monkeypatch):
    w = workflow
    w.batch.review_config = ReviewConfig(request_budget=2200)
    assert not run(w, max_tasks=1).success
    original = w.store.reviews.list("TASK-1")[0]
    assert original["api_requests"] == 0
    # Reproduce the legacy pre-splitting failure, preserving a realistic record.
    original["error"] = "A diff hunk or essential evidence exceeds the safe request budget"
    w.store.reviews.finish(original["review_id"], original)
    w.original = w.store.reviews.get(original["review_id"])
    w.execution = w.store.executions.get("TASK-1")
    w.sha = w.repo.current_commit_sha()
    w.batch.review_config = ReviewConfig(request_budget=12000)
    # All recovery paths must be unable to invoke any implementation mutation.
    w.codex.reset_mock()
    w.tests.reset_mock()
    monkeypatch.setattr(w.repo, "commit_files", Mock(side_effect=AssertionError("Recovery commit forbidden")))
    monkeypatch.setattr(w.repo, "push_branch", Mock(side_effect=AssertionError("Recovery push forbidden")))
    w.codex.side_effect = AssertionError("Recovery Codex forbidden")
    w.tests.side_effect = AssertionError("Recovery tests forbidden")
    yield w
    w.codex.assert_not_called()
    w.tests.assert_not_called()
    w.repo.commit_files.assert_not_called()
    w.repo.push_branch.assert_not_called()


def retry(w, **options):
    return w.batch.review_retry("BATCH", w.repo.path, task_id="TASK-1", **options)


def test_success_preserves_history_identity_and_private_usage(stopped):
    w = stopped
    before = w.database.read_bytes()
    dry = retry(w)
    assert dry.success and not dry.executed and dry.commit_sha == w.sha
    assert w.database.read_bytes() == before
    assert w.store.executions.get("TASK-1") == w.execution
    assert w.store.executions.recovery("TASK-1") is None
    w.client.chat.completions.with_raw_response.create.assert_not_called()
    result = retry(w, execute=True)
    assert result.success and result.executed and result.decision == "APPROVED", result
    event = w.store.executions.recovery("TASK-1")
    assert event["original_execution"] == w.execution
    assert event["original_review"] == w.original
    assert event["stage"] == "finished" and event["decision"] == "APPROVED"
    assert w.store.reviews.get(w.original["review_id"]) == w.original
    review = w.store.reviews.get(result.review_id)
    assert review["original_review_id"] == w.original["review_id"]
    assert review["recovery_timestamp"] == event["started_at"]
    assert review["recovery_id"] == result.recovery_id
    assert review["commit_sha"] == result.commit_sha == event["commit_sha"] == w.sha
    assert review["model"] == "openai/gpt-oss-120b" and review["reasoning_effort"] == "medium"
    assert review["usage"]["total_tokens"] == 125 and review["coverage_complete"]
    assert w.store.reviews.usage()["api_requests"] == 1
    execution = w.store.executions.get("TASK-1")
    assert execution["checkpoints"][:len(w.execution["checkpoints"])] == w.execution["checkpoints"]
    assert execution["stage"] == "finalized" and execution["review_id"] == result.review_id
    assert not execution["recovery_required"]
    assert w.store.get_task("TASK-1").status == TaskStatus.APPROVED
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    assert w.repo.current_commit_sha() == w.sha
    assert git(w.repo.path, "ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}").split()[0] == w.sha
    w.client.chat.completions.with_raw_response.create.assert_called_once()
    assert not retry(w, execute=True).success  # No duplicate finalization/review.
    assert len(w.store.reviews.list("TASK-1")) == 2
    with TaskStore(w.database, read_only=True) as reopened:
        assert reopened.executions.recovery("TASK-1") == event
        assert reopened.get_task("TASK-1").status == TaskStatus.APPROVED


@pytest.mark.parametrize("kind", ["branch", "dirty", "remote", "origin", "head", "parent"])
def test_git_identity_failures_never_review_or_write(stopped, kind):
    w = stopped
    if kind == "branch":
        git(w.repo.path, "switch", "-c", "feature/wrong")
    elif kind == "dirty":
        (w.repo.path / "initial.txt").write_text("unexpected")
    elif kind == "origin":
        git(w.repo.path, "remote", "set-url", "origin", str(w.repo.path / "missing.git"))
    elif kind == "remote":
        origin = git(w.repo.path, "remote", "get-url", "origin")
        git(origin, "update-ref", f"refs/heads/{BRANCH}", w.execution["baseline_sha"])
        # Only a disposable local bare remote is mutated by this adversarial fixture.
    elif kind == "head":
        git(w.repo.path, "commit", "--allow-empty", "--no-gpg-sign", "-m", "unapproved later commit")
    else:
        w.store.executions.checkpoint("TASK-1", "stopped", {"baseline_sha": "b" * 40})
    before = w.database.read_bytes()
    result = retry(w, execute=True)
    assert not result.success and not result.executed
    assert w.database.read_bytes() == before
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    w.client.chat.completions.with_raw_response.create.assert_not_called()


@pytest.mark.parametrize("kind", [
    "batch", "task", "fingerprint", "prompt", "test_missing", "test_failed", "test_command",
    "test_cleanup", "test_boolean_exit", "codex_failed", "checkpoint", "commit_missing",
    "sha", "unpushed", "rejected", "finalized", "legitimate_uncertain", "review_rejected",
    "unknown_failure", "request_attempted", "extra_review", "unresolved_concern",
])
def test_ambiguous_or_ineligible_evidence_fails_closed(stopped, kind):
    w = stopped
    batch, task = "BATCH", "TASK-1"
    connection = w.store._connection
    if kind == "batch":
        batch = "OTHER"
    elif kind == "task":
        task = "TASK-2"
    elif kind == "fingerprint":
        connection.execute("DROP TRIGGER frozen_task_plans_update")
        connection.execute("UPDATE task_plans SET fingerprint = ?", ("0" * 64,))
        connection.commit()
    elif kind == "prompt":
        connection.execute("DROP TRIGGER frozen_task_definition")
        connection.execute("UPDATE tasks SET prompt = 'changed' WHERE task_id = 'TASK-1'")
        connection.commit()
    elif kind.startswith("test_"):
        tests = deepcopy(w.execution["tests"])
        if kind == "test_missing":
            tests = None
        elif kind == "test_failed":
            tests["success"] = False
        elif kind == "test_command":
            tests["commands"][0]["arguments"] = ["unapproved"]
        elif kind == "test_cleanup":
            tests["commands"][0]["cleanup_error"] = "uncertain cleanup"
        else:
            tests["commands"][0]["exit_code"] = False
        # Update the JSON directly to isolate evidence validation from chronology.
        saved = {**w.execution, "tests": tests}
        connection.execute("UPDATE task_executions SET record_json = ? WHERE task_id = 'TASK-1'",
                           (json.dumps(saved),))
        connection.commit()
    elif kind in ("codex_failed", "checkpoint", "finalized"):
        saved = deepcopy(w.execution)
        if kind == "codex_failed":
            saved["codex"]["success"] = False
        elif kind == "checkpoint":
            saved["checkpoints"].pop(4)
        else:
            saved["stage"] = "finalized"
        connection.execute("UPDATE task_executions SET record_json = ? WHERE task_id = 'TASK-1'",
                           (json.dumps(saved),))
        connection.commit()
    elif kind in ("commit_missing", "sha", "unpushed"):
        if kind == "commit_missing":
            connection.execute("DELETE FROM task_commits WHERE task_id = 'TASK-1'")
        else:
            field, value = ("commit_sha", "a" * 40) if kind == "sha" else ("push_state", "push_uncertain")
            connection.execute(f"UPDATE task_commits SET {field} = ? WHERE task_id = 'TASK-1'", (value,))
        connection.commit()
    elif kind == "rejected":
        w.store.update_status("TASK-1", TaskStatus.REJECTED)
    elif kind == "extra_review":
        w.store.reviews.create("TASK-1", w.sha, {"decision": "UNCERTAIN"})
    else:
        previous = dict(w.original)
        if kind == "legitimate_uncertain":
            previous.update(error=None, needs_final_review=["Insufficient implementation context"])
        elif kind == "review_rejected":
            previous.update(decision="REJECTED", blocking_issues=["Bug"])
        elif kind == "unknown_failure":
            previous["error"] = "Arbitrary technical error"
        elif kind == "unresolved_concern":
            previous["needs_final_review"] = ["Unresolved implementation concern"]
        else:
            previous["api_requests"] = 1
        w.store.reviews.finish(previous["review_id"], previous)
    before = w.database.read_bytes()
    result = w.batch.review_retry(batch, w.repo.path, task_id=task, execute=True)
    assert not result.success and not result.executed
    assert w.database.read_bytes() == before
    assert w.store.executions.recovery("TASK-1") is None
    w.client.chat.completions.with_raw_response.create.assert_not_called()


@pytest.mark.parametrize("outcome", ["REJECTED", "UNCERTAIN", "malformed", "api"])
def test_new_nonapproval_stays_blocked_and_cannot_seek_another_approval(stopped, outcome):
    w = stopped
    def reply(**arguments):
        if outcome == "api":
            raise RuntimeError("private provider failure")
        raw = w.reply(**arguments)
        data = json.loads(raw.parse().choices[0].message.content)
        data.update(decision=outcome, blocking_issues=["Bug"] if outcome == "REJECTED" else [])
        return raw_reply(type("Packet", (), data), data="{" if outcome == "malformed" else data)
    w.client.chat.completions.with_raw_response.create.side_effect = reply
    result = retry(w, execute=True)
    assert not result.success and result.executed
    assert w.store.get_task("TASK-1").status == (
        TaskStatus.REJECTED if outcome == "REJECTED" else TaskStatus.AWAITING_REVIEW)
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    assert w.store.reviews.get(w.original["review_id"]) == w.original
    assert len(w.store.reviews.list("TASK-1")) == 2
    assert not retry(w, execute=True).success
    w.client.chat.completions.with_raw_response.create.assert_called_once()
    assert "private provider failure" not in json.dumps(w.store.reviews.list("TASK-1"))


def test_essential_overflow_keeps_uncertain_without_requests(stopped):
    w = stopped
    w.batch.review_config = ReviewConfig(request_budget=2200)
    before = w.database.read_bytes()
    assert "Essential review evidence" in retry(w).error
    assert w.database.read_bytes() == before
    result = retry(w, execute=True)
    assert result.decision == "UNCERTAIN" and not result.success
    assert "Essential review evidence" in result.error
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert len(w.store.reviews.list("TASK-1")) == 2
    w.client.chat.completions.with_raw_response.create.assert_not_called()


def test_concurrent_recovery_loser_cannot_overwrite_winner(stopped):
    w = stopped
    entered, release = Event(), Event()
    def blocked(**arguments):
        entered.set()
        assert release.wait(30)
        return w.reply(**arguments)
    w.client.chat.completions.with_raw_response.create.side_effect = blocked
    def winner():
        with TaskStore(w.database) as store:
            single = SingleTaskController(store, codex_runner=w.codex, test_runner=w.tests,
                                          repository_factory=lambda _: w.repo,
                                          lock_factory=w.single.lock_factory)
            batch = BatchController(store, single=single, reviewer_factory=w.batch.reviewer_factory)
            return batch.review_retry("BATCH", w.repo.path, task_id="TASK-1", execute=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(winner)
        try:
            assert entered.wait(30)
            event = w.store.executions.recovery("TASK-1")
            loser = retry(w, execute=True)
            assert not loser.success and not loser.executed and "busy" in loser.error.lower()
            assert w.store.executions.recovery("TASK-1") == event
        finally:
            release.set()
        assert future.result(timeout=30).success
    w.client.chat.completions.with_raw_response.create.assert_called_once()


@pytest.mark.parametrize("where", ["review", "finalize"])
def test_interruption_and_restart_never_replay(stopped, monkeypatch, where):
    w = stopped
    if where == "review":
        w.client.chat.completions.with_raw_response.create.side_effect = KeyboardInterrupt()
    else:
        monkeypatch.setattr(w.store, "finish_review_recovery", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        retry(w, execute=True)
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert w.store.executions.get("TASK-1") == w.execution
    assert w.store.executions.recovery("TASK-1")["stage"] == "blocked"
    with TaskStore(w.database) as store:
        single = SingleTaskController(store, repository_factory=lambda _: w.repo,
                                      lock_factory=w.single.lock_factory)
        result = BatchController(store, single=single).review_retry(
            "BATCH", w.repo.path, task_id="TASK-1", execute=True)
        assert not result.success and not result.executed
    assert not run(w, max_tasks=1).success
    w.client.chat.completions.with_raw_response.create.assert_called_once()


def test_preflight_cli_is_read_only_and_execute_is_explicit(stopped, monkeypatch, capsys):
    w = stopped
    make = lambda store, **kwargs: BatchController(
        store, single=SingleTaskController(store, repository_factory=lambda _: w.repo,
                                          lock_factory=w.single.lock_factory),
        reviewer_factory=w.batch.reviewer_factory, **kwargs,
    )
    monkeypatch.setattr("auto_coding.batch_controller.BatchController", make)
    arguments = ["batch", "review-retry", "BATCH", "--db", str(w.database),
                 "--repo", str(w.repo.path), "--task", "TASK-1"]
    before = w.database.read_bytes()
    assert main(arguments) == 0
    assert "eligible" in capsys.readouterr().out
    assert w.database.read_bytes() == before
    w.client.chat.completions.with_raw_response.create.assert_not_called()
    assert main([*arguments, "--execute"]) == 0
    assert "approved" in capsys.readouterr().out


def test_missing_database_preflight_creates_nothing(tmp_path):
    database = tmp_path / "missing" / "tasks.sqlite3"
    assert main(["batch", "review-retry", "BATCH", "--db", str(database),
                 "--repo", str(tmp_path), "--task", "TASK-1"]) == 1
    assert not database.parent.exists()


@pytest.mark.parametrize("kind", ["remote", "dirty", "task", "binding", "history"])
def test_drift_during_recovery_cannot_finalize(stopped, kind):
    w = stopped
    def drift(**arguments):
        raw = w.reply(**arguments)
        if kind == "remote":
            origin = git(w.repo.path, "remote", "get-url", "origin")
            git(origin, "update-ref", f"refs/heads/{BRANCH}", w.execution["baseline_sha"])
        elif kind == "dirty":
            (w.repo.path / "initial.txt").write_text("drift")
        elif kind == "task":
            w.store.update_status("TASK-1", TaskStatus.FAILED)
        elif kind == "binding":
            w.store._connection.execute("UPDATE batch_workspaces SET origin = 'changed'")
            w.store._connection.commit()
        else:
            w.store.reviews.create("TASK-1", w.sha, {"decision": "UNCERTAIN"})
        return raw
    w.client.chat.completions.with_raw_response.create.side_effect = drift
    result = retry(w, execute=True)
    assert not result.success
    assert w.store.get_task("TASK-1").status != TaskStatus.APPROVED
    assert w.store.executions.get("TASK-1") == w.execution
    assert w.store.executions.recovery("TASK-1")["stage"] == "blocked"


def test_atomic_finalization_rolls_back_all_state_on_last_write_failure(stopped, monkeypatch):
    w = stopped
    save = w.store.executions._save_recovery
    def failure(record):
        if record["stage"] == "finished":
            raise RuntimeError("private disk detail")
        save(record)
    monkeypatch.setattr(w.store.executions, "_save_recovery", failure)
    result = retry(w, execute=True)
    assert not result.success and "private disk detail" not in result.error
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert w.store.executions.get("TASK-1") == w.execution
    assert w.store.executions.recovery("TASK-1")["stage"] == "blocked"
    assert w.store.reviews.get(result.review_id)["decision"] == "APPROVED"
    assert not retry(w, execute=True).success


def test_hard_crash_after_claim_blocks_restart_without_new_requests(stopped):
    w = stopped
    event = w.store.claim_review_recovery(
        w.plan, w.store.get_task("TASK-1"), w.execution, w.original, asdict(w.batch.review_config),
        w.store.executions.binding("BATCH"))
    with TaskStore(w.database) as store:
        single = SingleTaskController(store, repository_factory=lambda _: w.repo,
                                      lock_factory=w.single.lock_factory)
        result = BatchController(store, single=single).review_retry(
            "BATCH", w.repo.path, task_id="TASK-1", execute=True)
        assert not result.success and not result.executed
    assert w.store.executions.recovery("TASK-1") == event
    assert w.store.executions.get("TASK-1") == w.execution
    w.client.chat.completions.with_raw_response.create.assert_not_called()


def test_later_separately_authorized_batch_skips_recovered_task(workflow):
    w = workflow
    w.batch.review_config = ReviewConfig(request_budget=2200)
    assert not run(w, max_tasks=1).success
    sha = w.store.get_commit("TASK-1").commit_sha
    w.batch.review_config = ReviewConfig(request_budget=12000)
    result = w.batch.review_retry("BATCH", w.repo.path, task_id="TASK-1", execute=True)
    assert result.success and result.commit_sha == sha
    assert w.codex.call_count == w.tests.call_count == w.repo.push_branch.call_count == 1
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    resumed = run(w, max_tasks=1)  # A distinct, explicit normal batch invocation.
    assert resumed.success and resumed.last_completed_task == "TASK-2"
    assert w.codex.call_count == w.tests.call_count == w.repo.push_branch.call_count == 2
    assert w.store.get_commit("TASK-1").commit_sha == sha
    assert w.store.executions.get("TASK-2")["baseline_sha"] == sha
    assert w.store.get_task("TASK-3").status == TaskStatus.QUEUED


def test_database_claim_is_atomic_even_without_workspace_serialization(stopped):
    w = stopped
    barrier = Barrier(2)
    task = w.store.get_task("TASK-1")
    binding = w.store.executions.binding("BATCH")
    def claim():
        with TaskStore(w.database) as store:
            barrier.wait(timeout=20)
            try:
                return store.claim_review_recovery(
                    w.plan, task, w.execution, w.original, asdict(w.batch.review_config), binding)
            except ValueError:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result)
    assert w.store.executions.recovery("TASK-1") == winner
    assert w.store.executions.get("TASK-1") == w.execution
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    w.client.chat.completions.with_raw_response.create.assert_not_called()


def test_incomplete_durable_chunk_approval_cannot_finalize(stopped, monkeypatch):
    w = stopped
    finish = w.store.reviews.finish
    def corrupt(review_id, record):
        finish(review_id, {**record, "coverage_complete": False})
    monkeypatch.setattr(w.store.reviews, "finish", corrupt)
    result = retry(w, execute=True)
    assert not result.success
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert w.store.executions.get("TASK-1") == w.execution


@pytest.mark.parametrize("config", [
    ReviewConfig(request_budget=12000, reasoning_effort="high"),
    ReviewConfig(request_budget=12000, model="different-model"),
])
def test_recovery_model_and_effort_cannot_silently_change(tmp_path, config):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        batch = BatchController(store, review_config=config)
        with pytest.raises(ValueError, match="medium"):
            batch.review_retry("BATCH", tmp_path, task_id="TASK-1")


def test_missing_key_does_not_consume_a_recovery_attempt(stopped):
    w = stopped
    w.batch.reviewer_factory = GroqReviewer
    before = w.database.read_bytes()
    result = retry(w, execute=True)
    assert not result.success and not result.executed and "GROQ_API_KEY" in result.error
    assert w.database.read_bytes() == before
    assert w.store.executions.recovery("TASK-1") is None


def test_read_only_preflight_and_additive_migration_of_legacy_database(stopped):
    w = stopped
    w.store._connection.execute("DROP TABLE review_recoveries")
    w.store._connection.commit()
    before = w.database.read_bytes()
    with TaskStore(w.database, read_only=True) as store:
        single = SingleTaskController(store, repository_factory=lambda _: w.repo,
                                      lock_factory=w.single.lock_factory)
        assert BatchController(store, single=single).review_retry(
            "BATCH", w.repo.path, task_id="TASK-1").success
    assert w.database.read_bytes() == before
    with TaskStore(w.database) as store:
        single = SingleTaskController(store, repository_factory=lambda _: w.repo,
                                      lock_factory=w.single.lock_factory)
        result = BatchController(store, single=single, reviewer_factory=w.batch.reviewer_factory).review_retry(
            "BATCH", w.repo.path, task_id="TASK-1", execute=True)
        assert result.success
        assert store.get_plan("BATCH") == w.plan
        assert store.reviews.get(w.original["review_id"]) == w.original
        assert store.get_commit("TASK-1").commit_sha == w.sha
    w.client.chat.completions.with_raw_response.create.assert_called_once()


@pytest.mark.parametrize("kind", ["review", "history"])
def test_last_moment_review_drift_is_rejected_inside_finalization_transaction(stopped, monkeypatch, kind):
    w = stopped
    finish = w.store.finish_review_recovery
    def drift(task_id, recovery_id, review_id, expected_review):
        if kind == "review":
            changed = w.store.reviews.get(review_id)
            changed["needs_final_review"] = ["Changed after verification"]
            w.store.reviews.finish(review_id, changed)
        else:
            w.store.reviews.create(task_id, w.sha, {"decision": "UNCERTAIN"})
        finish(task_id, recovery_id, review_id, expected_review)
    monkeypatch.setattr(w.store, "finish_review_recovery", drift)
    result = retry(w, execute=True)
    assert not result.success
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert w.store.executions.get("TASK-1") == w.execution
    assert w.store.executions.recovery("TASK-1")["stage"] == "blocked"


def test_failed_audit_write_does_not_mask_interruption_or_replay(stopped, monkeypatch):
    w = stopped
    monkeypatch.setattr(w.store, "finish_review_recovery", Mock(side_effect=KeyboardInterrupt()))
    monkeypatch.setattr(w.store, "block_review_recovery", Mock(side_effect=RuntimeError("private disk error")))
    with pytest.raises(KeyboardInterrupt) as interrupted:
        retry(w, execute=True)
    assert any("audit persistence failed" in note for note in interrupted.value.__notes__)
    assert w.store.executions.recovery("TASK-1")["stage"] == "reviewing"
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert w.store.executions.get("TASK-1") == w.execution
    assert not retry(w, execute=True).success
    w.client.chat.completions.with_raw_response.create.assert_called_once()
