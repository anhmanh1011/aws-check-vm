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
        inputs=[f"http://127.0.0.1/ip?n={task_id}"],
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
        "task_id", "name", "inputs", "worker_id", "proxy", "status",
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


def _check_result(task_id, inputs, status="ok", email_rows=None, error=None):
    """A TaskResult shaped like check_VM's output (data.results = per-email list)."""
    return TaskResult(
        task_id=task_id, name=f"task-{task_id}", inputs=list(inputs), worker_id=1,
        proxy=None, status=status,  # type: ignore[arg-type]
        started_at="2026-10-05T00:00:00+00:00", duration_s=1.0,
        data={"results": email_rows} if email_rows is not None else {},
        error=error,
    )


def test_write_split_outputs_buckets_emails_by_status(tmp_path):
    from utils import write_split_outputs
    rows = [
        {"email": "a@x.com", "status": "account_exists"},
        {"email": "b@x.com", "status": "account_not_found"},
        {"email": "c@x.com", "status": "captcha_failed"},
    ]
    result = _check_result(1, [r["email"] for r in rows], email_rows=rows)
    write_split_outputs([result], tmp_path)

    assert (tmp_path / "exists.txt").read_text(encoding="utf-8") == "a@x.com\n"
    assert (tmp_path / "not_found.txt").read_text(encoding="utf-8") == "b@x.com\n"
    assert (tmp_path / "error.txt").read_text(encoding="utf-8") == "c@x.com\tcaptcha_failed\n"


def test_write_split_outputs_sends_failed_task_inputs_to_error_file(tmp_path):
    from utils import write_split_outputs
    result = _check_result(1, ["d@x.com", "e@x.com"], status="timeout",
                           email_rows=None, error="handler exceeded 60s")
    write_split_outputs([result], tmp_path)

    assert not (tmp_path / "exists.txt").exists()
    assert (tmp_path / "error.txt").read_text(encoding="utf-8") == "d@x.com\ttimeout\ne@x.com\ttimeout\n"


def test_write_split_outputs_appends_across_calls(tmp_path):
    from utils import write_split_outputs
    r1 = _check_result(1, ["a@x.com"], email_rows=[{"email": "a@x.com", "status": "account_exists"}])
    r2 = _check_result(2, ["f@x.com"], email_rows=[{"email": "f@x.com", "status": "account_exists"}])
    write_split_outputs([r1], tmp_path)
    write_split_outputs([r2], tmp_path)
    assert (tmp_path / "exists.txt").read_text(encoding="utf-8") == "a@x.com\nf@x.com\n"
