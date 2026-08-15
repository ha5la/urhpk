"""Tests for the shared MRASZ API cache — no real network."""

import json
from unittest.mock import MagicMock, patch

import mrasz_api
from mrasz_api import cached_get


def _urlopen_returning(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    ctx = MagicMock()
    ctx.__enter__.return_value = resp
    return patch("urllib.request.urlopen", return_value=ctx)


class TestCachedGet:
    def test_returns_cached_data_without_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mrasz_api, "CACHE_DIR", tmp_path)
        payload = [{"call": "HA5LA"}]
        (tmp_path / "_endpoint.json").write_text(json.dumps(payload))
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = cached_get(mrasz_api.BASE_URL + "/endpoint")
        assert result == payload
        mock_urlopen.assert_not_called()

    def test_fetches_and_caches_on_miss(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mrasz_api, "CACHE_DIR", tmp_path)
        payload = {"stations": 42}
        with _urlopen_returning(payload), patch("time.sleep"):
            result = cached_get(mrasz_api.BASE_URL + "/data")
        assert result == payload
        assert (tmp_path / "_data.json").exists()

    def test_network_error_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mrasz_api, "CACHE_DIR", tmp_path)
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = cached_get(mrasz_api.BASE_URL + "/bad")
        assert result is None
