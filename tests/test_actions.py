"""Integration tests for the action helpers in actions.py.

They drive real headless Chromium against the local server from conftest.py
(the ``/form`` and ``/page`` routes), so they prove the helpers wait and act
correctly, not just that they import.
"""

from __future__ import annotations

import time

import pytest
from playwright.async_api import async_playwright

import actions

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_chromium")]


async def _page():
    """Yield a fresh page plus a cleanup callable, without the full engine."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()

    async def cleanup():
        await browser.close()
        await pw.stop()

    return page, cleanup


async def test_goto_click_fill_and_get_text_drive_the_form(local_server):
    page, cleanup = await _page()
    try:
        await actions.goto(page, f"{local_server}/form")
        await actions.fill(page, "#email", "a@x.com")
        await actions.click(page, "button[type=submit]")
        status = await actions.get_text(page, "#status")
        assert status == "accepted: a@x.com"
    finally:
        await cleanup()


async def test_wait_returns_a_usable_locator(local_server):
    page, cleanup = await _page()
    try:
        await actions.goto(page, f"{local_server}/page")
        heading = await actions.wait(page, "h1")
        assert (await heading.inner_text()) == "Hello from the local server"
    finally:
        await cleanup()


async def test_click_raises_when_selector_never_appears(local_server):
    page, cleanup = await _page()
    try:
        await actions.goto(page, f"{local_server}/page")
        with pytest.raises(Exception) as excinfo:
            await actions.click(page, "#does-not-exist", timeout_ms=500)
        assert "Timeout" in str(excinfo.value) or "timeout" in str(excinfo.value)
    finally:
        await cleanup()


async def test_capture_screenshots_one_element_and_returns_png_bytes(local_server, tmp_path):
    page, cleanup = await _page()
    try:
        await actions.goto(page, f"{local_server}/page")
        out = tmp_path / "shot.png"
        data = await actions.capture(page.locator("h1"), out)
        # Returns the PNG bytes and writes the same bytes to the path.
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert out.read_bytes() == data
    finally:
        await cleanup()


async def test_capture_returns_bytes_without_writing_a_file(local_server, tmp_path):
    page, cleanup = await _page()
    try:
        await actions.goto(page, f"{local_server}/page")
        data = await actions.capture(page.locator("h1"))  # no path -> buffer only
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        # Nothing should have been written to disk.
        assert list(tmp_path.iterdir()) == []
    finally:
        await cleanup()


async def test_capture_raises_when_element_never_appears(local_server, tmp_path):
    page, cleanup = await _page()
    try:
        await actions.goto(page, f"{local_server}/page")
        with pytest.raises(Exception) as excinfo:
            await actions.capture(page.locator("#nope"), tmp_path / "x.png", timeout_ms=500)
        assert "Timeout" in str(excinfo.value) or "timeout" in str(excinfo.value)
    finally:
        await cleanup()


async def test_sleep_waits_at_least_the_requested_time():
    start = time.monotonic()
    await actions.sleep(0.2)
    assert time.monotonic() - start >= 0.2
