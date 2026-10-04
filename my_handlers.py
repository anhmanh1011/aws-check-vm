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

import asyncio
import logging
import re
from pathlib import Path
from time import time
from typing import Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import omocaptcha
from actions import capture, click, fill, goto, sleep
from models import Task

log = logging.getLogger(__name__)

# Screenshots are written here; the folder is created on first use and is
# git-ignored. Tests point this at a temporary directory.
SCREENSHOT_DIR = Path("screenshots")

# The site ``process_lines`` works against. The flow owns this choice; the
# input file only supplies data. Tests point it at the local test server.
FORM_URL = "https://console.aws.amazon.com/console/home?nc2=h_si&src=header-signin"

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

# The AWS sign-in CAPTCHA lives inside a cross-origin iframe. These anchors
# were found by inspecting the live DOM; they are the stable ones (the ids on
# the input and the icon buttons are regenerated per render, so they are
# avoided in favour of name/alt/type).
_CAPTCHA_FRAME = "iframe#core-container"          # the only iframe in the modal
_CAPTCHA_IMAGE = "img[alt='captcha']"             # 200x70 distorted-text image
_CAPTCHA_INPUT = "input[name='captchaGuess']"     # the "Verification answer" box
_CAPTCHA_SUBMIT = "button[type='submit']"         # the Submit button
_CAPTCHA_NEW_IMAGE = "button:has(img[alt='Display new security image.'])"

# A wrong answer makes AWS discard the challenge (the iframe detaches) and
# present a fresh one, so the solver gets a few tries. Kept small -- every try
# is a paid OmoCaptcha solve.
_CAPTCHA_MAX_ATTEMPTS = 3
# After Submit, how long to let AWS accept or re-challenge before we judge it.
_CAPTCHA_SETTLE_S = 2.0
# After clicking Next, AWS auto-opens the "Security Verification" modal, but the
# iframe + the S3 image take a few seconds to render. Wait generously for the
# image rather than racing it -- the earlier 2 s wait expired before the image
# appeared, which made the handler give up without ever solving anything.
_CAPTCHA_OPEN_TIMEOUT_MS = 20_000
# Verdict check after a submit: short, because "did it clear?" must not cost the
# full open-timeout on the (common) success path.
_CAPTCHA_CLEAR_TIMEOUT_MS = 5_000


async def _open_captcha(page: Page) -> bool:
    """Ensure a CAPTCHA challenge is on screen; return True if its image is visible.

    After Next (and, as observed live, after a wrong answer) AWS auto-opens the
    modal, so the normal path is simply to wait for the image to render. Only if
    no image appears at all do we fall back to clicking the "Making sure its you"
    Verify button to start the challenge manually. Return False when neither the
    image nor a startable Verify button yields an image -- i.e. no challenge is
    being presented and we are past it.

    WHY not click Verify first: once the modal is open the Verify button sits
    *behind* it and is unclickable, so clicking it there just times out. The
    image wait is both the common case and the reliable signal.
    """
    image = page.frame_locator(_CAPTCHA_FRAME).locator(_CAPTCHA_IMAGE)
    try:
        await image.wait_for(state="visible", timeout=_CAPTCHA_OPEN_TIMEOUT_MS)
        return True
    except PlaywrightTimeoutError:
        pass
    # No challenge auto-opened: try to start one via the Verify button.
    try:
        await page.get_by_role("button", name="Verify").click(timeout=5_000)
    except PlaywrightTimeoutError:
        return False  # no Verify button -> nothing to open
    try:
        await image.wait_for(state="visible", timeout=_CAPTCHA_OPEN_TIMEOUT_MS)
        return True
    except PlaywrightTimeoutError:
        return False


