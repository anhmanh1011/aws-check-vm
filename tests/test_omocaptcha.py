"""Tests for the OmoCaptcha solver client. Fully offline via the local server.

The POST routes ``/createTask``, ``/getTaskResult`` and ``/getBalance`` in
conftest return OmoCaptcha-shaped JSON, so the happy paths and the API-error
branches run against a real round-trip. The polling loop, the timeout and the
log-masking are driven with monkeypatch so they stay fast and deterministic.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import omocaptcha
from omocaptcha import (
    OmoCaptchaError,
    create_task,
    get_balance,
    get_task_result,
    load_client_key,
    mask_key,
    solve,
)


def test_create_task_returns_task_id(local_server):
    assert create_task(b"imgbytes", client_key="goodkey", base_url=local_server) == "task-123"


def test_create_task_raises_on_api_error(local_server):
    with pytest.raises(OmoCaptchaError) as excinfo:
        create_task(b"x", client_key="badkey", base_url=local_server)
    assert excinfo.value.code == "ERROR_KEY_DOES_NOT_EXIST"


def test_get_task_result_ready_returns_solution(local_server):
    result = get_task_result("task-123", client_key="goodkey", base_url=local_server)
    assert result["status"] == "ready"
    assert result["solution"]["text"] == "ABCD12"


def test_get_task_result_fail_raises(local_server):
    with pytest.raises(OmoCaptchaError) as excinfo:
        get_task_result("task-fail", client_key="goodkey", base_url=local_server)
    assert excinfo.value.code == "ERROR_JOB_STATUS"


def test_solve_end_to_end_returns_text(local_server):
    assert solve(b"imgbytes", client_key="goodkey", base_url=local_server) == "ABCD12"


def test_solve_loads_key_from_file_when_not_given(local_server, tmp_path, monkeypatch):
    keyfile = tmp_path / "omocaptcha.txt"
    keyfile.write_text("filekey\n", encoding="utf-8")
    monkeypatch.setattr(omocaptcha, "KEY_FILE", keyfile)
    assert solve(b"imgbytes", base_url=local_server) == "ABCD12"


def test_solve_polls_until_ready(monkeypatch):
    calls = {"n": 0}

    def _fake_create(image, *, client_key, module=None, base_url=None, timeout_s=30.0):
        return "tid"

    def _fake_result(task_id, *, client_key, base_url=None, timeout_s=30.0):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"errorId": 0, "status": "processing"}
        return {"errorId": 0, "status": "ready", "solution": {"text": "SOLVED"}}

    monkeypatch.setattr(omocaptcha, "create_task", _fake_create)
    monkeypatch.setattr(omocaptcha, "get_task_result", _fake_result)
    text = solve(b"x", client_key="k", poll_interval_s=0.0)
    assert text == "SOLVED"
    assert calls["n"] == 3


def test_solve_raises_when_it_times_out(monkeypatch):
    monkeypatch.setattr(omocaptcha, "create_task",
                        lambda image, **kw: "tid")
    monkeypatch.setattr(omocaptcha, "get_task_result",
                        lambda task_id, **kw: {"errorId": 0, "status": "processing"})
    with pytest.raises(OmoCaptchaError, match="did not solve"):
        solve(b"x", client_key="k", poll_interval_s=0.0, max_wait_s=0.0)


def test_get_balance_returns_body(local_server):
    body = get_balance(client_key="goodkey", base_url=local_server)
    assert body["balance"] == "50.00000"


def test_load_client_key_reads_single_key(tmp_path: Path):
    path = tmp_path / "omocaptcha.txt"
    path.write_text("# my omo key\n\n  OMO_abc123  \n", encoding="utf-8")
    assert load_client_key(path) == "OMO_abc123"


def test_load_client_key_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="key file not found"):
        load_client_key(tmp_path / "nope.txt")


def test_load_client_key_empty_file_raises(tmp_path: Path):
    path = tmp_path / "omocaptcha.txt"
    path.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no key"):
        load_client_key(path)


def test_mask_key_hides_middle_of_long_key():
    masked = mask_key("OMO1234567890abcdef")
    assert masked.startswith("OMO1")
    assert masked.endswith("cdef")
    assert "234567890abc" not in masked


class _FakeResp:
    status = 200

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_post_json_masks_key_and_omits_image_in_logs(caplog, monkeypatch):
    """The request log masks clientKey and never contains the base64 image."""
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = req.data
        return _FakeResp(b'{"errorId": 0, "taskId": "task-123"}')

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    caplog.set_level(logging.INFO, logger="omocaptcha")

    key = "OMO1234567890abcdef"
    image_b64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVld" * 20  # long, obviously-an-image blob
    omocaptcha._post_json(
        "http://example.invalid/createTask",
        {"clientKey": key, "task": {"type": "ImageToTextTask", "imageBase64": image_b64}},
        30.0,
    )

    text = caplog.text
    assert key not in text                  # raw key never logged
    assert mask_key(key) in text            # masked form is
    assert image_b64 not in text            # the base64 image is never logged
    # The real request still carried the real key and image.
    assert key.encode() in captured["body"]
    assert image_b64.encode() in captured["body"]
