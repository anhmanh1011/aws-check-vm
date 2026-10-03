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

# Edit proxies.txt (optional) and input.txt, then:
python main.py
```

Common variations:

```bash
python main.py --concurrency 5 --output results.csv
python main.py --no-headless --timeout 60        # watch the browser work
python main.py --no-dashboard                     # plain logs, good for CI
python main.py --input emails.txt --batch-size 10 # 10 lines per browser context
python main.py --handler my_handlers:process_lines # your own flow over the lines
python main.py -v                                 # DEBUG-level logging
```

Exit code is `0` when every task succeeded, `1` when any task failed, `2`
for bad input files, a bad `--handler`, or a bad `--output` extension (only
`.json` and `.csv` are supported), and `130` after Ctrl+C (partial results
are still written).

## How it works

```
main.py ── argparse ──► load_tasks()        input.txt → batches of lines
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

## Input file and batching

`--input` (default `input.txt`) is a plain text file with one data item per
line: an email, a URL, an account ID, anything. Blank lines and `#` comments
are ignored. The framework never interprets the lines; your handler receives
them in `task.lines` and decides which site to open and what to click. The
run ends when every line has been processed.

`--batch-size` controls how many lines share one browser context:

| Value | Meaning |
| --- | --- |
| `1` (default) | one line per context: maximum isolation, one task per line |
| `N` | fixed batches of N lines; 1000 lines with `N=10` become 100 tasks that the queue hands to whichever of the `--concurrency` workers is free |
| `0` | auto: split evenly so every worker gets exactly one batch of `ceil(total / concurrency)` lines |

Each batch is one `Task`, one browser context, one row in the results. The
default handler treats lines as URLs, so the shipped `input.txt` lists
IP-echo endpoints.

## Writing your own handler

A handler is any coroutine with this signature:

```python
from playwright.async_api import Page
from models import Task

async def my_handler(page: Page, task: Task) -> dict:
    # task.lines is one batch from --input (a single line by default).
    # Your flow decides what a line means and which site to open.
    await page.goto("https://example.com/login")
    results = []
    for email in task.lines:
        await page.fill("#email", email)
        await page.click("button[type=submit]")
        results.append({"email": email, "status": await page.locator("#status").inner_text()})
    return {"results": results}
```

Save it in, say, `my_handlers.py` next to `main.py` and run
`python main.py --handler my_handlers:my_handler`. The returned dict is
stored in the `data` field of each result. The page you receive lives in a
fresh context, so log-ins, cookies and storage from other tasks are never
visible.

To keep a flow short, `actions.py` wraps the common steps so each one waits
for its element to be visible first: `goto`, `click`, `fill`, `get_text`,
`wait` (returns a locator), and `sleep`. They raise on timeout, which the
engine records as a `failed` result. Import what you need:
`from actions import goto, click, fill, get_text, sleep`.

Two worked examples ship in `my_handlers.py`. `extract_page` treats each
line as a URL: it reads the title, the first heading and the link count, and
saves a screenshot per line under `screenshots/`. `process_lines` treats each
line as data (an email) and submits every one through a single form on a
site the flow itself chooses, reusing the same page for the whole batch. Try
`python main.py --handler my_handlers:extract_page`, then read the comments
in that file to see why each Playwright call is written the way it is. The
tests in `tests/test_my_handlers.py` show how to test a handler of your own
against the local server in `tests/conftest.py`.

## Reading the results

`results.json` is a list of objects:

```json
{
  "task_id": 1,
  "name": "https://httpbin.org/ip (+1 more)",
  "inputs": ["https://httpbin.org/ip", "https://api.ipify.org?format=json"],
  "worker_id": 2,
  "proxy": "http://***:***@203.0.113.10:8080",
  "status": "ok",
  "started_at": "2026-10-03T09:15:02.123456+00:00",
  "duration_s": 1.482,
  "data": {"results": [
    {"url": "https://httpbin.org/ip", "ip": "203.0.113.10", "raw": "{\"origin\": \"203.0.113.10\"}"},
    {"url": "https://api.ipify.org?format=json", "ip": "203.0.113.10", "raw": "{\"ip\":\"203.0.113.10\"}"}
  ]},
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
