#!/usr/bin/env python3
"""Logging helpers shared by ClipCopy entrypoints."""

from __future__ import annotations

import logging


LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    """Configure a process-wide console logger once."""
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
    )


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger after ensuring logging is configured."""
    configure_logging()
    return logging.getLogger(name)
