"""Example custom handlers: this is where YOUR automation logic goes.

The engine never needs to change when you automate a new site. You write an
``async def handler(page, task) -> dict`` here (or in any module you like),
and point the CLI at it::

    python main.py --handler my_handlers:extract_page

What the engine guarantees before it calls you:

* ``page`` is a fresh Playwright ``Page`` inside a fresh ``BrowserContext``
  (no cookies, no storage, its own proxy), so nothing from another task can
  leak into yours.
* ``task`` is the ``Task`` from ``tasks.txt`` / ``tasks.json``: ``task.id``
  (1-based), ``task.url`` and ``task.name``.
* Your coroutine runs under ``--timeout`` seconds. If you overrun, the task
  is recorded as ``timeout``; if you raise, it is recorded as ``failed`` with
  the exception text. You do not need to catch errors yourself unless you
  want to turn them into data.
* Whatever dict you return is stored verbatim in the ``data`` field of the
  result (``results.json`` / ``results.csv``). Keep it JSON-serialisable.

``extract_page`` below is a realistic "read a page" example: title, first
heading, link count, and a screenshot named after the task.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from models import Task

# Screenshots are written here; the folder is created on first use and is
# git-ignored. Tests point this at a temporary directory.
SCREENSHOT_DIR = Path("screenshots")

# How long to wait for the page's first heading before deciding it has none.
# Short on purpose: a JSON endpoint or a bare page should not stall a worker.
_HEADING_TIMEOUT_MS = 2_000

_MAX_FILENAME_LENGTH = 60


def _safe_filename(name: str) -> str:
    """Turn a task name into a lowercase, filesystem-safe slug.

    Anything that is not a letter or digit becomes ``-``; runs are collapsed,
    edges trimmed, and the result capped so very long URLs used as names do
    not exceed path limits on Windows. Falls back to ``"task"`` when nothing
    usable is left.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:_MAX_FILENAME_LENGTH].rstrip("-") or "task"


async def extract_page(page: Page, task: Task) -> dict[str, Any]:
    """Visit ``task.url`` and collect a few facts about the page.

    Returns ``{"title", "heading", "link_count", "screenshot"}``.
    """
    # ``domcontentloaded`` fires once the HTML is parsed, which is enough to
    # query the DOM. The default ``load`` waits for every image and script
    # and is often several seconds slower for no benefit here.
    await page.goto(task.url, wait_until="domcontentloaded")

    title = await page.title()

    # ``locator`` is lazy: nothing happens until an action or an ``await``.
    # Checking ``count()`` first avoids the default 30 s auto-wait that
    # ``inner_text()`` would spend looking for a heading that is not there.
    heading_locator = page.locator("h1").first
    heading: str | None = None
    if await heading_locator.count() > 0:
        heading = (await heading_locator.inner_text(timeout=_HEADING_TIMEOUT_MS)).strip()

    link_count = await page.locator("a").count()

    # One file per task, named so it can be matched back to results.json.
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = SCREENSHOT_DIR / f"{task.id}-{_safe_filename(task.name)}.png"
    await page.screenshot(path=str(screenshot_path), full_page=True)

    return {
        "title": title,
        "heading": heading,
        "link_count": link_count,
        "screenshot": str(screenshot_path),
    }
