"""Tests for puskas_standings — pure aggregation, no network."""

import json
from pathlib import Path

import puskas_standings as ps
from puskas_standings import (
    Retention,
    Round,
    categories_of,
    category_of,
    scores_of,
    standings,
)


def _round(code, claimed, evaluated=None):
    return Round(code=code, claimed=claimed, evaluated=evaluated)


class TestCategoryOf:
    def test_strips_the_event_prefix(self):
        assert category_of("PKUV-2026-08-SO-BP") == "SO-BP"

    def test_handles_single_digit_month(self):
        assert category_of("PKUV-2025-2-KEZDO") == "KEZDO"

    def test_survives_the_servers_mistyped_year(self):
        # The May 2026 round is filed under PKUV-2006-04-* on the server.
        assert category_of("PKUV-2006-04-SO-BP") == "SO-BP"


class TestScoresOf:
    def test_keys_scores_by_category_and_callsign(self):
        payload = [
            {
                "evalCategory": {"code": "PKUV-2026-08-SO-BP"},
                "logs": [{"_id": {"callsign": "HA5LA"}, "score": 3289}],
            }
        ]
        assert scores_of(payload) == {("SO-BP", "HA5LA"): 3289}

    def test_a_station_listed_in_two_categories_is_kept_in_both(self):
        payload = [
            {
                "evalCategory": {"code": "PKUV-2025-2-KEZDO"},
                "logs": [{"_id": {"callsign": "HA5LA"}, "score": 332}],
            },
            {
                "evalCategory": {"code": "PKUV-2025-2-SO"},
                "logs": [{"_id": {"callsign": "HA5LA"}, "score": 332}],
            },
        ]
        assert scores_of(payload) == {
            ("KEZDO", "HA5LA"): 332,
            ("SO", "HA5LA"): 332,
        }

    def test_checklogs_are_not_scores(self):
        payload = [
            {
                "evalCategory": {"code": "PKUV-2026-08-CHECKLOG", "isCheckLog": True},
                "logs": [{"_id": {"callsign": "HA5XX"}, "score": 999}],
            }
        ]
        assert scores_of(payload) == {}

    def test_a_log_without_a_score_is_skipped(self):
        payload = [
            {
                "evalCategory": {"code": "PKUV-2026-08-SO-BP"},
                "logs": [{"_id": {"callsign": "HA5LA"}}],
            }
        ]
        assert scores_of(payload) == {}


class TestRetention:
    def test_ratio_is_evaluated_over_claimed(self):
        rounds = [
            _round(
                "R1",
                claimed={("SO-BP", "HA5LA"): 1000},
                evaluated={("SO-BP", "HA5LA"): 800},
            )
        ]
        assert Retention(rounds, min_samples=1).of("HA5LA") == 0.8

    def test_averages_over_rounds(self):
        rounds = [
            _round("R1", {("SO-BP", "X"): 100}, {("SO-BP", "X"): 100}),
            _round("R2", {("SO-BP", "X"): 100}, {("SO-BP", "X"): 50}),
        ]
        assert Retention(rounds, min_samples=1).of("X") == 0.75

    def test_a_station_counts_once_per_round_despite_two_categories(self):
        rounds = [
            _round(
                "R1",
                {("KEZDO", "X"): 100, ("SO", "X"): 100},
                {("KEZDO", "X"): 50, ("SO", "X"): 50},
            )
        ]
        r = Retention(rounds, min_samples=1)
        assert r.samples("X") == 1
        assert r.of("X") == 0.5

    def test_unevaluated_rounds_yield_no_samples(self):
        rounds = [_round("R1", {("SO-BP", "X"): 100}, evaluated=None)]
        assert Retention(rounds, min_samples=1).samples("X") == 0

    def test_thin_history_falls_back_to_the_pooled_mean(self):
        rounds = [
            _round(
                "R1",
                {("SO-BP", "A"): 100, ("SO-BP", "B"): 100},
                {("SO-BP", "A"): 100, ("SO-BP", "B"): 50},
            )
        ]
        # A and B have one sample each, below the threshold, so both get the
        # pooled mean of every ratio seen: (1.0 + 0.5) / 2.
        r = Retention(rounds, min_samples=3)
        assert r.of("A") == 0.75
        assert r.of("B") == 0.75

    def test_an_unknown_station_gets_the_pooled_mean(self):
        rounds = [_round("R1", {("SO-BP", "A"): 100}, {("SO-BP", "A"): 50})]
        assert Retention(rounds, min_samples=1).of("NEVER-SEEN") == 0.5

    def test_no_history_at_all_assumes_no_loss(self):
        assert Retention([], min_samples=1).of("X") == 1.0

    def test_a_zero_claimed_score_is_not_a_ratio(self):
        rounds = [_round("R1", {("SO-BP", "X"): 0}, {("SO-BP", "X"): 0})]
        assert Retention(rounds, min_samples=1).samples("X") == 0


