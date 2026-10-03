# Async Playwright Automation Framework — Design Spec

**Date:** 2026-10-03
**Status:** Approved by user, awaiting implementation plan

## 1. Purpose

An educational, production-quality CLI template that demonstrates high-performance
web automation in Python with Playwright. It teaches four patterns:

1. `asyncio` worker pools bounded by an `asyncio.Queue`.
2. One shared headless `Browser` per run.
3. One short-lived, fully isolated `BrowserContext` per task.
4. Per-context proxy assignment from a round-robin pool.

The default workload visits an IP-echo endpoint (`https://httpbin.org/ip` or
`https://api.ipify.org?format=json`) so a learner can see each context's outgoing
IP and confirm that proxies are actually applied.

Non-goals: a click/fill action DSL, retries with backoff, proxy health scoring,
distributed execution. Each is a natural extension and the README lists them as
exercises, but none ship.

## 2. Repository Layout

```
main.py            CLI entry (argparse) and wiring; calls asyncio.run(...)
engine.py          AutomationEngine: one Browser, N workers, asyncio.Queue
proxy_manager.py   Proxy line parser and round-robin ProxyManager
tasks.py           load_tasks() for tasks.txt / tasks.json
handlers.py        Handler contract, default fetch_ip coroutine, load_handler()
dashboard.py       rich Live table rendered from RunState
utils.py           Results export (json/csv), logging setup, credential masking
models.py          Task, Proxy, TaskResult, WorkerStatus, RunState dataclasses
tasks.txt          Example tasks (httpbin + ipify URLs)
proxies.txt        Example proxy file with commented format guide
requirements.txt   playwright, rich (+ pytest, pytest-asyncio for dev)
README.md          Conceptual explanation of the architecture
.gitignore
tests/
  conftest.py              Local HTTP server fixture, event loop config
  test_proxy_manager.py
  test_tasks.py
  test_utils.py
  test_handlers.py
  test_engine.py           Integration tests with real headless Chromium
```

Flat top-level modules are intentional: the learner can read each file in
isolation. Every module has a module docstring stating what it does and *why*
the pattern was chosen.

## 3. Data Model (`models.py`)

```python
@dataclass(frozen=True)
class Task:
    id: int            # 1-based index in the task file
    url: str
    name: str          # defaults to url if not given

@dataclass(frozen=True)
class Proxy:
    server: str                 # scheme://host:port (no credentials)
    username: str | None
    password: str | None
    def to_playwright(self) -> dict[str, str]   # Playwright's proxy option shape
    def masked(self) -> str                     # scheme://***:***@host:port for logs

@dataclass
class TaskResult:
    task_id: int
    name: str
    url: str
    worker_id: int
    proxy: str | None     # masked form, None when direct
    status: Literal["ok", "failed", "timeout"]
    started_at: str       # ISO-8601 UTC
    duration_s: float
    data: dict[str, Any]  # whatever the handler returned ({} on failure)
    error: str | None

@dataclass
class WorkerStatus:
    worker_id: int
    task: Task | None
    proxy: str | None     # masked
    state: Literal["idle", "running", "done"]
    started_at: float | None   # time.monotonic()

@dataclass
class RunState:
    total: int
    completed: int = 0
    ok: int = 0
    failed: int = 0
    workers: dict[int, WorkerStatus]
    results: list[TaskResult]
```

`Task` and `Proxy` live in `models.py` too so `tasks.py` and `proxy_manager.py`
import from one place and there are no circular imports.

## 4. Components

### 4.1 `proxy_manager.py`

- `parse_proxy_line(line: str) -> Proxy | None`
  - Accepts `http://user:pass@host:port`, `http://host:port`, `socks5://host:port`,
    also `https://` and `socks4://`.
  - Returns `None` for blank lines and lines starting with `#`.
  - Raises `ValueError` for malformed lines (missing host or port, unknown scheme).
  - Credentials are URL-decoded.
- `class ProxyManager`
  - `from_file(path: Path) -> ProxyManager`: missing file or zero valid proxies
    yields an empty manager and logs a warning. Malformed lines raise with the
    line number so the user can fix the file.
  - `next() -> Proxy | None`: round-robin via `itertools.cycle`; returns `None`
    when the pool is empty (meaning "connect directly").
  - `__len__`.
- Documented limitation: Chromium does not support authenticated SOCKS5 proxies;
  the parser accepts them but the README warns.

### 4.2 `tasks.py`

