"""Small action helpers for writing flows.

Playwright's ``page.click`` / ``page.fill`` already auto-wait for an element
to be actionable, so these wrappers are not strictly necessary. What they buy
you is a flow that reads as one line per step, a single place to set the
default per-step timeout, and a ``sleep`` that cannot be called without
``await`` (the most common beginner mistake).

Each helper waits for the selector to be **visible** before acting and
raises ``playwright.async_api.TimeoutError`` if it does not appear within
``timeout_ms``. The engine turns that into a ``failed`` result and moves on,
so a flow does not need its own try/except unless it wants to continue past
a missing element.

Use them from a handler::

    from actions import goto, click, fill, get_text, capture, sleep

    async def run(page, task):
        await goto(page, "https://example.com/login")
        await fill(page, "#user", "me")
        await click(page, "button[type=submit]")
        return {"status": await get_text(page, "#status")}

``capture`` takes a ``Locator`` instead of a selector, so it also reaches
elements inside an iframe (``page.frame_locator(...).locator(...)``) -- useful
for widgets served from another origin, such as a sign-in CAPTCHA.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import Locator, Page

# One place to change the default wait. Individual calls can override it.
DEFAULT_TIMEOUT_MS = 30_000


async def goto(page: Page, url: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    """Navigate to ``url`` and wait until the HTML is parsed.

    ``wait_until="domcontentloaded"`` returns as soon as the DOM is ready,
    which is enough to start locating elements; the per-element waits in the
    other helpers handle anything that renders later.
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)


async def wait(page: Page, selector: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Locator:
    """Wait for ``selector`` to be visible and return its locator.

    Use this when you need the element itself (to read an attribute, count
    matches, or act on it several times) rather than a one-shot click/fill.
    """
    locator = page.locator(selector)
    await locator.wait_for(state="visible", timeout=timeout_ms)
    return locator


async def click(page: Page, selector: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    """Wait for ``selector`` to be visible, then click it."""
    await (await wait(page, selector, timeout_ms=timeout_ms)).click(timeout=timeout_ms)


async def fill(
    page: Page, selector: str, value: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> None:
    """Wait for ``selector`` to be visible, then replace its contents with ``value``."""
    await (await wait(page, selector, timeout_ms=timeout_ms)).fill(value, timeout=timeout_ms)


async def get_text(page: Page, selector: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
    """Wait for ``selector`` to be visible, then return its stripped inner text."""
    locator = await wait(page, selector, timeout_ms=timeout_ms)
    return (await locator.inner_text(timeout=timeout_ms)).strip()


async def capture(
    locator: Locator, path: str | Path, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> bytes:
    """Wait for ``locator`` to be visible, screenshot just that element, return its PNG bytes.

    Takes a ``Locator`` rather than ``(page, selector)`` so it works the same
    for a plain page element and for one inside an iframe reached with
    ``page.frame_locator(...).locator(...)`` -- which is how a cross-origin
    widget such as the AWS sign-in CAPTCHA has to be addressed. The bytes are
    both written to ``path`` and returned, so a caller can hand them to a
    solver without reading the file back.
    """
    await locator.wait_for(state="visible", timeout=timeout_ms)
    return await locator.screenshot(path=str(path), timeout=timeout_ms)


async def sleep(seconds: float) -> None:
    """Pause the flow for ``seconds`` without blocking the other workers.

    A thin wrapper over ``asyncio.sleep`` so a flow never has to import
    ``asyncio`` just to pause, and so a forgotten ``await`` is impossible to
    write as ``sleep(5)`` alone does nothing useful and is easy to spot.
    """
    await asyncio.sleep(seconds)
