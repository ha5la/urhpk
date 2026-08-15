#!/usr/bin/env -S uv run
"""
Puskás URH Kupa – where the year stands, including the rounds not yet evaluated
===============================================================================
The organiser publishes an annual table only for rounds it has finished
evaluating, so between a round and its evaluation the standings on the website
are months behind. This reproduces that table from the per-round results and
then carries it forward over the rounds still outstanding, for which only the
competitors' own claimed scores exist.

Claimed scores flatter everybody: the cross-check has not run yet, and every
station loses something to it. Two totals are therefore shown side by side —
the naive one that takes claimed scores at face value, and one that scales each
pending score by how much of its claimed score that station has historically
kept. Where the two disagree about the order, the order is not yet knowable.

Usage:  uv run puskas_standings.py [--year 2026] [--callsign HA5LA]
                                   [--category SO-BP] [--refresh]
"""

import argparse
import netrc
import re
import sys
from dataclasses import dataclass

from mrasz_api import BASE_URL, EVENT_LIST_TTL, LIST_URL, cached_get

CONTEST_ID = "67952021b55b621ae6619a4e"
# How many rounds a station must have had evaluated before its own error rate is
# worth more than the field's. Below it, one unlucky round is the whole estimate.
MIN_SAMPLES = 3
# A claimed round is still being submitted and corrected; an evaluated one is
# finished and will never move again.
PENDING_TTL = 3600

Scores = dict[tuple[str, str], int]


@dataclass(frozen=True)
class Round:
    """One month, as claimed by the competitors and — later — as evaluated."""

    code: str
    claimed: Scores
    evaluated: Scores | None

    @property
    def pending(self) -> bool:
        return self.evaluated is None


@dataclass(frozen=True)
class Row:
    callsign: str
    naive: int
    adjusted: int
    retention: float
    samples: int
    pending_claimed: int


# ──────────────────────────────────────────────────────────────
# Reading the server's shape
# ──────────────────────────────────────────────────────────────


def category_of(code: str) -> str:
    """`PKUV-2026-08-SO-BP` → `SO-BP`, the category identity across rounds."""
    return re.sub(r"^PKUV-\d+-\d+-", "", code)


_MERGED_CATEGORIES = {
    "URH-PK-PEST": "SO-BP",
    "URH-PK-VIDEK": "SO-VI",
    "URH-OK-KEZDO": "KEZDO",
    "URH-PK-NON-HA": "NOHA",
}


def merged_category_of(code: str) -> str:
    """The annual table renames every category; this puts the names back."""
    for suffix, category in _MERGED_CATEGORIES.items():
        if code.endswith(suffix):
            return category
    return category_of(code)


def scores_of(payload, category_of=category_of) -> Scores:
    """Every (category, callsign) → score in one result payload.

    A station appears once per category it competes in, and a station that
    changes category mid-year appears in each for the rounds it spent there —
    which is exactly how the organiser's annual table adds them up.
    """
    scores: Scores = {}
    for group in payload:
        eval_category = group.get("evalCategory", {})
        if eval_category.get("isCheckLog"):
            continue
        category = category_of(eval_category.get("code", ""))
        for log in group.get("logs", []):
            callsign = log.get("_id", {}).get("callsign", "").upper().strip()
            if callsign and "score" in log:
                scores[(category, callsign)] = log["score"]
    return scores


# ──────────────────────────────────────────────────────────────
# How much of a claimed score survives evaluation
# ──────────────────────────────────────────────────────────────


class Retention:
    """Each station's history of keeping — or losing — its claimed points.

    Losses are neither negligible nor uniform: over 2025–26 they run from a
    few per cent to a third of a round, and they differ enough between
    stations to decide places. A station with too little history is judged by
    the field's average rather than by one bad month.
    """

    def __init__(self, rounds, min_samples: int = MIN_SAMPLES):
        self._samples: dict[str, list[float]] = {}
        for rnd in rounds:
            if rnd.pending:
                continue
            for callsign, ratio in _round_retention(rnd).items():
                self._samples.setdefault(callsign, []).append(ratio)
        pooled = [r for ratios in self._samples.values() for r in ratios]
        self._pooled_mean = _mean(pooled) if pooled else 1.0
        self._min_samples = min_samples

    def samples(self, callsign: str) -> int:
        return len(self._samples.get(callsign, []))

    def of(self, callsign: str) -> float:
        ratios = self._samples.get(callsign, [])
        if len(ratios) < self._min_samples:
            return self._pooled_mean
        return _mean(ratios)


def _round_retention(rnd: Round) -> dict[str, float]:
    """One ratio per station in this round, whatever categories it appears in."""
    claimed = _best_per_station(rnd.claimed)
    evaluated = _best_per_station(rnd.evaluated or {})
    return {
        callsign: evaluated[callsign] / score
        for callsign, score in claimed.items()
        if score > 0 and callsign in evaluated
    }


def _best_per_station(scores: Scores) -> dict[str, int]:
    per_station: dict[str, int] = {}
    for (_, callsign), score in scores.items():
        per_station[callsign] = max(per_station.get(callsign, 0), score)
    return per_station


def _mean(values) -> float:
    return sum(values) / len(values)


# ──────────────────────────────────────────────────────────────
# The table
# ──────────────────────────────────────────────────────────────