- `load_tasks(path: Path) -> list[Task]`
  - `.txt`: one URL per line; blank and `#` lines ignored; `name` = url.
  - `.json`: a list of objects `{"url": str, "name"?: str}`; or a list of bare
    strings.
  - Raises `FileNotFoundError` / `ValueError` with a clear message; `main.py`
    turns these into a non-zero exit with a one-line error.

### 4.3 `handlers.py`

- Contract: `Handler = Callable[[Page, Task], Awaitable[dict[str, Any]]]`.
- `async def fetch_ip(page, task) -> dict`: `page.goto(task.url, wait_until="domcontentloaded")`,
  read `page.locator("body").inner_text()`, `json.loads` with fallback to raw text,
  return `{"ip": <origin or ip key or None>, "raw": <text>}`.
- `load_handler(spec: str) -> Handler`: `"module:function"` resolved with
  `importlib.import_module` and `getattr`; validates it is a coroutine function.
  Default spec is `"handlers:fetch_ip"`.

### 4.4 `engine.py`

```python
class AutomationEngine:
    def __init__(self, tasks, proxy_manager, handler, *, concurrency: int,
                 headless: bool, timeout_s: float, state: RunState,
                 browser_type: str = "chromium") -> None
    async def run(self) -> list[TaskResult]
```

`run()`:

1. `async with async_playwright() as pw:` launch **one** browser.
2. Build an `asyncio.Queue`, put every `Task`, then put one `None` sentinel per
   worker. The sentinel approach is chosen over `queue.join()` + cancellation
   because it is explicit and easy to reason about for learners.
3. `asyncio.gather(*(self._worker(i, queue, browser) for i in range(1, N+1)))`.
4. `finally:` close the browser. Results live in `state.results` and are returned.

`_worker(worker_id, queue, browser)`:

```
loop:
  task = await queue.get()
  if task is None: mark worker done; break
  proxy = proxy_manager.next()
  update WorkerStatus -> running
  started = monotonic()
  context = None
  try:
      context = await browser.new_context(proxy=proxy.to_playwright() if proxy else None)
      page = await context.new_page()
      data = await asyncio.wait_for(handler(page, task), timeout=timeout_s)
      result = ok
  except asyncio.TimeoutError:          result = timeout
  except Exception as exc:              result = failed, error=f"{type(exc).__name__}: {exc}"
  finally:
      if context: with suppress(Exception): await context.close()
  append result; update counters; WorkerStatus -> idle
```

Why a broad `except Exception`: Playwright raises several error classes
(`TimeoutError`, `Error`, `TargetClosedError`) and a proxy that refuses
connections surfaces as a navigation `Error`. The worker must survive all of
them; the exception type and message are preserved in the result for diagnosis.
`asyncio.CancelledError` is a `BaseException` and so is **not** swallowed,
which lets Ctrl+C propagate.

Why a fresh context per task: a `BrowserContext` owns cookies, localStorage,
cache, and the proxy setting. Creating one per task gives each task a clean
identity at ~tens of milliseconds, versus seconds for a new browser process.

### 4.5 `dashboard.py`

- `class Dashboard` wrapping `rich.live.Live`.
- `render(state: RunState) -> Group` builds a header line
  (`completed/total  ok  failed  elapsed`) and a table with columns
  Worker | Task | Proxy | Status | Duration.
- `async def run_until(self, done: asyncio.Event)` refreshes every 0.25 s.
- `main.py` runs the dashboard and engine with `asyncio.gather`; the engine sets
  `done` when it finishes. With `--no-dashboard`, plain `logging` lines are
  emitted instead (INFO per task start/end). Rich's `RichHandler` is used for
  logging in both modes so output stays readable.

### 4.6 `utils.py`

- `setup_logging(verbose: bool, dashboard_active: bool)`: when the dashboard is
  active, logging goes to a file `run.log` rather than stderr so it does not
  fight the live table.
- `write_results(results, path)`: `.json` → indented list of `asdict(result)`;
  `.csv` → header row + one row per result with `data` flattened to a JSON
  string column. Any other extension raises `ValueError`.
- `mask_credentials(url: str) -> str`.

### 4.7 `main.py`

argparse flags:

