# Structure: compress the project

A project is finished when nothing more can be removed from it and it still
implements the spec. This effort applies that to `urhpk`, treating the codebase
the way a compressor treats data: find what is stored more than once, store it
once; find what is never read, drop it; and give the frequent concepts short
names so every later mention is cheap.

Three forces pull code out of a script, and only the first is deduplication:

1. **The same fact is known in two places** — it will diverge, and here it
   already has.
2. **A file is too big to hold in one head** — `contest_video.py` is 3,995
   lines, `puskas_logger.py` 2,130. Structure is a reason to split even when
   nothing is duplicated.
3. **A boundary is worth having** — the EDI log is the current source of
   truth, but it is not a permanent one. Code behind an interface can be
   replaced; code smeared across two scripts cannot.

Prose compresses the same way. "There's a problem when a lesson inside a
section of a course is made real (i.e. given a spot in the file system)"
becomes "there's a problem with the materialization cascade" — once the concept
has a name. `CONTEXT.md` is where those names live, so naming a concept and
shortening the prose that describes it are the same act.

## What the scan found

Measured with an AST scan (`.scratch/structure/` notes), not by eye.

**Drifted copies of one formula** — numerically identical, three notations:

| function | copies in | drift |
|---|---|---|
| `maidenhead_to_latlon` | logger, bridge, video | `2/24` vs `5/60`; video validates and returns `None`, the others assume valid input |
| `initial_bearing` | logger, bridge, video | `math.radians` vs hand-rolled `p = pi/180`; `% 360` vs `(+360) % 360` |
| `haversine_km` | logger, bridge | same split |

**Shared knowledge, independently implemented**: EDI is parsed twice —
`load_from_edi` plus the writer in `puskas_logger.py`, `parse_edi` plus
`_EDI_BAND` in `contest_video.py`.

**Constants in more than one file**, all the same value: `PUSKAS_DIR` ×3,
`ROTCTLD_PORT` ×2, `SCOPE_AMP_MAX` ×2, `SCOPE_WATERFALL_SPAN_S` ×2.

**Dependencies declared twice**: `pyproject.toml`'s dev-dependencies list
`prompt_toolkit`, `numpy`, `pyte`, `pillow` — the same four the scripts declare
in their own PEP 723 headers, with different version policies (`numpy>=1.26`
vs unpinned) and only the dev side locked by `uv.lock`.

**One finding that is the opposite problem**: `_mode_str` exists twice and is
*not* duplication. In `icom_net.py` it maps a CI-V integer to a mode name; in
`puskas_logger.py` it normalizes a mode string to a family (`USB`→`SSB`). Two
concepts, one name.

## Decisions taken

- The shared library covers geo, shared constants and EDI — every place two
  scripts know the same domain fact. Band identity (`band_from_hz` vs
  `_EDI_BAND` vs `_HUD_BANDS`) is held back: those answer three different
  questions and merging them needs its own thinking.
- When copies disagree, **the strict variant wins**. Callers that cannot
  currently fail must learn to handle `None`.
- `_mode_str`'s collision is resolved by renaming **both** halves to say what
  they are, and recording both in `CONTEXT.md`.
- The purge takes provably-unreferenced code **and** code reachable only via
  inputs that can no longer occur. Test coverage is not evidence of deadness —
  `puskas_logger.py` sits at 40% because its UI is verified by running rounds,
  not by unit tests. Each "input can no longer occur" candidate is argued
  individually before removal.
- `initial_bearing` keeps its name: `initial_` is the geodesy term for the
  forward azimuth at the start of a great circle, which differs from the final
  bearing. `bearing_from_to` would be a lossier name.

## Open decision: one dependency declaration, or two

Not settled — see `issues/02-dependency-declaration.md`. It blocks the library
work, because it decides whether the library is a sibling module or a package.

## Order

Each item is a separate issue file. They are ordered so that earlier ones
shrink the surface later ones have to move.

1. `01-drop-scope-preview.md` — remove the one-time preview tool
2. `02-dependency-declaration.md` — uv project or PEP 723 headers (decision)
3. `03-shared-library.md` — geo, constants, EDI behind a replaceable seam
4. `04-mode-str-collision.md` — two concepts, two names
5. `05-split-contest-video.md` — 3,995 lines into modules
6. `06-split-logger.md` — 2,130 lines into modules
7. `07-prose-compression.md` — name the concepts, shorten the comments
8. `08-purge.md` — unreferenced code and unreachable codepaths
