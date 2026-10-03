# KiotProxy Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Source browser proxies from the KiotProxy API (one IP per key, several keys = a pool) and feed them into the existing `ProxyManager`, selectable with new CLI flags.

**Architecture:** A new stdlib-only module `kiotproxy.py` calls `GET {BASE_URL}/proxies/new?key=&region=` per key and returns `models.Proxy` objects. `main.py` builds the pool from `--kiot-keys` (one key per line) when that flag is given, otherwise from `proxies.txt` as before. Everything downstream of `ProxyManager` is unchanged. Tests are fully offline against a local `/proxies/new` route added to the existing test server; one manual live call verifies the real key at the end.

**Tech Stack:** Python ≥ 3.11 (developed on 3.14), `urllib.request`/`json`/`pathlib` (stdlib only), existing `pytest` + `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-10-03-kiotproxy-integration-design.md`

## Global Constraints

- Python ≥ 3.11; `X | None` unions; `from __future__ import annotations`.
- No new dependencies. Transport is `urllib.request` (stdlib).
- Flat module layout: `kiotproxy.py` at repo root; tests in `tests/`.
- Every module opens with a docstring stating what it does and WHY. Public functions have docstrings. Full type hints. PEP 8.
- A KiotProxy key is a secret: it is sent only to KiotProxy's real `BASE_URL`, never to another host, and never appears unmasked in any log. Logs use `mask_key(key)`.
- Only the response `http` field (`ip:port`) is consumed; the resulting `Proxy` has no credentials. Protocol is fixed to HTTP (no flag).
- Exactly one `/proxies/new` call per key, at startup. No mid-run rotation, no `/proxies/out`.
- `--kiot-keys` replaces the `proxies.txt` source when present; the two are not combined.
- Valid regions: `bac`, `trung`, `nam`, `random` (default `random`).
- Exit codes unchanged: bad input (missing key file, empty key file, invalid region) exits 2; an all-keys-failed pool logs a warning and runs direct.
- Tests never contact the public internet. Run `pytest` from repo root `D:\workspace\AWS` with `python -m pytest`.
- `keys.txt` must be git-ignored.

---

### Task 1: `kiotproxy.py` module with offline tests

**Files:**
- Create: `kiotproxy.py`
- Modify: `tests/conftest.py` (add one `/proxies/new` route to `_EchoHandler`)
- Modify: `.gitignore` (add `keys.txt`)
- Test: `tests/test_kiotproxy.py`

**Interfaces:**
- Consumes: `models.Proxy(server: str, username=None, password=None)` (frozen dataclass; `Proxy(server="http://1.2.3.4:8080")` is valid).
- Produces (used by Task 2):
  - `kiotproxy.BASE_URL: str`
  - `kiotproxy.VALID_REGIONS: frozenset[str]`
  - `kiotproxy.KiotProxyError(Exception)` with attributes `.code: int | None` and `.error: str | None`
  - `kiotproxy.fetch_proxy(key: str, region: str = "random", *, base_url: str | None = None, timeout_s: float = 15.0) -> Proxy`
  - `kiotproxy.load_kiot_proxies(keys: Sequence[str], region: str = "random", *, base_url: str | None = None) -> list[Proxy]`
  - `kiotproxy.load_keys(path: Path) -> list[str]`
  - `kiotproxy.mask_key(key: str) -> str`

- [ ] **Step 1: Add the `/proxies/new` route to the test server**

In `tests/conftest.py`, the `_EchoHandler.do_GET` method is a chain of
`if self.path.startswith(...)` branches. Add a new branch **before** the
final `else`. The route reads the `key` query parameter and returns the
vendor's failure body when the key is exactly `badkey`, otherwise the success
body with a loopback `http` value. `parse_qs` and `urlsplit` are already
imported at the top of the file.

Add this branch (immediately after the existing `/form` branch, before `else`):

```python
        elif self.path.startswith("/proxies/new"):
            key = parse_qs(urlsplit(self.path).query).get("key", [""])[0]
            if key == "badkey":
                body = {
                    "success": False, "code": 40400006,
                    "message": "Key not found", "status": "FAIL",
                    "error": "KEY_NOT_FOUND",
                }
            else:
                body = {
                    "data": {
                        "realIpAddress": "127.0.0.1",
                        "http": "127.0.0.1:39008",
                        "socks5": "127.0.0.1:39009",
                        "httpPort": 39008, "socks5Port": 39009,
                        "host": "127.0.0.1", "location": "Test",
                        "expirationAt": 1718030731927, "ttl": 1200, "ttc": 59,
                    },
                    "success": True, "code": 200, "status": "SUCCESS",
                }
            self._send(200, json.dumps(body), "application/json")
```