class TestStandings:
    def test_evaluated_rounds_are_summed_as_they_stand(self):
        rounds = [
            _round("R1", {("SO-BP", "X"): 999}, {("SO-BP", "X"): 100}),
            _round("R2", {("SO-BP", "X"): 999}, {("SO-BP", "X"): 200}),
        ]
        (row,) = standings(rounds, "SO-BP", Retention(rounds, min_samples=1))
        assert (row.naive, row.adjusted) == (300, 300)

    def test_a_pending_round_counts_claimed_naively_and_scaled_adjusted(self):
        rounds = [
            _round("R1", {("SO-BP", "X"): 1000}, {("SO-BP", "X"): 500}),
            _round("R2", {("SO-BP", "X"): 1000}, evaluated=None),
        ]
        row = standings(rounds, "SO-BP", Retention(rounds, min_samples=1))[0]
        assert row.naive == 1500
        assert row.adjusted == 1000  # 500 evaluated + 1000 × 0.5

    def test_only_the_rounds_spent_in_the_category_count(self):
        # HG5P moved from the beginners' category to Budapest mid-year; the
        # server credits each category only with the rounds it saw.
        rounds = [
            _round("R1", {("KEZDO", "HG5P"): 741}, {("KEZDO", "HG5P"): 741}),
            _round("R2", {("SO-BP", "HG5P"): 3029}, {("SO-BP", "HG5P"): 3029}),
        ]
        r = Retention(rounds, min_samples=1)
        assert standings(rounds, "SO-BP", r)[0].naive == 3029
        assert standings(rounds, "KEZDO", r)[0].naive == 741

    def test_rows_are_ordered_by_the_adjusted_total(self):
        rounds = [
            _round("R1", {("SO-BP", "LOW"): 10, ("SO-BP", "HIGH"): 20}, None),
        ]
        rows = standings(rounds, "SO-BP", Retention([], min_samples=1))
        assert [r.callsign for r in rows] == ["HIGH", "LOW"]

    def test_the_fault_model_can_overturn_the_naive_order(self):
        history = [
            _round(
                "H",
                {("SO-BP", "SLOPPY"): 100, ("SO-BP", "CLEAN"): 100},
                {("SO-BP", "SLOPPY"): 50, ("SO-BP", "CLEAN"): 100},
            )
        ]
        pending = [
            _round("P", {("SO-BP", "SLOPPY"): 300, ("SO-BP", "CLEAN"): 200}, None)
        ]
        rounds = history + pending
        rows = standings(rounds, "SO-BP", Retention(rounds, min_samples=1))
        assert [r.callsign for r in rows] == ["CLEAN", "SLOPPY"]
        by_naive = sorted(rows, key=lambda r: -r.naive)
        assert [r.callsign for r in by_naive] == ["SLOPPY", "CLEAN"]

    def test_an_absent_station_scores_nothing_for_that_round(self):
        rounds = [
            _round("R1", {("SO-BP", "A"): 100, ("SO-BP", "B"): 100}, None),
            _round("R2", {("SO-BP", "A"): 100}, None),
        ]
        rows = {r.callsign: r for r in standings(rounds, "SO-BP", Retention([]))}
        assert rows["A"].naive == 200
        assert rows["B"].naive == 100

    def test_an_unknown_category_has_no_standings(self):
        rounds = [_round("R1", {("SO-BP", "A"): 100}, None)]
        assert standings(rounds, "SO-VI", Retention([])) == []


