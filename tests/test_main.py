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
