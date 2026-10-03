"""Live terminal dashboard built on ``rich.live.Live``.

WHY a pull model: workers never call the dashboard. They mutate the shared
``RunState`` and the dashboard re-renders it a few times per second. That
keeps the engine free of any UI dependency, and because asyncio is
single-threaded there is no race between the writer and the reader.
"""

from __future__ import annotations

import asyncio
import time

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from models import RunState


class Dashboard:
    """Renders ``RunState`` as a header line plus one table row per worker."""

    def __init__(
        self,
        state: RunState,
        console: Console | None = None,
        refresh_per_second: float = 4.0,
    ) -> None:
        self._state = state
        self._console = console or Console()
        self._refresh_per_second = refresh_per_second
        self._started = time.monotonic()

    def render(self) -> Group:
        """Build the renderable for the current state (pure function of state + clock)."""
        state = self._state
        now = time.monotonic()
        elapsed = now - self._started
        header = Text(
            f"Completed {state.completed}/{state.total}   "
            f"ok {state.ok}   failed {state.failed}   elapsed {elapsed:5.1f}s",
            style="bold",
        )

        table = Table(title="Workers", expand=True)
        table.add_column("Worker", justify="right", no_wrap=True)
        table.add_column("Task", overflow="fold")
        table.add_column("Proxy", overflow="fold")
        table.add_column("Status", no_wrap=True)
        table.add_column("Duration", justify="right", no_wrap=True)

        for worker_id in sorted(state.workers):
            worker = state.workers[worker_id]
            duration = f"{now - worker.started_at:.1f}s" if worker.started_at is not None else "-"
            style = {"running": "yellow", "done": "green", "idle": "dim"}[worker.state]
            table.add_row(
                str(worker_id),
                worker.task.name if worker.task else "-",
                worker.proxy or "direct",
                Text(worker.state, style=style),
                duration,
            )
        return Group(header, table)

    async def run_until(self, done: asyncio.Event) -> None:
        """Redraw until ``done`` is set, then draw one final frame."""
        interval = 1.0 / self._refresh_per_second
        with Live(
            self.render(),
            console=self._console,
            refresh_per_second=self._refresh_per_second,
            transient=False,
        ) as live:
            while not done.is_set():
                live.update(self.render())
                await asyncio.sleep(interval)
            live.update(self.render())
