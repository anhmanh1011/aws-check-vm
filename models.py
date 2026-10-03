"""Shared data types for the automation framework.

WHY a separate module: ``tasks.py`` needs ``Task``, ``proxy_manager.py`` needs
``Proxy``, and ``engine.py`` / ``dashboard.py`` need all of them. Keeping the
types here means every module imports from one place and there are no
circular imports. Dataclasses with ``slots=True`` are cheap to create, which
matters because we build one ``TaskResult`` per task and one ``Proxy`` per
proxy line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

Status = Literal["ok", "failed", "timeout"]
WorkerState = Literal["idle", "running", "done"]


@dataclass(frozen=True, slots=True)
class Task:
    """One unit of work: a URL plus a human-readable name.

    ``id`` is the 1-based position in the task file so results can be
    correlated back to the input.
    """

    id: int
    url: str
    name: str


@dataclass(frozen=True, slots=True)
class Proxy:
    """A parsed proxy endpoint.

    ``server`` never carries credentials; they live in ``username`` /
    ``password`` so the masked form can be produced without re-parsing.
    """

    server: str
    username: str | None = None
    password: str | None = None

    def to_playwright(self) -> dict[str, str]:
        """Return the dict shape Playwright expects for ``new_context(proxy=...)``."""
        options: dict[str, str] = {"server": self.server}
        if self.username is not None:
            options["username"] = self.username
        if self.password is not None:
            options["password"] = self.password
        return options

    def masked(self) -> str:
        """Return a log-safe representation with credentials replaced by ``***``."""
        if self.username is None and self.password is None:
            return self.server
        parts = urlsplit(self.server)
        # WHY hostname/port, not netloc: if ``server`` itself embeds
        # ``user:pass@`` (rather than carrying them separately in
        # ``self.username``/``self.password``), ``netloc`` would include that
        # real, unmasked credential pair verbatim alongside our ``***:***``.
        # Rebuilding from ``hostname``/``port`` drops any such embedded
        # credentials unconditionally.
        host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
        return f"{parts.scheme}://***:***@{host}"


@dataclass(slots=True)
class TaskResult:
    """Outcome of running one task inside one browser context."""

    task_id: int
    name: str
    url: str
    worker_id: int
    proxy: str | None
    status: Status
    started_at: str
    duration_s: float
    data: dict[str, Any]
    error: str | None


@dataclass(slots=True)
class WorkerStatus:
    """Live view of one worker, read by the dashboard."""

    worker_id: int
    task: Task | None = None
    proxy: str | None = None
    state: WorkerState = "idle"
    started_at: float | None = None


@dataclass(slots=True)
class RunState:
    """Mutable shared state for a whole run.

    WHY no locks: asyncio runs all coroutines on one thread, and control only
    switches at ``await`` points. Workers mutate this object between awaits and
    the dashboard reads it between awaits, so plain attribute access is safe.
    """

    total: int
    completed: int = 0
    ok: int = 0
    failed: int = 0
    workers: dict[int, WorkerStatus] = field(default_factory=dict)
    results: list[TaskResult] = field(default_factory=list)
