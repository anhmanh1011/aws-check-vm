# Async Playwright Automation Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an educational CLI that runs many web-automation tasks concurrently with one shared Playwright `Browser`, one isolated `BrowserContext` per task, round-robin proxy assignment per context, a `rich` live dashboard, and JSON/CSV result export.

**Architecture:** Flat top-level modules, each with one job. `main.py` wires argparse → `tasks.py` / `proxy_manager.py` / `handlers.py` → `engine.py`. The engine launches one headless browser, fills an `asyncio.Queue`, and runs exactly `--concurrency` worker coroutines; each worker creates a fresh context (with the next proxy) per task, runs the handler under a timeout, records a `TaskResult`, and closes the context. `dashboard.py` renders a shared `RunState` with `rich.live.Live`. All tests use a local threaded HTTP server; none hit the public internet.

**Tech Stack:** Python ≥ 3.11 (developed on 3.14), `playwright`, `rich`, `pytest`, `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-10-03-async-playwright-framework-design.md`

## Global Constraints

- Python ≥ 3.11; use `X | None` unions and `dataclass(slots=True)`.
- Dependencies: `playwright>=1.47`, `rich>=13.7`, `pytest>=8`, `pytest-asyncio>=0.23`. Nothing else.
- Flat module layout: `main.py`, `engine.py`, `proxy_manager.py`, `tasks.py`, `handlers.py`, `dashboard.py`, `utils.py`, `models.py` at repo root; tests in `tests/`.
- Every module starts with a module docstring that states what it does and WHY the pattern was chosen. Public functions/classes have docstrings. Type hints everywhere. PEP 8.
- Exit codes: `0` all tasks ok, `1` some task failed or browser launch failed, `2` bad input (task file, proxy file, handler spec), `130` Ctrl+C.
- Credentials never appear unmasked in logs or results. Masked form: `scheme://***:***@host:port`.
- Tests never contact the public internet. Browser-backed tests are marked `integration` and skip when Chromium is not installed.
- Default CLI values: `--tasks tasks.txt`, `--proxies proxies.txt`, `--concurrency 3`, `--headless` (true), `--output results.json`, `--handler handlers:fetch_ip`, `--timeout 30`.
- Run every `pytest` command from the repo root `D:\workspace\AWS`.
- Commit after every task with the exact message given. Git identity: configure once in Task 1.

---

### Task 1: Project scaffolding and `models.py`

**Files:**
- Create: `requirements.txt`, `pytest.ini`, `.gitignore` (modify existing), `models.py`
- Test: `tests/__init__.py` (empty), `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces (used by every later task):
  - `models.Task(id: int, url: str, name: str)` frozen dataclass.
  - `models.Proxy(server: str, username: str | None = None, password: str | None = None)` frozen dataclass with `to_playwright() -> dict[str, str]` and `masked() -> str`.
  - `models.TaskResult(task_id, name, url, worker_id, proxy, status, started_at, duration_s, data, error)`.
  - `models.WorkerStatus(worker_id, task=None, proxy=None, state="idle", started_at=None)`.
  - `models.RunState(total, completed=0, ok=0, failed=0, workers={}, results=[])`.
  - Type aliases `models.Status = Literal["ok", "failed", "timeout"]`, `models.WorkerState = Literal["idle", "running", "done"]`.

- [ ] **Step 1: Configure git identity and install dependencies**

```bash
cd D:/workspace/AWS
git config user.name "Dao Duc Manh"
git config user.email "daoducmanh28101997@outlook.com"
```

Create `requirements.txt`:

```text
# Runtime
playwright>=1.47
rich>=13.7

# Development / tests
pytest>=8
pytest-asyncio>=0.23
```

Install and fetch Chromium:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Expected: both commands exit 0. If `pip install playwright` fails to find a wheel for Python 3.14, create a Python 3.12 virtual environment (`py -3.12 -m venv .venv`, activate, repeat the two commands) and use it for every later step.

- [ ] **Step 2: Create `pytest.ini` and extend `.gitignore`**

`pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
pythonpath = .
markers =
    integration: launches a real headless browser (skipped when Chromium is missing)
```

Replace `.gitignore` contents with:

```text
.playwright-mcp/
__pycache__/
*.pyc
.pytest_cache/
.venv/
results.json
results.csv
run.log
```

Create empty `tests/__init__.py`.

- [ ] **Step 3: Write the failing tests for `models.py`**

`tests/test_models.py`:

```python
"""Tests for the shared dataclasses in models.py."""

from models import Proxy, RunState, Task, TaskResult, WorkerStatus


def test_proxy_to_playwright_without_credentials_has_only_server():
    proxy = Proxy(server="socks5://10.0.0.1:1080")
    assert proxy.to_playwright() == {"server": "socks5://10.0.0.1:1080"}


def test_proxy_to_playwright_with_credentials():
    proxy = Proxy(server="http://1.2.3.4:8080", username="alice", password="s3cret")
    assert proxy.to_playwright() == {
        "server": "http://1.2.3.4:8080",
        "username": "alice",
        "password": "s3cret",
    }


def test_proxy_masked_hides_credentials():
    proxy = Proxy(server="http://1.2.3.4:8080", username="alice", password="s3cret")
    assert proxy.masked() == "http://***:***@1.2.3.4:8080"


def test_proxy_masked_without_credentials_is_server():
    proxy = Proxy(server="http://1.2.3.4:8080")
    assert proxy.masked() == "http://1.2.3.4:8080"


def test_task_is_frozen():
    task = Task(id=1, url="https://example.com", name="example")
    try:
        task.url = "https://other"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("Task should be immutable")


def test_worker_status_defaults_to_idle():
    status = WorkerStatus(worker_id=1)
    assert status.state == "idle"
    assert status.task is None
    assert status.proxy is None
    assert status.started_at is None


def test_run_state_defaults():
    state = RunState(total=5)
    assert (state.completed, state.ok, state.failed) == (0, 0, 0)
    assert state.workers == {}
    assert state.results == []


def test_run_state_collections_are_not_shared_between_instances():
    a = RunState(total=1)
    b = RunState(total=1)
    a.results.append(
        TaskResult(
            task_id=1, name="n", url="u", worker_id=1, proxy=None, status="ok",
            started_at="2026-01-01T00:00:00+00:00", duration_s=0.1, data={}, error=None,
        )
    )
    assert b.results == []
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/test_models.py -v`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'models'`.

- [ ] **Step 5: Implement `models.py`**

