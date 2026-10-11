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

    configure_logging()
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

