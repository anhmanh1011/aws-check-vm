"""Tests for the rich live dashboard."""

import asyncio
import io
import time

from rich.console import Console

from dashboard import Dashboard
from models import RunState, Task, WorkerStatus


def _state() -> RunState:
    state = RunState(total=3, completed=1, ok=1, failed=0)
    state.workers[1] = WorkerStatus(worker_id=1)
    state.workers[2] = WorkerStatus(
        worker_id=2,
        task=Task(id=2, url="http://127.0.0.1/ip", name="task-2"),
        proxy="http://***:***@proxy.example:8080",
        state="running",
        started_at=time.monotonic() - 1.5,
    )
    return state


def _render_to_text(state: RunState) -> str:
    console = Console(record=True, width=120, file=io.StringIO(), force_terminal=False)
    dashboard = Dashboard(state, console=console)
    console.print(dashboard.render())
    return console.export_text()


def test_render_shows_progress_header():
    text = _render_to_text(_state())
    assert "1/3" in text
    assert "ok 1" in text
    assert "failed 0" in text


def test_render_shows_one_row_per_worker_with_masked_proxy():
    text = _render_to_text(_state())
    assert "task-2" in text
    assert "http://***:***@proxy.example:8080" in text
    assert "running" in text
    assert "idle" in text
    assert "direct" in text  # worker 1 has no proxy


def test_render_shows_running_duration_and_dash_for_idle():
    text = _render_to_text(_state())
    assert "1." in text  # ~1.5s elapsed for worker 2
    assert "-" in text


async def test_run_until_exits_when_event_is_set():
    console = Console(file=io.StringIO(), force_terminal=False, width=100)
    dashboard = Dashboard(_state(), console=console, refresh_per_second=20)
    done = asyncio.Event()

    async def finish_soon():
        await asyncio.sleep(0.2)
        done.set()

    await asyncio.wait_for(asyncio.gather(dashboard.run_until(done), finish_soon()), timeout=5)
    assert done.is_set()
