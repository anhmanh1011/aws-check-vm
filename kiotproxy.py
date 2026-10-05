"""Source proxies from the KiotProxy API.

WHY a separate module: the engine only ever sees a ``ProxyManager`` holding
``models.Proxy`` objects, so where those proxies come from is a detail that
lives here. KiotProxy gives one IP per key at a time; supplying several keys
produces a pool of several simultaneous IPs that drops straight into the
existing round-robin manager.

Each key is a paid secret. It is sent only to KiotProxy's ``BASE_URL`` and is
never written to a log in full -- use ``mask_key`` for that. Only the ``http``
field of the response (``ip:port``) is used; KiotProxy HTTP proxies take no
username or password.

Vendor doc: GET {BASE_URL}/proxies/new?key=<KEY>&region=<bac|trung|nam|random>
returns ``{"success": true, "data": {"http": "ip:port", ...}}`` or
``{"success": false, "code": ..., "error": ...}``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from models import Proxy

log = logging.getLogger(__name__)

BASE_URL = "https://api.kiotproxy.com/api/v1"
VALID_REGIONS = frozenset({"bac", "trung", "nam", "random"})

# KiotProxy's WAF rejects urllib's default User-Agent with HTTP 403, so send a browser-like one.
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class KiotProxyError(Exception):
    """A KiotProxy call failed: transport, bad body, or ``success: false``.

    ``code`` and ``error`` carry the API's values when the failure came from a
    structured error body; both are ``None`` for transport/parse failures.
    """

    def __init__(self, message: str, *, code: int | None = None, error: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.error = error


def mask_key(key: str) -> str:
    """Return a log-safe form of a key: first 4 + '…' + last 4, or all '*'.

    Short keys (<= 8 chars) are fully masked since first/last 4 would reveal
    the whole thing.
    """
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def _redact_url(url: str) -> str:
    """Return ``url`` with the ``key`` query value replaced by its masked form.

    Logging the request must never expose the raw key. The key value is
    replaced in place with ``mask_key(value)``, so the rest of the URL (host,
    region) is untouched and the masked marker stays human-readable (a plain
    string replace avoids percent-encoding the mask's ``…``).
    """
    parts = urllib.parse.urlsplit(url)
    for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if name == "key" and value:
            return url.replace(value, mask_key(value))
    return url


def _get_json(url: str, timeout_s: float) -> dict[str, Any]:
    """GET ``url`` and parse a JSON object, raising ``KiotProxyError`` on any failure.

    The request (with the key masked) and the raw response body are logged at
    INFO so a ``--no-dashboard`` run shows the KiotProxy exchange on the
    console. The body carries only the proxy IP, which is not a secret.
    """
    log.info("KiotProxy request: GET %s", _redact_url(url))
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
            status = getattr(response, "status", None)
    except urllib.error.HTTPError as exc:
        # An HTTP error (e.g. 400/403) still carries a body, which is exactly
        # the KiotProxy error the caller needs to see. Log it before raising.
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001 - body may be unreadable; the status still helps
            pass
        log.info("KiotProxy response (HTTP %s): %s", exc.code, body)
        raise KiotProxyError(f"request to KiotProxy failed: {exc}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise KiotProxyError(f"request to KiotProxy failed: {exc}") from exc
    log.info("KiotProxy response (HTTP %s): %s", status, raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KiotProxyError(f"KiotProxy returned non-JSON body: {raw[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise KiotProxyError(f"KiotProxy returned a non-object body: {payload!r}")
    return payload


def fetch_proxy(
    key: str, region: str = "random", *, base_url: str | None = None, timeout_s: float = 15.0
) -> Proxy:
    """Fetch one proxy for ``key`` and return it as an HTTP ``models.Proxy``.

    Raises ``ValueError`` for an invalid region (before any request) and
    ``KiotProxyError`` for an API failure, a transport error, a non-JSON body,
    or a success body without an ``http`` field.
    """
    if region not in VALID_REGIONS:
        raise ValueError(f"region must be one of {sorted(VALID_REGIONS)}, got {region!r}")

    # base_url resolved here (not in the default arg) so tests that monkeypatch
    # kiotproxy.BASE_URL are honoured.
    resolved = base_url or BASE_URL
    query = urllib.parse.urlencode({"key": key, "region": region})
    url = f"{resolved}/proxies/new?{query}"

    return _proxy_from_payload(_get_json(url, timeout_s))


def fetch_current_proxy(
    key: str, *, base_url: str | None = None, timeout_s: float = 15.0
) -> Proxy:
    """Fetch the proxy *currently* assigned to ``key`` via ``/proxies/current``.

    Unlike ``fetch_proxy`` this never rotates to a new IP; it returns the one the
    key already holds. Used as a fallback when ``/proxies/new`` fails (e.g. the
    pool is temporarily out of resources, or the key is still inside its
    rotation window). Takes no region -- ``/current`` ignores it.
    """
    resolved = base_url or BASE_URL
    url = f"{resolved}/proxies/current?{urllib.parse.urlencode({'key': key})}"
    return _proxy_from_payload(_get_json(url, timeout_s))


def _proxy_from_payload(payload: dict[str, Any]) -> Proxy:
    """Turn a KiotProxy success body into a ``Proxy``; raise ``KiotProxyError`` otherwise.

    Shared by ``/proxies/new`` and ``/proxies/current``, whose success and
    failure shapes are identical.
    """
    if not payload.get("success"):
        raise KiotProxyError(
            payload.get("message", "KiotProxy request was not successful"),
            code=payload.get("code"),
            error=payload.get("error"),
        )
    http_value = (payload.get("data") or {}).get("http")
    if not http_value:
        raise KiotProxyError(f"KiotProxy success body has no 'http' field: {payload!r}")
    return Proxy(server=f"http://{http_value}")


def load_kiot_proxies(
    keys: Sequence[str], region: str = "random", *, base_url: str | None = None
) -> list[Proxy]:
    """Fetch a proxy for every key; skip (with a warning) any key that fails.

    For each key, try ``/proxies/new`` first; if that fails (resources busy, or
    the key still inside its rotation window), fall back to ``/proxies/current``
    to use the IP the key already holds. A key is skipped only when *both*
    endpoints fail, so a transient ``/new`` outage no longer silently drops a
    key -- the reason both failed is logged (masked). One bad key never stops
    the others; an empty result tells the caller to run direct.
    """
    proxies: list[Proxy] = []
    for key in keys:
        try:
            proxies.append(fetch_proxy(key, region, base_url=base_url))
            continue
        except KiotProxyError as new_exc:
            try:
                proxy = fetch_current_proxy(key, base_url=base_url)
            except KiotProxyError as current_exc:
                log.warning(
                    "KiotProxy key %s failed: new=%s; current=%s",
                    mask_key(key), new_exc, current_exc,
                )
                continue
            log.info(
                "KiotProxy key %s: /new failed (%s); using current proxy",
                mask_key(key), new_exc,
            )
            proxies.append(proxy)
    if not proxies:
        log.warning("No usable KiotProxy proxies from %d key(s); running direct", len(keys))
    else:
        log.info("Loaded %d KiotProxy proxies from %d key(s)", len(proxies), len(keys))
    return proxies


def load_keys(path: Path) -> list[str]:
    """Read a key file: one key per line, ignoring blank lines and ``#`` comments.

    Raises ``FileNotFoundError`` if the file is missing and ``ValueError`` if
    it holds no keys.
    """
    if not path.exists():
        raise FileNotFoundError(f"key file not found: {path}")
    keys = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not keys:
        raise ValueError(f"no keys found in {path}")
    return keys