class TestCategoryOfMerged:
    """The merged event names the same categories differently."""

    def test_maps_the_merged_code_onto_the_round_category(self):
        assert ps.merged_category_of("PKUV-2026-0MERGED-URH-PK-PEST") == "SO-BP"
        assert ps.merged_category_of("PKUV-2026-0MERGED-URH-OK-KEZDO") == "KEZDO"
        assert ps.merged_category_of("PKUV-2026-0MERGED-URH-PK-VIDEK") == "SO-VI"


class TestAgainstTheOrganisersOwnTable:
    """The aggregation is checked against the published annual table, not just
    against my idea of it: same six evaluated rounds in, same 42 totals out."""

    @staticmethod
    def _fixture():
        path = Path(__file__).parent / "fixtures" / "mrasz-2026-evaluated.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _rounds(self, fixture):
        return [
            Round(code=code, claimed=scores_of(payload), evaluated=scores_of(payload))
            for code, payload in fixture["rounds"].items()
        ]

    def test_reproduces_every_published_total(self):
        fixture = self._fixture()
        rounds = self._rounds(fixture)
        published = {
            (
                ps.merged_category_of(group["evalCategory"]["code"]),
                log["_id"]["callsign"],
            ): log["score"]
            for group in fixture["merged"]
            for log in group["logs"]
        }
        computed = {
            (category, row.callsign): row.naive
            for category in categories_of(rounds)
            for row in standings(rounds, category, Retention(rounds))
        }
        assert published, "fixture lost its published table"
        assert {k: computed.get(k) for k in published} == published

    def test_reproduces_the_published_order(self):
        fixture = self._fixture()
        rounds = self._rounds(fixture)
        for group in fixture["merged"]:
            category = ps.merged_category_of(group["evalCategory"]["code"])
            published = [log["_id"]["callsign"] for log in group["logs"]]
            computed = [
                row.callsign for row in standings(rounds, category, Retention(rounds))
            ]
            assert computed[: len(published)] == published, category

    def test_a_station_that_changed_category_is_not_double_counted(self):
        # HG5P scored in the beginners' category early in the year and in
        # Budapest later; neither total includes the other's rounds.
        fixture = self._fixture()
        rounds = self._rounds(fixture)
        retention = Retention(rounds)
        totals = {
            category: {
                r.callsign: r.naive for r in standings(rounds, category, retention)
            }
            for category in ("KEZDO", "SO-BP")
        }
        assert totals["KEZDO"]["HG5P"] == 741
        assert totals["SO-BP"]["HG5P"] == 3029


def _event(code, final=False, deadline="2026-01-01", claimed=True):
    return {
        "_id": code,
        "code": code,
        "isClaimed": claimed,
        "isFinal": final,
        "submitDeadline": deadline,
        "contest": {"_id": ps.CONTEST_ID},
    }


class TestEventSelection:
    def test_contest_events_are_oldest_first(self, monkeypatch):
        monkeypatch.setattr(
            ps,
            "cached_get",
            lambda *a, **kw: [
                _event("PKUV-2026-02", deadline="2026-02-09"),
                _event("PKUV-2025-2", deadline="2025-02-10"),
            ],
        )
        assert [e["code"] for e in ps.contest_events()] == [
            "PKUV-2025-2",
            "PKUV-2026-02",
        ]

    def test_the_annual_aggregate_is_not_a_round(self, monkeypatch):
        monkeypatch.setattr(
            ps,
            "cached_get",
            lambda *a, **kw: [_event("PKUV-2026-01"), _event("PKUV-2026-MERGED")],
        )
        assert [e["code"] for e in ps.contest_events()] == ["PKUV-2026-01"]

    def test_another_contest_is_not_a_round(self, monkeypatch):
        other = _event("XX-2026-01")
        other["contest"] = {"_id": "000000000000000000000000"}
        monkeypatch.setattr(
            ps, "cached_get", lambda *a, **kw: [_event("PKUV-2026-01"), other]
        )
        assert [e["code"] for e in ps.contest_events()] == ["PKUV-2026-01"]

    def test_in_year_selects_by_code(self):
        events = [_event("PKUV-2025-12"), _event("PKUV-2026-01")]
        assert [e["code"] for e in ps.in_year(events, 2026)] == ["PKUV-2026-01"]

    def test_history_is_the_evaluated_rounds_of_other_years(self):
        events = [
            _event("PKUV-2025-2", final=True),
            _event("PKUV-2025-3", final=False),
            _event("PKUV-2026-01", final=True),
        ]
        # 2026's own rounds carry their own evaluation; only earlier years are
        # borrowed, and only where the organiser has finished with them.
        assert [e["code"] for e in ps.history_events(events, 2026)] == ["PKUV-2025-2"]


