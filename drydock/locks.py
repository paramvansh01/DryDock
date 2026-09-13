"""Cross-process serialisation. Merges can be triggered by the Drydock MCP
server (auto-merge through the gate) and by the orchestrator (human approval);
both run on one host, so an fcntl lock file serialises every write to GOLDEN.
Exasol has no advisory locks, and two concurrent stage-then-swaps on the same
table would each stage from a pre-swap image."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import SETTINGS


@contextmanager
def file_lock(name: str) -> Iterator[None]:
    d = Path(SETTINGS.lock_dir)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / f"{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def golden_write_lock():
    return file_lock("golden-write")
