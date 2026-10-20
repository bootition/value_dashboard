from __future__ import annotations

import logging

from app.cli import main as cli_main


def test_configure_update_logging_installs_info_handler_once() -> None:
    """自动更新子进程必须把 INFO 级日志落盘，否则更新过程不可见。"""
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    root.handlers.clear()
    try:
        cli_main._configure_update_logging()
        assert root.handlers, "auto-update child must install a logging handler"
        assert root.level == logging.INFO
        assert isinstance(root.handlers[0], logging.StreamHandler)

        installed = list(root.handlers)
        cli_main._configure_update_logging()
        assert root.handlers == installed, "logging setup must be idempotent"
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
