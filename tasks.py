"""Turn an input file into batches of work.

The input file is plain text: one data item per line (an email, a URL, an
account ID, ...). Blank lines and ``#`` comments are ignored. The framework
never interprets the content; your flow receives the raw lines in
``task.lines`` and decides which site to open and what to click.

WHY batches: opening a fresh browser context per item is the most isolated
option, but when one flow can process several items in a row (log in once,
then handle ten emails) it is wasteful. ``--batch-size`` groups lines so one
context handles one batch:

* ``1``  (default)  one line per context, maximum isolation.
* ``N``             fixed batches of N lines; the queue hands batches to
                    whichever worker is free, so 1000 lines with N=10 become
                    100 batches spread over ``--concurrency`` workers.
* ``0``  (auto)     split evenly so every worker gets exactly one batch of
                    ``ceil(total / concurrency)`` lines.
"""

from __future__ import annotations

import math
from pathlib import Path

from models import Task


def load_lines(path: Path) -> list[str]:
    """Read the input file and return its non-empty, non-comment lines.

    Raises ``FileNotFoundError`` if the file is missing and ``ValueError`` if
    no usable lines remain.
    """
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")

    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)

    if not lines:
        raise ValueError(f"no input lines found in {path}")
    return lines


def chunk_lines(lines: list[str], *, batch_size: int, concurrency: int) -> list[list[str]]:
    """Group ``lines`` into batches; see the module docstring for the modes.

    ``batch_size=0`` means auto: ``ceil(len(lines) / concurrency)`` per batch,
    which yields at most ``concurrency`` batches and never an empty one.
    """
    if batch_size < 0:
        raise ValueError(f"batch_size must be >= 0, got {batch_size}")
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    if batch_size == 0:
        batch_size = max(1, math.ceil(len(lines) / concurrency))

    return [lines[i:i + batch_size] for i in range(0, len(lines), batch_size)]


def make_tasks(batches: list[list[str]]) -> list[Task]:
    """Wrap each batch in a 1-indexed ``Task`` with a dashboard-friendly name."""
    tasks: list[Task] = []
    for index, batch in enumerate(batches, start=1):
        name = batch[0] if len(batch) == 1 else f"{batch[0]} (+{len(batch) - 1} more)"
        tasks.append(Task(id=index, lines=tuple(batch), name=name))
    return tasks


def load_tasks(path: Path, *, batch_size: int = 1, concurrency: int = 1) -> list[Task]:
    """Read ``path`` and return batched ``Task`` objects ready for the engine."""
    lines = load_lines(path)
    batches = chunk_lines(lines, batch_size=batch_size, concurrency=concurrency)
    return make_tasks(batches)
