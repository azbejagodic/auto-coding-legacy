"""Command-line interface for the auto-coding foundation."""

from __future__ import annotations

import argparse
import sys

from .diagnostics import run_doctor
from .logging_config import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-coding")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="check required local tools")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        results = run_doctor()
        for result in results:
            print(result.format())
        return 0 if all(result.available for result in results) else 1

    configure_logging()
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

