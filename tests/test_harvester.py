"""Tests for puskas_harvester pure logic — no network, no filesystem side-effects."""
import json

import puskas_harvester
from puskas_harvester import CONTEST_ID, fetch_event_ids


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
