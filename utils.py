"""Cross-cutting helpers: result export, logging setup, credential masking.

WHY logging goes to a file while the dashboard runs: ``rich.live.Live``
redraws a region of the terminal several times a second. Any other writer to
stderr would be painted over or would corrupt the table, so in dashboard mode
log records are appended to ``run.log`` instead. Without the dashboard, a
``RichHandler`` prints readable, colourised log lines.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, fields
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from rich.logging import RichHandler

from models import TaskResult


def mask_credentials(url: str) -> str:
    """Replace ``user:pass`` in a URL with ``***:***``; return other URLs unchanged.

    This is a helper for user-written handlers that log task URLs themselves;
    the engine does not call it to mask proxies -- that goes through
    ``Proxy.masked()`` instead.
    """
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, parts.query, parts.fragment))


def write_results(results: Sequence[TaskResult], path: Path) -> None:
    """Write results as JSON (``.json``) or CSV (``.csv``) based on the extension.

    In CSV mode the nested ``data`` dict is serialised to a JSON string so the
    file stays a flat table with one row per task.
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        path.write_text(
            json.dumps([asdict(result) for result in results], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    elif suffix == ".csv":
        fieldnames = [field.name for field in fields(TaskResult)]
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for result in results:
                row = asdict(result)
                row["data"] = json.dumps(row["data"], ensure_ascii=False)
                writer.writerow(row)
    else:
        raise ValueError(f"unsupported output extension {suffix!r} (use .json or .csv)")


# The per-email status strings check_VM records; everything else is an error.
_EXISTS_STATUS = "account_exists"
_NOT_FOUND_STATUS = "account_not_found"


def _append_lines(path: Path, lines: list[str]) -> None:
    """Append ``lines`` (newline-terminated) to ``path``; do nothing when empty."""
    if not lines:
        return
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_split_outputs(results: Sequence[TaskResult], directory: Path) -> None:
    """Append each email to one of three files by outcome (the ``check_VM`` convention).

    * ``exists.txt``    -- email per line, status ``account_exists``
    * ``not_found.txt`` -- email per line, status ``account_not_found``
    * ``error.txt``     -- ``email<TAB>reason`` for a CAPTCHA failure, an unknown
      page, or every email of a task that ``failed`` / timed out.

    Files are opened in append mode, so repeated runs accumulate instead of
    overwriting. A task is read from ``data["results"]`` (check_VM's per-email
    list); a task without that list (it failed before producing one) sends all
    of its ``inputs`` to ``error.txt`` with the task status as the reason.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    exists: list[str] = []
    not_found: list[str] = []
    errors: list[str] = []

    for result in results:
        rows = (
            result.data.get("results")
            if result.status == "ok" and isinstance(result.data, dict)
            else None
        )
        if rows:
            for entry in rows:
                email = str(entry.get("email", ""))
                status = str(entry.get("status", "")) or "unknown"
                if status == _EXISTS_STATUS:
                    exists.append(email)
                elif status == _NOT_FOUND_STATUS:
                    not_found.append(email)
                else:
                    errors.append(f"{email}\t{status}")
        else:
            reason = result.status if result.status != "ok" else "unknown"
            errors.extend(f"{email}\t{reason}" for email in result.inputs)

    _append_lines(directory / "exists.txt", exists)
    _append_lines(directory / "not_found.txt", not_found)
    _append_lines(directory / "error.txt", errors)


def setup_logging(
    *, verbose: bool, dashboard_active: bool, log_file: Path = Path("run.log")
) -> None:
    """Configure the root logger exactly once per process.

    ``dashboard_active=True`` sends records to ``log_file`` so they do not
    fight with the live table; otherwise records go to the terminal through
    ``RichHandler``.
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)

    handler: logging.Handler
    if dashboard_active:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    else:
        handler = RichHandler(show_path=False, rich_tracebacks=False)
        handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