Also update the `_EchoHandler` docstring to mention the new route, e.g. append
`, ``/proxies/new`` -> KiotProxy-shaped JSON (failure when key=badkey)` to it.

- [ ] **Step 2: Write the failing tests**

`tests/test_kiotproxy.py`:

```python
"""Tests for the KiotProxy proxy source. Fully offline via the local server."""

from __future__ import annotations

from pathlib import Path

import pytest

import kiotproxy
from kiotproxy import (
    KiotProxyError,
    fetch_proxy,
    load_keys,
    load_kiot_proxies,
    mask_key,
)
from models import Proxy


def test_fetch_proxy_parses_success_into_http_proxy(local_server):
    proxy = fetch_proxy("goodkey", "random", base_url=local_server)
    assert proxy == Proxy(server="http://127.0.0.1:39008")


def test_fetch_proxy_sends_key_and_region(local_server):
    # 'bac' is valid and the success body is returned for any non-bad key.
    proxy = fetch_proxy("abc", "bac", base_url=local_server)
    assert proxy.server == "http://127.0.0.1:39008"


def test_fetch_proxy_raises_kiotproxyerror_on_api_failure(local_server):
    with pytest.raises(KiotProxyError) as excinfo:
        fetch_proxy("badkey", "random", base_url=local_server)
    assert excinfo.value.code == 40400006
    assert excinfo.value.error == "KEY_NOT_FOUND"


def test_fetch_proxy_rejects_invalid_region_before_any_request():
    with pytest.raises(ValueError, match="region"):
        fetch_proxy("k", "europe", base_url="http://127.0.0.1:0")


def test_fetch_proxy_raises_when_body_is_not_json(monkeypatch, local_server):
    monkeypatch.setattr(kiotproxy, "_get_json", lambda url, timeout_s: (_ for _ in ()).throw(
        KiotProxyError("not JSON")))
    with pytest.raises(KiotProxyError):
        fetch_proxy("goodkey", base_url=local_server)


def test_fetch_proxy_raises_when_success_body_missing_http(monkeypatch, local_server):
    monkeypatch.setattr(kiotproxy, "_get_json", lambda url, timeout_s: {
        "success": True, "data": {"socks5": "1.2.3.4:5"}})
    with pytest.raises(KiotProxyError, match="http"):
        fetch_proxy("goodkey", base_url=local_server)


def test_load_kiot_proxies_skips_failed_keys_and_masks_them(local_server, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    proxies = load_kiot_proxies(["goodkey", "badkey"], base_url=local_server)
    assert proxies == [Proxy(server="http://127.0.0.1:39008")]
    assert "badkey" not in caplog.text          # raw key never logged
    assert mask_key("badkey") in caplog.text     # masked form is


def test_load_kiot_proxies_empty_when_all_fail(local_server):
    assert load_kiot_proxies(["badkey"], base_url=local_server) == []


def test_load_keys_parses_lines_ignoring_comments_and_blanks(tmp_path: Path):
    path = tmp_path / "keys.txt"
    path.write_text("# my keys\n\n  Kabc123  \nKdef456\n", encoding="utf-8")
    assert load_keys(path) == ["Kabc123", "Kdef456"]


def test_load_keys_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="key file not found"):
        load_keys(tmp_path / "nope.txt")


def test_load_keys_empty_file_raises(tmp_path: Path):
    path = tmp_path / "keys.txt"
    path.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no keys"):
        load_keys(path)


def test_mask_key_hides_middle_of_long_key():
    masked = mask_key("Keb294a4b014b4c44b9284a761274b0ae")
    assert masked.startswith("Keb2")
    assert masked.endswith("b0ae")
    assert "294a4b014b4c44b9284a761274" not in masked


def test_mask_key_fully_masks_short_key():
    assert mask_key("abcd") == "****"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_kiotproxy.py -v`
