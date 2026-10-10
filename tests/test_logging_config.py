import logging

from auto_coding.logging_config import configure_logging


def test_configure_logging_creates_log_file(tmp_path):
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    root.handlers.clear()
    try:
        configure_logging(tmp_path)
        assert (tmp_path / "auto-coding.log").exists()
        assert len(root.handlers) == 2
    finally:
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        root.handlers.extend(old_handlers)

