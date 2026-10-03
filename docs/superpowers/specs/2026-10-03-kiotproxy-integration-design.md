# KiotProxy Integration — Design Spec

**Date:** 2026-10-03
**Status:** Awaiting user review

## 1. Purpose

Let the framework source its browser proxies from the KiotProxy API instead
of (or as an alternative to) the static `proxies.txt` pool. Each KiotProxy
key yields one proxy IP at a time; supplying several keys produces a pool of
several simultaneous IPs, which plugs into the existing round-robin
`ProxyManager` with no change to the engine.

Non-goals (explicitly out of scope for this spec):

- Rotating a key's IP mid-run via repeated `/new` calls. One `/new` per key
  at startup only. The API's `ttc` rate limit (~59 s between changes) makes
  per-task rotation impractical at any real concurrency, and the "multiple
  keys = pool" model removes the need for it.
- Calling `/proxies/out` to release proxies. Keys expire on their own
  (`ttl`); releasing is a possible later enhancement, noted in the README.
- SOCKS5. The `http` field is used; protocol is fixed, not a flag.
- Mixing KiotProxy keys and `proxies.txt` in one run. `--kiot-keys` replaces
  the file source when given.

## 2. KiotProxy API (from the vendor doc)

- `GET https://api.kiotproxy.com/api/v1/proxies/new?key=<KEY>&region=<REGION>`
  returns a new or rotated proxy for the key.
- Region is one of `bac`, `trung`, `nam`, `random`.
- Success body:
  ```json
  {
    "data": {
      "http": "171.229.x.x:39008",
      "socks5": "171.229.x.x:39009",
      "httpPort": 39008, "socks5Port": 39009,
      "host": "171.229.x.x", "location": "Phú Thọ",
      "realIpAddress": "171.229.x.x",
      "expirationAt": 1718030731927, "ttl": 1200, "ttc": 59
    },
    "success": true, "code": 200, "status": "SUCCESS"
  }
  ```
- Failure body:
  ```json
  {
    "success": false, "code": 40400006,
    "message": "Key not found", "status": "FAIL", "error": "KEY_NOT_FOUND"
  }
  ```
- Only the `http` field (`ip:port`) is consumed. KiotProxy HTTP proxies take
  no username/password.

## 3. Components

### 3.1 `kiotproxy.py` (new)

```python
BASE_URL = "https://api.kiotproxy.com/api/v1"   # overridable in tests
VALID_REGIONS = frozenset({"bac", "trung", "nam", "random"})

class KiotProxyError(Exception):
    """An API call returned success=false or a transport/parse failure."""
    # carries .code (int | None) and .error (str | None) when available

def fetch_proxy(key: str, region: str = "random", *, base_url: str | None = None,
                timeout_s: float = 15.0) -> Proxy: ...

def load_kiot_proxies(keys: Sequence[str], region: str = "random", *,
                      base_url: str | None = None) -> list[Proxy]: ...

def load_keys(path: Path) -> list[str]: ...        # one key per line, # comments
def mask_key(key: str) -> str: ...                 # "Keb2…b0ae" for logs
```

- `base_url` defaults to `None` and is resolved to the module global
  `BASE_URL` **inside** the function body (`base_url = base_url or BASE_URL`),
  so a test that monkeypatches `kiotproxy.BASE_URL` is honoured. Callers that
  pass `base_url` explicitly override it.
- `fetch_proxy` builds `"{base_url}/proxies/new?key=…&region=…"`, performs a
  GET via `urllib.request`, parses JSON, and on `success: true` returns
  `Proxy(server=f"http://{data['http']}")` (no credentials). On
  `success: false` it raises `KiotProxyError(message, code=…, error=…)`. A
  non-200 HTTP status, a network error, a non-JSON body, or a missing `http`
  field also raises `KiotProxyError`. `region` not in `VALID_REGIONS` raises
  `ValueError` before any network call.
- `load_kiot_proxies` calls `fetch_proxy` for each key in order. A key that
  raises `KiotProxyError` is logged at WARNING (with the masked key and the
  error) and skipped; other keys continue. Returns the successful `Proxy`
  list (possibly empty). The key value never appears unmasked in any log.
- `load_keys` reads one key per line, ignoring blank lines and `#` comments;
  missing file raises `FileNotFoundError`, a file with no keys raises
  `ValueError`.
- Transport is `urllib.request.urlopen` (stdlib, no new dependency), wrapped
  in a private `_get_json(url, timeout_s)` helper so tests can point
  `base_url` at the local server.

### 3.2 `main.py` (modified)

New flags:

| Flag | Default | Notes |
| --- | --- | --- |
| `--kiot-keys` | `None` | path to a file of KiotProxy keys, one per line |
| `--kiot-region` | `random` | `bac` / `trung` / `nam` / `random`; validated by argparse `choices` |

Proxy-source selection, inside the existing try/except that returns exit 2
on bad input:

