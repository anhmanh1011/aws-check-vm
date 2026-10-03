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