class TestFetchRound:
    def _recording_get(self, monkeypatch, payloads):
        calls = []

        def fake_get(url, max_age=None, **kw):
            calls.append((url, max_age))
            for fragment, payload in payloads.items():
                if fragment in url:
                    return payload
            return None

        monkeypatch.setattr(ps, "cached_get", fake_get)
        return calls

    def _payload(self, callsign, score):
        return [
            {
                "evalCategory": {"code": "PKUV-2026-08-SO-BP"},
                "logs": [{"_id": {"callsign": callsign}, "score": score}],
            }
        ]

    def test_an_evaluated_round_brings_back_both_scores(self, monkeypatch):
        self._recording_get(
            monkeypatch,
            {
                "claimed": self._payload("HA5LA", 3289),
                "preliminary": self._payload("HA5LA", 3000),
            },
        )
        rnd = ps.fetch_round({"_id": "e1", "code": "PKUV-2026-06", "isFinal": True})
        assert rnd.claimed == {("SO-BP", "HA5LA"): 3289}
        assert rnd.evaluated == {("SO-BP", "HA5LA"): 3000}
        assert not rnd.pending

    def test_an_evaluated_round_is_cached_forever(self, monkeypatch):
        calls = self._recording_get(monkeypatch, {"claimed": [], "preliminary": []})
        ps.fetch_round({"_id": "e1", "code": "PKUV-2026-06", "isFinal": True})
        assert [max_age for _, max_age in calls] == [None, None]

    def test_a_pending_round_is_not_asked_for_an_evaluation(self, monkeypatch):
        calls = self._recording_get(
            monkeypatch, {"claimed": self._payload("HA5LA", 3289)}
        )
        rnd = ps.fetch_round({"_id": "e2", "code": "PKUV-2026-08", "isFinal": False})
        assert rnd.pending
        assert [url.split("?")[0].rsplit("/", 1)[-1] for url, _ in calls] == ["claimed"]

    def test_a_pending_round_expires_from_the_cache(self, monkeypatch):
        calls = self._recording_get(monkeypatch, {"claimed": []})
        ps.fetch_round({"_id": "e2", "code": "PKUV-2026-08", "isFinal": False})
        assert calls[0][1] == ps.PENDING_TTL

    def test_a_missing_payload_is_an_empty_round(self, monkeypatch):
        self._recording_get(monkeypatch, {})
        rnd = ps.fetch_round({"_id": "e3", "code": "PKUV-2026-08", "isFinal": False})
        assert rnd.claimed == {}


class TestCategoryForCallsign:
    def test_the_most_recent_round_decides(self):
        rounds = [
            _round("R1", {("KEZDO", "HG5P"): 1}),
            _round("R2", {("SO-BP", "HG5P"): 1}),
        ]
        assert ps.category_for_callsign(rounds, "HG5P") == "SO-BP"

    def test_a_checklog_is_not_a_category(self):
        rounds = [_round("R1", {("CHECKLOG", "HA5XX"): 0, ("SO-BP", "HA5XX"): 5})]
        assert ps.category_for_callsign(rounds, "HA5XX") == "SO-BP"

    def test_an_unknown_station_has_none(self):
        assert ps.category_for_callsign([_round("R1", {})], "HA5LA") is None