- If `--kiot-keys` is given: `keys = load_keys(path)`, then
  `proxies = load_kiot_proxies(keys, args.kiot_region)`, then
  `proxy_manager = ProxyManager(proxies)`. An empty result (every key
  failed) logs a warning and the run proceeds direct, mirroring an empty
  `proxies.txt`. A missing key file or a malformed region exits 2.
- Otherwise: unchanged, `ProxyManager.from_file(args.proxies)`.

`--kiot-keys` and `--proxies` are not combined; `--kiot-keys` wins when
present. This is stated in `--help` and the README.

### 3.3 `tests/conftest.py` (modified)

Add one route to the local echo server so the integration tests never touch
the public internet. The route matches the real path (`/proxies/new`) and
chooses its body from the `key` query parameter, so success and failure are
exercised through the same base URL:

- `GET /proxies/new?key=badkey&region=…` → the failure body from §2
  (`success: false`, `error: "KEY_NOT_FOUND"`).
- `GET /proxies/new?key=<anything else>&region=…` → the success body from §2
  with `http` pointing at a loopback `ip:port` (e.g. `127.0.0.1:39008`).

Tests pass `base_url=local_server`; the code appends `/proxies/new`. A test
selects success or failure purely by the key it sends.

## 4. Error Handling Summary

| Failure | Where | Effect |
| --- | --- | --- |
| Region not valid | `fetch_proxy` | `ValueError` → main exit 2 |
| Key file missing | `load_keys` | `FileNotFoundError` → main exit 2 |
| Key file empty | `load_keys` | `ValueError` → main exit 2 |
| One key rejected (`success:false`) | `load_kiot_proxies` | WARNING (masked key), skip, continue |
| Network / non-200 / bad JSON for a key | `load_kiot_proxies` | WARNING (masked key), skip, continue |
| Every key failed | `main` | WARNING, run direct (empty pool) |

## 5. Security

- A key is a paid secret. It is sent only to KiotProxy (over the real
  `BASE_URL`) and never to any other host. Logs show `mask_key(key)` only.
- `keys.txt` is added to `.gitignore` so the committed repo never contains a
  key. The spec and README tell the user to keep keys out of version
  control.
- The one-time live verification (below) is the only call made with the real
  key during development; the automated suite is fully offline.

## 6. Testing Strategy

Framework: existing `pytest` + `pytest-asyncio`. All tests offline.

Unit (`tests/test_kiotproxy.py`):

- `fetch_proxy(key="goodkey", base_url=local_server)` parses the success body
  into `Proxy(server="http://127.0.0.1:39008")`.
- `fetch_proxy(key="badkey", base_url=local_server)` raises `KiotProxyError`
  carrying `code` (40400006) and `error` ("KEY_NOT_FOUND").
- `fetch_proxy` raises `ValueError` for an invalid region before any request.
- `fetch_proxy` raises `KiotProxyError` on a non-JSON body and on a success
  body missing the `http` field (a second local route, `/proxies/garbage`
  served as the `base_url`'s `/proxies/new` via a dedicated base, returns
  non-JSON — or the test monkeypatches `_get_json` to return `{}`). Prefer
  the monkeypatch: it keeps the server minimal.
- `load_kiot_proxies(["goodkey", "badkey"], base_url=local_server)` returns
  exactly one `Proxy` and logs a WARNING; `mask_key("badkey")` appears in the
  log, the context captured via `caplog` never contains a raw full key from a
  realistic long key.
- `load_keys` parses one-per-line with comments/blanks; missing file and
  empty file raise.
- `mask_key` hides the middle of the key (first 4 + "…" + last 4 for a long
  key; a short key is fully masked).

No `AutomationEngine` or browser is needed for these.

Main wiring (`tests/test_main.py`): one test monkeypatches
`kiotproxy.BASE_URL` to the `local_server` base and stubs `main.asyncio.run`
with the existing coroutine-closing lambda, so
`main(["--kiot-keys", keyfile, "--kiot-region", "random", "--no-dashboard",
"--output", out])` loads the proxy pool from the fake endpoint, does not
launch a browser, and returns without an exit-2 input error (it prints the
summary line). The key file holds one non-`badkey` key. This proves the CLI
path wires `--kiot-keys` into a `ProxyManager` without hitting the network.

Live verification (manual, once, not in the suite): after the offline tests
pass, call `fetch_proxy` once with the real key from `keys.txt` against the
real `BASE_URL`, print the returned IP and `location` (key masked), confirm a
`Proxy` is built. A single call respects the abuse policy.

## 7. Dependencies

None added. `urllib.request`, `json`, and `pathlib` are all stdlib.

## 8. README additions

- A "KiotProxy" subsection under proxies: the two flags, the key-file format,
  the one-IP-per-key / multiple-keys-for-a-pool model, the `http`-only and
  no-rotation notes, and the security warning to keep keys out of git.
