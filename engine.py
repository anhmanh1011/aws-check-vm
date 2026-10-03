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
                await asyncio.gather(
                    *(self._worker(worker_id, queue, browser)
                      for worker_id in range(1, self._concurrency + 1))
                )
            finally:
                # Runs on success, on error, and on cancellation (Ctrl+C), so the
                # browser process never outlives the Python process.
                await browser.close()

        return list(self._state.results)

    async def _worker(
        self, worker_id: int, queue: asyncio.Queue[Task | None], browser: Browser
    ) -> None:
        """Consume tasks until the ``None`` sentinel arrives."""
        status = self._state.workers[worker_id]
        while True:
            task = await queue.get()
            if task is None:
                status.state = "done"
                status.task = None
                status.proxy = None
                status.started_at = None
                log.debug("worker %d finished", worker_id)
                return

            proxy = self._proxy_manager.next()
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
            # ``wait_for`` cancels the handler if it overruns; the ``finally``
            # below still closes the context.
            data = await asyncio.wait_for(self._handler(page, task), timeout=self._timeout_s)
            status = "ok"
        except asyncio.TimeoutError:
            status = "timeout"
            error = f"handler exceeded {self._timeout_s}s"
        except Exception as exc:  # noqa: BLE001
            # WHY broad: Playwright raises several classes (Error, TimeoutError,
            # TargetClosedError) and a dead proxy surfaces as a navigation
            # Error. All of them must be recorded, none may kill the worker.
            # CancelledError is a BaseException and is deliberately NOT caught,
            # so Ctrl+C still propagates.
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if context is not None:
                with suppress(Exception):
                    await context.close()

        if not isinstance(data, dict):
            data = {"value": data}

        return TaskResult(
            task_id=task.id,
            name=task.name,
            url=task.url,
            worker_id=worker_id,
            proxy=proxy.masked() if proxy else None,
            status=status,
            started_at=started_wall.isoformat(),
            duration_s=round(time.monotonic() - started, 3),
            data=data if status == "ok" else {},
            error=error,
        )
