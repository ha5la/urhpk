"""Tests for puskas_harvester pure logic — no network, no filesystem side-effects."""
import json
from unittest.mock import MagicMock, patch

import puskas_harvester
from puskas_harvester import (
    CONTEST_ID,
    fetch_claimed,
    fetch_event_ids,
    fetch_qsos,
    fetch_round_codes,
)


def _event(eid: str, deadline: str, claimed: bool = True) -> dict:
    return {
        "_id": eid,
        "isClaimed": claimed,
        "contest": {"_id": CONTEST_ID},
        "submitDeadline": deadline,
    }


class TestFetchEventIds:
    def test_returns_ids_oldest_submitdeadline_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(puskas_harvester, "CACHE_DIR", tmp_path)
        events = [
            _event("newest", "2026-05-11T22:59:00.000Z"),
            _event("oldest", "2026-02-09T22:59:00.000Z"),
            _event("middle", "2026-03-09T22:59:00.000Z"),
        ]
        (tmp_path / "events_list.json").write_text(json.dumps(events))
        assert fetch_event_ids() == ["oldest", "middle", "newest"]

    def test_excludes_unclaimed_events(self, tmp_path, monkeypatch):
        monkeypatch.setattr(puskas_harvester, "CACHE_DIR", tmp_path)
        events = [
            _event("claimed",   "2026-03-09T22:59:00.000Z", claimed=True),
            _event("unclaimed", "2026-04-13T21:59:00.000Z", claimed=False),
        ]
        (tmp_path / "events_list.json").write_text(json.dumps(events))
        assert fetch_event_ids() == ["claimed"]

    def test_excludes_other_contests(self, tmp_path, monkeypatch):
        monkeypatch.setattr(puskas_harvester, "CACHE_DIR", tmp_path)
        events = [
            _event("puskas", "2026-03-09T22:59:00.000Z"),
            {
                "_id": "other",
                "isClaimed": True,
                "contest": {"_id": "000000000000000000000000"},
                "submitDeadline": "2026-04-01T00:00:00.000Z",
            },
        ]
        (tmp_path / "events_list.json").write_text(json.dumps(events))
        assert fetch_event_ids() == ["puskas"]


class TestCachedGet:
    def test_returns_cached_data_without_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(puskas_harvester, "CACHE_DIR", tmp_path)
        payload = [{"call": "HA5LA"}]
        (tmp_path / "_endpoint.json").write_text(json.dumps(payload))
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = puskas_harvester._cached_get(puskas_harvester.BASE_URL + "/endpoint")
        assert result == payload
        mock_urlopen.assert_not_called()

    def test_fetches_and_caches_on_miss(self, tmp_path, monkeypatch):
        monkeypatch.setattr(puskas_harvester, "CACHE_DIR", tmp_path)
        payload = {"stations": 42}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_ctx), patch("time.sleep"):
            result = puskas_harvester._cached_get(puskas_harvester.BASE_URL + "/data")
        assert result == payload
        assert (tmp_path / "_data.json").exists()

    def test_network_error_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(puskas_harvester, "CACHE_DIR", tmp_path)
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = puskas_harvester._cached_get(puskas_harvester.BASE_URL + "/bad")
        assert result is None


class TestFetchClaimed:
    def _mock(self, monkeypatch, data):
        monkeypatch.setattr(puskas_harvester, "_cached_get", lambda url: data)

    def test_extracts_callsigns_and_wwls(self, monkeypatch):
        self._mock(monkeypatch, [
            {"logs": [{"_id": {"callsign": "ha5la", "WWL": "jn97tf"}}]},
        ])
        assert fetch_claimed("eid") == [{"callsign": "HA5LA", "wwl": "JN97TF"}]

    def test_first_occurrence_wins_across_categories(self, monkeypatch):
        self._mock(monkeypatch, [
            {"logs": [{"_id": {"callsign": "HA5LA", "WWL": "JN97TF"}}]},
            {"logs": [{"_id": {"callsign": "HA5LA", "WWL": "JN97MX"}}]},
        ])
        result = fetch_claimed("eid")
        assert len(result) == 1
        assert result[0]["wwl"] == "JN97TF"

    def test_skips_entries_with_empty_call_or_wwl(self, monkeypatch):
        self._mock(monkeypatch, [{"logs": [
            {"_id": {"callsign": "",      "WWL": "JN97TF"}},
            {"_id": {"callsign": "HA5LA", "WWL": ""}},
            {"_id": {"callsign": "HA7NS", "WWL": "JN97WM"}},
        ]}])
        result = fetch_claimed("eid")
        assert len(result) == 1
        assert result[0]["callsign"] == "HA7NS"

    def test_no_data_returns_empty(self, monkeypatch):
        self._mock(monkeypatch, None)
        assert fetch_claimed("eid") == []


class TestFetchRoundCodes:
    def _mock(self, monkeypatch, data):
        monkeypatch.setattr(puskas_harvester, "_cached_get", lambda url: data)

    def test_extracts_codes(self, monkeypatch):
        self._mock(monkeypatch, {"logs": [
            {"rounds": [{"code": "2M"}, {"code": "70CM"}]},
        ]})
        assert fetch_round_codes("eid", "HA5LA") == ["2M", "70CM"]

    def test_deduplicates_codes(self, monkeypatch):
        self._mock(monkeypatch, {"logs": [
            {"rounds": [{"code": "2M"}]},
            {"rounds": [{"code": "2M"}, {"code": "70CM"}]},
        ]})
        assert fetch_round_codes("eid", "HA5LA") == ["2M", "70CM"]

    def test_no_data_returns_empty(self, monkeypatch):
        self._mock(monkeypatch, None)
        assert fetch_round_codes("eid", "HA5LA") == []


