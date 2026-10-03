"""Tests for input loading and batching in tasks.py.

Each non-empty, non-comment line of the input file is one data item. Items
are grouped into batches; one batch becomes one ``Task`` and therefore one
browser context. These tests pin the batching arithmetic without a browser.
"""

from pathlib import Path

import pytest

from models import Task
from tasks import chunk_lines, load_lines, load_tasks


def _write(tmp_path: Path, text: str, name: str = "input.txt") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- load_lines -------------------------------------------------------------


def test_load_lines_skips_blanks_comments_and_strips(tmp_path: Path):
    path = _write(tmp_path, "# emails\n\n  a@x.com  \nb@x.com\n")
    assert load_lines(path) == ["a@x.com", "b@x.com"]


def test_load_lines_accepts_any_extension(tmp_path: Path):
    path = _write(tmp_path, "one\ntwo\n", name="emails.list")
    assert load_lines(path) == ["one", "two"]


def test_load_lines_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="input file not found"):
        load_lines(tmp_path / "nope.txt")


def test_load_lines_empty_file_raises(tmp_path: Path):
    path = _write(tmp_path, "# only a comment\n")
    with pytest.raises(ValueError, match="no input lines"):
        load_lines(path)


# --- chunk_lines ------------------------------------------------------------


def test_chunk_default_batch_size_one_line_per_batch():
    assert chunk_lines(["a", "b", "c"], batch_size=1, concurrency=3) == [["a"], ["b"], ["c"]]


def test_chunk_fixed_batch_size_last_batch_may_be_short():
    lines = ["a", "b", "c", "d", "e"]
    assert chunk_lines(lines, batch_size=2, concurrency=3) == [["a", "b"], ["c", "d"], ["e"]]


def test_chunk_batch_size_larger_than_input_is_one_batch():
    assert chunk_lines(["a", "b"], batch_size=10, concurrency=3) == [["a", "b"]]


def test_chunk_auto_splits_evenly_across_workers():
    lines = [str(i) for i in range(10)]
    batches = chunk_lines(lines, batch_size=0, concurrency=3)
    # ceil(10 / 3) = 4 per batch -> 4, 4, 2: every worker gets exactly one batch.
    assert [len(b) for b in batches] == [4, 4, 2]
    assert [line for b in batches for line in b] == lines


def test_chunk_auto_never_creates_empty_batches():
    batches = chunk_lines(["a", "b", "c"], batch_size=0, concurrency=5)
    assert batches == [["a"], ["b"], ["c"]]


def test_chunk_rejects_negative_batch_size():
    with pytest.raises(ValueError, match="batch_size"):
        chunk_lines(["a"], batch_size=-1, concurrency=1)


def test_chunk_rejects_non_positive_concurrency():
    with pytest.raises(ValueError, match="concurrency"):
        chunk_lines(["a"], batch_size=0, concurrency=0)


# --- load_tasks -------------------------------------------------------------


def test_load_tasks_default_is_one_task_per_line(tmp_path: Path):
    path = _write(tmp_path, "https://httpbin.org/ip\nhttps://api.ipify.org?format=json\n")
    assert load_tasks(path) == [
        Task(id=1, lines=("https://httpbin.org/ip",), name="https://httpbin.org/ip"),
        Task(id=2, lines=("https://api.ipify.org?format=json",), name="https://api.ipify.org?format=json"),
    ]


def test_load_tasks_batches_and_names_show_batch_size(tmp_path: Path):
    path = _write(tmp_path, "a\nb\nc\nd\ne\n")
    tasks = load_tasks(path, batch_size=2, concurrency=1)
    assert [t.lines for t in tasks] == [("a", "b"), ("c", "d"), ("e",)]
    assert [t.name for t in tasks] == ["a (+1 more)", "c (+1 more)", "e"]
    assert [t.id for t in tasks] == [1, 2, 3]


def test_load_tasks_auto_batch_uses_concurrency(tmp_path: Path):
    path = _write(tmp_path, "".join(f"{i}\n" for i in range(10)))
    tasks = load_tasks(path, batch_size=0, concurrency=3)
    assert [len(t.lines) for t in tasks] == [4, 4, 2]