```python
"""Shared data types for the automation framework.

WHY a separate module: ``tasks.py`` needs ``Task``, ``proxy_manager.py`` needs
``Proxy``, and ``engine.py`` / ``dashboard.py`` need all of them. Keeping the
types here means every module imports from one place and there are no
circular imports. Dataclasses with ``slots=True`` are cheap to create, which
matters because we build one ``TaskResult`` per task and one ``Proxy`` per
proxy line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

Status = Literal["ok", "failed", "timeout"]
WorkerState = Literal["idle", "running", "done"]


@dataclass(frozen=True, slots=True)
class Task:
    """One unit of work: a URL plus a human-readable name.

    ``id`` is the 1-based position in the task file so results can be
    correlated back to the input.
    """

    id: int
    url: str
    name: str


@dataclass(frozen=True, slots=True)
class Proxy:
    """A parsed proxy endpoint.

    ``server`` never carries credentials; they live in ``username`` /
    ``password`` so the masked form can be produced without re-parsing.
    """

    server: str
    username: str | None = None
    password: str | None = None

    def to_playwright(self) -> dict[str, str]:
        """Return the dict shape Playwright expects for ``new_context(proxy=...)``."""
        options: dict[str, str] = {"server": self.server}
        if self.username is not None:
            options["username"] = self.username
        if self.password is not None:
            options["password"] = self.password
        return options

    def masked(self) -> str:
        """Return a log-safe representation with credentials replaced by ``***``."""
        if self.username is None and self.password is None:
            return self.server
        parts = urlsplit(self.server)
        return f"{parts.scheme}://***:***@{parts.netloc}"


@dataclass(slots=True)
class TaskResult:
    """Outcome of running one task inside one browser context."""

    task_id: int
    name: str
    url: str
    worker_id: int
    proxy: str | None
    status: Status
    started_at: str
    duration_s: float
    data: dict[str, Any]
    error: str | None


@dataclass(slots=True)
class WorkerStatus:
    """Live view of one worker, read by the dashboard."""

    worker_id: int
    task: Task | None = None
    proxy: str | None = None
    state: WorkerState = "idle"
    started_at: float | None = None


@dataclass(slots=True)
class RunState:
    """Mutable shared state for a whole run.

    WHY no locks: asyncio runs all coroutines on one thread, and control only
    switches at ``await`` points. Workers mutate this object between awaits and
    the dashboard reads it between awaits, so plain attribute access is safe.
    """

    total: int
    completed: int = 0
    ok: int = 0
    failed: int = 0
    workers: dict[int, WorkerStatus] = field(default_factory=dict)
    results: list[TaskResult] = field(default_factory=list)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_models.py -v`
Expected: 8 passed.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt pytest.ini .gitignore models.py tests/__init__.py tests/test_models.py
git commit -m "feat: project scaffolding and shared dataclasses"
```

---

### Task 2: `proxy_manager.py`

**Files:**
- Create: `proxy_manager.py`
- Test: `tests/test_proxy_manager.py`

**Interfaces:**
- Consumes: `models.Proxy`.
- Produces:
  - `proxy_manager.parse_proxy_line(line: str) -> Proxy | None` (None for blank/comment, `ValueError` on malformed).
  - `proxy_manager.ProxyManager(proxies: Sequence[Proxy])` with `from_file(path: Path) -> ProxyManager` (classmethod), `next() -> Proxy | None`, `__len__() -> int`.

- [ ] **Step 1: Write the failing tests**

`tests/test_proxy_manager.py`:

```python
"""Tests for proxy line parsing and round-robin assignment."""

from pathlib import Path

import pytest

from models import Proxy
from proxy_manager import ProxyManager, parse_proxy_line


def test_parse_http_with_credentials():
    proxy = parse_proxy_line("http://alice:s3cret@1.2.3.4:8080")
    assert proxy == Proxy(server="http://1.2.3.4:8080", username="alice", password="s3cret")


def test_parse_http_without_credentials():
    assert parse_proxy_line("http://1.2.3.4:8080") == Proxy(server="http://1.2.3.4:8080")


def test_parse_socks5():
    assert parse_proxy_line("socks5://10.0.0.1:1080") == Proxy(server="socks5://10.0.0.1:1080")


def test_parse_https_scheme():
    assert parse_proxy_line("https://proxy.example:443") == Proxy(server="https://proxy.example:443")


def test_parse_url_encoded_credentials():
    proxy = parse_proxy_line("http://user%40corp:p%40ss@1.2.3.4:8080")
    assert proxy is not None
    assert proxy.username == "user@corp"
    assert proxy.password == "p@ss"


def test_parse_strips_whitespace():
    assert parse_proxy_line("  http://1.2.3.4:8080  \n") == Proxy(server="http://1.2.3.4:8080")


@pytest.mark.parametrize("line", ["", "   ", "# a comment", "   # indented comment"])
def test_parse_blank_and_comment_lines_return_none(line):
    assert parse_proxy_line(line) is None


@pytest.mark.parametrize(
    "line",
    [
        "ftp://1.2.3.4:21",          # unsupported scheme
        "1.2.3.4:8080",              # no scheme
        "http://1.2.3.4",            # missing port
        "http://:8080",              # missing host
        "http://1.2.3.4:notaport",   # bad port
    ],
)
def test_parse_malformed_lines_raise(line):
    with pytest.raises(ValueError):
        parse_proxy_line(line)


def test_next_round_robins_and_wraps():
    a, b, c = (Proxy(server=f"http://10.0.0.{i}:8080") for i in (1, 2, 3))
    manager = ProxyManager([a, b, c])
    assert [manager.next() for _ in range(5)] == [a, b, c, a, b]
    assert len(manager) == 3


def test_empty_pool_returns_none():
    manager = ProxyManager([])
    assert manager.next() is None
    assert manager.next() is None
    assert len(manager) == 0


def test_from_file_missing_path_returns_empty_manager(tmp_path: Path, caplog):
    manager = ProxyManager.from_file(tmp_path / "nope.txt")
    assert len(manager) == 0
    assert "not found" in caplog.text


def test_from_file_skips_comments_and_blanks(tmp_path: Path):
    path = tmp_path / "proxies.txt"
    path.write_text(
        "# proxies\n\nhttp://alice:pw@1.2.3.4:8080\n\nsocks5://10.0.0.1:1080\n",
        encoding="utf-8",
    )
    manager = ProxyManager.from_file(path)
    assert len(manager) == 2
    assert manager.next() == Proxy(server="http://1.2.3.4:8080", username="alice", password="pw")
    assert manager.next() == Proxy(server="socks5://10.0.0.1:1080")


