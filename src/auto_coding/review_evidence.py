"""Build a review packet from persisted approval, real Git and Python tests."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path

from .integrations.git import GitRepository
from .reviews import FileChange, ReviewError, ReviewPacket, validate_packet
from .storage import TaskStatus, TaskStore
from .test_runner import TestRunResult


def build_review_packet(
    store: TaskStore, task_id: str, repository_path: str | Path,
    commit_sha: str, test_results: TestRunResult | None, *,
    tests_commit_sha: str | None, safety_passed: bool,
) -> ReviewPacket:
    """Caller supplies the actual Python runner result and its tested SHA.

    This does not execute tests or derive test results from model statements.
    The caller is responsible for test provenance and independent safety checks.
    """
    task = store.get_task(task_id)
    commit = store.get_commit(task_id)
    if task is None or commit is None or commit.commit_sha != commit_sha:
        raise ReviewError("Task and persisted commit evidence do not match")
    if task.status != TaskStatus.AWAITING_REVIEW:
        raise ReviewError("Task is not awaiting review")
    if test_results is not None and not isinstance(test_results, TestRunResult):
        raise ReviewError("Actual Python TestRunResult evidence is required")
    approved = store.get_task_plan(task_id)
    fingerprint = None
    if approved is not None:
        plan, definition = approved
        fingerprint = plan.fingerprint
        if plan.to_dict()["expected_branch"] != commit.branch:
            raise ReviewError("Frozen plan branch differs from the commit")
        if test_results is not None:
            actual = [list(command.arguments) for command in test_results.commands]
            if actual != definition["test_commands"]:
                raise ReviewError("Tests differ from the frozen approved commands")
    changes = GitRepository(repository_path).review_changes(commit.branch, commit_sha)
    if approved is not None:
        scope = {os.path.normcase(path.replace("\\", "/")) for path in definition["permitted_files"]}
        if any(os.path.normcase(path.replace("\\", "/")) not in scope
               for change in changes for path in (change.path, change.old_path) if path is not None):
            raise ReviewError("Commit changes exceed the frozen permitted files")
    tests = None if test_results is None else {
        **asdict(test_results), "commit_sha": tests_commit_sha,
    }
    packet = ReviewPacket(
        task_id, commit_sha, commit.branch, task.prompt, tuple(task.acceptance_criteria),
        tuple(FileChange(c.path, c.change_type, c.patch, c.old_path) for c in changes),
        tests, safety_passed, plan_fingerprint=fingerprint,
        approved_configuration=None if approved is None else {
            key: value for key, value in definition.items()
            if key not in ("prompt", "title", "acceptance_criteria")
        },
    )
    validate_packet(packet)
    return packet