def standings(rounds, category: str, retention: Retention) -> list[Row]:
    """The year's table for one category, ordered by the adjusted total."""
    naive: dict[str, int] = {}
    settled: dict[str, int] = {}
    pending: dict[str, int] = {}
    for rnd in rounds:
        for (cat, callsign), score in rnd.claimed.items():
            if cat != category:
                continue
            naive.setdefault(callsign, 0)
            settled.setdefault(callsign, 0)
            pending.setdefault(callsign, 0)
            if rnd.pending:
                naive[callsign] += score
                pending[callsign] += score
            else:
                evaluated = (rnd.evaluated or {}).get((cat, callsign), 0)
                naive[callsign] += evaluated
                settled[callsign] += evaluated

    rows = [
        Row(
            callsign=callsign,
            naive=naive[callsign],
            adjusted=round(
                settled[callsign] + retention.of(callsign) * pending[callsign]
            ),
            retention=retention.of(callsign),
            samples=retention.samples(callsign),
            pending_claimed=pending[callsign],
        )
        for callsign in naive
    ]
    rows.sort(key=lambda r: (-r.adjusted, r.callsign))
    return rows


def categories_of(rounds) -> list[str]:
    return sorted({cat for rnd in rounds for cat, _ in rnd.claimed})


def category_for_callsign(rounds, callsign: str) -> str | None:
    """The category the station last competed in — the one it is ranked in."""
    for rnd in reversed(rounds):
        for cat, call in rnd.claimed:
            if call == callsign and cat not in ("CHECKLOG", "NOHA"):
                return cat
    return None


# ──────────────────────────────────────────────────────────────
# Fetching
# ──────────────────────────────────────────────────────────────


def contest_events() -> list[dict]:
    """Every round the contest has ever held, oldest first, aggregates excluded."""
    events = cached_get(LIST_URL, max_age=EVENT_LIST_TTL) or []
    rounds = [
        e
        for e in events
        if e.get("contest", {}).get("_id") == CONTEST_ID
        and e.get("isClaimed")
        and "MERGED" not in e.get("code", "")
    ]
    rounds.sort(key=lambda e: e.get("submitDeadline", ""))
    return rounds


def in_year(events, year: int) -> list[dict]:
    return [e for e in events if e.get("code", "").startswith(f"PKUV-{year}-")]


def history_events(events, year: int) -> list[dict]:
    """Earlier years' evaluated rounds — the evidence for how a station scores.

    Six rounds of one year is a thin basis for a station's error rate, and the
    rate is a property of the operator rather than of the season, so previous
    years count too.
    """
    return [
        e
        for e in events
        if e.get("isFinal") and not e.get("code", "").startswith(f"PKUV-{year}-")
    ]


def fetch_round(event: dict) -> Round:
    """One round's claimed and — where the organiser gave one — evaluated result."""
    event_id = event["_id"]
    final = bool(event.get("isFinal"))
    max_age = None if final else PENDING_TTL
    claimed = cached_get(f"{BASE_URL}/claimed?eventId={event_id}", max_age=max_age)
    evaluated = None
    if final:
        payload = cached_get(f"{BASE_URL}/preliminary?eventId={event_id}")
        if isinstance(payload, list):
            evaluated = scores_of(payload)
    return Round(
        code=event.get("code", event_id),
        claimed=scores_of(claimed) if isinstance(claimed, list) else {},
        evaluated=evaluated,
    )


def my_callsign() -> str | None:
    try:
        entry = netrc.netrc().authenticators("www.on4kst.info")
    except (FileNotFoundError, netrc.NetrcParseError):
        return None
    return entry[0].upper() if entry else None


# ──────────────────────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────────────────────


def render(rows: list[Row], category: str, rounds, me: str | None) -> str:
    naive_rank = {
        row.callsign: i
        for i, row in enumerate(sorted(rows, key=lambda r: (-r.naive, r.callsign)), 1)
    }
    settled = [r.code for r in rounds if not r.pending]
    outstanding = [r.code for r in rounds if r.pending]

    lines = [
        f"{category} — {len(settled)} evaluated, {len(outstanding)} claimed only",
        f"  evaluated: {', '.join(settled) or 'none'}",
        f"  pending:   {', '.join(outstanding) or 'none'}",
        "",
        f"{'':2} {'call':9}{'naive':>8}{'adj':>8}{'pending':>9}{'keep':>7}{'n':>4}  ",
    ]
    for place, row in enumerate(rows, 1):
        moved = naive_rank[row.callsign] - place
        marker = "*" if row.callsign == me else " "
        shift = f"  (naive #{naive_rank[row.callsign]})" if moved else ""
        lines.append(
            f"{marker}{place:>2}. {row.callsign:9}{row.naive:>7}{row.adjusted:>8}"
            f"{row.pending_claimed:>9}{row.retention:>7.1%}{row.samples:>4}{shift}"
        )
    if not rows:
        lines.append("  (nobody)")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--callsign", help="whose category to show (default: ~/.netrc)")
    parser.add_argument("--category", help="e.g. SO-BP, SO-VI, KEZDO")
    parser.add_argument(
        "--refresh", action="store_true", help="ignore cached un-evaluated rounds"
    )
    args = parser.parse_args(argv)

    if args.refresh:
        global PENDING_TTL
        PENDING_TTL = 0

    events = contest_events()
    this_year = in_year(events, args.year)
    if not this_year:
        print(f"[!] no {args.year} rounds found", file=sys.stderr)
        return 1
    rounds = [fetch_round(e) for e in this_year]
    history = [fetch_round(e) for e in history_events(events, args.year)]

    me = (args.callsign or my_callsign() or "").upper() or None
    category = args.category or (me and category_for_callsign(rounds, me))
    if not category:
        print(
            f"[!] no category found; try --category with one of "
            f"{', '.join(categories_of(rounds))}",
            file=sys.stderr,
        )
        return 1

    retention = Retention(history + rounds)
    print(render(standings(rounds, category, retention), category, rounds, me))
    return 0


if __name__ == "__main__":
    sys.exit(main())
