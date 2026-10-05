"""Tests for the CLI: argument defaults, input validation exit codes, end-to-end run."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from main import build_parser, main

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.input == Path("input.txt")
    assert args.batch_size == 3
    assert args.proxies == Path("proxies.txt")
    assert args.concurrency == 3
    assert args.headless is True
    assert args.output == Path("results.json")
    assert args.handler == "handlers:fetch_ip"
    assert args.timeout == 60.0
    assert args.no_dashboard is False
    assert args.verbose is False
    assert args.proxy_per_worker is False
    assert args.screen_size is None
    assert args.split_output is False


def test_parser_screen_size_parses_wxh():
    assert build_parser().parse_args(["--screen-size", "1920x1080"]).screen_size == (1920, 1080)


def test_parser_rejects_bad_screen_size():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--screen-size", "wide"])


def test_parser_no_headless_flag():
    assert build_parser().parse_args(["--no-headless"]).headless is False


def test_parser_rejects_zero_concurrency():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--concurrency", "0"])


def test_parser_accepts_zero_batch_size_as_auto_but_rejects_negative():
    assert build_parser().parse_args(["--batch-size", "0"]).batch_size == 0
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--batch-size", "-1"])


def test_parser_kiot_defaults():
    args = build_parser().parse_args([])
    assert args.kiot_keys is None
    assert args.kiot_region == "random"


def test_parser_rejects_unknown_region():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--kiot-region", "europe"])


def test_kiot_keys_builds_pool_without_network(tmp_path, capsys, monkeypatch, local_server):
    import kiotproxy
    monkeypatch.setattr(kiotproxy, "BASE_URL", local_server)
    monkeypatch.setattr("main.asyncio.run", lambda coro: coro.close())

    keyfile = tmp_path / "keys.txt"
    keyfile.write_text("goodkey\n", encoding="utf-8")
    out = tmp_path / "results.json"
    inp = tmp_path / "input.txt"
    inp.write_text("line1\n", encoding="utf-8")

    code = main([
        "--kiot-keys", str(keyfile),
        "--kiot-region", "random",
        "--input", str(inp),
        "--output", str(out),
        "--no-dashboard",
    ])

    # asyncio.run is stubbed, so no tasks complete: summary prints, exit is 1
    # (fewer results than tasks), never 2 (which would mean proxy loading
    # failed as bad input).
    assert code != 2
    assert "tasks finished" in capsys.readouterr().out


def test_kiot_keys_sets_concurrency_to_proxy_count_and_pins(tmp_path, monkeypatch, local_server):
    import kiotproxy
    import main as main_mod
    monkeypatch.setattr(kiotproxy, "BASE_URL", local_server)

    captured: dict = {}

    class _SpyEngine:
        def __init__(self, tasks, proxy_manager, handler, *, concurrency,
                     proxy_per_worker=False, **kwargs):
            captured["concurrency"] = concurrency
            captured["proxy_per_worker"] = proxy_per_worker

        async def run(self):
            return []

    monkeypatch.setattr(main_mod, "AutomationEngine", _SpyEngine)
    monkeypatch.setattr("main.asyncio.run", lambda coro: coro.close())

    keyfile = tmp_path / "keys.txt"
    keyfile.write_text("key1\nkey2\nkey3\n", encoding="utf-8")  # 3 keys -> 3 proxies
    inp = tmp_path / "input.txt"
    inp.write_text("a\nb\n", encoding="utf-8")

    main([
        "--kiot-keys", str(keyfile),
        "--concurrency", "1",          # should be overridden to 3
        "--input", str(inp),
        "--output", str(tmp_path / "results.json"),
        "--no-dashboard",
    ])

    assert captured["concurrency"] == 3
    assert captured["proxy_per_worker"] is True


def test_kiot_keys_missing_file_exits_2(tmp_path, capsys):
    code = main([
        "--kiot-keys", str(tmp_path / "nope.txt"),
        "--input", str(tmp_path / "input.txt"),
        "--no-dashboard",
    ])
    assert code == 2
    assert "key file not found" in capsys.readouterr().err


def test_missing_input_file_exits_2(tmp_path: Path, capsys):
    code = main(["--input", str(tmp_path / "nope.txt"), "--no-dashboard"])
    assert code == 2
    assert "input file not found" in capsys.readouterr().err


def test_bad_proxy_line_exits_2(tmp_path: Path, capsys):
    tasks = tmp_path / "input.txt"
    tasks.write_text("http://127.0.0.1/ip\n", encoding="utf-8")
    proxies = tmp_path / "proxies.txt"
    proxies.write_text("ftp://nope:21\n", encoding="utf-8")
    code = main(["--input", str(tasks), "--proxies", str(proxies), "--no-dashboard"])
    assert code == 2
    assert "proxies.txt:1" in capsys.readouterr().err


def test_bad_handler_exits_2(tmp_path: Path, capsys):
    tasks = tmp_path / "input.txt"
    tasks.write_text("http://127.0.0.1/ip\n", encoding="utf-8")
    code = main(["--input", str(tasks), "--handler", "nope", "--no-dashboard"])
    assert code == 2
    assert "module:function" in capsys.readouterr().err


def test_bad_output_extension_exits_2_before_running(tmp_path: Path, capsys):
    tasks = tmp_path / "input.txt"
    tasks.write_text("http://127.0.0.1/ip\n", encoding="utf-8")
    code = main([
        "--input", str(tasks),
        "--output", str(tmp_path / "results.xml"),
        "--no-dashboard",
    ])
    assert code == 2
    assert "unsupported output extension" in capsys.readouterr().err


def test_unwritable_output_path_exits_1_with_message(tmp_path: Path, capsys, monkeypatch):
    tasks = tmp_path / "input.txt"
    tasks.write_text("http://127.0.0.1/ip\n", encoding="utf-8")

    def _boom(results, path):
        raise OSError("disk full")

    monkeypatch.setattr("main.write_results", _boom)
    monkeypatch.setattr("main.asyncio.run", lambda coro: coro.close())

    code = main([
        "--input", str(tasks),
        "--output", str(tmp_path / "results.json"),
        "--no-dashboard",
    ])
    assert code == 1
    assert "could not write results" in capsys.readouterr().err


def test_keyboard_interrupt_exits_130_and_writes_partial_results(
    tmp_path: Path, capsys, monkeypatch
):
    tasks = tmp_path / "input.txt"
    tasks.write_text("http://127.0.0.1/ip\n", encoding="utf-8")
    out = tmp_path / "results.json"

    def _interrupt(coro):
        coro.close()  # avoid a "coroutine was never awaited" warning
        raise KeyboardInterrupt

    monkeypatch.setattr("main.asyncio.run", _interrupt)

    code = main([
        "--input", str(tasks),
        "--output", str(out),
        "--no-dashboard",
    ])

    assert code == 130
    assert "Interrupted" in capsys.readouterr().err
    assert json.loads(out.read_text(encoding="utf-8")) == []


def test_cancelled_error_exits_130(tmp_path: Path, capsys, monkeypatch):
    tasks = tmp_path / "input.txt"
    tasks.write_text("http://127.0.0.1/ip\n", encoding="utf-8")
    out = tmp_path / "results.json"

    def _cancel(coro):
        coro.close()
        raise asyncio.CancelledError

    monkeypatch.setattr("main.asyncio.run", _cancel)

    code = main([
        "--input", str(tasks),
        "--output", str(out),
        "--no-dashboard",
    ])

    assert code == 130
    assert "Interrupted" in capsys.readouterr().err
    assert json.loads(out.read_text(encoding="utf-8")) == []


@pytest.mark.integration
@pytest.mark.usefixtures("require_chromium")
def test_cli_end_to_end_json(local_server, tmp_path: Path):
    tasks = tmp_path / "input.txt"
    tasks.write_text("".join(f"{local_server}/ip?n={i}\n" for i in range(1, 5)), encoding="utf-8")
    out = tmp_path / "results.json"

    proc = subprocess.run(
        [
            sys.executable, "main.py",
            "--input", str(tasks),
            "--proxies", str(tmp_path / "absent.txt"),
            "--output", str(out),
            "--concurrency", "2",
            "--batch-size", "2",
            "--no-dashboard",
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=180,
    )

    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 2  # 4 lines / batch of 2
    assert all(d["status"] == "ok" for d in data)
    assert all(len(d["inputs"]) == 2 for d in data)
    assert all(len(d["data"]["results"]) == 2 for d in data)
    assert all(r["ip"] == "127.0.0.1" for d in data for r in d["data"]["results"])
    assert "2/2 tasks finished, 0 failed" in proc.stdout


@pytest.mark.integration
@pytest.mark.usefixtures("require_chromium")
def test_cli_end_to_end_csv_with_failure_exits_1(local_server, tmp_path: Path):
    tasks = tmp_path / "input.txt"
    tasks.write_text(f"{local_server}/ip\n{local_server}/slow\n", encoding="utf-8")
    out = tmp_path / "results.csv"

    proc = subprocess.run(
        [
            sys.executable, "main.py",
            "--input", str(tasks),
            "--proxies", str(tmp_path / "absent.txt"),
            "--output", str(out),
            "--batch-size", "1",  # one task per line so the ok/timeout split is two rows
            "--timeout", "1",
            "--no-dashboard",
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=180,
    )

    assert proc.returncode == 1, proc.stderr
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("task_id,name,inputs,worker_id,proxy,status")
    assert len(lines) == 3
    assert "timeout" in out.read_text(encoding="utf-8")
