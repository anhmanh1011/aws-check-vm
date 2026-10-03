"""Tests for the example custom handler in my_handlers.py.

The unit test covers the filename helper without a browser. The integration
tests run the real engine with ``extract_page`` against the local server, so
they prove the example actually works end to end, not just that it imports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import my_handlers
from engine import AutomationEngine
from models import RunState, Task
from my_handlers import _safe_filename, extract_page
from proxy_manager import ProxyManager


def test_safe_filename_slugifies_spaces_and_punctuation():
    assert _safe_filename("My Page: a/b?c") == "my-page-a-b-c"


def test_safe_filename_falls_back_when_nothing_is_left():
    assert _safe_filename("///???") == "task"


def test_safe_filename_truncates_long_names():
    assert len(_safe_filename("x" * 200)) <= 60


async def _run_one(url: str, name: str) -> dict:
    task = Task(id=1, url=url, name=name)
    state = RunState(total=1)
    engine = AutomationEngine(
        [task], ProxyManager([]), extract_page,
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

    data = await _run_one(f"{local_server}/page", "Demo Page")

    assert data["title"] == "Demo Page"
    assert data["heading"] == "Hello from the local server"
    assert data["link_count"] == 2
    screenshot = Path(data["screenshot"])
    assert screenshot.parent == tmp_path
    assert screenshot.name == "1-demo-page.png"
    assert screenshot.stat().st_size > 0


@pytest.mark.integration
@pytest.mark.usefixtures("require_chromium")
async def test_extract_page_handles_page_without_heading_or_links(
    local_server, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(my_handlers, "SCREENSHOT_DIR", tmp_path)

    data = await _run_one(f"{local_server}/ip", "json only")

    assert data["heading"] is None
    assert data["link_count"] == 0
    assert Path(data["screenshot"]).exists()
