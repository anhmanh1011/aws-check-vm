"""Example custom handlers: this is where YOUR automation logic goes.

The engine never needs to change when you automate a new site. You write an
``async def handler(page, task) -> dict`` here (or in any module you like),
and point the CLI at it::

    python main.py --handler my_handlers:extract_page
    python main.py --handler my_handlers:process_lines --input emails.txt --batch-size 10

What the engine guarantees before it calls you:

* ``page`` is a fresh Playwright ``Page`` inside a fresh ``BrowserContext``
  (no cookies, no storage, its own proxy), so nothing from another batch can
  leak into yours.
* ``task.lines`` is one batch of raw lines from ``--input`` (one line per
  batch by default; ``--batch-size N`` groups N lines; ``0`` splits the file
  evenly across workers). The framework never interprets the lines; your
  flow decides what a line means and which site to open. ``task.id`` is the
  1-based batch number and ``task.name`` is what the dashboard shows.
* Your coroutine runs under ``--timeout`` seconds for the whole batch. If
  you overrun, the task is recorded as ``timeout``; if you raise, it is
  recorded as ``failed`` with the exception text. You do not need to catch
  errors yourself unless you want to turn them into data.
* Whatever dict you return is stored verbatim in the ``data`` field of the
  result (``results.json`` / ``results.csv``). Keep it JSON-serialisable and
  return one entry per line so results can be matched back to inputs.

Two examples follow. ``extract_page`` treats each line as a URL to read.
``process_lines`` treats each line as a piece of data (an email) and feeds
it into one fixed site that the flow itself chooses.
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

# The site ``process_lines`` works against. The flow owns this choice; the
# input file only supplies data. Tests point it at the local test server.
FORM_URL = "http://127.0.0.1:8000/form"

# How long to wait for the page's first heading before deciding it has none.
# Short on purpose: a JSON endpoint or a bare page should not stall a worker.
_HEADING_TIMEOUT_MS = 2_000

_MAX_FILENAME_LENGTH = 60


def _safe_filename(name: str) -> str:
    """Turn free text into a lowercase, filesystem-safe slug.

    Anything that is not a letter or digit becomes ``-``; runs are collapsed,
    edges trimmed, and the result capped so very long URLs do not exceed
    path limits on Windows. Falls back to ``"task"`` when nothing is left.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:_MAX_FILENAME_LENGTH].rstrip("-") or "task"


async def extract_page(page: Page, task: Task) -> dict[str, Any]:
    """Treat each line as a URL: visit it and collect a few facts about the page.

    Returns ``{"pages": [{"url", "title", "heading", "link_count",
    "screenshot"}, ...]}`` with one entry per line, in input order. All lines
    in the batch share the same page, context and proxy.
    """
    pages: list[dict[str, Any]] = []
    for position, url in enumerate(task.lines, start=1):
        # ``domcontentloaded`` fires once the HTML is parsed, which is enough
        # to query the DOM. The default ``load`` waits for every image and
        # script and is often several seconds slower for no benefit here.
        await page.goto(url, wait_until="domcontentloaded")

        title = await page.title()

        # ``locator`` is lazy: nothing happens until an action or an ``await``.
        # Checking ``count()`` first avoids the default 30 s auto-wait that
        # ``inner_text()`` would spend looking for a heading that is not there.
        heading_locator = page.locator("h1").first
        heading: str | None = None
        if await heading_locator.count() > 0:
            heading = (await heading_locator.inner_text(timeout=_HEADING_TIMEOUT_MS)).strip()

        link_count = await page.locator("a").count()

        # One file per line, named <batch>-<position>-<slug> so it can be
        # matched back to results.json even when a batch holds many URLs.
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = SCREENSHOT_DIR / f"{task.id}-{position}-{_safe_filename(url)}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)

        pages.append({
            "url": url,
            "title": title,
            "heading": heading,
            "link_count": link_count,
            "screenshot": str(screenshot_path),
        })

    return {"pages": pages}


async def process_lines(page: Page, task: Task) -> dict[str, Any]:
    """Treat each line as an email: submit it through one fixed form.

    This is the shape of a "data in, site fixed" flow. The site is opened
    once per batch; every line is then filled in, submitted, and its result
    read, all on the same page. Returns ``{"processed": [{"email", "status"},
    ...]}`` in input order.
    """
    # Open the site once; the batch reuses the page (and its login state, if
    # your real flow logs in here) for every line.
    await page.goto(FORM_URL, wait_until="domcontentloaded")

    processed: list[dict[str, Any]] = []
    for email in task.lines:
        await page.fill("#email", email)
        # Clicking submit navigates; ``expect_navigation`` is deprecated, so
        # wait for the response marker that only the post-submit page has.
        await page.click("button[type=submit]")
        status_locator = page.locator("#status")
        await status_locator.wait_for(state="visible")
        status = (await status_locator.inner_text()).strip()
        processed.append({"email": email, "status": status})

    return {"processed": processed}
