# 03 — The shared library

Status: resolved

Blocked by: 02

Unify the definitions the AST scan found in more than one file. Shape depends
on issue 02 (sibling module vs package).

## Contents

**Geo** — three formulas, each copied 2-3 times, all numerically identical but
written three ways:
- `maidenhead_to_latlon` (logger, bridge, video)
- `initial_bearing` (logger, bridge, video) — keeps its name; `initial_` is the
  geodesy term for the forward azimuth and is not noise
- `haversine_km` (logger, bridge)

The **strict variant wins**: the video's version validates with a regex and
returns `None`. Its callers already handle that; the logger's and bridge's do
not, and their inputs are no more trustworthy — locators arrive from a typed
callsign, a harvested JSON file and an ON4KST user list, none validated today.
Auditing those call sites is the real work here, not the move itself.

**Constants**: `PUSKAS_DIR` (3 files), `ROTCTLD_PORT` (2). `SCOPE_AMP_MAX` and
`SCOPE_WATERFALL_SPAN_S` stop being duplicated once issue 01 lands.

**EDI**, behind a seam. Parsed twice today — `load_from_edi` plus the writer in
`puskas_logger.py`, `parse_edi` plus `_EDI_BAND` in `contest_video.py`. EDI is
the current source of truth but not a permanent one, so this is an interface
question as much as a dedup one: the rest of the code should talk about QSOs,
not about `PCall=` lines. Getting that boundary right is what makes EDI
replaceable later.

Explicitly **out of scope**: band identity. `band_from_hz` (from Hz),
`_EDI_BAND` (from an EDI string) and `_HUD_BANDS` (a tuple) answer three
different questions and need their own thinking, not a mechanical merge.

## Done when

Each fact has one definition, the suite passes, and `CONTEXT.md` names anything
the unification gave a name to.

## Answer

Three modules, not one, because these are three different kinds of fact:

- `geo.py` — the formulas, plus a pair-level layer (`distance_between`,
  `bearing_between`) matching CONTEXT.md's definition of both as properties of
  a station *pair*. Strict variant won; six blanket `except Exception:` blocks
  went with it. Proved equivalent to all three originals on 300 locators and
  400 pairs before anything moved.
- `wiring.py` — the contracts between components. The real finding was not the
  duplicated `PUSKAS_DIR` but the same value under different names at each end:
  `SEEN_STATIONS`/`OUTPUT`, `ON4KST_SEEN`/`ON4KST_SEEN_PATH`,
  `RIG_SERVER_PORT`/`RIGCTLD_PORT`.
- `edi.py` — the format, reading only. Writing stayed in the logger, since
  emitting a log needs the logbook and the scoring; it borrows the tables.

Verified against the real August round, not just the suite: both readers and
the writer produce identical output on the two `260803-HA5LA-*.edi` logs.

Band identity stayed out of scope as planned.
