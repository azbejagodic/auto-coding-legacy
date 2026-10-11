"""Command-line interface for the auto-coding foundation."""

from __future__ import annotations

import argparse
import sqlite3
import sys

from .diagnostics import run_doctor
from .logging_config import configure_logging
from .plans import PlanConflictError, PlanValidationError, load_plan
from .storage import TaskStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-coding")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="check required local tools")
    plan = subparsers.add_parser("plan", help="validate or import an offline task plan")
    commands = plan.add_subparsers(dest="plan_command", required=True)
    validate = commands.add_parser("validate", help="read-only task-plan validation")
    validate.add_argument("path")
    importer = commands.add_parser("import", help="freeze a task plan in SQLite")
    importer.add_argument("path")
    importer.add_argument("--db", required=True)
    importer.add_argument("--approve", action="store_true", help="explicitly approve this exact local plan")
    batch = subparsers.add_parser("batch", help="inspect or explicitly execute approved batches")
    batches = batch.add_subparsers(dest="batch_command", required=True)
    status = batches.add_parser("status", help="read-only local batch status; no model calls")
    status.add_argument("--db", required=True)
    status.add_argument("--batch")
    run = batches.add_parser("run", help="execute a frozen batch (real model usage, commits and pushes)")
    run.add_argument("batch_id")
    run.add_argument("--db", required=True)
    run.add_argument("--repo", required=True)
    run.add_argument("--max-tasks", type=int)
    run.add_argument("--review-budget", type=int, default=12000,
                     help="conservative per-request wire budget including reserved output")
    run.add_argument("--review-token-limit", type=int, default=30000,
                     help="maximum conservative total budget per review")
    run.add_argument("--execute", action="store_true", help="explicitly authorize real execution")
    retry = batches.add_parser("review-retry", help="preflight or explicitly retry one technical review stop")
    retry.add_argument("batch_id")
    retry.add_argument("--db", required=True)
    retry.add_argument("--repo", required=True)
    retry.add_argument("--task", required=True)
    retry.add_argument("--review-budget", type=int, default=12000)
    retry.add_argument("--review-token-limit", type=int, default=30000)
    retry.add_argument("--execute", action="store_true", help="authorize one review only; never coding or push")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        results = run_doctor()
        for result in results:
            print(result.format())
        return 0 if all(result.available for result in results) else 1

    if args.command == "plan":
        if args.plan_command == "import" and not args.approve:
            print("Import requires explicit --approve; nothing was imported.", file=sys.stderr)
            return 1
        try:
            plan = load_plan(args.path)  # Always validate before opening/creating a database.
            if args.plan_command == "import":
                with TaskStore(args.db) as store:
                    store.import_plan(plan, approved=True)
            verb = "Imported" if args.plan_command == "import" else "Validated"
            print(f"{verb} batch {plan.batch_id}: {plan.task_count} tasks; SHA-256 {plan.fingerprint}")
            return 0
        except (PlanValidationError, PlanConflictError) as exc:
            print(f"Plan rejected: {exc}", file=sys.stderr)
        except (OSError, sqlite3.Error):
            print("Plan database operation failed; no batch was imported.", file=sys.stderr)
        return 1

    if args.command == "batch":
        if args.batch_command == "run" and not args.execute:
            print("Execution requires --execute; no task was started.", file=sys.stderr)
            return 1
        try:
            if args.batch_command == "status":
                with TaskStore(args.db, read_only=True) as store:
                    plans = [store.get_plan(args.batch)] if args.batch else store.list_plans()
                    if any(plan is None for plan in plans):
                        raise ValueError("Approved batch not found")
                    for plan in plans:
                        print(f"Batch {plan.batch_id}: {plan.task_count} tasks; SHA-256 {plan.fingerprint}")
                        store.validate_plan_tasks(plan)
                        tasks = store.list_plan_tasks(plan.batch_id)
                        completed = [task.task_id for task in tasks if task.status == "approved"]
                        active = [task.task_id for task in tasks if task.status not in ("queued", "approved")]
                        print(f"  Current/stopped={active[0] if active else '-'}; "
                              f"last completed={completed[-1] if completed else '-'}")
                        for task in tasks:
                            execution = store.executions.get(task.task_id) or {}
                            commit = store.get_commit(task.task_id)
                            review = (store.reviews.get(execution["review_id"]) or {}
                                      if execution.get("review_id") else {})
                            print(f"  {task.task_id}: {task.status}; stage={execution.get('stage', 'not_started')}; "
                                  f"SHA={commit.commit_sha if commit else '-'}; review={review.get('decision', '-')}")
                            if execution.get("recovery_required") and execution:
                                print(f"    Inspection required; last/failing stage="
                                      f"{execution.get('failure_stage') or execution.get('stage')}")
                                if execution.get("error"):
                                    print(f"    {execution['error']}")
                return 0
            from .batch_controller import BatchController
            from .reviews import ReviewConfig
            config = ReviewConfig(request_budget=args.review_budget, max_review_tokens=args.review_token_limit)
            if args.batch_command == "review-retry":
                # Inspect an existing database read-only even when execution was
                # requested. No migration/write precedes the verified preflight.
                with TaskStore(args.db, read_only=True) as store:
                    preflight = BatchController(store, review_config=config).review_retry(
                        args.batch_id, args.repo, task_id=args.task,
                    )
                if not args.execute or not preflight.success:
                    result = preflight
                else:
                    with TaskStore(args.db) as store:
                        result = BatchController(store, review_config=config).review_retry(
                            args.batch_id, args.repo, task_id=args.task, execute=True,
                        )
                print(f"Review recovery {'attempt' if result.executed else 'preflight'}: "
                      f"batch={result.batch_id}; task={result.task_id}; "
                      f"{'approved' if result.executed and result.success else 'eligible' if result.success else 'blocked'}; "
                      f"SHA={result.commit_sha or '-'}; original review={result.original_review_id or '-'}; "
                      f"new review={result.review_id or '-'}; decision={result.decision or '-'}")
                if result.error:
                    print(result.error, file=sys.stderr)
                return 0 if result.success else 1
            with TaskStore(args.db) as store:
                result = BatchController(store, review_config=config).run(
                    args.batch_id, args.repo, max_tasks=args.max_tasks
                )
            print(f"Batch {result.batch_id}: {'completed' if result.completed else 'stopped'}; "
                  f"successful tasks this run={result.successful_tasks}; current={result.current_task or '-'}; "
                  f"last completed={result.last_completed_task or '-'}; SHA={result.commit_sha or '-'}; "
                  f"review={result.review_status or '-'}; stage={result.stopped_stage or '-'}")
            if result.error:
                print(result.error, file=sys.stderr)
            return 0 if result.success else 1
        except KeyboardInterrupt:
            print("Interrupted; inspect batch status and workspace before recovery.", file=sys.stderr)
            return 130
        except (ValueError, OSError, sqlite3.Error):
            print("Batch operation failed; check database identity, integrity and permissions.", file=sys.stderr)
            return 1

    configure_logging()
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