async def _captcha_present(page: Page, timeout_ms: int) -> bool:
    """Fast read: is a CAPTCHA challenge on screen (image visible, or the Verify
    button that would start one)?

    Used after a submit to decide whether the check cleared. Unlike
    ``_open_captcha`` it never clicks anything, so the "did we get past it?"
    decision is cheap: a passed answer removes both the image and the Verify
    button, a wrong one brings one of them back.
    """
    try:
        if await page.get_by_role("button", name="Verify").is_visible():
            return True
    except PlaywrightTimeoutError:
        pass
    image = page.frame_locator(_CAPTCHA_FRAME).locator(_CAPTCHA_IMAGE)
    try:
        await image.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


async def check_VM(page: Page, task: Task) -> dict[str, Any]:
    """Walk AWS root sign-in, solve the security CAPTCHA via OmoCaptcha, submit.

    The CAPTCHA image sits in a **cross-origin iframe** (``iframe#core-container``),
    so it cannot be reached with ``page.locator`` nor refetched from its ``src``
    (an S3 URL bound to this session); it is addressed through
    ``page.frame_locator`` and captured as rendered pixels. The PNG goes to
    OmoCaptcha, whose ``solve`` blocks on HTTP plus polling -- hence
    ``asyncio.to_thread``, so the other workers' event loop is not frozen.

    A wrong answer is retried up to ``_CAPTCHA_MAX_ATTEMPTS`` times: ``solve`` ->
    fill -> submit, re-opening the fresh challenge each round via ``_open_captcha``.

    Verdict (verified live): submitting the *right* answer removes the CAPTCHA
    iframe and the Verify button and advances the flow (with a real root email,
    to the password step); a *wrong* answer re-presents the challenge. So right
    after a submit, "no CAPTCHA present" means we got past it. ``solved`` here is
    "the CAPTCHA gate was passed", independent of whether the account then
    exists -- a non-existent email still passes the CAPTCHA and lands on the
    sign-in error page, which is not a CAPTCHA.

    Returns ``{"solved": bool, "attempts": [{"attempt", "answer", "image"}, ...]}``.
    The OmoCaptcha client key is read from ``omocaptcha.txt`` (gitignored).
    """
    await goto(page, FORM_URL)
    await click(page, "#root_account_signin")
    await fill(page, "#resolving_input", "daoducmasssnh28101997@gmail.com")
    await click(page, "#next_button")

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    attempts: list[dict[str, Any]] = []
    solved = False

    for attempt in range(1, _CAPTCHA_MAX_ATTEMPTS + 1):
        if not await _open_captcha(page):
            log.info("check_VM: no CAPTCHA on screen at attempt %d; stopping", attempt)
            break  # no challenge presented -> nothing (more) to solve

        # frame_locator().locator() is a normal Locator, so capture waits for
        # visibility and crops to just the 200x70 image, returning its bytes.
        image_path = SCREENSHOT_DIR / f"captcha-{task.id}-{attempt}.png"
        image_bytes = await capture(
            page.frame_locator(_CAPTCHA_FRAME).locator(_CAPTCHA_IMAGE), image_path
        )
        log.info("check_VM attempt %d: captcha captured -> %s, solving", attempt, image_path)

        answer = await asyncio.to_thread(omocaptcha.solve, image_bytes)
        log.info("check_VM attempt %d: OmoCaptcha answered %r", attempt, answer)

        frame = page.frame_locator(_CAPTCHA_FRAME)
        await frame.locator(_CAPTCHA_INPUT).fill(answer)
        await frame.locator(_CAPTCHA_SUBMIT).click()
        attempts.append({"attempt": attempt, "answer": answer, "image": str(image_path)})
        log.info("check_VM attempt %d: submitted %r", attempt, answer)

        await sleep(_CAPTCHA_SETTLE_S)  # actions.sleep is in SECONDS

        # Right answer -> the challenge is gone; wrong -> a fresh one is back.
        if not await _captcha_present(page, _CAPTCHA_CLEAR_TIMEOUT_MS):
            solved = True
            log.info("check_VM attempt %d: CAPTCHA passed", attempt)
            break
        log.info("check_VM attempt %d: still challenged, retrying", attempt)

    log.info("check_VM: solved=%s after %d attempt(s)", solved, len(attempts))
    return {"solved": solved, "attempts": attempts}

