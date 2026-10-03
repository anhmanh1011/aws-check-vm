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