def test_from_file_reports_line_number_on_error(tmp_path: Path):
    path = tmp_path / "proxies.txt"
    path.write_text("http://1.2.3.4:8080\nftp://bad:21\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"proxies\.txt:2"):
        ProxyManager.from_file(path)


def test_from_file_only_comments_warns_and_is_empty(tmp_path: Path, caplog):
    path = tmp_path / "proxies.txt"
    path.write_text("# nothing here\n", encoding="utf-8")
    manager = ProxyManager.from_file(path)
    assert len(manager) == 0
    assert "no proxies" in caplog.text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_proxy_manager.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'proxy_manager'`.

- [ ] **Step 3: Implement `proxy_manager.py`**

```python
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
from collections.abc import Iterator, Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

from models import Proxy

log = logging.getLogger(__name__)

SUPPORTED_SCHEMES = frozenset({"http", "https", "socks4", "socks5"})


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
            f"unsupported proxy scheme {parts.scheme!r} in {stripped!r} "
            f"(expected one of {sorted(SUPPORTED_SCHEMES)})"
        )
    if not parts.hostname:
        raise ValueError(f"missing host in proxy {stripped!r}")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid port in proxy {stripped!r}") from exc
    if port is None:
        raise ValueError(f"missing port in proxy {stripped!r}")

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

    def __len__(self) -> int:
        return len(self._proxies)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_proxy_manager.py -v`
Expected: all passed (20 tests including parametrized cases).

- [ ] **Step 5: Commit**

```bash
git add proxy_manager.py tests/test_proxy_manager.py
git commit -m "feat: proxy file parser and round-robin ProxyManager"
```

---

### Task 3: `tasks.py`

**Files:**
- Create: `tasks.py`
- Test: `tests/test_tasks.py`

**Interfaces:**
- Consumes: `models.Task`.
- Produces: `tasks.load_tasks(path: Path) -> list[Task]` (raises `FileNotFoundError` or `ValueError`).

- [ ] **Step 1: Write the failing tests**

`tests/test_tasks.py`:

```python
"""Tests for task file loading (.txt and .json)."""

import json
from pathlib import Path

import pytest

from models import Task
from tasks import load_tasks


def test_txt_one_url_per_line_with_comments_and_blanks(tmp_path: Path):
    path = tmp_path / "tasks.txt"
    path.write_text(
        "# targets\nhttps://httpbin.org/ip\n\n  https://api.ipify.org?format=json  \n",
        encoding="utf-8",
    )
    assert load_tasks(path) == [
        Task(id=1, url="https://httpbin.org/ip", name="https://httpbin.org/ip"),
        Task(id=2, url="https://api.ipify.org?format=json", name="https://api.ipify.org?format=json"),
    ]


def test_json_list_of_objects_with_optional_name(tmp_path: Path):
    path = tmp_path / "tasks.json"
    path.write_text(
        json.dumps([
            {"url": "https://httpbin.org/ip", "name": "httpbin"},
            {"url": "https://api.ipify.org?format=json"},
        ]),
        encoding="utf-8",
    )
    assert load_tasks(path) == [
        Task(id=1, url="https://httpbin.org/ip", name="httpbin"),
        Task(id=2, url="https://api.ipify.org?format=json", name="https://api.ipify.org?format=json"),
    ]


def test_json_list_of_strings(tmp_path: Path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(["https://a.example", "https://b.example"]), encoding="utf-8")
    tasks = load_tasks(path)
    assert [t.id for t in tasks] == [1, 2]
    assert tasks[1].name == "https://b.example"


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="nope.txt"):
        load_tasks(tmp_path / "nope.txt")


def test_malformed_json_raises_value_error(tmp_path: Path):
    path = tmp_path / "tasks.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_tasks(path)


