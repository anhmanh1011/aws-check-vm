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
