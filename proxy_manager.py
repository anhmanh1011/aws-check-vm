"""Proxy file parsing and round-robin proxy assignment.

WHY a dedicated manager: Playwright lets every ``BrowserContext`` carry its
own proxy, which is what makes "one context per task, one proxy per context"
possible with a single browser process. The engine should not care how
proxies are chosen, so that policy lives here. Round-robin via
``itertools.cycle`` is the simplest policy that never blocks a worker: once
the pool is exhausted it wraps around and proxies are reused.

Supported line formats (one per line, ``#`` starts a comment)::

    http://user:pass@host:port
    http://host:port
    https://host:port
    socks5://host:port
    socks4://host:port

Caveat: Chromium does not support authenticated SOCKS5 proxies. The parser
accepts ``socks5://user:pass@host:port`` but Playwright will ignore the
credentials.
"""

from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

from models import Proxy

log = logging.getLogger(__name__)

SUPPORTED_SCHEMES = frozenset({"http", "https", "socks4", "socks5"})
_CREDENTIALS_RE = re.compile(r"://[^/@\s]+@")


def _redact(line: str) -> str:
    """Replace credentials in proxy URL with placeholder for safe error messages."""
    return _CREDENTIALS_RE.sub("://***:***@", line)


def parse_proxy_line(line: str) -> Proxy | None:
    """Parse one line of ``proxies.txt``.

    Returns ``None`` for blank lines and comments so callers can simply skip
    them. Raises ``ValueError`` with a descriptive message for anything that
    is not a usable proxy URL.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    parts = urlsplit(stripped)
    if parts.scheme not in SUPPORTED_SCHEMES:
        raise ValueError(
            f"unsupported proxy scheme {parts.scheme!r} in {_redact(stripped)!r} "
            f"(expected one of {sorted(SUPPORTED_SCHEMES)})"
        )
    if not parts.hostname:
        raise ValueError(f"missing host in proxy {_redact(stripped)!r}")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid port in proxy {_redact(stripped)!r}") from exc
    if port is None:
        raise ValueError(f"missing port in proxy {_redact(stripped)!r}")

    # Credentials may be percent-encoded (e.g. ``user%40corp``); Playwright
    # wants the decoded form.
    username = unquote(parts.username) if parts.username is not None else None
    password = unquote(parts.password) if parts.password is not None else None

    return Proxy(
        server=f"{parts.scheme}://{parts.hostname}:{port}",
        username=username,
        password=password,
    )


class ProxyManager:
    """Hands out proxies to new browser contexts in round-robin order."""

    def __init__(self, proxies: Sequence[Proxy]) -> None:
        self._proxies: list[Proxy] = list(proxies)
        # ``cycle`` holds an infinite iterator over the pool; ``None`` when the
        # pool is empty so ``next()`` can signal "connect directly".
        self._cycle: Iterator[Proxy] | None = (
            itertools.cycle(self._proxies) if self._proxies else None
        )

    @classmethod
    def from_file(cls, path: Path) -> "ProxyManager":
        """Build a manager from a proxy list file.

        A missing file or a file with no usable lines is not an error: the run
        continues without proxies and a warning is logged. A malformed line IS
        an error, reported with its line number so the user can fix the file.
        """
        if not path.exists():
            log.warning("Proxy file %s not found; running without proxies", path)
            return cls([])

        proxies: list[Proxy] = []
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                proxy = parse_proxy_line(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc
            if proxy is not None:
                proxies.append(proxy)

        if not proxies:
            log.warning("No proxies found in %s; running without proxies", path)
        else:
            log.info("Loaded %d proxies from %s", len(proxies), path)
        return cls(proxies)

    def next(self) -> Proxy | None:
        """Return the next proxy in rotation, or ``None`` if the pool is empty."""
        if self._cycle is None:
            return None
        return next(self._cycle)

    def at(self, index: int) -> Proxy | None:
        """Return the proxy at ``index`` (wrapping around), or ``None`` if empty.

        Used for "one proxy per worker" assignment: worker *i* always gets
        ``at(i - 1)``, so each worker keeps a fixed proxy for the whole run
        instead of sharing the round-robin rotation.
        """
        if not self._proxies:
            return None
        return self._proxies[index % len(self._proxies)]

    def __len__(self) -> int:
        return len(self._proxies)
