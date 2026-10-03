"""Tests for the example custom handlers in my_handlers.py.

The unit tests cover the filename helper without a browser. The integration
tests run the real engine against the local server, so they prove the
examples actually work end to end, not just that they import.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import my_handlers
from engine import AutomationEngine
from models import RunState, Task
from my_handlers import _safe_filename, extract_page, process_lines
from proxy_manager import ProxyManager


def test_safe_filename_slugifies_spaces_and_punctuation():
    assert _safe_filename("My Page: a/b?c") == "my-page-a-b-c"


def test_safe_filename_falls_back_when_nothing_is_left():
    assert _safe_filename("///???") == "task"


def test_safe_filename_truncates_long_names():
    assert len(_safe_filename("x" * 200)) <= 60


async def _run_batch(handler, lines: tuple[str, ...], name: str = "batch") -> dict:
    task = Task(id=1, lines=lines, name=name)
    state = RunState(total=1)
    engine = AutomationEngine(
        [task], ProxyManager([]), handler,
        concurrency=1, headless=True, timeout_s=30.0, state=state,
    )
    (result,) = await engine.run()
    assert result.status == "ok", result.error
    return result.data


@pytest.mark.integration
@pytest.mark.usefixtures("require_chromium")
async def test_extract_page_reads_title_heading_links_and_screenshot(
    local_server, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(my_handlers, "SCREENSHOT_DIR", tmp_path)

    data = await _run_batch(extract_page, (f"{local_server}/page",), name="Demo Page")

    (page,) = data["pages"]
    assert page["url"] == f"{local_server}/page"
    assert page["title"] == "Demo Page"
    assert page["heading"] == "Hello from the local server"
    assert page["link_count"] == 2
    screenshot = Path(page["screenshot"])
    assert screenshot.parent == tmp_path
    # <batch id>-<position in batch>-<slug of the URL>.png
    assert screenshot.name.startswith("1-1-http-127-0-0-1-")
    assert screenshot.name.endswith("-page.png")
    assert screenshot.stat().st_size > 0


@pytest.mark.integration
@pytest.mark.usefixtures("require_chromium")
async def test_extract_page_handles_page_without_heading_or_links(
    local_server, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(my_handlers, "SCREENSHOT_DIR", tmp_path)

    data = await _run_batch(extract_page, (f"{local_server}/ip",), name="json only")

    (page,) = data["pages"]
    assert page["heading"] is None
    assert page["link_count"] == 0
    assert Path(page["screenshot"]).exists()


@pytest.mark.integration
@pytest.mark.usefixtures("require_chromium")
async def test_extract_page_visits_every_line_in_the_batch(
    local_server, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(my_handlers, "SCREENSHOT_DIR", tmp_path)
    lines = (f"{local_server}/page", f"{local_server}/ip")

    data = await _run_batch(extract_page, lines, name="two pages")

    assert [p["url"] for p in data["pages"]] == list(lines)
    assert [p["link_count"] for p in data["pages"]] == [2, 0]
    names = sorted(Path(p["screenshot"]).name for p in data["pages"])
    assert names[0].startswith("1-1-") and names[1].startswith("1-2-")


@pytest.mark.integration
@pytest.mark.usefixtures("require_chromium")
async def test_process_lines_submits_each_email_on_one_page(local_server, monkeypatch):
    monkeypatch.setattr(my_handlers, "FORM_URL", f"{local_server}/form")

    data = await _run_batch(process_lines, ("a@x.com", "b@x.com", "c@x.com"), name="emails")

    assert data["processed"] == [
        {"email": "a@x.com", "status": "accepted: a@x.com"},
        {"email": "b@x.com", "status": "accepted: b@x.com"},
        {"email": "c@x.com", "status": "accepted: c@x.com"},
    ]
