"""Tests for the handler contract, the default fetch_ip handler, and load_handler."""

import pytest

import handlers
from handlers import DEFAULT_HANDLER, fetch_ip, load_handler
from models import Task


class _FakeLocator:
    def __init__(self, text: str) -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text


class _FakePage:
    """Minimal stand-in for playwright.async_api.Page used by fetch_ip."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.visited: list[tuple[str, dict]] = []

    async def goto(self, url: str, **kwargs) -> None:
        self.visited.append((url, kwargs))

    def locator(self, selector: str) -> _FakeLocator:
        assert selector == "body"
        return _FakeLocator(self.body)


TASK = Task(id=1, lines=("https://httpbin.org/ip",), name="ip")


async def test_fetch_ip_reads_httpbin_origin():
    page = _FakePage('{"origin": "203.0.113.7"}')
    result = await fetch_ip(page, TASK)  # type: ignore[arg-type]
    assert result == {
        "results": [
            {"url": "https://httpbin.org/ip", "ip": "203.0.113.7", "raw": '{"origin": "203.0.113.7"}'}
        ]
    }
    assert page.visited[0][0] == "https://httpbin.org/ip"
    assert page.visited[0][1].get("wait_until") == "domcontentloaded"


async def test_fetch_ip_reads_ipify_ip_key():
    page = _FakePage('{"ip": "198.51.100.9"}')
    result = await fetch_ip(page, TASK)  # type: ignore[arg-type]
    assert result["results"][0]["ip"] == "198.51.100.9"


async def test_fetch_ip_non_json_body_returns_raw_and_none_ip():
    page = _FakePage("<html>not json</html>")
    result = await fetch_ip(page, TASK)  # type: ignore[arg-type]
    assert result["results"][0] == {
        "url": "https://httpbin.org/ip", "ip": None, "raw": "<html>not json</html>"
    }


async def test_fetch_ip_visits_every_line_in_the_batch():
    batch = Task(id=1, lines=("https://a.example/ip", "https://b.example/ip"), name="batch")
    page = _FakePage('{"origin": "203.0.113.7"}')
    result = await fetch_ip(page, batch)  # type: ignore[arg-type]
    assert [url for url, _ in page.visited] == ["https://a.example/ip", "https://b.example/ip"]
    assert [r["url"] for r in result["results"]] == list(batch.lines)
    assert all(r["ip"] == "203.0.113.7" for r in result["results"])


def test_load_handler_resolves_default():
    assert load_handler(DEFAULT_HANDLER) is handlers.fetch_ip


def test_load_handler_rejects_spec_without_colon():
    with pytest.raises(ValueError, match="module:function"):
        load_handler("handlers.fetch_ip")


def test_load_handler_rejects_unknown_module():
    with pytest.raises(ValueError, match="cannot import"):
        load_handler("no_such_module_xyz:fn")


def test_load_handler_rejects_unknown_attribute():
    with pytest.raises(ValueError, match="no attribute"):
        load_handler("handlers:does_not_exist")


def test_load_handler_rejects_non_coroutine():
    with pytest.raises(ValueError, match="async def"):
        load_handler("handlers:load_handler")
