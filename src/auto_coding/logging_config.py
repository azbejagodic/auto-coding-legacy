"""Application logging setup."""

from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(log_directory: str | Path = "logs", level: int = logging.INFO) -> None:
    """Configure console and file logging without changing system configuration."""
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(directory / "auto-coding.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(console)
    root.addHandler(file_handler)