Expected: collection error / FAIL with `ModuleNotFoundError: No module named 'kiotproxy'`.

- [ ] **Step 4: Implement `kiotproxy.py`**

```python
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


def _get_json(url: str, timeout_s: float) -> dict[str, Any]:
    """GET ``url`` and parse a JSON object, raising ``KiotProxyError`` on any failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        raise KiotProxyError(f"request to KiotProxy failed: {exc}") from exc
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

    payload = _get_json(url, timeout_s)
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

    One bad key never stops the others. The returned list may be empty if
    every key failed, which the caller treats as "run direct".
    """
    proxies: list[Proxy] = []
    for key in keys:
        try:
            proxies.append(fetch_proxy(key, region, base_url=base_url))
        except KiotProxyError as exc:
            log.warning("KiotProxy key %s failed: %s", mask_key(key), exc)
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_kiotproxy.py -v`
Expected: all tests pass. If `test_fetch_proxy_raises_when_body_is_not_json` fails because the `_get_json` monkeypatch lambda is awkward, replace it with a module-level helper in the test file:

```python
def _raise_not_json(url, timeout_s):
    raise KiotProxyError("not JSON")
```
and `monkeypatch.setattr(kiotproxy, "_get_json", _raise_not_json)`.

- [ ] **Step 6: Add `keys.txt` to `.gitignore`**

Append a line `keys.txt` to `.gitignore` (after `screenshots/`). Then confirm
the key file is not tracked:

Run: `git check-ignore keys.txt`
Expected: prints `keys.txt` (it is now ignored).

If `keys.txt` was already committed in an earlier change, untrack it without
deleting the working copy:

Run: `git rm --cached keys.txt 2>/dev/null; true`

- [ ] **Step 7: Run the whole suite**

Run: `python -m pytest -q`
Expected: all tests pass (new `tests/test_kiotproxy.py` plus the existing suite), no warnings.

- [ ] **Step 8: Commit**

```bash
git add kiotproxy.py tests/test_kiotproxy.py tests/conftest.py .gitignore
git commit -m "feat: KiotProxy proxy source module with offline tests"
```

---

### Task 2: Wire `--kiot-keys` / `--kiot-region` into the CLI, verify live

**Files:**
- Modify: `main.py` (imports, `build_parser`, the proxy-source block in `main`)
- Modify: `README.md` (KiotProxy subsection)
- Test: `tests/test_main.py` (parser defaults + one wiring test)