class TestRender:
    def _rounds(self):
        return [
            _round(
                "PKUV-2026-01",
                {("SO-BP", "SLOPPY"): 100, ("SO-BP", "CLEAN"): 100},
                {("SO-BP", "SLOPPY"): 50, ("SO-BP", "CLEAN"): 100},
            ),
            _round("PKUV-2026-02", {("SO-BP", "SLOPPY"): 300, ("SO-BP", "CLEAN"): 200}),
        ]

    def _render(self, me="CLEAN"):
        rounds = self._rounds()
        retention = Retention(rounds, min_samples=1)
        return ps.render(standings(rounds, "SO-BP", retention), "SO-BP", rounds, me)

    def test_names_the_evaluated_and_the_pending_rounds(self):
        out = self._render()
        assert "1 evaluated, 1 claimed only" in out
        assert "PKUV-2026-01" in out and "PKUV-2026-02" in out

    def test_marks_the_operators_own_row(self):
        assert "* 1. CLEAN" in self._render(me="CLEAN")

    def test_a_row_that_moved_carries_its_naive_place(self):
        out = self._render()
        assert "(naive #2)" in out  # CLEAN leads on adjusted, trails on naive
        assert "(naive #1)" in out

    def test_a_row_that_did_not_move_says_nothing(self):
        rounds = [_round("PKUV-2026-01", {("SO-BP", "A"): 100})]
        out = ps.render(standings(rounds, "SO-BP", Retention([])), "SO-BP", rounds, "A")
        assert "naive #" not in out

    def test_an_empty_category_says_so(self):
        assert "(nobody)" in ps.render([], "SO-VI", [], None)


class TestMain:
    def _stub(self, monkeypatch, rounds, events=None):
        monkeypatch.setattr(
            ps, "contest_events", lambda: events or [_event("PKUV-2026-01")]
        )
        monkeypatch.setattr(ps, "fetch_round", lambda e: rounds[e["code"]])
        monkeypatch.setattr(ps, "my_callsign", lambda: None)

    def test_prints_the_table_for_the_operators_category(self, monkeypatch, capsys):
        self._stub(
            monkeypatch,
            {"PKUV-2026-01": _round("PKUV-2026-01", {("SO-BP", "HA5LA"): 543})},
        )
        assert ps.main(["--callsign", "ha5la"]) == 0
        out = capsys.readouterr().out
        assert "SO-BP" in out
        assert "* 1. HA5LA" in out

    def test_an_explicit_category_overrides_the_callsign(self, monkeypatch, capsys):
        self._stub(
            monkeypatch,
            {
                "PKUV-2026-01": _round(
                    "PKUV-2026-01",
                    {("SO-BP", "HA5LA"): 543, ("SO-VI", "HA1VHF"): 7142},
                )
            },
        )
        assert ps.main(["--callsign", "HA5LA", "--category", "SO-VI"]) == 0
        assert "HA1VHF" in capsys.readouterr().out

    def test_a_year_without_rounds_is_an_error(self, monkeypatch, capsys):
        self._stub(monkeypatch, {})
        assert ps.main(["--year", "1999"]) == 1
        assert "no 1999 rounds" in capsys.readouterr().err

    def test_an_unplaceable_callsign_lists_the_categories(self, monkeypatch, capsys):
        self._stub(
            monkeypatch,
            {"PKUV-2026-01": _round("PKUV-2026-01", {("SO-BP", "HA5LA"): 543})},
        )
        assert ps.main(["--callsign", "NOBODY"]) == 1
        assert "SO-BP" in capsys.readouterr().err

    def test_refresh_stops_pending_rounds_being_served_from_cache(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(ps, "PENDING_TTL", 3600)
        self._stub(
            monkeypatch,
            {"PKUV-2026-01": _round("PKUV-2026-01", {("SO-BP", "HA5LA"): 543})},
        )
        ps.main(["--callsign", "HA5LA", "--refresh"])
        assert ps.PENDING_TTL == 0
