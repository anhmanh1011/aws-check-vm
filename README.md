# Async Playwright Automation Framework

An educational, production-style CLI that runs many browser tasks
concurrently with **one shared Playwright `Browser`**, **one isolated
`BrowserContext` per task**, and **a different proxy for each context**.
Out of the box it visits IP-echo endpoints so you can watch each context
report a different outgoing address.

## Quick start

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium

# Edit proxies.txt (optional) and tasks.txt, then:
python main.py
```

Common variations:

```bash
python main.py --concurrency 5 --output results.csv
python main.py --no-headless --timeout 60        # watch the browser work
python main.py --tasks tasks.json --no-dashboard  # plain logs, good for CI
python main.py --handler my_handlers:login_probe  # your own per-task logic
```

Exit code is `0` when every task succeeded, `1` when any task failed, `2`
for bad input files or a bad `--handler`, and `130` after Ctrl+C (partial
results are still written).

## How it works

```
main.py ── argparse ──► load_tasks()        tasks.txt / tasks.json
                     ──► ProxyManager        proxies.txt (round-robin)
                     ──► load_handler()      handlers:fetch_ip (default)
                     ──► AutomationEngine
                               │
                               │  one headless Browser
                               ▼
         ┌──────────── asyncio.Queue [t1, t2, t3, ..., None×N] ────────────┐
         │                        │                        │               │
     Worker 1                 Worker 2                 Worker 3     (N = --concurrency)
   ctx(proxy A)             ctx(proxy B)             ctx(proxy C)
   page → handler           page → handler           page → handler
   ctx.close()              ctx.close()              ctx.close()
         │                        │                        │
         └────────────► RunState.results  ◄───────────────┘
                               │                 ▲
                        write_results()      Dashboard (rich Live, 4 fps)
```

### Why one `Browser`

Launching Chromium takes seconds and hundreds of megabytes. Every task
sharing one browser process keeps start-up cost constant no matter how many
tasks you run.

### Why a `BrowserContext` per task

A context is Playwright's isolation boundary. It owns its cookies,
`localStorage`, cache, permissions and, crucially, its **proxy**. Creating
one costs tens of milliseconds. The engine creates a context, runs the
handler, and closes the context in a `finally` block, so no state ever leaks
between tasks even when they run on the same worker.

### Why a `Queue` and a fixed worker pool

Exactly `--concurrency` worker coroutines exist. Each pulls a task from an
`asyncio.Queue`, processes it, and pulls the next, until it receives a
`None` sentinel. This bounds live contexts by construction, gives each worker
a stable id for the dashboard, and demonstrates the standard asyncio
producer/consumer pattern. Everything runs on one thread, so the shared
`RunState` needs no locks.

### Why failures are recorded, not raised

A dead proxy or a slow site raises inside the worker. The engine catches
every `Exception` per task, records the type and message in the result, and
the worker keeps going. `asyncio.CancelledError` is not caught, so Ctrl+C
still stops everything cleanly and the browser is closed.

## Proxy file format

```
http://user:pass@host:port
http://host:port
https://host:port
socks5://host:port
socks4://host:port
```

Blank lines and `#` comments are ignored. Percent-encode special characters
in credentials (`user%40corp`). Credentials never appear in logs or results;
they are shown as `http://***:***@host:port`.

Proxies are assigned **round-robin**: the first context gets proxy 1, the
second gets proxy 2, and the pool wraps around when exhausted. A missing or
empty `proxies.txt` makes every context connect directly, with a warning.

**Chromium limitation:** authenticated SOCKS5 proxies are not supported by
Chromium; the credentials are ignored. Use HTTP proxies when you need
authentication.

## Task file format

`tasks.txt`: one URL per line.

`tasks.json`: a list of URL strings, or objects with `url` and an optional
`name`:

```json
[
  {"url": "https://httpbin.org/ip", "name": "httpbin"},
  "https://api.ipify.org?format=json"
]
```

## Writing your own handler

A handler is any coroutine with this signature:

```python
from playwright.async_api import Page
from models import Task

async def my_handler(page: Page, task: Task) -> dict:
    await page.goto(task.url)
    title = await page.title()
    return {"title": title}
```

Save it in, say, `my_handlers.py` next to `main.py` and run
`python main.py --handler my_handlers:my_handler`. The returned dict is
stored in the `data` field of each result. The page you receive lives in a
fresh context, so log-ins, cookies and storage from other tasks are never
visible.

## Reading the results

`results.json` is a list of objects:

```json
{
  "task_id": 1,
  "name": "https://httpbin.org/ip",
  "url": "https://httpbin.org/ip",
  "worker_id": 2,
  "proxy": "http://***:***@203.0.113.10:8080",
  "status": "ok",
  "started_at": "2026-10-03T09:15:02.123456+00:00",
  "duration_s": 1.482,
  "data": {"ip": "203.0.113.10", "raw": "{\"origin\": \"203.0.113.10\"}"},
  "error": null
}
```

`status` is `ok`, `failed` (any exception, including proxy and navigation
errors) or `timeout` (the handler exceeded `--timeout`). With `--output
results.csv` the same fields become columns and `data` is a JSON string.

While the dashboard is active, log lines go to `run.log` instead of the
terminal so they do not fight with the live table.

## Running the tests

```bash
python -m pytest
```

Unit tests need no browser. Tests marked `integration` launch real headless
Chromium against a local HTTP server started by `tests/conftest.py`; they are
skipped automatically if Chromium is not installed. Nothing in the test suite
contacts the public internet.

## Exercises

1. **Retries with back-off.** Add `--retries N` and re-queue a failed task
   with a fresh context and the next proxy.
2. **Proxy health.** Track failures per proxy in `ProxyManager` and skip
   proxies that fail three times in a row.
3. **Persistent sessions.** Add a handler that logs in, then use
   `context.storage_state()` to save and reuse the session.
4. **Other browsers.** `AutomationEngine` accepts `browser_type="firefox"` or
   `"webkit"`; expose it as a CLI flag and compare proxy behaviour.
