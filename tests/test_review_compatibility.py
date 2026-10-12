"""One extra, explicitly bound recovery; temporary Git and mocked HTTP only."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
import json
from threading import Barrier, Event
from unittest.mock import Mock

import groq
import httpx
import pytest

from auto_coding.batch_controller import BatchController
from auto_coding.cli import main
from auto_coding.controller import SingleTaskController
from auto_coding.review_recovery import CompatibilityRetry
from auto_coding.storage import TaskStore, TaskStatus

from test_batch_controller import BRANCH, offline, workflow
from test_git import git, repository
from test_review_recovery import stopped, retry
from test_groq_reviewer import raw_reply


@pytest.fixture
def compatibility_stop(stopped, monkeypatch):
    from auto_coding.integrations import groq as integration
    w = stopped
    arguments = integration.request_arguments
    # Model the exact historical unsupported request, without live usage.
    with monkeypatch.context() as legacy:
        legacy.setattr(integration, "request_arguments", lambda *args: {**arguments(*args), "store": False})
        w.client.chat.completions.with_raw_response.create.side_effect = groq.BadRequestError(
            "private provider error", response=httpx.Response(
                400, request=httpx.Request("POST", "https://api.groq.com")),
            body={"error": {"type": "invalid_request_error", "code": "unsupported_parameter"}},
        )
        first = retry(w, execute=True)
    assert not first.success and first.decision == "UNCERTAIN"
    # The real legacy failure did not preserve an HTTP status/type/code.
    request = w.store.reviews.get(first.review_id)["requests"][0]
    for key in ("http_status", "provider_error_type", "provider_error_code"):
        request.pop(key, None)
    w.store.reviews.finish_request(first.review_id, 1, request)
    w.first = w.store.executions.recovery("TASK-1")
    w.failed = w.store.reviews.get(first.review_id)
    w.stopped_execution = w.store.executions.get("TASK-1")
    w.authorization = CompatibilityRetry(first.review_id, w.plan.fingerprint, w.sha, BRANCH)
    w.client.chat.completions.with_raw_response.create.reset_mock()
    w.client.chat.completions.with_raw_response.create.side_effect = w.reply
    return w


def second(w, **options):
    return w.batch.review_retry("BATCH", w.repo.path, task_id="TASK-1",
                               compatibility=w.authorization, **options)


def test_second_recovery_is_readonly_until_execution_and_preserves_both_attempts(compatibility_stop):
    w = compatibility_stop
    before = w.database.read_bytes()
    result = second(w)
    assert result.success and not result.executed, result
    assert w.database.read_bytes() == before
    w.client.chat.completions.with_raw_response.create.assert_not_called()
    result = second(w, execute=True)
    assert result.success and result.decision == "APPROVED", result
    event = w.store.executions.recovery("TASK-1", compatibility=True)
    assert event["previous_recovery"] == w.first and event["stage"] == "finished"
    assert event["original_execution"] == w.stopped_execution
    assert w.store.executions.recovery("TASK-1") == w.first
    assert w.store.reviews.get(w.original["review_id"]) == w.original
    assert w.store.reviews.get(w.failed["review_id"]) == w.failed
    review = w.store.reviews.get(result.review_id)
    assert review["original_review_id"] == w.failed["review_id"]
    assert review["previous_recovery_id"] == w.first["recovery_id"] and review["recovery_attempt"] == 2
    assert review["usage"]["total_tokens"] == 125 and review["coverage_complete"]
    assert "store" not in review["requests"][0]["submitted"]
    assert w.store.get_task("TASK-1").status == TaskStatus.APPROVED
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    assert w.repo.current_commit_sha() == w.sha
    assert len(w.store.reviews.list("TASK-1")) == 3
    assert not second(w, execute=True).success
    assert not retry(w, execute=True).success
    w.client.chat.completions.with_raw_response.create.assert_called_once()
    with TaskStore(w.database, read_only=True) as reopened:
        assert reopened.executions.recovery("TASK-1") == w.first
        assert reopened.executions.recovery("TASK-1", compatibility=True) == event


@pytest.mark.parametrize("field,value", [
    ("failed_review_id", "a" * 32), ("fingerprint", "b" * 64),
    ("commit_sha", "c" * 40), ("branch", "feature/wrong"),
])
def test_second_recovery_requires_exact_explicit_binding(compatibility_stop, field, value):
    w = compatibility_stop
    w.authorization = replace(w.authorization, **{field: value})
    before = w.database.read_bytes()
    result = second(w, execute=True)
    assert not result.success and not result.executed
    assert w.database.read_bytes() == before
    w.client.chat.completions.with_raw_response.create.assert_not_called()


@pytest.mark.parametrize("kind", ["store_absent", "unfinished", "status500", "response", "rejected",
                                  "uncertain_judgment", "usage", "history", "first_audit", "checkpoint"])
def test_ambiguous_or_substantive_failures_cannot_get_second_recovery(compatibility_stop, kind):
    w = compatibility_stop
    request = deepcopy(w.failed["requests"][0])
    if kind == "store_absent":
        request["submitted"].pop("store")
    elif kind == "unfinished":
        request["completed"] = False
    elif kind == "status500":
        request["http_status"] = 500
    elif kind == "response":
        request["response"] = {"decision": "UNCERTAIN"}
    elif kind == "usage":
        request["usage"] = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    elif kind in ("rejected", "uncertain_judgment"):
        record = deepcopy(w.failed)
        record.update(decision="REJECTED" if kind == "rejected" else "UNCERTAIN", error=None)
        w.store.reviews.finish(record["review_id"], record)
    elif kind == "history":
        w.store.reviews.create("TASK-1", w.sha, {"decision": "UNCERTAIN"})
    elif kind == "first_audit":
        record = deepcopy(w.first)
        record["stage"] = "blocked"
        w.store.executions._save_recovery(record)
    else:
        w.store.executions.checkpoint("TASK-1", "stopped", {})
    if kind in ("store_absent", "unfinished", "status500", "response", "usage"):
        w.store.reviews.finish_request(w.failed["review_id"], 1, request)
    before = w.database.read_bytes()
    result = second(w, execute=True)
    assert not result.success and not result.executed
    assert w.database.read_bytes() == before
    w.client.chat.completions.with_raw_response.create.assert_not_called()


def test_cli_second_preflight_requires_flag_and_all_bindings(compatibility_stop, monkeypatch, capsys):
    w = compatibility_stop
    monkeypatch.setattr("auto_coding.batch_controller.BatchController", lambda store, **kwargs: BatchController(
        store, single=SingleTaskController(store, repository_factory=lambda _: w.repo,
                                          lock_factory=w.single.lock_factory),
        reviewer_factory=w.batch.reviewer_factory, **kwargs,
    ))
    arguments = ["batch", "review-retry", "BATCH", "--db", str(w.database), "--repo", str(w.repo.path),
                 "--task", "TASK-1", "--retry-api-compatibility"]
    before = w.database.read_bytes()
    assert main(arguments) == 1
    assert "requires" in capsys.readouterr().err
    bindings = ["--failed-review-id", w.failed["review_id"], "--expected-fingerprint", w.plan.fingerprint,
                "--expected-sha", w.sha, "--expected-branch", BRANCH]
    assert main([*arguments, *bindings]) == 0
    assert "eligible" in capsys.readouterr().out
    assert w.database.read_bytes() == before
    w.client.chat.completions.with_raw_response.create.assert_not_called()
    assert main([*arguments, *bindings, "--execute"]) == 0
    w.client.chat.completions.with_raw_response.create.assert_called_once()


@pytest.mark.parametrize("kind", ["branch", "dirty", "remote"])
def test_second_recovery_reverifies_repository_without_writes(compatibility_stop, kind):
    w = compatibility_stop
    if kind == "branch":
        git(w.repo.path, "switch", "-c", "feature/wrong")
    elif kind == "dirty":
        (w.repo.path / "initial.txt").write_text("unexpected")
    else:
        origin = git(w.repo.path, "remote", "get-url", "origin")
        git(origin, "update-ref", f"refs/heads/{BRANCH}", w.execution["baseline_sha"])
    before = w.database.read_bytes()
    result = second(w, execute=True)
    assert not result.success and not result.executed
    assert w.database.read_bytes() == before
    w.client.chat.completions.with_raw_response.create.assert_not_called()


@pytest.mark.parametrize("outcome", ["REJECTED", "UNCERTAIN", "api"])
def test_second_nonapproval_never_allows_third_attempt(compatibility_stop, outcome):
    w = compatibility_stop
    def reply(**arguments):
        if outcome == "api":
            raise RuntimeError("private provider failure")
        raw = w.reply(**arguments)
        data = json.loads(raw.parse().choices[0].message.content)
        data.update(decision=outcome, blocking_issues=["Bug"] if outcome == "REJECTED" else [])
        return raw_reply(type("Packet", (), data), data=data)
    w.client.chat.completions.with_raw_response.create.side_effect = reply
    result = second(w, execute=True)
    assert not result.success and result.executed
    assert w.store.get_task("TASK-1").status == (
        TaskStatus.REJECTED if outcome == "REJECTED" else TaskStatus.AWAITING_REVIEW)
    assert w.store.executions.recovery("TASK-1") == w.first
    assert w.store.reviews.get(w.failed["review_id"]) == w.failed
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    assert len(w.store.reviews.list("TASK-1")) == 3
    assert not second(w, execute=True).success
    assert not retry(w, execute=True).success
    w.client.chat.completions.with_raw_response.create.assert_called_once()
    assert "private provider failure" not in json.dumps(w.store.reviews.list("TASK-1"))


@pytest.mark.parametrize("where", ["review", "finalize"])
def test_second_interruption_preserves_history_and_blocks_restart(compatibility_stop, monkeypatch, where):
    w = compatibility_stop
    if where == "review":
        w.client.chat.completions.with_raw_response.create.side_effect = KeyboardInterrupt()
    else:
        monkeypatch.setattr(w.store, "finish_review_recovery", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        second(w, execute=True)
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert w.store.executions.get("TASK-1") == w.stopped_execution
    assert w.store.executions.recovery("TASK-1") == w.first
    assert w.store.executions.recovery("TASK-1", compatibility=True)["stage"] == "blocked"
    with TaskStore(w.database) as store:
        single = SingleTaskController(store, repository_factory=lambda _: w.repo,
                                      lock_factory=w.single.lock_factory)
        result = BatchController(store, single=single).review_retry(
            "BATCH", w.repo.path, task_id="TASK-1", execute=True, compatibility=w.authorization)
        assert not result.success and not result.executed
    w.client.chat.completions.with_raw_response.create.assert_called_once()


def test_second_finalization_rolls_back_on_audit_write_failure(compatibility_stop, monkeypatch):
    w = compatibility_stop
    save = w.store.executions._save_recovery
    def failure(record):
        if record["stage"] == "finished":
            raise RuntimeError("private disk detail")
        save(record)
    monkeypatch.setattr(w.store.executions, "_save_recovery", failure)
    result = second(w, execute=True)
    assert not result.success and "private disk detail" not in result.error
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert w.store.executions.get("TASK-1") == w.stopped_execution
    assert w.store.executions.recovery("TASK-1") == w.first
    assert w.store.executions.recovery("TASK-1", compatibility=True)["stage"] == "blocked"
    assert w.store.reviews.get(result.review_id)["decision"] == "APPROVED"
    assert not second(w, execute=True).success


def test_second_claim_is_atomic_even_without_workspace_lock(compatibility_stop):
    w = compatibility_stop
    barrier = Barrier(2)
    task = w.store.get_task("TASK-1")
    binding = w.store.executions.binding("BATCH")
    def claim():
        with TaskStore(w.database) as store:
            barrier.wait(timeout=20)
            try:
                return store.claim_review_recovery(
                    w.plan, task, w.stopped_execution, w.failed, asdict(w.batch.review_config),
                    binding, previous_recovery=w.first)
            except ValueError as exc:
                assert "already claimed" in str(exc)
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert w.store.executions.recovery("TASK-1", compatibility=True) == next(r for r in results if r)
    assert w.store.executions.recovery("TASK-1") == w.first
    assert w.store.executions.get("TASK-1") == w.stopped_execution
    w.client.chat.completions.with_raw_response.create.assert_not_called()


def test_second_workspace_lock_loser_cannot_overwrite_winner(compatibility_stop):
    w = compatibility_stop
    entered, release = Event(), Event()
    def blocked(**arguments):
        entered.set()
        assert release.wait(30)
        return w.reply(**arguments)
    w.client.chat.completions.with_raw_response.create.side_effect = blocked
    def winner():
        with TaskStore(w.database) as store:
            single = SingleTaskController(store, repository_factory=lambda _: w.repo,
                                          lock_factory=w.single.lock_factory)
            return BatchController(store, single=single, reviewer_factory=w.batch.reviewer_factory).review_retry(
                "BATCH", w.repo.path, task_id="TASK-1", execute=True, compatibility=w.authorization)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(winner)
        try:
            assert entered.wait(30)
            event = w.store.executions.recovery("TASK-1", compatibility=True)
            loser = second(w, execute=True)
            assert not loser.success and not loser.executed and "busy" in loser.error.lower()
            assert w.store.executions.recovery("TASK-1", compatibility=True) == event
        finally:
            release.set()
        assert future.result(timeout=30).success
    assert w.store.executions.recovery("TASK-1") == w.first
    w.client.chat.completions.with_raw_response.create.assert_called_once()


def test_old_database_preflight_does_not_migrate_second_claim_table(compatibility_stop):
    w = compatibility_stop
    w.store._connection.execute("DROP TABLE review_compatibility_recoveries")
    w.store._connection.commit()
    before = w.database.read_bytes()
    with TaskStore(w.database, read_only=True) as store:
        single = SingleTaskController(store, repository_factory=lambda _: w.repo,
                                      lock_factory=w.single.lock_factory)
        result = BatchController(store, single=single).review_retry(
            "BATCH", w.repo.path, task_id="TASK-1", compatibility=w.authorization)
        assert result.success, result
    assert w.database.read_bytes() == before
    with TaskStore(w.database) as store:
        single = SingleTaskController(store, repository_factory=lambda _: w.repo,
                                      lock_factory=w.single.lock_factory)
        result = BatchController(store, single=single, reviewer_factory=w.batch.reviewer_factory).review_retry(
            "BATCH", w.repo.path, task_id="TASK-1", execute=True, compatibility=w.authorization)
        assert result.success, result
        assert store.executions.recovery("TASK-1") == w.first
    w.client.chat.completions.with_raw_response.create.assert_called_once()
