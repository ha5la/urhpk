# 04 — My computed score disagrees with the server's

Status: resolved

The server is the authority and reports 34 × 2m QSOs = 2352 points, 22 × 70cm
= 937 points. Our own figure differs — probably rounding, but that is a guess.

Approach: datamine previous scores from the server the way `puskas_harvester.py`
already mines claimed rounds, and find where the difference enters. Done when
our number equals the server's for every past round we can fetch, not just for
this one.

## Answer

Not rounding — a missing point. The server scores `int(km) + 1`; we scored
`int(km)`, so we were short by exactly one point per QSO (34 on 2m, 22 on 70cm,
which is the whole discrepancy). A QSO inside my own square is 0 km and pays 1.

Datamined from the `/qso` endpoint's per-QSO `points` field: 8290 of 8290 QSOs
the evaluator scored, for every station in every cached round, are `int(km) + 1`
of `geo.py`'s own haversine. The 79 exceptions are QSOs it threw out (INVALID,
X-QSO), not distances it disagrees about.

`LogBook.points()` now applies the rule and `QSO.dist_km` is `QSO.points`,
because that is what the field always was. `tests/fixtures/mrasz-scored-qsos.json`
holds this station's 499 scored QSOs over 28 rounds, and the tests reproduce
every QSO and every round total. Recomputing the last two rounds' EDI files
end-to-end gives 2352 + 937 (August) and 2438 + 909 (July) — the server's
numbers exactly. Evidence in FINDINGS.md.

Old EDI files keep their old points: `load_from_edi` prefers the points already
in the file, which is what crash recovery must do.
