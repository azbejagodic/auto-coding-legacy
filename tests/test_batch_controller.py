"""Offline batch integration with real temporary Git and mocked AI transports."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import hashlib
import logging
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock

import groq
import httpx
import pytest

from auto_coding.batch_controller import BatchController
from auto_coding.cli import main
from auto_coding.controller import SingleTaskController
from auto_coding.integrations.codex import CodexResult
from auto_coding.integrations.git import GitError
from auto_coding.integrations.groq import GroqReviewer
from auto_coding.plans import validate_plan, PlanValidationError
from auto_coding.reviews import ReviewConfig
from auto_coding.storage import PushState, TaskStatus, TaskStore
from auto_coding.test_runner import CommandResult, TestRunResult as RunResult
from auto_coding.workspace_lock import WorkspaceLock
from test_git import git, repository
from test_groq_reviewer import raw_reply


BRANCH = "feature/approved-batch"


def definition():
    return {
        "schema_version": 1, "batch_id": "BATCH", "expected_branch": BRANCH,
        "tasks": [{
            "task_id": f"TASK-{i}", "title": f"Task {i}",
            "prompt": f"Implement task {i} on {{{{BRANCH}}}} after {{{{LATEST_SHA}}}}.",
            "model": f"exact-model-{i}", "reasoning_effort": ["high", "medium", "low"][i - 1],
            "task_order": i, "acceptance_criteria": [f"Requirement {i}"],
            "dependencies": [] if i == 1 else [f"TASK-{i - 1}"],
            "permitted_files": ["initial.txt"], "test_commands": [["approved-test", str(i)]],
            "approved_placeholders": ["BRANCH", "LATEST_SHA"],
            "execution_limits": {"codex_timeout": 100 + i, "test_timeout": 10 + i},
        } for i in range(1, 4)],
    }


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        Mock(side_effect=AssertionError("Live HTTP is forbidden")))
    monkeypatch.setattr("auto_coding.integrations.codex._run_process",
                        Mock(side_effect=AssertionError("Live Codex is forbidden")))


@pytest.fixture
def workflow(repository, tmp_path, monkeypatch):
    repo = repository
    git(repo.path, "switch", "-c", BRANCH)
    monkeypatch.setattr(repo, "commit_url", lambda sha: "https://github.com/offline/test/commit/" + sha)
    push = repo.push_branch
    monkeypatch.setattr(repo, "push_branch", Mock(side_effect=lambda *args: replace(
        push(*args), github_commit_url=repo.commit_url(args[1])
    )))
    database = tmp_path / "tasks.sqlite3"
    with TaskStore(database) as store:
        plan = store.import_plan(definition(), approved=True)
        calls = []

        def code(**kwargs):
            for previous in range(1, len(calls) + 1):
                assert store.get_task(f"TASK-{previous}").status == TaskStatus.APPROVED
            calls.append(kwargs)
            (repo.path / "initial.txt").write_text(f"task {len(calls)}\n", encoding="utf-8")
            return CodexResult(True, kwargs["model"], 0, "done", "", "")

        codex = Mock(side_effect=code)
        tests = Mock(side_effect=lambda **kwargs: RunResult(
            True, tuple(CommandResult(tuple(argv), 0, "", "", .1, True)
                        for argv in kwargs["commands"]), None, .1,
        ))
        client = Mock()
        client.with_options.return_value = client

        def reply(**arguments):
            content = json.loads(arguments["messages"][1]["content"])
            packet = SimpleNamespace(task_id=content["task_id"], commit_sha=content["commit_sha"])
            data = {
                "task_id": packet.task_id, "commit_sha": packet.commit_sha,
                "decision": "APPROVED", "confidence": "HIGH",
                "requirements_checked": content["acceptance_criteria"],
                "blocking_issues": [], "needs_final_review": [],
            }
            return raw_reply(packet, data=data)

        client.chat.completions.with_raw_response.create.side_effect = reply
        single = SingleTaskController(
            store, codex_runner=codex, test_runner=tests,
            repository_factory=lambda _: repo,
            lock_factory=lambda path: WorkspaceLock(path, lock_directory=tmp_path / "locks"),
        )
        factory = lambda store, **kwargs: GroqReviewer(store, client=client, **kwargs)
        batch = BatchController(store, single=single, reviewer_factory=factory)
        yield SimpleNamespace(repo=repo, store=store, single=single, batch=batch, codex=codex, tests=tests,
                              client=client, plan=plan, database=database, calls=calls, reply=reply,
                              lock_directory=tmp_path / "locks")


def run(w, **kwargs):
    return w.batch.run("BATCH", w.repo.path, **kwargs)


def test_three_tasks_order_exact_frozen_configuration_and_progress(workflow):
    w = workflow
    initial = w.repo.current_commit_sha()
    result = run(w)
    assert result.success and result.completed and result.successful_tasks == 3, result
    assert [call["model"] for call in w.calls] == ["exact-model-1", "exact-model-2", "exact-model-3"]
    assert [call["reasoning_effort"] for call in w.calls] == ["high", "medium", "low"]
    parent = initial
    for index, task in enumerate(w.plan.to_dict()["tasks"]):
        call = w.calls[index]
        assert call["prompt"] == task["prompt"].replace("{{BRANCH}}", BRANCH).replace("{{LATEST_SHA}}", parent)
        assert call["timeout"] == 101 + index and call["controller_owned_git"] is True
        assert w.tests.call_args_list[index].kwargs["commands"] == (("approved-test", str(index + 1)),)
        assert w.tests.call_args_list[index].kwargs["timeout"] == 11 + index
        saved = w.store.get_commit(task["task_id"])
        assert saved.push_state == PushState.VERIFIED and len(saved.commit_sha) == 40
        assert w.store.get_task(task["task_id"]).status == TaskStatus.APPROVED
        execution = w.store.executions.get(task["task_id"])
        stages = [item["stage"] for item in execution["checkpoints"]]
        assert stages == ["claimed", "codex_started", "codex_finished", "tests_started", "tests_completed",
                          "commit_started", "commit_created", "push_started", "push_verified",
                          "review_started", "review_finished", "finalized"]
        assert execution["baseline_sha"] == parent
        assert execution["local_sha"] == execution["remote_sha"] == saved.commit_sha
        assert execution["fingerprint"] == w.plan.fingerprint
        assert execution["tests"]["success"] and execution["codex"]["success"]
        assert not execution["recovery_required"] and execution["ended_at"]
        history = w.store.reviews.get(execution["review_id"])
        assert history["decision"] == "APPROVED" and history["usage"]["total_tokens"] == 125
        parent = saved.commit_sha
    assert w.repo.push_branch.call_count == 3 and w.codex.call_count == w.tests.call_count == 3
    assert git(w.repo.path, "rev-list", "--count", f"{initial}..HEAD") == "3"
    assert w.store.get_plan("BATCH") == w.plan  # Runtime values never rewrite the definition.
    with TaskStore(w.database, read_only=True) as reopened:
        assert reopened.executions.get("TASK-3") == w.store.executions.get("TASK-3")
    again = run(w)
    assert again.success and again.completed and again.successful_tasks == 0
    assert w.codex.call_count == w.repo.push_branch.call_count == 3


def test_limit_then_restart_only_runs_remaining_tasks(workflow):
    w = workflow
    first = run(w, max_tasks=1)
    assert first.success and not first.completed and first.stopped_stage == "limit", first
    with TaskStore(w.database) as reopened:
        single = SingleTaskController(
            reopened, codex_runner=w.codex, test_runner=w.tests,
            repository_factory=lambda _: w.repo, lock_factory=w.single.lock_factory,
        )
        result = BatchController(reopened, single=single, reviewer_factory=w.batch.reviewer_factory).run(
            "BATCH", w.repo.path
        )
    assert result.success and result.completed and result.successful_tasks == 2, result
    assert w.codex.call_count == 3 and w.repo.push_branch.call_count == 3


@pytest.mark.parametrize("state", ["wrong_branch", "main", "dirty", "staged", "no_origin", "merge"])
def test_preflight_does_not_claim(workflow, state):
    w = workflow
    if state in ("wrong_branch", "main"):
        git(w.repo.path, "switch", "main")
    elif state in ("dirty", "staged"):
        (w.repo.path / "initial.txt").write_text("unexpected")
        if state == "staged":
            git(w.repo.path, "add", "--", "initial.txt")
    elif state == "no_origin":
        git(w.repo.path, "remote", "remove", "origin")
    else:
        (w.repo.path / ".git" / "MERGE_HEAD").write_text(w.repo.current_commit_sha())
    assert not run(w).success
    assert w.store.get_task("TASK-1").status == TaskStatus.QUEUED
    assert w.store.executions.get("TASK-1") is None
    w.codex.assert_not_called()
    w.tests.assert_not_called()


@pytest.mark.parametrize("kind", ["failed", "timeout", "model_mismatch", "exception", "cleanup"])
def test_codex_failure_prevents_tests_commit_and_next_task(workflow, kind):
    w = workflow
    initial = w.repo.current_commit_sha()
    if kind == "exception":
        w.codex.side_effect = RuntimeError("private exception")
    else:
        w.codex.side_effect = None
        w.codex.return_value = CodexResult(
            kind == "model_mismatch", "wrong" if kind == "model_mismatch" else "exact-model-1",
            0 if kind in ("model_mismatch", "cleanup") else 1, None, "", "",
            error="cleanup failed" if kind == "cleanup" else "failed", timed_out=kind == "timeout",
        )
    result = run(w)
    assert not result.success and result.successful_tasks == 0
    assert w.store.get_task("TASK-1").status == TaskStatus.FAILED
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    assert w.repo.current_commit_sha() == initial
    assert w.store.executions.get("TASK-1")["stage"] == "stopped"
    w.codex.assert_called_once()
    w.tests.assert_not_called()
    w.repo.push_branch.assert_not_called()


@pytest.mark.parametrize("kind", ["failed", "timeout", "cleanup", "wrong_argv", "source_mutation", "exception"])
def test_test_failures_prevent_commit(workflow, kind):
    w = workflow
    initial = w.repo.current_commit_sha()
    original = w.tests.side_effect

    def testing(**kwargs):
        if kind == "exception":
            raise RuntimeError("private")
        if kind == "source_mutation":
            (w.repo.path / "initial.txt").write_text("changed during tests")
            return original(**kwargs)
        command = CommandResult(
            ("wrong",) if kind == "wrong_argv" else tuple(kwargs["commands"][0]),
            1 if kind == "failed" else 0, "", "", .1, kind != "failed",
            timed_out=kind == "timeout", cleanup_error="failed" if kind == "cleanup" else None,
        )
        return RunResult(kind != "failed", (command,), None, .1)

    w.tests.side_effect = testing
    assert not run(w).success
    assert w.repo.current_commit_sha() == initial and w.store.get_commit("TASK-1") is None
    w.codex.assert_called_once()
    w.tests.assert_called_once()
    w.repo.push_branch.assert_not_called()


@pytest.mark.parametrize("kind", ["outside", "sensitive", "staged", "branch", "head", "no_changes"])
def test_unsafe_codex_changes_prevent_tests_and_commit(workflow, kind):
    w = workflow
    original = w.codex.side_effect

    def code(**kwargs):
        result = original(**kwargs)
        if kind in ("outside", "sensitive"):
            (w.repo.path / ("other.txt" if kind == "outside" else ".env")).write_text("private")
        elif kind == "staged":
            git(w.repo.path, "add", "--", "initial.txt")
        elif kind == "branch":
            git(w.repo.path, "switch", "-c", "wrong")
        elif kind == "head":
            git(w.repo.path, "add", "--", "initial.txt")
            git(w.repo.path, "commit", "--no-gpg-sign", "-m", "Unexpected Codex commit")
        else:
            (w.repo.path / "initial.txt").write_text("initial\n")
        return result

    w.codex.side_effect = code
    result = run(w)
    assert not result.success
    w.codex.assert_called_once()
    w.tests.assert_not_called()
    w.repo.push_branch.assert_not_called()
    if kind == "head":
        assert w.store.get_commit("TASK-1").commit_sha == w.repo.current_commit_sha()
    if kind == "no_changes":
        assert "No valid changes to commit" in result.error
    if kind == "outside":
        assert "outside the approved file list" in result.error


@pytest.mark.parametrize("decision", ["REJECTED", "UNCERTAIN", "invalid", "quota"])
def test_review_nonapproval_stops_and_preserves_pushed_commit(workflow, decision):
    w = workflow

    def reply(**arguments):
        raw = w.reply(**arguments)
        completion = raw.parse()
        data = json.loads(completion.choices[0].message.content)
        if decision == "quota":
            raise groq.RateLimitError("private", response=httpx.Response(
                429, request=httpx.Request("POST", "https://api.groq.com")
            ), body={"error": {"code": "insufficient_quota"}})
        data["decision"] = decision if decision != "invalid" else "APPROVED"
        data["blocking_issues"] = ["Missing requirement"] if decision == "REJECTED" else []
        return raw_reply(SimpleNamespace(**data), data="{" if decision == "invalid" else data)

    w.client.chat.completions.with_raw_response.create.side_effect = reply
    result = run(w)
    assert not result.success and result.successful_tasks == 0, result
    saved = w.store.get_commit("TASK-1")
    assert saved.push_state == PushState.VERIFIED and saved.commit_sha == w.repo.current_commit_sha()
    assert w.store.get_task("TASK-1").status == (
        TaskStatus.REJECTED if decision == "REJECTED" else TaskStatus.AWAITING_REVIEW
    )
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    history = w.store.reviews.list("TASK-1")[0]
    assert history["decision"] == ("REJECTED" if decision == "REJECTED" else "UNCERTAIN")
    if decision == "quota":
        assert history["error"] == "quota_exhausted"
    assert not run(w).success  # No approval-seeking review or Codex retries.
    w.codex.assert_called_once()
    w.tests.assert_called_once()
    w.client.chat.completions.with_raw_response.create.assert_called_once()
    w.repo.push_branch.assert_called_once()


@pytest.mark.parametrize("kind", ["timeout", "wrong_sha", "remote_drift"])
def test_uncertain_push_is_never_repeated(workflow, kind):
    w = workflow
    push = w.repo.push_branch.side_effect

    def uncertain(*args):
        result = push(*args)
        if kind == "timeout":
            raise GitError("push", "timeout after actual local push")
        if kind == "remote_drift":
            git(w.repo.path, "push", "origin", f"{w.store.executions.get('TASK-1')['baseline_sha']}:refs/heads/other")
            return replace(result, branch="wrong")
        return replace(result, commit_sha="a" * 40)

    w.repo.push_branch.side_effect = uncertain
    first = run(w)
    assert not first.success and first.commit_sha == w.repo.current_commit_sha()
    assert w.store.get_commit("TASK-1").push_state == PushState.UNCERTAIN
    second = run(w)
    assert not second.success and second.stopped_stage == "reconciliation"
    assert "manual inspection" in second.error
    w.codex.assert_called_once()
    w.tests.assert_called_once()
    w.repo.push_branch.assert_called_once()
    w.client.chat.completions.with_raw_response.create.assert_not_called()


@pytest.mark.parametrize("interrupt_at", ["codex", "test", "commit", "push", "review", "finalize"])
def test_interrupt_and_restart_never_replay(workflow, monkeypatch, interrupt_at):
    w = workflow
    expected = {
        "codex": (w.codex, "side_effect"), "test": (w.tests, "side_effect"),
        "commit": (w.repo, "commit_files"), "push": (w.repo, "push_branch"),
        "review": (w.client.chat.completions.with_raw_response.create, "side_effect"),
        "finalize": (w.store, "finalize_plan_task"),
    }
    owner, attribute = expected[interrupt_at]
    original = getattr(owner, attribute)

    def interrupt(*args, **kwargs):
        if interrupt_at == "commit":
            original(*args, **kwargs)  # Commit exists in Git, not yet in SQLite.
        raise KeyboardInterrupt()

    monkeypatch.setattr(owner, attribute, interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(w)
    history = w.store.executions.get("TASK-1")
    assert history["recovery_required"] and history["stage"] == "stopped"
    before = w.codex.call_count
    monkeypatch.setattr(owner, attribute, original)
    result = run(w)
    assert not result.success and "manual inspection" in result.error
    assert w.codex.call_count == before
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    if interrupt_at == "commit":
        assert result.commit_sha == w.repo.current_commit_sha() != history["baseline_sha"]


def test_concurrent_workspace_attempt_does_not_claim_or_overwrite(workflow):
    w = workflow
    entered, release = Event(), Event()
    original = w.codex.side_effect

    def blocked(**kwargs):
        entered.set()
        assert release.wait(20)
        return original(**kwargs)

    w.codex.side_effect = blocked

    def winner():
        with TaskStore(w.database) as store:
            single = SingleTaskController(store, codex_runner=w.codex, test_runner=w.tests,
                                          repository_factory=lambda _: w.repo,
                                          lock_factory=w.single.lock_factory)
            return BatchController(store, single=single, reviewer_factory=w.batch.reviewer_factory).run(
                "BATCH", w.repo.path, max_tasks=1
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(winner)
        try:
            assert entered.wait(20)
            before = w.store.executions.get("TASK-1")
            loser = run(w, max_tasks=1)
            assert not loser.success and "busy" in loser.error.lower()
            assert w.store.executions.get("TASK-1") == before
        finally:
            release.set()
        assert future.result(timeout=30).success
    w.codex.assert_called_once()
    w.tests.assert_called_once()
    w.repo.push_branch.assert_called_once()


@pytest.mark.parametrize("kind", ["missing", "tampered", "status", "limits", "dependency"])
def test_invalid_or_unapproved_batch_is_never_executed(workflow, kind):
    w = workflow
    if kind == "missing":
        result = w.batch.run("not-imported", w.repo.path)
    elif kind == "status":
        w.store.update_status("TASK-2", TaskStatus.APPROVED)
        result = run(w)
    elif kind == "tampered":
        w.store._connection.execute("DROP TRIGGER frozen_task_plans_update")
        w.store._connection.execute("UPDATE task_plans SET fingerprint = ?", ("0" * 64,))
        w.store._connection.commit()
        result = run(w)
    else:
        data = definition()
        if kind == "dependency":
            data["tasks"][0]["dependencies"] = ["TASK-3"]
            with pytest.raises(PlanValidationError):
                validate_plan(data)
            return
        for task in data["tasks"]:
            task.pop("execution_limits")
            task["task_id"] += "-OLD"
            task["dependencies"] = [dep + "-OLD" for dep in task["dependencies"]]
        data["batch_id"] = "OLD"
        w.store.import_plan(data, approved=True)
        result = w.batch.run("OLD", w.repo.path)
    assert not result.success
    w.codex.assert_not_called()
    w.tests.assert_not_called()


def test_manual_status_reset_cannot_repeat_execution(workflow):
    w = workflow
    w.codex.side_effect = RuntimeError("failed")
    assert not run(w).success
    w.store.update_status("TASK-1", TaskStatus.QUEUED)
    assert not run(w).success
    w.codex.assert_called_once()


def test_reviewer_history_missing_or_incomplete_cannot_approve(workflow, monkeypatch):
    w = workflow
    finish = w.store.reviews.finish

    def corrupt(review_id, record):
        finish(review_id, {**record, "coverage_complete": False})

    monkeypatch.setattr(w.store.reviews, "finish", corrupt)
    result = run(w)
    assert not result.success
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED


def test_remote_drift_after_review_prevents_finalization(workflow, monkeypatch):
    w = workflow
    original = w.reply

    def drift(**arguments):
        reply = original(**arguments)
        git(w.repo.path, "remote", "remove", "origin")
        return reply

    w.client.chat.completions.with_raw_response.create.side_effect = drift
    result = run(w)
    assert not result.success
    assert w.store.get_task("TASK-1").status == TaskStatus.AWAITING_REVIEW
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED


def test_missing_key_prevents_claim(workflow):
    w = workflow
    w.batch.reviewer_factory = GroqReviewer
    result = run(w)
    assert not result.success and "GROQ_API_KEY" in result.error
    w.codex.assert_not_called()
    assert w.store.get_task("TASK-1").status == TaskStatus.QUEUED


def test_status_is_read_only_and_execution_requires_explicit_action(workflow, capsys, monkeypatch):
    w = workflow
    assert run(w, max_tasks=1).success
    w.codex.reset_mock()
    w.client.chat.completions.with_raw_response.create.reset_mock()
    before = w.database.read_bytes()
    assert main(["batch", "status", "--db", str(w.database)]) == 0
    output = capsys.readouterr().out
    assert "TASK-1: approved" in output and w.store.get_commit("TASK-1").commit_sha in output
    assert w.plan.fingerprint in output and "Implement task" not in output
    assert before == w.database.read_bytes()
    assert main(["batch", "run", "BATCH", "--db", str(w.database), "--repo", str(w.repo.path)]) == 1
    w.codex.assert_not_called()
    w.client.chat.completions.with_raw_response.create.assert_not_called()


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_success_limit_does_not_claim(workflow, value):
    assert not run(workflow, max_tasks=value).success
    workflow.codex.assert_not_called()


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), 86401, "120"])
def test_frozen_timeout_validation(value):
    data = definition()
    data["tasks"][0]["execution_limits"]["codex_timeout"] = value
    with pytest.raises(PlanValidationError):
        validate_plan(data)


def test_timeout_is_fingerprinted():
    data = definition()
    original = validate_plan(data).fingerprint
    data["tasks"][0]["execution_limits"]["test_timeout"] += 1
    assert validate_plan(data).fingerprint != original


def test_real_diff_chunking_in_batch_gate(workflow):
    w = workflow
    # Several distant hunks: no arbitrary line slicing, and all must approve.
    lines = [f"line {i}: " + "x" * 45 + "\n" for i in range(240)]
    (w.repo.path / "initial.txt").write_text("".join(lines), encoding="utf-8")
    git(w.repo.path, "add", "--", "initial.txt")
    git(w.repo.path, "commit", "--no-gpg-sign", "-m", "Seed separated hunks")

    def code(**kwargs):
        changed = lines.copy()
        for index in range(10, 230, 20):
            changed[index] = f"changed line {index}: " + "y" * 45 + "\n"
        (w.repo.path / "initial.txt").write_text("".join(changed), encoding="utf-8")
        return CodexResult(True, kwargs["model"], 0, None, "", "")

    w.codex.side_effect = code
    w.batch.review_config = ReviewConfig(request_budget=8500, max_review_tokens=120000)
    result = run(w, max_tasks=1)
    assert result.success, (result, w.store.reviews.list())
    review = w.store.reviews.list("TASK-1")[0]
    assert review["chunk_count"] > 1 and review["coverage_complete"]
    assert review["api_requests"] == len(review["chunks"]) == len(review["requests"])
    coverage = [section for chunk in review["chunks"] for section in chunk["coverage"]]
    assert len(coverage) == len(set(coverage)) == 11
    assert all(request["decision"] == "APPROVED" for request in review["requests"])
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED


def test_lock_is_held_through_review_and_finalization(workflow, monkeypatch):
    w = workflow
    reply = w.reply
    finalize = w.store.finalize_plan_task

    def assert_busy():
        with TaskStore(w.database) as competing_store:
            single = SingleTaskController(
                competing_store, codex_runner=Mock(), test_runner=Mock(),
                repository_factory=lambda _: w.repo, lock_factory=w.single.lock_factory,
            )
            other = BatchController(competing_store, single=single, reviewer_factory=w.batch.reviewer_factory)
            result = other.run("BATCH", w.repo.path)
            assert not result.success and "busy" in result.error.lower()
            single.codex_runner.assert_not_called()

    def locked_review(**kwargs):
        assert_busy()
        return reply(**kwargs)

    def locked_finalize(*args, **kwargs):
        assert_busy()
        return finalize(*args, **kwargs)

    w.client.chat.completions.with_raw_response.create.side_effect = locked_review
    monkeypatch.setattr(w.store, "finalize_plan_task", locked_finalize)
    assert run(w, max_tasks=1).success


@pytest.mark.parametrize("stage", ["claimed", "codex_started", "codex_finished", "tests_completed",
                                  "commit_started", "push_verified", "review_started", "review_finished"])
def test_hard_crash_checkpoint_is_not_replayed(workflow, stage):
    w = workflow
    baseline = w.repo.current_commit_sha()
    w.store.bind_batch(w.plan, str(w.repo.path), hashlib.sha256(
        w.repo.verify_origin().encode("utf-8")
    ).hexdigest(), baseline)
    assert w.store.claim_plan_task(w.plan, "TASK-1", baseline)
    if stage != "claimed":
        w.store.executions.checkpoint("TASK-1", stage)
    # No finally/exception checkpoint is available after a hard process death.
    result = run(w)
    assert not result.success and result.stopped_stage == "reconciliation"
    assert w.store.executions.get("TASK-1")["stage"] == stage
    w.codex.assert_not_called()
    w.tests.assert_not_called()
    w.repo.push_branch.assert_not_called()


def test_atomic_plan_claim_between_connections(workflow):
    w = workflow
    baseline = w.repo.current_commit_sha()
    w.store.bind_batch(w.plan, str(w.repo.path), "hashed-origin", baseline)
    barrier = Barrier(2)

    def claim():
        with TaskStore(w.database) as store:
            barrier.wait(timeout=10)
            return store.claim_plan_task(w.plan, "TASK-1", baseline)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(results) == [False, True]
    assert w.store.get_task("TASK-1").status == TaskStatus.RUNNING
    assert w.store.executions.get("TASK-1")["stage"] == "claimed"
    w.codex.assert_not_called()


def test_claim_checks_dependencies_even_without_batch_loop(workflow):
    w = workflow
    w.store.bind_batch(w.plan, str(w.repo.path), "hashed-origin", w.repo.current_commit_sha())
    with pytest.raises(ValueError, match="Earlier tasks"):
        w.store.claim_plan_task(w.plan, "TASK-2", w.repo.current_commit_sha())
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED
    assert w.store.executions.get("TASK-2") is None


@pytest.mark.parametrize("operation", ["record_commit", "complete_push", "finalize_plan_task"])
def test_persistence_failure_stops_without_duplicate_work(workflow, monkeypatch, operation):
    w = workflow
    monkeypatch.setattr(w.store, operation, Mock(side_effect=sqlite_error()))
    result = run(w)
    assert not result.success and w.repo.current_commit_sha() != w.store.executions.get("TASK-1")["baseline_sha"]
    assert w.store.executions.get("TASK-1")["local_sha"] == w.repo.current_commit_sha()
    assert not run(w).success
    w.codex.assert_called_once()
    w.tests.assert_called_once()
    assert w.store.get_task("TASK-2").status == TaskStatus.QUEUED


def sqlite_error():
    import sqlite3
    return sqlite3.OperationalError("private database detail")


def test_status_missing_database_creates_nothing(tmp_path, capsys):
    path = tmp_path / "missing" / "tasks.sqlite3"
    assert main(["batch", "status", "--db", str(path)]) == 1
    assert not path.parent.exists()
    assert "failed" in capsys.readouterr().err


def test_no_model_or_test_output_is_logged_or_secret_persisted(workflow, caplog):
    w = workflow
    caplog.set_level(logging.INFO)
    original = w.codex.side_effect

    def code(**kwargs):
        result = original(**kwargs)
        return replace(result, stdout="private session", stderr="private session", final_response="private prompt")

    w.codex.side_effect = code
    w.tests.side_effect = lambda **kwargs: RunResult(True, (
        CommandResult(tuple(kwargs["commands"][0]), 0, "Bearer " + "sensitive" * 4, "", .1, True),
    ), None, .1)
    # Sensitive test evidence must stop the review, not be silently approved.
    assert not run(w, max_tasks=1).success
    w.client.chat.completions.with_raw_response.create.assert_not_called()
    history = json.dumps(w.store.executions.get("TASK-1"))
    assert "private session" not in history + caplog.text
    assert "private prompt" not in history + caplog.text
    assert "sensitive" * 4 not in history + caplog.text