| Flag | Default | Notes |
| --- | --- | --- |
| `--tasks` | `tasks.txt` | `.txt` or `.json` |
| `--proxies` | `proxies.txt` | missing/empty → direct, with warning |
| `--concurrency` | `3` | number of workers, ≥ 1 |
| `--headless` / `--no-headless` | `True` | `argparse.BooleanOptionalAction` |
| `--output` | `results.json` | `.json` or `.csv` |
| `--handler` | `handlers:fetch_ip` | `module:function` |
| `--timeout` | `30` | seconds per task |
| `--no-dashboard` | off | plain logging instead of rich Live |
| `-v/--verbose` | off | DEBUG logging |

Flow: parse → `setup_logging` → `load_tasks` → `ProxyManager.from_file` →
`load_handler` → build `RunState` → `asyncio.run(_main(...))` → `write_results`
→ print a one-line summary → exit 0 if every task is `ok`, else 1.

`KeyboardInterrupt` is caught around `asyncio.run`: partial results are still
written and the exit code is 130.

## 5. Error Handling Summary

| Failure | Where caught | Effect |
| --- | --- | --- |
| Malformed proxy line | `ProxyManager.from_file` | exit 2 with line number |
| Missing task file / bad JSON | `load_tasks` | exit 2 with message |
| Bad `--handler` spec | `load_handler` | exit 2 with message |
| Browser fails to launch | `engine.run` | exception propagates; exit 1 |
| Proxy refuses / DNS fail / navigation error | worker `except Exception` | result `failed`, worker continues |
| Handler exceeds `--timeout` | worker `except asyncio.TimeoutError` | result `timeout`, worker continues |
| Handler raises | worker `except Exception` | result `failed`, worker continues |
| `context.close()` raises | worker `finally` + `suppress` | ignored, logged at DEBUG |
| Ctrl+C | `main` | workers cancelled, browser closed, partial results written, exit 130 |

## 6. Testing Strategy

Framework: `pytest` + `pytest-asyncio` (asyncio mode `auto`). Playwright
browsers must be installed (`playwright install chromium`); integration tests
are marked `@pytest.mark.integration` and skipped when the browser is missing.

Unit tests (no browser):

- `test_proxy_manager.py`: parse http with/without auth, socks5, https, comments,
  blanks, malformed → `ValueError`; URL-encoded credentials; `to_playwright()`
  shape; `masked()`; round-robin wraps; empty pool returns `None`; `from_file`
  on missing path returns empty manager.
- `test_tasks.py`: txt parsing, json list of objects, json list of strings,
  missing file, malformed json, 1-based ids.
- `test_utils.py`: json export round-trips; csv export has header and flattened
  `data`; unknown extension raises; `mask_credentials`.
- `test_handlers.py`: `load_handler` resolves default, rejects non-coroutine and
  bad spec; `fetch_ip` normalization tested with a fake page object exposing
  `goto` and `locator().inner_text()`.

Integration tests (`test_engine.py`), using a `conftest.py` fixture that starts
`http.server` in a daemon thread on an ephemeral port, serving:

- `/ip` → `{"origin": "<client ip>"}` JSON
- `/slow` → sleeps 5 s before responding
- `/boom` → HTTP 500

Cases:

1. Six tasks, concurrency 3, no proxies → six `ok` results, worker ids ⊆ {1,2,3},
   every result has `data["ip"]`.
2. A handler that raises on one task → that result `failed`, others `ok`,
   `completed == total`.
3. `--timeout 1` against `/slow` → result `timeout`, run completes.
4. Proxy pool containing `http://127.0.0.1:9` (unreachable) only → results
   `failed` with a non-empty error; the engine still finishes and the browser
   closes cleanly.
5. Context isolation: handler sets a cookie and returns `context.cookies()`;
   with two sequential tasks on one worker, the second sees zero cookies.

## 7. Dependencies

```
playwright>=1.47
rich>=13.7
# dev
pytest>=8
pytest-asyncio>=0.23
```

Target Python ≥ 3.11 (uses `X | None` unions, `dataclass(slots=True)`,
`asyncio.TaskGroup` not required). Developed on 3.14.

## 8. README Outline

1. What this is and what it teaches.
2. Quick start (`pip install -r requirements.txt`, `playwright install chromium`,
   `python main.py`).
3. Architecture diagram (one Browser → N workers → context per task).
4. Why one Browser, why a context per task, why a Queue-based pool.
5. Proxy file format and the SOCKS5-auth caveat.
6. Writing your own handler.
7. Reading `results.json` / `results.csv`.
8. Exercises: retries, proxy health, persistent sessions, multiple browser types.