def test_json_not_a_list_raises(tmp_path: Path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"url": "https://a.example"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list"):
        load_tasks(path)


def test_json_item_without_url_raises(tmp_path: Path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps([{"name": "no url"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="task #1"):
        load_tasks(path)


def test_unsupported_extension_raises(tmp_path: Path):
    path = tmp_path / "tasks.yaml"
    path.write_text("- https://a.example\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported task file type"):
        load_tasks(path)


def test_empty_file_raises(tmp_path: Path):
    path = tmp_path / "tasks.txt"
    path.write_text("# only a comment\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no tasks"):
        load_tasks(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tasks.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'tasks'`.

- [ ] **Step 3: Implement `tasks.py`**

```python
"""Load tasks from ``tasks.txt`` (one URL per line) or ``tasks.json``.

WHY two formats: a plain text file is the fastest way to try the tool, while
JSON lets a task carry a friendly ``name`` (and, if you extend the framework,
any extra parameters your handler needs). Both produce the same ``Task``
objects, so nothing downstream cares which one was used.
"""

from __future__ import annotations

import json
from pathlib import Path

from models import Task


def load_tasks(path: Path) -> list[Task]:
    """Read a task file and return 1-indexed ``Task`` objects.

    Raises ``FileNotFoundError`` if the file is missing and ``ValueError`` for
    an unsupported extension, malformed content, or an empty task list.
    """
    if not path.exists():
        raise FileNotFoundError(f"task file not found: {path}")

    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".txt":
        entries = _parse_txt(text)
    elif suffix == ".json":
        entries = _parse_json(text)
    else:
        raise ValueError(f"unsupported task file type {suffix!r} (use .txt or .json)")

    if not entries:
        raise ValueError(f"no tasks found in {path}")

    return [
        Task(id=index, url=url, name=name)
        for index, (url, name) in enumerate(entries, start=1)
    ]


def _parse_txt(text: str) -> list[tuple[str, str]]:
    """One URL per line; blank lines and ``#`` comments are ignored."""
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append((stripped, stripped))
    return entries


def _parse_json(text: str) -> list[tuple[str, str]]:
    """A JSON list of URL strings or ``{"url": ..., "name"?: ...}`` objects."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in task file: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("task JSON must be a list of URLs or objects with a 'url' key")

    entries: list[tuple[str, str]] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            entries.append((item, item))
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            url = item["url"]
            entries.append((url, str(item.get("name") or url)))
        else:
            raise ValueError(
                f"task #{index} must be a URL string or an object with a string 'url' key"
            )
    return entries
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tasks.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add tasks.py tests/test_tasks.py
git commit -m "feat: load tasks from txt or json files"
```

---

### Task 4: `utils.py` (export, logging, masking)

**Files:**
- Create: `utils.py`
- Test: `tests/test_utils.py`

**Interfaces:**
- Consumes: `models.TaskResult`.
- Produces:
  - `utils.mask_credentials(url: str) -> str`
  - `utils.write_results(results: Sequence[TaskResult], path: Path) -> None` (`.json` or `.csv`, else `ValueError`)
  - `utils.setup_logging(*, verbose: bool, dashboard_active: bool, log_file: Path = Path("run.log")) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_utils.py`:

```python
"""Tests for result export, logging setup, and credential masking."""

import csv
import json
import logging
from pathlib import Path

import pytest

from models import TaskResult
from utils import mask_credentials, setup_logging, write_results


def _result(task_id: int, status: str = "ok") -> TaskResult:
    return TaskResult(
        task_id=task_id,
        name=f"task-{task_id}",
        url=f"http://127.0.0.1/ip?n={task_id}",
        worker_id=1,
        proxy="http://***:***@1.2.3.4:8080",
        status=status,  # type: ignore[arg-type]
        started_at="2026-10-03T00:00:00+00:00",
        duration_s=0.5,
        data={"ip": "1.2.3.4", "raw": '{"origin": "1.2.3.4"}'},
        error=None if status == "ok" else "RuntimeError: boom",
    )


def test_mask_credentials_replaces_user_and_password():
    assert mask_credentials("http://alice:pw@1.2.3.4:8080/path?q=1") == "http://***:***@1.2.3.4:8080/path?q=1"


def test_mask_credentials_leaves_plain_url_alone():
    assert mask_credentials("https://example.com/x") == "https://example.com/x"


def test_write_results_json_round_trips(tmp_path: Path):
    out = tmp_path / "results.json"
    write_results([_result(1), _result(2, "failed")], out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [d["task_id"] for d in data] == [1, 2]
    assert data[0]["data"] == {"ip": "1.2.3.4", "raw": '{"origin": "1.2.3.4"}'}
    assert data[1]["status"] == "failed"
    assert data[1]["error"] == "RuntimeError: boom"


def test_write_results_csv_has_header_and_flattened_data(tmp_path: Path):
    out = tmp_path / "results.csv"
    write_results([_result(1)], out)
    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == "1"
    assert row["proxy"] == "http://***:***@1.2.3.4:8080"
    assert json.loads(row["data"]) == {"ip": "1.2.3.4", "raw": '{"origin": "1.2.3.4"}'}
    assert set(row) == {
        "task_id", "name", "url", "worker_id", "proxy", "status",
        "started_at", "duration_s", "data", "error",
    }


def test_write_results_empty_list_still_writes_file(tmp_path: Path):
    out = tmp_path / "results.json"
    write_results([], out)
    assert json.loads(out.read_text(encoding="utf-8")) == []


def test_write_results_unknown_extension_raises(tmp_path: Path):
    with pytest.raises(ValueError, match=r"\.xml"):
        write_results([], tmp_path / "results.xml")


def test_setup_logging_dashboard_mode_writes_to_file(tmp_path: Path):
    log_file = tmp_path / "run.log"
    setup_logging(verbose=False, dashboard_active=True, log_file=log_file)
    logging.getLogger("probe").info("hello from test")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hello from test" in log_file.read_text(encoding="utf-8")


def test_setup_logging_verbose_sets_debug_level(tmp_path: Path):
    setup_logging(verbose=True, dashboard_active=True, log_file=tmp_path / "run.log")
    assert logging.getLogger().level == logging.DEBUG


def test_setup_logging_plain_mode_uses_rich_handler():
    from rich.logging import RichHandler

    setup_logging(verbose=False, dashboard_active=False)
    assert any(isinstance(h, RichHandler) for h in logging.getLogger().handlers)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_utils.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'utils'`.

- [ ] **Step 3: Implement `utils.py`**

```python
"""Cross-cutting helpers: result export, logging setup, credential masking.

WHY logging goes to a file while the dashboard runs: ``rich.live.Live``
redraws a region of the terminal several times a second. Any other writer to
stderr would be painted over or would corrupt the table, so in dashboard mode
log records are appended to ``run.log`` instead. Without the dashboard, a
``RichHandler`` prints readable, colourised log lines.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, fields
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from rich.logging import RichHandler

from models import TaskResult


def mask_credentials(url: str) -> str:
    """Replace ``user:pass`` in a URL with ``***:***``; return other URLs unchanged."""
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, parts.query, parts.fragment))


def write_results(results: Sequence[TaskResult], path: Path) -> None:
    """Write results as JSON (``.json``) or CSV (``.csv``) based on the extension.

    In CSV mode the nested ``data`` dict is serialised to a JSON string so the
    file stays a flat table with one row per task.
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        path.write_text(
            json.dumps([asdict(result) for result in results], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    elif suffix == ".csv":
        fieldnames = [field.name for field in fields(TaskResult)]
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for result in results:
                row = asdict(result)
                row["data"] = json.dumps(row["data"], ensure_ascii=False)
                writer.writerow(row)
    else:
        raise ValueError(f"unsupported output extension {suffix!r} (use .json or .csv)")


def setup_logging(
    *, verbose: bool, dashboard_active: bool, log_file: Path = Path("run.log")
) -> None:
    """Configure the root logger exactly once per process.

    ``dashboard_active=True`` sends records to ``log_file`` so they do not
    fight with the live table; otherwise records go to the terminal through
    ``RichHandler``.
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)

    handler: logging.Handler
    if dashboard_active:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    else:
        handler = RichHandler(show_path=False, rich_tracebacks=False)
        handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_utils.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add utils.py tests/test_utils.py
git commit -m "feat: result export, logging setup, and credential masking"
```

---

### Task 5: `handlers.py`

**Files:**
- Create: `handlers.py`
- Test: `tests/test_handlers.py`

**Interfaces:**
- Consumes: `models.Task`.
- Produces:
  - `handlers.Handler` type alias: `Callable[[Page, Task], Awaitable[dict[str, Any]]]`
  - `handlers.DEFAULT_HANDLER = "handlers:fetch_ip"`
  - `async handlers.fetch_ip(page: Page, task: Task) -> dict[str, Any]` returning `{"ip": str | None, "raw": str}`
  - `handlers.load_handler(spec: str) -> Handler` (raises `ValueError`)

- [ ] **Step 1: Write the failing tests**

`tests/test_handlers.py`:

```python
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


TASK = Task(id=1, url="https://httpbin.org/ip", name="ip")


async def test_fetch_ip_reads_httpbin_origin():
    page = _FakePage('{"origin": "203.0.113.7"}')
    result = await fetch_ip(page, TASK)  # type: ignore[arg-type]
    assert result == {"ip": "203.0.113.7", "raw": '{"origin": "203.0.113.7"}'}
    assert page.visited[0][0] == "https://httpbin.org/ip"
    assert page.visited[0][1].get("wait_until") == "domcontentloaded"


async def test_fetch_ip_reads_ipify_ip_key():
    page = _FakePage('{"ip": "198.51.100.9"}')
    result = await fetch_ip(page, TASK)  # type: ignore[arg-type]
    assert result["ip"] == "198.51.100.9"


async def test_fetch_ip_non_json_body_returns_raw_and_none_ip():
    page = _FakePage("<html>not json</html>")
    result = await fetch_ip(page, TASK)  # type: ignore[arg-type]
    assert result == {"ip": None, "raw": "<html>not json</html>"}


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_handlers.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'handlers'`.

- [ ] **Step 3: Implement `handlers.py`**

```python
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
    """Navigate to ``task.url`` and extract the reported IP address.

    ``wait_until="domcontentloaded"`` is enough for a JSON endpoint and
    returns sooner than the default ``load`` event.
    """
    await page.goto(task.url, wait_until="domcontentloaded")
    raw = await page.locator("body").inner_text()

    ip: str | None = None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        value = payload.get("origin") or payload.get("ip")
        ip = str(value) if value is not None else None

    return {"ip": ip, "raw": raw}


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_handlers.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add handlers.py tests/test_handlers.py
git commit -m "feat: handler contract, default fetch_ip handler, and loader"
```

---

### Task 6: `engine.py` with local-server integration tests

**Files:**
- Create: `engine.py`, `tests/conftest.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `models.Task`, `models.Proxy`, `models.TaskResult`, `models.WorkerStatus`, `models.RunState`, `models.Status`, `proxy_manager.ProxyManager` (`next()`, `__len__`), `handlers.Handler`.
- Produces:
  - `engine.AutomationEngine(tasks: Sequence[Task], proxy_manager: ProxyManager, handler: Handler, *, concurrency: int, headless: bool, timeout_s: float, state: RunState, browser_type: str = "chromium")`
  - `async engine.AutomationEngine.run() -> list[TaskResult]`
  - Test fixtures: `local_server` (session-scoped, yields base URL string serving `/ip`, `/slow`, `/boom`) and `require_chromium` (skips when Chromium is not installed).

- [ ] **Step 1: Write `tests/conftest.py`**

```python
"""Shared pytest fixtures.

``local_server`` runs a tiny HTTP server in a daemon thread so engine tests
never touch the public internet. ``require_chromium`` skips browser-backed
tests on machines where ``playwright install chromium`` has not been run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _EchoHandler(BaseHTTPRequestHandler):
    """Routes: ``/ip`` -> JSON origin, ``/slow`` -> 3 s delay, ``/boom`` -> 500."""

    def do_GET(self) -> None:  # noqa: N802 (name mandated by BaseHTTPRequestHandler)
        if self.path.startswith("/ip"):
            self._send(200, json.dumps({"origin": self.client_address[0]}), "application/json")
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
```

- [ ] **Step 2: Write the failing integration tests**

`tests/test_engine.py`:

```python
"""Integration tests: real headless Chromium against the local echo server."""

from __future__ import annotations

import pytest

from engine import AutomationEngine
from handlers import fetch_ip
from models import Proxy, RunState, Task
from proxy_manager import ProxyManager

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_chromium")]


def _tasks(base_url: str, count: int, path: str = "/ip") -> list[Task]:
    return [
        Task(id=i, url=f"{base_url}{path}?n={i}", name=f"task-{i}")
        for i in range(1, count + 1)
    ]


def _engine(tasks, *, handler=fetch_ip, proxies=(), concurrency=3, timeout_s=30.0):
    state = RunState(total=len(tasks))
    engine = AutomationEngine(
        tasks,
        ProxyManager(list(proxies)),
        handler,
        concurrency=concurrency,
        headless=True,
        timeout_s=timeout_s,
        state=state,
    )
    return engine, state


def test_concurrency_must_be_positive():
    with pytest.raises(ValueError):
        AutomationEngine(
            [], ProxyManager([]), fetch_ip,
            concurrency=0, headless=True, timeout_s=1.0, state=RunState(total=0),
        )


async def test_all_tasks_succeed_across_workers(local_server):
    engine, state = _engine(_tasks(local_server, 6))
    results = await engine.run()

    assert len(results) == 6
    assert {r.status for r in results} == {"ok"}
    assert {r.worker_id for r in results} <= {1, 2, 3}
    assert all(r.data["ip"] == "127.0.0.1" for r in results)
    assert all(r.proxy is None for r in results)
    assert (state.completed, state.ok, state.failed) == (6, 6, 0)
    assert all(w.state == "done" for w in state.workers.values())
    assert sorted(state.workers) == [1, 2, 3]


async def test_handler_exception_does_not_stop_other_workers(local_server):
    async def flaky(page, task):
        if task.id == 2:
            raise RuntimeError("kaboom")
        return await fetch_ip(page, task)

    engine, state = _engine(_tasks(local_server, 4), handler=flaky)
    results = await engine.run()
    by_id = {r.task_id: r for r in results}

    assert by_id[2].status == "failed"
    assert by_id[2].error == "RuntimeError: kaboom"
    assert by_id[2].data == {}
    assert all(by_id[i].status == "ok" for i in (1, 3, 4))
    assert (state.completed, state.ok, state.failed) == (4, 3, 1)


async def test_slow_handler_times_out(local_server):
    engine, state = _engine(_tasks(local_server, 1, path="/slow"), timeout_s=1.0)
    (result,) = await engine.run()

    assert result.status == "timeout"
    assert result.error is not None and "1.0" in result.error
    assert result.duration_s < 2.5
    assert state.failed == 1


async def test_unreachable_proxy_is_recorded_as_failure(local_server):
    # Chromium bypasses proxies for loopback addresses, so target a
    # non-loopback hostname. Name resolution is delegated to an HTTP proxy,
    # and the proxy refuses the connection first.
    bad_proxy = Proxy(server="http://127.0.0.1:9")
    tasks = [
        Task(id=1, url="http://proxy-test.invalid/ip", name="via-bad-proxy-1"),
        Task(id=2, url="http://proxy-test.invalid/ip", name="via-bad-proxy-2"),
    ]
    engine, state = _engine(tasks, proxies=[bad_proxy], concurrency=2)
    results = await engine.run()

    assert len(results) == 2
    assert all(r.status == "failed" for r in results)
    assert all(r.error and "ERR_PROXY" in r.error for r in results)
    assert all(r.proxy == "http://127.0.0.1:9" for r in results)
    assert state.completed == 2


async def test_contexts_do_not_share_cookies(local_server):
    async def cookie_probe(page, task):
        await page.goto(task.url)
        before = len(await page.context.cookies())
        await page.context.add_cookies([{"name": "seen", "value": "1", "url": task.url}])
        return {"cookies_before": before}

    engine, _ = _engine(_tasks(local_server, 3), handler=cookie_probe, concurrency=1)
    results = await engine.run()

    assert [r.worker_id for r in results] == [1, 1, 1]
    assert [r.data["cookies_before"] for r in results] == [0, 0, 0]


async def test_non_dict_handler_return_is_wrapped(local_server):
    async def returns_text(page, task):
        await page.goto(task.url)
        return "plain string"

    engine, _ = _engine(_tasks(local_server, 1), handler=returns_text)
    (result,) = await engine.run()
    assert result.status == "ok"
    assert result.data == {"value": "plain string"}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_engine.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'engine'`.

- [ ] **Step 4: Implement `engine.py`**

```python
"""The concurrency core: one Browser, N workers, one BrowserContext per task.

Pattern summary
---------------
* **One ``Browser``.** Launching Chromium costs seconds and hundreds of MB.
  We pay that once per run.
* **One ``BrowserContext`` per task.** A context is Playwright's isolation
  unit: it owns its cookies, localStorage, cache, and proxy. Creating one
  costs tens of milliseconds, so giving every task a pristine identity is
  cheap. We destroy it afterwards so nothing leaks to the next task.
* **A fixed pool of worker coroutines fed by ``asyncio.Queue``.** Exactly
  ``concurrency`` workers exist, so at most that many contexts are alive at
  once. Workers pull tasks until they receive a ``None`` sentinel. This is
  the classic producer/consumer shape and gives each worker a stable id for
  the dashboard.
* **Failures are data, not exceptions.** Every per-task error is captured in
  a ``TaskResult`` and the worker moves on. One dead proxy must never stop
  the other workers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Browser, BrowserContext, async_playwright

from handlers import Handler
from models import Proxy, RunState, Status, Task, TaskResult, WorkerStatus
from proxy_manager import ProxyManager

log = logging.getLogger(__name__)

# WHY: on Windows, Chromium only honours a per-context proxy when the browser
# itself was launched with a proxy. Playwright documents a placeholder server
# for exactly this situation; it is never actually contacted when every
# context overrides it.
_PER_CONTEXT_PROXY_PLACEHOLDER = {"server": "http://per-context"}


class AutomationEngine:
    """Runs every task through a bounded pool of workers sharing one browser."""

    def __init__(
        self,
        tasks: Sequence[Task],
        proxy_manager: ProxyManager,
        handler: Handler,
        *,
        concurrency: int,
        headless: bool,
        timeout_s: float,
        state: RunState,
        browser_type: str = "chromium",
    ) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        self._tasks = list(tasks)
        self._proxy_manager = proxy_manager
        self._handler = handler
        self._concurrency = concurrency
        self._headless = headless
        self._timeout_s = timeout_s
        self._state = state
        self._browser_type = browser_type

    async def run(self) -> list[TaskResult]:
        """Launch the browser, drain the task queue with N workers, close the browser."""
        queue: asyncio.Queue[Task | None] = asyncio.Queue()
        for task in self._tasks:
            queue.put_nowait(task)
        # One sentinel per worker: each worker exits after consuming exactly one.
        for _ in range(self._concurrency):
            queue.put_nowait(None)

        for worker_id in range(1, self._concurrency + 1):
            self._state.workers[worker_id] = WorkerStatus(worker_id=worker_id)

        async with async_playwright() as playwright:
            launcher = getattr(playwright, self._browser_type)
            launch_kwargs: dict[str, Any] = {"headless": self._headless}
            if len(self._proxy_manager) > 0:
                launch_kwargs["proxy"] = _PER_CONTEXT_PROXY_PLACEHOLDER
            browser: Browser = await launcher.launch(**launch_kwargs)
            log.info(
                "Launched %s (headless=%s) with %d workers for %d tasks",
                self._browser_type, self._headless, self._concurrency, len(self._tasks),
            )
            try:
                await asyncio.gather(
                    *(self._worker(worker_id, queue, browser)
                      for worker_id in range(1, self._concurrency + 1))
                )
            finally:
                # Runs on success, on error, and on cancellation (Ctrl+C), so the
                # browser process never outlives the Python process.
                await browser.close()

        return list(self._state.results)

    async def _worker(
        self, worker_id: int, queue: asyncio.Queue[Task | None], browser: Browser
    ) -> None:
        """Consume tasks until the ``None`` sentinel arrives."""
        status = self._state.workers[worker_id]
        while True:
            task = await queue.get()
            if task is None:
                status.state = "done"
                status.task = None
                status.proxy = None
                status.started_at = None
                log.debug("worker %d finished", worker_id)
                return

            proxy = self._proxy_manager.next()
            status.task = task
            status.proxy = proxy.masked() if proxy else None
            status.state = "running"
            status.started_at = time.monotonic()
            log.info("worker %d -> task %d (%s) via %s",
                     worker_id, task.id, task.name, status.proxy or "direct")

            result = await self._run_one(worker_id, task, proxy, browser)

            self._state.results.append(result)
            self._state.completed += 1
            if result.status == "ok":
                self._state.ok += 1
            else:
                self._state.failed += 1
            status.state = "idle"
            status.task = None
            status.proxy = None
            status.started_at = None
            log.info("worker %d <- task %d %s in %.2fs%s",
                     worker_id, task.id, result.status, result.duration_s,
                     f": {result.error}" if result.error else "")

    async def _run_one(
        self, worker_id: int, task: Task, proxy: Proxy | None, browser: Browser
    ) -> TaskResult:
        """Run one task in a fresh context and always return a ``TaskResult``."""
        started_wall = datetime.now(timezone.utc)
        started = time.monotonic()
        context: BrowserContext | None = None
        status: Status = "failed"
        data: Any = {}
        error: str | None = None

        try:
            context = await browser.new_context(
                proxy=proxy.to_playwright() if proxy else None
            )
            page = await context.new_page()
            # ``wait_for`` cancels the handler if it overruns; the ``finally``
            # below still closes the context.
            data = await asyncio.wait_for(self._handler(page, task), timeout=self._timeout_s)
            status = "ok"
        except asyncio.TimeoutError:
            status = "timeout"
            error = f"handler exceeded {self._timeout_s}s"
        except Exception as exc:  # noqa: BLE001
            # WHY broad: Playwright raises several classes (Error, TimeoutError,
            # TargetClosedError) and a dead proxy surfaces as a navigation
            # Error. All of them must be recorded, none may kill the worker.
            # CancelledError is a BaseException and is deliberately NOT caught,
            # so Ctrl+C still propagates.
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if context is not None:
                with suppress(Exception):
                    await context.close()

        if not isinstance(data, dict):
            data = {"value": data}

        return TaskResult(
            task_id=task.id,
            name=task.name,
            url=task.url,
            worker_id=worker_id,
            proxy=proxy.masked() if proxy else None,
            status=status,
            started_at=started_wall.isoformat(),
            duration_s=round(time.monotonic() - started, 3),
            data=data if status == "ok" else {},
            error=error,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_engine.py -v`
Expected: 7 passed (each browser test takes a few seconds). If `test_unreachable_proxy_is_recorded_as_failure` fails because the error text lacks `ERR_PROXY`, print `results[0].error`, confirm it is still a connection failure (for example `net::ERR_CONNECTION_REFUSED`), and relax the assertion to `"net::ERR_" in r.error`. Do not weaken the `status == "failed"` assertion.

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest -v`
Expected: all tests pass; no warnings about unknown marks.

- [ ] **Step 7: Commit**

```bash
git add engine.py tests/conftest.py tests/test_engine.py
git commit -m "feat: queue-based worker pool engine with context-per-task isolation"
```

---

### Task 7: `dashboard.py`

**Files:**
- Create: `dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `models.RunState`, `models.WorkerStatus`, `models.Task`.
- Produces:
  - `dashboard.Dashboard(state: RunState, console: rich.console.Console | None = None, refresh_per_second: float = 4.0)`
  - `Dashboard.render() -> rich.console.Group`
  - `async Dashboard.run_until(done: asyncio.Event) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_dashboard.py`:

```python
"""Tests for the rich live dashboard."""

import asyncio
import io
import time

from rich.console import Console

from dashboard import Dashboard
from models import RunState, Task, WorkerStatus


def _state() -> RunState:
    state = RunState(total=3, completed=1, ok=1, failed=0)
    state.workers[1] = WorkerStatus(worker_id=1)
    state.workers[2] = WorkerStatus(
        worker_id=2,
        task=Task(id=2, url="http://127.0.0.1/ip", name="task-2"),
        proxy="http://***:***@proxy.example:8080",
        state="running",
        started_at=time.monotonic() - 1.5,
    )
    return state


def _render_to_text(state: RunState) -> str:
    console = Console(record=True, width=120, file=io.StringIO(), force_terminal=False)
    dashboard = Dashboard(state, console=console)
    console.print(dashboard.render())
    return console.export_text()


def test_render_shows_progress_header():
    text = _render_to_text(_state())
    assert "1/3" in text
    assert "ok 1" in text
    assert "failed 0" in text


def test_render_shows_one_row_per_worker_with_masked_proxy():
    text = _render_to_text(_state())
    assert "task-2" in text
    assert "http://***:***@proxy.example:8080" in text
    assert "running" in text
    assert "idle" in text
    assert "direct" in text  # worker 1 has no proxy


def test_render_shows_running_duration_and_dash_for_idle():
    text = _render_to_text(_state())
    assert "1." in text  # ~1.5s elapsed for worker 2
    assert "-" in text


async def test_run_until_exits_when_event_is_set():
    console = Console(file=io.StringIO(), force_terminal=False, width=100)
    dashboard = Dashboard(_state(), console=console, refresh_per_second=20)
    done = asyncio.Event()

    async def finish_soon():
        await asyncio.sleep(0.2)
        done.set()

    await asyncio.wait_for(asyncio.gather(dashboard.run_until(done), finish_soon()), timeout=5)
    assert done.is_set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dashboard.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'dashboard'`.

- [ ] **Step 3: Implement `dashboard.py`**

```python
"""Live terminal dashboard built on ``rich.live.Live``.

WHY a pull model: workers never call the dashboard. They mutate the shared
``RunState`` and the dashboard re-renders it a few times per second. That
keeps the engine free of any UI dependency, and because asyncio is
single-threaded there is no race between the writer and the reader.
"""

from __future__ import annotations

import asyncio
import time

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from models import RunState


class Dashboard:
    """Renders ``RunState`` as a header line plus one table row per worker."""

    def __init__(
        self,
        state: RunState,
        console: Console | None = None,
        refresh_per_second: float = 4.0,
    ) -> None:
        self._state = state
        self._console = console or Console()
        self._refresh_per_second = refresh_per_second
        self._started = time.monotonic()

    def render(self) -> Group:
        """Build the renderable for the current state (pure function of state + clock)."""
        state = self._state
        now = time.monotonic()
        elapsed = now - self._started
        header = Text(
            f"Completed {state.completed}/{state.total}   "
            f"ok {state.ok}   failed {state.failed}   elapsed {elapsed:5.1f}s",
            style="bold",
        )

        table = Table(title="Workers", expand=True)
        table.add_column("Worker", justify="right", no_wrap=True)
        table.add_column("Task", overflow="fold")
        table.add_column("Proxy", overflow="fold")
        table.add_column("Status", no_wrap=True)
        table.add_column("Duration", justify="right", no_wrap=True)

        for worker_id in sorted(state.workers):
            worker = state.workers[worker_id]
            duration = f"{now - worker.started_at:.1f}s" if worker.started_at is not None else "-"
            style = {"running": "yellow", "done": "green", "idle": "dim"}[worker.state]
            table.add_row(
                str(worker_id),
                worker.task.name if worker.task else "-",
                worker.proxy or "direct",
                Text(worker.state, style=style),
                duration,
            )
        return Group(header, table)

    async def run_until(self, done: asyncio.Event) -> None:
        """Redraw until ``done`` is set, then draw one final frame."""
        interval = 1.0 / self._refresh_per_second
        with Live(
            self.render(),
            console=self._console,
            refresh_per_second=self._refresh_per_second,
            transient=False,
        ) as live:
            while not done.is_set():
                live.update(self.render())
                await asyncio.sleep(interval)
            live.update(self.render())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_dashboard.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: rich live dashboard rendered from RunState"
```

---

### Task 8: `main.py` CLI plus example input files

**Files:**
- Create: `main.py`, `tasks.txt`, `proxies.txt`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: everything above: `load_tasks`, `ProxyManager.from_file`, `load_handler`, `DEFAULT_HANDLER`, `AutomationEngine`, `Dashboard`, `RunState`, `setup_logging`, `write_results`.
- Produces:
  - `main.build_parser() -> argparse.ArgumentParser`
  - `main.main(argv: Sequence[str] | None = None) -> int`
  - `async main.run_async(engine: AutomationEngine, dashboard: Dashboard | None) -> list[TaskResult]`

- [ ] **Step 1: Write the failing tests**

`tests/test_main.py`:

```python
"""Tests for the CLI: argument defaults, input validation exit codes, end-to-end run."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from main import build_parser, main

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.tasks == Path("tasks.txt")
    assert args.proxies == Path("proxies.txt")
    assert args.concurrency == 3
    assert args.headless is True
    assert args.output == Path("results.json")
    assert args.handler == "handlers:fetch_ip"
    assert args.timeout == 30.0
    assert args.no_dashboard is False
    assert args.verbose is False


def test_parser_no_headless_flag():
    assert build_parser().parse_args(["--no-headless"]).headless is False


def test_parser_rejects_zero_concurrency():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--concurrency", "0"])


def test_missing_task_file_exits_2(tmp_path: Path, capsys):
    code = main(["--tasks", str(tmp_path / "nope.txt"), "--no-dashboard"])
    assert code == 2
    assert "task file not found" in capsys.readouterr().err


def test_bad_proxy_line_exits_2(tmp_path: Path, capsys):
    tasks = tmp_path / "tasks.txt"
    tasks.write_text("http://127.0.0.1/ip\n", encoding="utf-8")
    proxies = tmp_path / "proxies.txt"
    proxies.write_text("ftp://nope:21\n", encoding="utf-8")
    code = main(["--tasks", str(tasks), "--proxies", str(proxies), "--no-dashboard"])
    assert code == 2
    assert "proxies.txt:1" in capsys.readouterr().err


def test_bad_handler_exits_2(tmp_path: Path, capsys):
    tasks = tmp_path / "tasks.txt"
    tasks.write_text("http://127.0.0.1/ip\n", encoding="utf-8")
    code = main(["--tasks", str(tasks), "--handler", "nope", "--no-dashboard"])
    assert code == 2
    assert "module:function" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.usefixtures("require_chromium")
def test_cli_end_to_end_json(local_server, tmp_path: Path):
    tasks = tmp_path / "tasks.txt"
    tasks.write_text(f"{local_server}/ip?n=1\n{local_server}/ip?n=2\n", encoding="utf-8")
    out = tmp_path / "results.json"

    proc = subprocess.run(
        [
            sys.executable, "main.py",
            "--tasks", str(tasks),
            "--proxies", str(tmp_path / "absent.txt"),
            "--output", str(out),
            "--concurrency", "2",
            "--no-dashboard",
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=180,
    )

    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert all(d["status"] == "ok" for d in data)
    assert all(d["data"]["ip"] == "127.0.0.1" for d in data)
    assert "2/2 tasks finished, 0 failed" in proc.stdout


@pytest.mark.integration
@pytest.mark.usefixtures("require_chromium")
def test_cli_end_to_end_csv_with_failure_exits_1(local_server, tmp_path: Path):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(
        json.dumps([
            {"url": f"{local_server}/ip", "name": "good"},
            {"url": f"{local_server}/slow", "name": "too-slow"},
        ]),
        encoding="utf-8",
    )
    out = tmp_path / "results.csv"

    proc = subprocess.run(
        [
            sys.executable, "main.py",
            "--tasks", str(tasks),
            "--proxies", str(tmp_path / "absent.txt"),
            "--output", str(out),
            "--timeout", "1",
            "--no-dashboard",
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=180,
    )

    assert proc.returncode == 1, proc.stderr
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("task_id,name,url,worker_id,proxy,status")
    assert len(lines) == 3
    assert "timeout" in out.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'main'`.

- [ ] **Step 3: Implement `main.py`**

```python
"""Command-line entry point.

Wires the pieces together in one obvious place::

    argparse -> load_tasks / ProxyManager.from_file / load_handler
             -> AutomationEngine (+ Dashboard) -> write_results -> exit code

WHY the dashboard and the engine run under one ``asyncio.gather``: both are
coroutines on the same event loop. The engine sets an ``asyncio.Event`` when
it finishes so the dashboard knows to draw a final frame and stop.

Exit codes: 0 all tasks ok, 1 some task failed or the browser could not
launch, 2 bad input, 130 interrupted with Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from dashboard import Dashboard
from engine import AutomationEngine
from handlers import DEFAULT_HANDLER, load_handler
from models import RunState, TaskResult
from proxy_manager import ProxyManager
from tasks import load_tasks
from utils import setup_logging, write_results

log = logging.getLogger(__name__)


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    """Define the CLI. Kept separate from ``main`` so tests can inspect defaults."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Run web-automation tasks concurrently with isolated, proxied browser contexts.",
    )
    parser.add_argument("--tasks", type=Path, default=Path("tasks.txt"),
                        help="task file: .txt (one URL per line) or .json (default: %(default)s)")
    parser.add_argument("--proxies", type=Path, default=Path("proxies.txt"),
                        help="proxy list; missing or empty means connect directly (default: %(default)s)")
    parser.add_argument("--concurrency", type=_positive_int, default=3,
                        help="number of concurrent workers / live contexts (default: %(default)s)")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True,
                        help="run the browser headless (default: --headless)")
    parser.add_argument("--output", type=Path, default=Path("results.json"),
                        help="results file; .json or .csv (default: %(default)s)")
    parser.add_argument("--handler", default=DEFAULT_HANDLER,
                        help="per-task coroutine as module:function (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="seconds allowed per task (default: %(default)s)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="print log lines instead of the live table (useful in CI)")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    return parser


async def run_async(engine: AutomationEngine, dashboard: Dashboard | None) -> list[TaskResult]:
    """Run the engine, with the dashboard alongside it when requested."""
    if dashboard is None:
        return await engine.run()

    done = asyncio.Event()

    async def run_engine() -> list[TaskResult]:
        try:
            return await engine.run()
        finally:
            done.set()

    results, _ = await asyncio.gather(run_engine(), dashboard.run_until(done))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    use_dashboard = not args.no_dashboard
    setup_logging(verbose=args.verbose, dashboard_active=use_dashboard)

    try:
        tasks = load_tasks(args.tasks)
        proxy_manager = ProxyManager.from_file(args.proxies)
        handler = load_handler(args.handler)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    state = RunState(total=len(tasks))
    engine = AutomationEngine(
        tasks,
        proxy_manager,
        handler,
        concurrency=args.concurrency,
        headless=args.headless,
        timeout_s=args.timeout,
        state=state,
    )
    dashboard = Dashboard(state) if use_dashboard else None

    exit_code = 0
    try:
        asyncio.run(run_async(engine, dashboard))
    except KeyboardInterrupt:
        print("\nInterrupted; writing partial results", file=sys.stderr)
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 - e.g. browser failed to launch
        log.exception("run aborted")
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        exit_code = 1

    # Partial results are still valuable after an interrupt or a crash.
    results = state.results
    write_results(results, args.output)
    failed = sum(1 for result in results if result.status != "ok")
    print(f"{len(results)}/{len(tasks)} tasks finished, {failed} failed -> {args.output}")

    if exit_code == 0 and (failed or len(results) != len(tasks)):
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Create the example input files**

`tasks.txt`:

```text
# One URL per line. Blank lines and lines starting with # are ignored.
# Both endpoints echo the outgoing IP address, which lets you verify that
# each browser context really used its assigned proxy.
https://httpbin.org/ip
https://api.ipify.org?format=json
https://httpbin.org/ip
https://api.ipify.org?format=json
https://httpbin.org/ip
https://api.ipify.org?format=json
```

`proxies.txt`:

```text
# One proxy per line. Supported formats:
#   http://user:pass@host:port
#   http://host:port
#   https://host:port
#   socks5://host:port        (Chromium ignores credentials on SOCKS5)
#   socks4://host:port
#
# Percent-encode special characters in credentials, e.g. user%40corp for user@corp.
# If this file is missing or has no proxies, every context connects directly.
#
# http://alice:s3cret@203.0.113.10:8080
# socks5://198.51.100.20:1080
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_main.py -v`
Expected: 8 passed (the two integration tests each launch a subprocess that launches Chromium).

- [ ] **Step 6: Smoke-run the dashboard by hand against the example file**

Run: `python main.py --tasks tasks.txt --concurrency 3 --timeout 20`
Expected: a live table with three worker rows updates for a few seconds, then a summary line like `6/6 tasks finished, 0 failed -> results.json`. If your machine has no internet access, some tasks show `failed`; that is correct behaviour and the exit code is 1. Delete `results.json` and `run.log` afterwards (both are git-ignored anyway).

- [ ] **Step 7: Commit**

```bash
git add main.py tasks.txt proxies.txt tests/test_main.py
git commit -m "feat: argparse CLI wiring engine, dashboard, and result export"
```

---

### Task 9: `README.md` and final verification

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: the finished code (document real flag names and file names from Tasks 1–8).
- Produces: user-facing documentation.

- [ ] **Step 1: Write `README.md`**

````markdown
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
````

- [ ] **Step 2: Run the complete test suite one final time**

Run: `python -m pytest -v`
Expected: every test passes (unit tests in well under a second; integration tests a few seconds each).

- [ ] **Step 3: Verify the CLI help renders and the tree matches the spec**

Run:

```bash
python main.py --help
git status --short
```

Expected: help text lists `--tasks`, `--proxies`, `--concurrency`, `--headless/--no-headless`, `--output`, `--handler`, `--timeout`, `--no-dashboard`, `-v`. `git status` shows only `README.md` as untracked (no stray `results.json` or `run.log`; both are ignored).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: README explaining architecture, proxy format, handlers, and tests"
```
