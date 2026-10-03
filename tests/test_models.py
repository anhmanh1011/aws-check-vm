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


def test_proxy_masked_does_not_leak_credentials_embedded_in_server():
    # ``server`` itself (not just username/password) may carry a real
    # credential pair; masked() must not depend on it being credential-free.
    proxy = Proxy(server="http://u:p@1.2.3.4:8080", username="u", password="p")
    assert proxy.masked() == "http://***:***@1.2.3.4:8080"


def test_proxy_masked_without_port_omits_trailing_none():
    proxy = Proxy(server="http://1.2.3.4", username="u", password="p")
    assert proxy.masked() == "http://***:***@1.2.3.4"


def test_task_is_frozen():
    task = Task(id=1, lines=("https://example.com",), name="example")
    try:
        task.lines = ("https://other",)  # type: ignore[misc]
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
            task_id=1, name="n", inputs=["u"], worker_id=1, proxy=None, status="ok",
            started_at="2026-01-01T00:00:00+00:00", duration_s=0.1, data={}, error=None,
        )
    )
    assert b.results == []
