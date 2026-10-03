"""Per-task behaviour lives here, decoupled from the engine.

A *handler* is any ``async def handler(page, task) -> dict``. The engine gives
it a brand-new ``Page`` inside a brand-new ``BrowserContext`` (fresh cookies,
fresh storage, its own proxy) and records whatever dict it returns.

WHY this split: the engine is about concurrency and isolation and should
never change when you want to automate a different site. Write a new
coroutine in your own module and point the CLI at it with
``--handler mymodule:myfunc``.

The default handler, ``fetch_ip``, visits an IP-echo endpoint so you can see
which outgoing address each context used. It understands both
``https://httpbin.org/ip`` (``{"origin": ...}``) and
``https://api.ipify.org?format=json`` (``{"ip": ...}``).
"""

from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Page

from models import Task

Handler = Callable[[Page, Task], Awaitable[dict[str, Any]]]

DEFAULT_HANDLER = "handlers:fetch_ip"


async def fetch_ip(page: Page, task: Task) -> dict[str, Any]:
    """Treat every line in the batch as a URL, visit it, and read the IP.

    Returns ``{"results": [{"url", "ip", "raw"}, ...]}`` with one entry per
    line, in input order. This is the shape to copy for your own batch
    handlers: loop over ``task.lines`` on the same page (same context, same
    proxy) and collect one result per item.

    ``wait_until="domcontentloaded"`` is enough for a JSON endpoint and
    returns sooner than the default ``load`` event.
    """
    results: list[dict[str, Any]] = []
    for url in task.lines:
        await page.goto(url, wait_until="domcontentloaded")
        raw = await page.locator("body").inner_text()
        results.append({"url": url, "ip": _extract_ip(raw), "raw": raw})
    return {"results": results}


def _extract_ip(raw: str) -> str | None:
    """Pull the IP out of an httpbin (``origin``) or ipify (``ip``) JSON body."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("origin") or payload.get("ip")
    return str(value) if value is not None else None


def load_handler(spec: str) -> Handler:
    """Resolve ``"module:function"`` to an ``async def`` callable.

    Raises ``ValueError`` with a user-facing message for a malformed spec, an
    unimportable module, a missing attribute, or a non-coroutine function.
    """
    module_name, separator, func_name = spec.partition(":")
    if not separator or not module_name or not func_name:
        raise ValueError(f"handler spec must look like 'module:function', got {spec!r}")

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise ValueError(f"cannot import handler module {module_name!r}: {exc}") from exc

    func = getattr(module, func_name, None)
    if func is None:
        raise ValueError(f"module {module_name!r} has no attribute {func_name!r}")
    if not inspect.iscoroutinefunction(func):
        raise ValueError(f"handler {spec!r} must be an 'async def' function")
    return func
