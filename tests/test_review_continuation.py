"""Explicit reusable continuations; disposable Git/SQLite and mocked SDK only."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from threading import Barrier
from unittest.mock import Mock

import pytest

from auto_coding.batch_controller import BatchController
from auto_coding.cli import main
from auto_coding.controller import SingleTaskController
from auto_coding.review_recovery import TechnicalContinuation
from auto_coding.storage import TaskStore, TaskStatus

from test_batch_controller import BRANCH, offline, workflow, run
from test_git import git, repository
from test_review_recovery import stopped, retry
from test_review_compatibility import compatibility_stop, second


@pytest.fixture
def local_stop(compatibility_stop, monkeypatch):
    w = compatibility_stop
    with monkeypatch.context() as blocked:
        block = Mock(side_effect=[False, True])
        blocked.setattr("auto_coding.integrations.groq.rate_blocked", block)
        blocked.setattr("auto_coding.review_recovery.rate_blocked", block)
        result = second(w, execute=True)
    assert result.decision == "UNCERTAIN" and result.executed
    assert w.store.reviews.get(result.review_id)["api_requests"] == 0
    w.prior_reviews = w.store.reviews.list("TASK-1")
    w.prior_events = w.store.executions.recoveries("TASK-1")
    w.current_execution = w.store.executions.get("TASK-1")
    w.continuation = TechnicalContinuation(result.review_id, w.plan.fingerprint, w.sha, BRANCH)
    w.client.chat.completions.with_raw_response.create.assert_not_called()
    return w


def continued(w, **options):
    return w.batch.review_retry("BATCH", w.repo.path, task_id="TASK-1", continuation=w.continuation, **options)


def test_local_block_continuation_is_readonly_until_explicit_execution(local_stop):
    w = local_stop
    before = w.database.read_bytes()
    result = continued(w)
    assert result.success and not result.executed, result
    assert w.database.read_bytes() == before
    w.client.chat.completions.with_raw_response.create.assert_not_called()
    result = continued(w, execute=True)
    assert result.success and result.decision == "APPROVED", result
    assert w.store.reviews.list("TASK-1")[:3] == w.prior_reviews
    assert w.store.executions.recoveries("TASK-1")[:2] == w.prior_events
    assert w.store.get_task("TASK-1").status == TaskStatus.APPROVED
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    assert w.repo.current_commit_sha() == w.sha
    assert not continued(w, execute=True).success
    w.client.chat.completions.with_raw_response.create.assert_called_once()


def test_another_completed_local_block_can_be_explicitly_continued_without_new_code(local_stop, monkeypatch):
    w = local_stop
    with monkeypatch.context() as blocked:
        block = Mock(side_effect=[False, True])
        blocked.setattr("auto_coding.integrations.groq.rate_blocked", block)
        blocked.setattr("auto_coding.review_recovery.rate_blocked", block)
        first = continued(w, execute=True)
    assert not first.success and first.executed
    assert not continued(w, execute=True).success  # Old authorization cannot be reused.
    w.continuation = replace(w.continuation, failed_review_id=first.review_id)
    before = w.database.read_bytes()
    assert continued(w).success
    assert w.database.read_bytes() == before
    assert continued(w, execute=True).success
    assert len(w.store.reviews.list("TASK-1")) == 5
    assert len(w.store.executions.continuations("TASK-1")) == 2
    assert w.store.executions.recoveries("TASK-1")[:2] == w.prior_events
    w.client.chat.completions.with_raw_response.create.assert_called_once()


@pytest.mark.parametrize("kind", ["binding", "dirty", "remote", "pending", "rejected", "finding", "history", "audit"])
def test_uncertain_or_unapproved_continuation_is_refused_without_mutation(local_stop, kind):
    w = local_stop
    if kind == "binding":
        w.continuation = replace(w.continuation, fingerprint="b" * 64)
    elif kind == "dirty":
        (w.repo.path / "initial.txt").write_text("unexpected")
    elif kind == "remote":
        origin = git(w.repo.path, "remote", "get-url", "origin")
        git(origin, "update-ref", f"refs/heads/{BRANCH}", w.execution["baseline_sha"])
    elif kind == "audit":
        event = deepcopy(w.prior_events[-1])
        event["stage"] = "blocked"
        w.store.executions._save_recovery(event)
    elif kind == "history":
        w.store.reviews.create("TASK-1", w.sha, {"decision": "UNCERTAIN"})
    elif kind == "pending":
        w.store.reviews.start_request(w.continuation.failed_review_id, 1, {"completed": False})
    else:
        review = deepcopy(w.prior_reviews[-1])
        if kind == "rejected":
            review.update(decision="REJECTED", blocking_issues=["Bug"])
        else:
            review["needs_final_review"] = ["Unresolved architecture finding"]
        w.store.reviews.finish(review["review_id"], review)
    before = w.database.read_bytes()
    result = continued(w, execute=True)
    assert not result.success and not result.executed, result
    assert w.database.read_bytes() == before
    w.client.chat.completions.with_raw_response.create.assert_not_called()


@pytest.mark.parametrize("where", ["interruption", "rollback"])
def test_claimed_continuation_never_replays_after_uncertain_completion(local_stop, monkeypatch, where):
    w = local_stop
    if where == "interruption":
        w.client.chat.completions.with_raw_response.create.side_effect = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            continued(w, execute=True)
    else:
        save = w.store.executions._save_recovery
        def failure(record):
            if record["stage"] == "finished":
                raise RuntimeError("private disk error")
            save(record)
        monkeypatch.setattr(w.store.executions, "_save_recovery", failure)
        result = continued(w, execute=True)
        assert not result.success
    assert w.store.executions.get("TASK-1") == w.current_execution
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert w.store.executions.continuations("TASK-1")[-1]["stage"] == "blocked"
    assert not continued(w, execute=True).success
    assert w.store.executions.recoveries("TASK-1")[:2] == w.prior_events
    w.client.chat.completions.with_raw_response.create.assert_called_once()


def test_continuation_claims_are_atomic_across_database_connections(local_stop):
    w = local_stop
    task, binding = w.store.get_task("TASK-1"), w.store.executions.binding("BATCH")
    context = {"prior_recoveries": w.store.executions.signatures(w.prior_events), "previous_reviews": w.prior_reviews}
    barrier = Barrier(2)
    def claim():
        with TaskStore(w.database) as store:
            barrier.wait(timeout=20)
            try:
                return store.claim_review_recovery(w.plan, task, w.current_execution, w.prior_reviews[-1],
                    asdict(w.batch.review_config), binding, continuation=context)
            except ValueError as exc:
                assert "already claimed" in str(exc)
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert w.store.executions.get("TASK-1") == w.current_execution
    assert w.store.executions.recoveries("TASK-1")[:2] == w.prior_events
    w.client.chat.completions.with_raw_response.create.assert_not_called()


def test_cli_continuation_requires_full_binding_and_execute(local_stop, monkeypatch):
    w = local_stop
    monkeypatch.setattr("auto_coding.batch_controller.BatchController", lambda store, **kwargs: BatchController(
        store, single=SingleTaskController(store, repository_factory=lambda _: w.repo,
                                          lock_factory=w.single.lock_factory),
        reviewer_factory=w.batch.reviewer_factory, **kwargs))
    arguments = ["batch", "review-retry", "BATCH", "--db", str(w.database), "--repo", str(w.repo.path),
                 "--task", "TASK-1", "--continue-technical"]
    assert main(arguments) == 1
    arguments += ["--failed-review-id", w.continuation.failed_review_id, "--expected-fingerprint", w.plan.fingerprint,
                  "--expected-sha", w.sha, "--expected-branch", BRANCH]
    before = w.database.read_bytes()
    assert main(arguments) == 0
    assert w.database.read_bytes() == before
    w.client.chat.completions.with_raw_response.create.assert_not_called()
    assert main([*arguments, "--execute"]) == 0
    w.client.chat.completions.with_raw_response.create.assert_called_once()


def test_initial_controller_local_block_needs_no_one_off_recovery(workflow, monkeypatch):
    w = workflow
    with monkeypatch.context() as blocked:
        blocked.setattr("auto_coding.integrations.groq.rate_blocked", lambda *args: True)
        result = run(w, max_tasks=1)
    assert not result.success
    first = w.store.reviews.list("TASK-1")[0]
    w.sha = w.repo.current_commit_sha()
    w.continuation = TechnicalContinuation(first["review_id"], w.plan.fingerprint, w.sha, BRANCH)
    w.codex.reset_mock()
    w.tests.reset_mock()
    w.codex.side_effect = w.tests.side_effect = AssertionError("No coding or tests during continuation")
    monkeypatch.setattr(w.repo, "commit_files", Mock(side_effect=AssertionError("No commit")))
    monkeypatch.setattr(w.repo, "push_branch", Mock(side_effect=AssertionError("No push")))
    before = w.database.read_bytes()
    assert continued(w).success
    assert w.database.read_bytes() == before
    result = continued(w, execute=True)
    assert result.success, result
    assert w.store.reviews.get(first["review_id"]) == first
    assert len(w.store.executions.continuations("TASK-1")) == 1
    for operation in (w.codex, w.tests, w.repo.commit_files, w.repo.push_branch):
        operation.assert_not_called()


@pytest.mark.parametrize("decision", ["REJECTED", "UNCERTAIN"])
def test_new_model_nonapproval_cannot_be_overridden_or_continued(local_stop, decision):
    import json
    from test_groq_reviewer import raw_reply
    w = local_stop
    def reply(**arguments):
        raw = w.reply(**arguments)
        data = json.loads(raw.parse().choices[0].message.content)
        data.update(decision=decision, blocking_issues=["Bug"] if decision == "REJECTED" else [],
                    needs_final_review=["Unresolved finding"])
        return raw_reply(type("Packet", (), data), data=data)
    w.client.chat.completions.with_raw_response.create.side_effect = reply
    result = continued(w, execute=True)
    assert not result.success and result.decision == decision
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    w.continuation = replace(w.continuation, failed_review_id=result.review_id)
    assert not continued(w, execute=True).success
    w.client.chat.completions.with_raw_response.create.assert_called_once()