**Interfaces:**
- Consumes from Task 1: `kiotproxy.load_keys(path) -> list[str]`, `kiotproxy.load_kiot_proxies(keys, region, *, base_url=None) -> list[Proxy]`, `kiotproxy.VALID_REGIONS`, and the module attribute `kiotproxy.BASE_URL`.
- Consumes existing: `proxy_manager.ProxyManager(proxies)` and `ProxyManager.from_file(path)`.
- Produces: no new public API; adds argparse options `--kiot-keys` (`Path | None`, default `None`) and `--kiot-region` (str, default `"random"`, `choices` = sorted `VALID_REGIONS`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py`. The first extends the existing defaults test; add
it as new assertions in a new test so nothing existing breaks. The wiring test
monkeypatches `kiotproxy.BASE_URL` to the local server and stubs
`main.asyncio.run` so no browser launches (the pattern already used by
`test_unwritable_output_path_exits_1_with_message`).

```python
def test_parser_kiot_defaults():
    args = build_parser().parse_args([])
    assert args.kiot_keys is None
    assert args.kiot_region == "random"


def test_parser_rejects_unknown_region():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--kiot-region", "europe"])


def test_kiot_keys_builds_pool_without_network(tmp_path, capsys, monkeypatch, local_server):
    import kiotproxy
    monkeypatch.setattr(kiotproxy, "BASE_URL", local_server)
    monkeypatch.setattr("main.asyncio.run", lambda coro: coro.close())

    keyfile = tmp_path / "keys.txt"
    keyfile.write_text("goodkey\n", encoding="utf-8")
    out = tmp_path / "results.json"
    inp = tmp_path / "input.txt"
    inp.write_text("line1\n", encoding="utf-8")

    code = main([
        "--kiot-keys", str(keyfile),
        "--kiot-region", "random",
        "--input", str(inp),
        "--output", str(out),
        "--no-dashboard",
    ])

    # asyncio.run is stubbed, so no tasks complete: summary prints, exit is 1
    # (fewer results than tasks), never 2 (which would mean proxy loading
    # failed as bad input).
    assert code != 2
    assert "tasks finished" in capsys.readouterr().out


def test_kiot_keys_missing_file_exits_2(tmp_path, capsys):
    code = main([
        "--kiot-keys", str(tmp_path / "nope.txt"),
        "--input", str(tmp_path / "input.txt"),
        "--no-dashboard",
    ])
    assert code == 2
    assert "key file not found" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py -k kiot -v`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'kiot_keys'` (and the missing-file test returns the wrong code).

- [ ] **Step 3: Add the imports and the CLI flags in `main.py`**

Add the import near the other local imports (after `from handlers import ...`):

```python
import kiotproxy
from kiotproxy import load_keys, load_kiot_proxies
```

In `build_parser`, immediately after the `--proxies` argument, add:

```python
    parser.add_argument("--kiot-keys", type=Path, default=None,
                        help="KiotProxy key file, one key per line; when given it replaces "
                             "--proxies as the proxy source (default: %(default)s)")
    parser.add_argument("--kiot-region", default="random", choices=sorted(kiotproxy.VALID_REGIONS),
                        help="KiotProxy region for --kiot-keys (default: %(default)s)")
```

- [ ] **Step 4: Select the proxy source in `main`**

In `main`, replace this single line inside the `try` block:

```python
        proxy_manager = ProxyManager.from_file(args.proxies)
```

with:

```python
        if args.kiot_keys is not None:
            # --kiot-keys replaces proxies.txt: fetch one IP per key up front
            # and build the same round-robin pool the engine already expects.
            keys = load_keys(args.kiot_keys)
            proxy_manager = ProxyManager(load_kiot_proxies(keys, args.kiot_region))
        else:
            proxy_manager = ProxyManager.from_file(args.proxies)
```

`load_keys` raises `FileNotFoundError`/`ValueError`, both already caught by the
surrounding `except (FileNotFoundError, ValueError)` that returns exit 2.
`load_kiot_proxies` never raises for a rejected key (it warns and skips), so an
all-failed pool yields an empty `ProxyManager` and the run proceeds direct.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_main.py -k kiot -v`
Expected: the four `kiot` tests pass.

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest -q`
Expected: all tests pass, no warnings.

- [ ] **Step 7: Live verification with the real key (one call only)**

The key in `keys.txt` is real and paid; the vendor forbids abuse, so make
**exactly one** call. Run this one-off snippet from the repo root:

```bash
python -c "import kiotproxy; k=open('keys.txt').read().split()[0]; p=kiotproxy.fetch_proxy(k,'random'); print('OK', p.server)"
```

Expected: prints `OK http://<ip>:<port>` with a real IP. Record the IP and the
masked key (`kiotproxy.mask_key(k)`) in your report. Do NOT loop or retry on
success. If it fails with `KiotProxyError`, report the `code`/`error` verbatim
(e.g. the key may have an active proxy already, or need a first `/new`); a
second single retry is acceptable, more is not.

- [ ] **Step 8: Update the README**

In `README.md`, under the proxy documentation (after the "Proxy file format"
section, before "Input file and batching"), add:

````markdown
## KiotProxy

Instead of a static `proxies.txt`, the pool can come from the
[KiotProxy](https://kiotproxy.com) API. Put one key per line in a file and
pass it:

```bash
python main.py --kiot-keys keys.txt --kiot-region random --concurrency 3
```

Each key yields **one** IP at a time, so the pool is as wide as the number of
keys: three keys give three simultaneous IPs round-robined across contexts.
`--kiot-region` is one of `bac`, `trung`, `nam`, `random`. The framework calls
`/proxies/new` once per key at startup and uses the HTTP proxy it returns; it
does not rotate IPs mid-run. A key that the API rejects is logged (masked) and
skipped; if every key fails the run continues with no proxy.

`--kiot-keys` replaces `--proxies` when both are present. Keep your key file
out of version control: `keys.txt` is already in `.gitignore`.
````

- [ ] **Step 9: Commit**

```bash
git add main.py tests/test_main.py README.md
git commit -m "feat: --kiot-keys/--kiot-region source proxies from KiotProxy"
```
