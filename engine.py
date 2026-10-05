"""The concurrency core: one Browser, N workers, one BrowserContext per task.

Pattern summary
---------------
* **One ``Browser``.** Launching Chromium costs seconds and hundreds of MB.
  We pay that once per run.
* **One ``BrowserContext`` per task.** A context is Playwright's isolation
  unit: it owns its cookies, localStorage, cache, and proxy. Creating one
  costs tens of milliseconds, so giving every task a pristine identity is
  cheap. We destroy it afterwards so nothing leaks to the next task.
* **A fixed pool of worker coroutines fed by ``asyncio.Queue``.** Exactly
  ``concurrency`` workers exist, so at most that many contexts are alive at
  once. Workers pull tasks until they receive a ``None`` sentinel. This is
  the classic producer/consumer shape and gives each worker a stable id for
  the dashboard.
* **Failures are data, not exceptions.** Every per-task error is captured in
  a ``TaskResult`` and the worker moves on. One dead proxy must never stop
  the other workers.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Browser, BrowserContext, async_playwright

from handlers import Handler
from models import Proxy, RunState, Status, Task, TaskResult, WorkerStatus
from proxy_manager import ProxyManager

log = logging.getLogger(__name__)

# Fallback screen size when it cannot be detected (non-Windows, or a headless
# host). 1920x1080 is the most common desktop resolution.
_DEFAULT_SCREEN = (1920, 1080)


def tile_bounds(index: int, count: int, screen_w: int, screen_h: int) -> dict[str, int]:
    """Return the ``{left, top, width, height}`` cell for window ``index`` of ``count``.

    The windows tile a ``screen_w`` x ``screen_h`` screen in a near-square grid
    (``ceil(sqrt(count))`` columns), so with ``--no-headless`` each worker's
    window sits in its own cell instead of stacking at the origin. ``index`` is
    taken modulo ``count`` so an out-of-range worker reuses a cell rather than
    landing off-screen; ``count`` of 0 is treated as 1 (one full-screen window).
    """
    count = max(count, 1)
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    index %= count
    width = screen_w // cols
    height = screen_h // rows
    return {
        "left": (index % cols) * width,
        "top": (index // cols) * height,
        "width": width,
        "height": height,
    }


def _detect_screen_size() -> tuple[int, int]:
    """Best-effort primary-screen size in pixels; falls back to ``_DEFAULT_SCREEN``.

    Uses the Win32 API on Windows (where this runs headed). ``ctypes.windll``
    does not exist on other platforms, so the ``except`` returns the default.
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        if width > 0 and height > 0:
            return width, height
    except Exception:  # noqa: BLE001 - detection is best-effort
        pass
    return _DEFAULT_SCREEN

# WHY: on Windows, Chromium only honours a per-context proxy when the browser
# itself was launched with a proxy. Playwright documents a placeholder server
# for exactly this situation; it is never actually contacted when every
# context overrides it.
_PER_CONTEXT_PROXY_PLACEHOLDER = {"server": "http://per-context"}


