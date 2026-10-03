"""Integration tests: real headless Chromium against the local echo server."""

from __future__ import annotations

import pytest
from playwright.async_api import Browser

from engine import AutomationEngine
from handlers import fetch_ip
from models import Proxy, RunState, Task
from proxy_manager import ProxyManager

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_chromium")]


def _tasks(base_url: str, count: int, path: str = "/ip") -> list[Task]:
    return [
        Task(id=i, lines=(f"{base_url}{path}?n={i}",), name=f"task-{i}")
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
    # Proves the work was actually spread across workers, not run serially
    # by a single one that happened to finish everything.
    assert len({r.worker_id for r in results}) >= 2
    assert all(r.data["results"][0]["ip"] == "127.0.0.1" for r in results)
    assert all(r.inputs == [f"{local_server}/ip?n={r.task_id}"] for r in results)
    assert all(r.proxy is None for r in results)
    assert (state.completed, state.ok, state.failed) == (6, 6, 0)
    assert all(w.state == "done" for w in state.workers.values())
    assert sorted(state.workers) == [1, 2, 3]


async def test_batch_task_hands_every_line_to_the_handler(local_server):
    urls = tuple(f"{local_server}/ip?n={i}" for i in range(1, 4))
    engine, state = _engine([Task(id=1, lines=urls, name="batch of 3")], concurrency=1)
    (result,) = await engine.run()

    assert result.status == "ok"
    assert result.inputs == list(urls)
    assert [r["url"] for r in result.data["results"]] == list(urls)
    assert state.completed == 1


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


async def test_handler_raising_its_own_timeout_error_is_failed_not_timeout(local_server):
    # On Python 3.11+, asyncio.TimeoutError is TimeoutError, so a handler
    # that raises a builtin TimeoutError of its own (e.g. a socket read
    # timeout) must not be confused with wait_for's deadline firing.
    async def raises_own_timeout(page, task):
        await page.goto(task.lines[0])
        raise TimeoutError("socket read timed out")

    engine, state = _engine(_tasks(local_server, 1), handler=raises_own_timeout)
    (result,) = await engine.run()

    assert result.status == "failed"
    assert result.error == "TimeoutError: socket read timed out"
    assert state.failed == 1


async def test_unreachable_proxy_is_recorded_as_failure(local_server):
    # Chromium bypasses proxies for loopback addresses, so target a
    # non-loopback hostname. Name resolution is delegated to an HTTP proxy,
    # and the proxy refuses the connection first.
    bad_proxy = Proxy(server="http://127.0.0.1:9", username="alice", password="s3cret")
    tasks = [
        Task(id=1, lines=("http://proxy-test.invalid/ip",), name="via-bad-proxy-1"),
        Task(id=2, lines=("http://proxy-test.invalid/ip",), name="via-bad-proxy-2"),
    ]
    engine, state = _engine(tasks, proxies=[bad_proxy], concurrency=2)
    results = await engine.run()

    assert len(results) == 2
    assert all(r.status == "failed" for r in results)
    assert all(r.error and "ERR_PROXY" in r.error for r in results)
    assert all(r.proxy == "http://***:***@127.0.0.1:9" for r in results)
    # End-to-end check that credentials never leak into the recorded error
    # text either, not just into the masked proxy field.
    assert all("s3cret" not in (r.error or "") and "alice" not in (r.error or "") for r in results)
    assert state.completed == 2


async def test_context_creation_failure_is_recorded_not_raised(local_server, monkeypatch):
    # A RuntimeError from browser.new_context() (e.g. a proxy Playwright
    # rejects, or a mid-run TargetClosedError) must be recorded as a failed
    # TaskResult for that one task, not propagate out of _run_one -- an
    # escape there would reach the TaskGroup and cancel every sibling,
    # aborting the whole run over a single context-creation failure.
    original_new_context = Browser.new_context
    calls = {"n": 0}

    async def fake_new_context(self, *args, **kwargs):
        # Single-threaded asyncio: no await happens between the increment
        # and the check, so this is race-free without a lock.
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("new_context boom")
        return await original_new_context(self, *args, **kwargs)

    monkeypatch.setattr(Browser, "new_context", fake_new_context)

    engine, state = _engine(_tasks(local_server, 3), concurrency=2)
    results = await engine.run()

    assert len(results) == 3
    failed = [r for r in results if r.status == "failed"]
    ok = [r for r in results if r.status == "ok"]
    assert len(failed) == 1
    assert failed[0].error == "RuntimeError: new_context boom"
    assert len(ok) == 2
    assert state.completed == 3


async def test_contexts_do_not_share_cookies(local_server):
    async def cookie_probe(page, task):
        await page.goto(task.lines[0])
        before = len(await page.context.cookies())
        await page.context.add_cookies([{"name": "seen", "value": "1", "url": task.lines[0]}])
        return {"cookies_before": before}

    engine, _ = _engine(_tasks(local_server, 3), handler=cookie_probe, concurrency=1)
    results = await engine.run()

    assert [r.worker_id for r in results] == [1, 1, 1]
    assert [r.data["cookies_before"] for r in results] == [0, 0, 0]


async def test_non_dict_handler_return_is_wrapped(local_server):
    async def returns_text(page, task):
        await page.goto(task.lines[0])
        return "plain string"

    engine, _ = _engine(_tasks(local_server, 1), handler=returns_text)
    (result,) = await engine.run()
    assert result.status == "ok"
    assert result.data == {"value": "plain string"}


async def test_unexpected_worker_error_cancels_siblings_and_closes_browser(
    local_server, monkeypatch
):
    # ``_run_one`` is the one place that already converts failures into data;
    # bypassing it here simulates a bug that lets an exception escape a
    # worker, which is exactly the path ``asyncio.TaskGroup`` must handle by
    # cancelling the other workers before the browser is closed.
    original_run_one = AutomationEngine._run_one

    async def crashing_run_one(self, worker_id, task, proxy, browser):
        if task.id == 1:
            raise RuntimeError("worker crashed")
        return await original_run_one(self, worker_id, task, proxy, browser)

    monkeypatch.setattr(AutomationEngine, "_run_one", crashing_run_one)

    engine, _ = _engine(_tasks(local_server, 4), concurrency=2)

    with pytest.raises(ExceptionGroup) as exc_info:
        await engine.run()

    runtime_errors = [
        exc for exc in exc_info.value.exceptions if isinstance(exc, RuntimeError)
    ]
    assert len(runtime_errors) == 1
    assert str(runtime_errors[0]) == "worker crashed"
