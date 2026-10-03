"""Tests for the KiotProxy proxy source. Fully offline via the local server."""

from __future__ import annotations

from pathlib import Path

import pytest

import kiotproxy
from kiotproxy import (
    KiotProxyError,
    fetch_proxy,
    load_keys,
    load_kiot_proxies,
    mask_key,
)
from models import Proxy


def test_fetch_proxy_parses_success_into_http_proxy(local_server):
    proxy = fetch_proxy("goodkey", "random", base_url=local_server)
    assert proxy == Proxy(server="http://127.0.0.1:39008")


def test_fetch_proxy_sends_key_and_region(local_server):
    # 'bac' is valid and the success body is returned for any non-bad key.
    proxy = fetch_proxy("abc", "bac", base_url=local_server)
    assert proxy.server == "http://127.0.0.1:39008"


def test_fetch_proxy_raises_kiotproxyerror_on_api_failure(local_server):
    with pytest.raises(KiotProxyError) as excinfo:
        fetch_proxy("badkey", "random", base_url=local_server)
    assert excinfo.value.code == 40400006
    assert excinfo.value.error == "KEY_NOT_FOUND"


def test_fetch_proxy_rejects_invalid_region_before_any_request():
    with pytest.raises(ValueError, match="region"):
        fetch_proxy("k", "europe", base_url="http://127.0.0.1:0")


def _raise_not_json(url, timeout_s):
    raise KiotProxyError("not JSON")


def test_fetch_proxy_raises_when_body_is_not_json(monkeypatch, local_server):
    monkeypatch.setattr(kiotproxy, "_get_json", _raise_not_json)
    with pytest.raises(KiotProxyError):
        fetch_proxy("goodkey", base_url=local_server)


def test_fetch_proxy_raises_when_success_body_missing_http(monkeypatch, local_server):
    monkeypatch.setattr(kiotproxy, "_get_json", lambda url, timeout_s: {
        "success": True, "data": {"socks5": "1.2.3.4:5"}})
    with pytest.raises(KiotProxyError, match="http"):
        fetch_proxy("goodkey", base_url=local_server)


def test_load_kiot_proxies_skips_failed_keys_and_masks_them(local_server, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    proxies = load_kiot_proxies(["goodkey", "badkey"], base_url=local_server)
    assert proxies == [Proxy(server="http://127.0.0.1:39008")]
    assert "badkey" not in caplog.text          # raw key never logged
    assert mask_key("badkey") in caplog.text     # masked form is


def test_load_kiot_proxies_empty_when_all_fail(local_server):
    assert load_kiot_proxies(["badkey"], base_url=local_server) == []


def test_load_keys_parses_lines_ignoring_comments_and_blanks(tmp_path: Path):
    path = tmp_path / "keys.txt"
    path.write_text("# my keys\n\n  Kabc123  \nKdef456\n", encoding="utf-8")
    assert load_keys(path) == ["Kabc123", "Kdef456"]


def test_load_keys_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="key file not found"):
        load_keys(tmp_path / "nope.txt")


def test_load_keys_empty_file_raises(tmp_path: Path):
    path = tmp_path / "keys.txt"
    path.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no keys"):
        load_keys(path)


def test_mask_key_hides_middle_of_long_key():
    masked = mask_key("Keb294a4b014b4c44b9284a761274b0ae")
    assert masked.startswith("Keb2")
    assert masked.endswith("b0ae")
    assert "294a4b014b4c44b9284a761274" not in masked


def test_mask_key_fully_masks_short_key():
    assert mask_key("abcd") == "****"
