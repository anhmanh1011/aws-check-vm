"""Load tasks from ``tasks.txt`` (one URL per line) or ``tasks.json``.

WHY two formats: a plain text file is the fastest way to try the tool, while
JSON lets a task carry a friendly ``name`` (and, if you extend the framework,
any extra parameters your handler needs). Both produce the same ``Task``
objects, so nothing downstream cares which one was used.
"""

from __future__ import annotations

import json
from pathlib import Path

from models import Task


def load_tasks(path: Path) -> list[Task]:
    """Read a task file and return 1-indexed ``Task`` objects.

    Raises ``FileNotFoundError`` if the file is missing and ``ValueError`` for
    an unsupported extension, malformed content, or an empty task list.
    """
    if not path.exists():
        raise FileNotFoundError(f"task file not found: {path}")

    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".txt":
        entries = _parse_txt(text)
    elif suffix == ".json":
        entries = _parse_json(text)
    else:
        raise ValueError(f"unsupported task file type {suffix!r} (use .txt or .json)")

    if not entries:
        raise ValueError(f"no tasks found in {path}")

    return [
        Task(id=index, url=url, name=name)
        for index, (url, name) in enumerate(entries, start=1)
    ]


def _parse_txt(text: str) -> list[tuple[str, str]]:
    """One URL per line; blank lines and ``#`` comments are ignored."""
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append((stripped, stripped))
    return entries


def _parse_json(text: str) -> list[tuple[str, str]]:
    """A JSON list of URL strings or ``{"url": ..., "name"?: ...}`` objects."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in task file: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("task JSON must be a list of URLs or objects with a 'url' key")

    entries: list[tuple[str, str]] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            entries.append((item, item))
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            url = item["url"]
            entries.append((url, str(item.get("name") or url)))
        else:
            raise ValueError(
                f"task #{index} must be a URL string or an object with a string 'url' key"
            )
    return entries