class TestFetchQsos:
    def _mock(self, monkeypatch, data):
        monkeypatch.setattr(puskas_harvester, "_cached_get", lambda url: data)

    def test_extracts_qso_fields(self, monkeypatch):
        self._mock(monkeypatch, {"qsos": [
            {"callsign": "ha7ns", "rWWL": "jn97wm", "band": "144"},
        ]})
        assert fetch_qsos("eid", "HA5LA", "2M") == [
            {"callsign": "HA7NS", "wwl": "JN97WM", "band": "144"},
        ]

    def test_skips_entries_missing_call_or_wwl(self, monkeypatch):
        self._mock(monkeypatch, {"qsos": [
            {"callsign": "",      "rWWL": "JN97WM", "band": "144"},
            {"callsign": "HA7NS", "rWWL": "",       "band": "144"},
            {"callsign": "HA8IB", "rWWL": "KN06HT", "band": "432"},
        ]})
        result = fetch_qsos("eid", "HA5LA", "2M")
        assert len(result) == 1
        assert result[0]["callsign"] == "HA8IB"

    def test_no_data_returns_empty(self, monkeypatch):
        self._mock(monkeypatch, None)
        assert fetch_qsos("eid", "HA5LA", "2M") == []


class TestMain:
    def _setup(self, monkeypatch, tmp_path, *, event_ids, claimed_by_id,
               round_codes=None, qsos=None):
        monkeypatch.setattr(puskas_harvester, "fetch_event_ids", lambda: event_ids)
        monkeypatch.setattr(puskas_harvester, "fetch_claimed",
                            lambda eid: claimed_by_id.get(eid, []))
        monkeypatch.setattr(puskas_harvester, "fetch_round_codes",
                            lambda eid, call: (round_codes or {}).get((eid, call), []))
        monkeypatch.setattr(puskas_harvester, "fetch_qsos",
                            lambda eid, call, code: (qsos or {}).get((eid, call, code), []))
        output = tmp_path / "output.json"
        monkeypatch.setattr(puskas_harvester, "PUSKAS_DIR", tmp_path)
        monkeypatch.setattr(puskas_harvester, "OUTPUT", output)
        return output

    def test_newer_round_locator_appears_first(self, tmp_path, monkeypatch):
        output = self._setup(
            monkeypatch, tmp_path,
            event_ids=["old", "new"],
            claimed_by_id={
                "old": [{"callsign": "HA5LA", "wwl": "JN97TF"}],
                "new": [{"callsign": "HA5LA", "wwl": "JN97MX"}],
            },
        )
        puskas_harvester.main()
        data = json.loads(output.read_text())
        assert data["HA5LA"]["wwls"] == ["JN97MX", "JN97TF"]

    def test_duplicate_locator_not_stored_twice(self, tmp_path, monkeypatch):
        output = self._setup(
            monkeypatch, tmp_path,
            event_ids=["r1", "r2"],
            claimed_by_id={
                "r1": [{"callsign": "HA5LA", "wwl": "JN97TF"}],
                "r2": [{"callsign": "HA5LA", "wwl": "JN97TF"}],
            },
        )
        puskas_harvester.main()
        data = json.loads(output.read_text())
        assert data["HA5LA"]["wwls"] == ["JN97TF"]

    def test_accumulates_bands_from_qsos(self, tmp_path, monkeypatch):
        output = self._setup(
            monkeypatch, tmp_path,
            event_ids=["e1"],
            claimed_by_id={"e1": [{"callsign": "HA5LA", "wwl": "JN97TF"}]},
            round_codes={("e1", "HA5LA"): ["2M", "70CM"]},
            qsos={
                ("e1", "HA5LA", "2M"):   [{"callsign": "HA7NS", "wwl": "JN97WM", "band": "144"}],
                ("e1", "HA5LA", "70CM"): [{"callsign": "HA7NS", "wwl": "JN97WM", "band": "432"}],
            },
        )
        puskas_harvester.main()
        data = json.loads(output.read_text())
        assert set(data["HA5LA"]["bands"]) == {"144", "432"}

    def test_qso_partner_callsigns_not_recorded(self, tmp_path, monkeypatch):
        output = self._setup(
            monkeypatch, tmp_path,
            event_ids=["e1"],
            claimed_by_id={"e1": [{"callsign": "HA5LA", "wwl": "JN97TF"}]},
            round_codes={("e1", "HA5LA"): ["2M"]},
            qsos={("e1", "HA5LA", "2M"): [{"callsign": "HA7NS", "wwl": "JN97WM", "band": "144"}]},
        )
        puskas_harvester.main()
        data = json.loads(output.read_text())
        assert "HA7NS" not in data

    def test_no_events_writes_no_file(self, tmp_path, monkeypatch):
        output = self._setup(
            monkeypatch, tmp_path,
            event_ids=[],
            claimed_by_id={},
        )
        puskas_harvester.main()
        assert not output.exists()