class AutomationEngine:
    """Runs every task through a bounded pool of workers sharing one browser."""

    def __init__(
        self,
        tasks: Sequence[Task],
        proxy_manager: ProxyManager,
        handler: Handler,
        *,
        concurrency: int,
        headless: bool,
        timeout_s: float,
        state: RunState,
        browser_type: str = "chromium",
        proxy_per_worker: bool = False,
        screen_size: tuple[int, int] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        self._tasks = list(tasks)
        self._proxy_manager = proxy_manager
        self._handler = handler
        self._concurrency = concurrency
        self._headless = headless
        self._timeout_s = timeout_s
        self._state = state
        self._browser_type = browser_type
        # When True, each worker is pinned to one proxy for the whole run
        # (worker i always uses proxy i-1) instead of pulling the next proxy in
        # the shared round-robin for every task. This keeps each browser "lane"
        # on a fixed IP -- the natural fit for one KiotProxy key per worker.
        self._proxy_per_worker = proxy_per_worker
        # Where to tile headed windows. Detected once here (cheap) and only
        # actually used when running non-headless.
        self._screen = screen_size or _detect_screen_size()

    async def run(self) -> list[TaskResult]:
        """Launch the browser, drain the task queue with N workers, close the browser."""
        queue: asyncio.Queue[Task | None] = asyncio.Queue()
        for task in self._tasks:
            queue.put_nowait(task)
        # One sentinel per worker: each worker exits after consuming exactly one.
        for _ in range(self._concurrency):
            queue.put_nowait(None)

        for worker_id in range(1, self._concurrency + 1):
            self._state.workers[worker_id] = WorkerStatus(worker_id=worker_id)

        async with async_playwright() as playwright:
            launcher = getattr(playwright, self._browser_type)
            launch_kwargs: dict[str, Any] = {"headless": self._headless}
            if len(self._proxy_manager) > 0:
                launch_kwargs["proxy"] = _PER_CONTEXT_PROXY_PLACEHOLDER
            browser: Browser = await launcher.launch(**launch_kwargs)
            log.info(
                "Launched %s (headless=%s) with %d workers for %d tasks",
                self._browser_type, self._headless, self._concurrency, len(self._tasks),
            )
            try:
                # WHY TaskGroup, not gather: if one worker raises, TaskGroup
                # cancels every sibling task and waits for them before
                # propagating, so the browser is never closed out from under a
                # still-running worker. ``asyncio.gather`` would instead let
                # the other workers keep executing concurrently with the
                # ``finally`` block's ``browser.close()`` below, racing their
                # ``new_context``/``goto`` calls against a driver that is
                # being torn down.
                async with asyncio.TaskGroup() as group:
                    for worker_id in range(1, self._concurrency + 1):
                        group.create_task(self._worker(worker_id, queue, browser))
            finally:
                # Closes the browser on success, on error (after TaskGroup has
                # finished cancelling and awaiting every worker), and on
                # cancellation (Ctrl+C). Chromium also dies on its own when the
                # Playwright driver process exits at the end of the
                # ``async_playwright()`` context, but closing it explicitly
                # here keeps teardown deterministic and immediate.
                #
                # WHY suppressed: on Ctrl+C, the console interrupt (SIGINT on
                # POSIX, CTRL_C_EVENT on Windows) is delivered to the whole
                # process group at once, so the Playwright driver subprocess
                # and the Chromium process it manages may already be dying or
                # gone by the time we get here, and ``close()`` can raise
                # ``TargetClosedError``. An exception raised inside this
                # ``finally`` would replace the ``CancelledError`` that is
                # already propagating, turning a clean exit code 130 into an
                # unrelated exit code 1.
                with suppress(Exception):
                    await browser.close()

        return list(self._state.results)

    async def _worker(
        self, worker_id: int, queue: asyncio.Queue[Task | None], browser: Browser
    ) -> None:
        """Consume tasks until the ``None`` sentinel arrives."""
        status = self._state.workers[worker_id]
        # In per-worker mode the proxy is fixed for this worker's whole life.
        pinned_proxy = (
            self._proxy_manager.at(worker_id - 1) if self._proxy_per_worker else None
        )
        while True:
            task = await queue.get()
            if task is None:
                status.state = "done"
                status.task = None
                status.proxy = None
                status.started_at = None
                log.debug("worker %d finished", worker_id)
                return

            proxy = pinned_proxy if self._proxy_per_worker else self._proxy_manager.next()
            status.task = task
            status.proxy = proxy.masked() if proxy else None
            status.state = "running"
            status.started_at = time.monotonic()
            log.info("worker %d -> task %d (%s) via %s",
                     worker_id, task.id, task.name, status.proxy or "direct")

            result = await self._run_one(worker_id, task, proxy, browser)

            self._state.results.append(result)
            self._state.completed += 1
            if result.status == "ok":
                self._state.ok += 1
            else:
                self._state.failed += 1
            status.state = "idle"
            status.task = None
            status.proxy = None
            status.started_at = None
            log.info("worker %d <- task %d %s in %.2fs%s",
                     worker_id, task.id, result.status, result.duration_s,
                     f": {result.error}" if result.error else "")

    async def _tile_window(
        self, context: BrowserContext, page: Any, worker_id: int
    ) -> None:
        """Move/resize this context's window into worker ``worker_id``'s grid cell.

        Chromium only. Uses CDP ``Browser.setWindowBounds``; any failure (an
        unsupported build, a window already gone) is swallowed at DEBUG because
        window placement must never turn a working task into a failed one.
        """
        bounds = tile_bounds(worker_id - 1, self._concurrency, *self._screen)
        try:
            cdp = await context.new_cdp_session(page)
            try:
                info = await cdp.send("Browser.getWindowForTarget")
                await cdp.send(
                    "Browser.setWindowBounds",
                    {"windowId": info["windowId"], "bounds": {"windowState": "normal", **bounds}},
                )
            finally:
                await cdp.detach()
        except Exception as exc:  # noqa: BLE001 - tiling is cosmetic
            log.debug("window tiling failed for worker %d: %s", worker_id, exc)

    async def _run_one(
        self, worker_id: int, task: Task, proxy: Proxy | None, browser: Browser
    ) -> TaskResult:
        """Run one task in a fresh context and always return a ``TaskResult``."""
        started_wall = datetime.now(timezone.utc)
        started = time.monotonic()
        context: BrowserContext | None = None
        status: Status = "failed"
        data: Any = {}
        error: str | None = None

        try:
            context = await browser.new_context(
                proxy=proxy.to_playwright() if proxy else None
            )
            page = await context.new_page()
            # Headed runs open one window per context; without this they stack
            # at the origin and hide each other. Tile each worker's window into
            # its own screen cell. Cosmetic and best-effort: never fail a task.
            if not self._headless and self._browser_type == "chromium":
                await self._tile_window(context, page, worker_id)
            # ``wait_for`` cancels the handler if it overruns; the ``finally``
            # below still closes the context. The handler runs as its own
            # Task (rather than a bare coroutine) so that, after ``wait_for``
            # raises, we can ask the task whether *it* was cancelled -- that's
            # what tells a real deadline-overrun apart from the handler racing
            # its own ``TimeoutError`` past the deadline (see below).
            handler_task = asyncio.create_task(self._handler(page, task))
            try:
                data = await asyncio.wait_for(handler_task, timeout=self._timeout_s)
            except TimeoutError:
                # WHY this isn't just ``except asyncio.TimeoutError``: on
                # Python 3.11+ ``asyncio.TimeoutError is TimeoutError``, so a
                # handler that raises a *builtin* ``TimeoutError`` of its own
                # (e.g. a socket read timeout) is indistinguishable from
                # ``wait_for``'s deadline by type alone. We disambiguate by
                # asking whether ``wait_for`` actually cancelled the handler
                # task: it only does that when its own deadline fires.
                if handler_task.cancelled():
                    # wait_for hit the deadline and cancelled the handler: a
                    # real overrun.
                    status, error = "timeout", f"handler exceeded {self._timeout_s}s"
                else:
                    # The handler raised its own TimeoutError before the
                    # deadline; re-raise so the outer ``except Exception``
                    # below records it like any other handler failure.
                    raise
            else:
                status = "ok"
        except Exception as exc:  # noqa: BLE001
            # WHY broad, and WHY this covers new_context/new_page too: Playwright
            # raises several classes (Error, TimeoutError, TargetClosedError) from
            # context/page setup as well as from the handler -- a proxy Playwright
            # rejects or a mid-run TargetClosedError at ``new_context`` must be
            # recorded exactly like a handler failure, never crash the worker (and
            # from there the whole TaskGroup). A dead proxy surfaces as a
            # navigation Error. All of them must be recorded, none may kill the
            # worker. CancelledError is a BaseException and is deliberately NOT
            # caught, so Ctrl+C still propagates.
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception as exc:  # noqa: BLE001
                    # Spec: a failure here is ignored (it never changes the
                    # task's recorded status/error) but logged at DEBUG so it
                    # is still visible when diagnosing teardown issues.
                    # CancelledError is a BaseException, so it is not caught
                    # here and still propagates on Ctrl+C.
                    log.debug("context.close() failed for task %d: %s", task.id, exc)

        if not isinstance(data, dict):
            data = {"value": data}

        return TaskResult(
            task_id=task.id,
            name=task.name,
            inputs=list(task.lines),
            worker_id=worker_id,
            proxy=proxy.masked() if proxy else None,
            status=status,
            started_at=started_wall.isoformat(),
            duration_s=round(time.monotonic() - started, 3),
            data=data if status == "ok" else {},
            error=error,
        )
