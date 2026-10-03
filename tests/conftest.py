"""Shared pytest fixtures.

``local_server`` runs a tiny HTTP server in a daemon thread so engine tests
never touch the public internet. ``require_chromium`` skips browser-backed
tests on machines where ``playwright install chromium`` has not been run.
"""

from __future__ import annotations

import html
import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Undo whatever a test's call to ``setup_logging`` did to the root logger.

    ``setup_logging`` replaces the root logger's handlers wholesale, so
    without this fixture a handler (and its open file) installed by one test
    would leak into every test that runs after it.
    """
    root = logging.getLogger()
    handlers_before = root.handlers[:]
    level_before = root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers_before:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = handlers_before
    root.setLevel(level_before)


_DEMO_HTML = """<!doctype html>
<html><head><title>Demo Page</title></head>
<body>
  <h1>Hello from the local server</h1>
  <p>Used by the example handler tests.</p>
  <a href="/ip">first link</a>
  <a href="/boom">second link</a>
</body></html>
"""


_FORM_HTML = """<!doctype html>
<html><head><title>Demo Form</title></head>
<body>
  {status}
  <form action="/form" method="get">
    <input id="email" name="email" type="text">
    <button type="submit">Send</button>
  </form>
</body></html>
"""


class _EchoHandler(BaseHTTPRequestHandler):
    """Routes: ``/ip`` -> JSON origin, ``/page`` -> small HTML page,
    ``/slow`` -> 3 s delay, ``/boom`` -> 500."""

    def do_GET(self) -> None:  # noqa: N802 (name mandated by BaseHTTPRequestHandler)
        if self.path.startswith("/ip"):
            self._send(200, json.dumps({"origin": self.client_address[0]}), "application/json")
        elif self.path.startswith("/page"):
            self._send(200, _DEMO_HTML, "text/html")
        elif self.path.startswith("/form"):
            # A GET form: submitting navigates to /form?email=... and the page
            # echoes what it received, so a handler can fill, submit, read,
            # and repeat on the same page.
            query = parse_qs(urlsplit(self.path).query)
            email = query.get("email", [""])[0]
            status = f'<p id="status">accepted: {html.escape(email)}</p>' if email else ""
            self._send(200, _FORM_HTML.format(status=status), "text/html")
        elif self.path.startswith("/slow"):
            time.sleep(3)
            self._send(200, json.dumps({"origin": "slow"}), "application/json")
        elif self.path.startswith("/boom"):
            self._send(500, "boom", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def _send(self, code: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # silence per-request stderr noise
        return


@pytest.fixture(scope="session")
def local_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


_CHROMIUM_PROBE = (
    "from playwright.sync_api import sync_playwright\n"
    "with sync_playwright() as p:\n"
    "    p.chromium.launch(headless=True).close()\n"
)


@pytest.fixture(scope="session")
def require_chromium() -> None:
    """Skip the requesting test when Playwright's Chromium cannot launch."""
    probe = subprocess.run(
        [sys.executable, "-c", _CHROMIUM_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if probe.returncode != 0:
        pytest.skip(f"Chromium not available: {probe.stderr.strip()[-300:]}")
