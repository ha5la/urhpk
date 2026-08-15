# 17 — The library modules leave the root

Status: resolved

Blocked by: 01 (end-to-end test)

The root holds 27 Python files with no visible line between the six a person
runs and the twenty-one they import. Move the libraries into a `urhpk/`
package; leave the entry points where every documented command, every shebang
and `run-recorded-round.sh` already names them.

**Stay at the root** (6, 4,830 lines): `contest_video.py`, `puskas_logger.py`,
`on4kst_irc_bridge.py`, `puskas_harvester.py`, `hamlib_supervisor.py`,
`icom_net.py`. Each has a `#!/usr/bin/env -S uv run` shebang and is launched by
path.

**Move to `urhpk/`** (21, 4,413 lines): `cast_render`, `chapters`, `cw_decode`,
`edi`, `geo`, `hud`, `hud_draw`, `loc_cache`, `logbook`, `qso_windows`,
`recorders`, `rig_server`, `rig_state`, `rotator`, `scope_render`, `timeline`,
`video_format`, `wav`, `webcam_log`, `webcam_sync`, `wiring`.

`icom_net.py` is the one judgement call: it is imported as a library by the
logger *and* runs standalone. It keeps its shebang, so it stays.

## Why after 01

Ticket 01 wants the e2e test specifically in the window after a refactor that
moves nearly every file, because the suite covers neither how ffmpeg filter
branches combine nor how a drawn frame looks. This move re-opens that same gap,
and its payoff is legibility, not correctness — the wrong thing to spend
unverified risk on. After 01 is green it is a mechanical afternoon.

## What the move actually touches

- **Imports**: every intra-module import (`import edi` → `from urhpk import
  edi`) and the tests'. `sys.path[0]` is the *script's* directory, so a root
  entry point launched from a round directory still resolves `urhpk.*` — the
  reason the entry points must not move too.
- **`pyproject.toml`**: `pythonpath = ["."]` and `--cov=.`. Coverage scoped to
  the package plus the named entry points is more useful than `.` anyway.
- **Two modules compute paths from `__file__`** and both break silently:
  - `hud_draw.py:56` — `HUD_THEME_DIR` is `dirname(__file__)/hud-theme`. Either
    the artwork moves with it or the expression gains a `.parent`.
  - `wiring.py:16` — `PROJECT_ROOT = Path(__file__).resolve().parent`, carrying
    a comment that says the module sits in the root. This one guards
    `require_round_directory`; a wrong value either refuses a legitimate round
    directory or lets a round run in the root. Its test pins the root as an
    argument, so the test will not catch a wrong default — check by hand.
- **Docs**: ARCHITECTURE.md names the modules throughout; README's components
  table lists only entry points and is unaffected.

## Answer

22 modules moved, 6 entry points left at the root. Two departures from the
plan above:

- **`icom_net.py` went into the package after all.** The ticket kept it at the
  root for its shebang, but three modules that moved — `hud`, `recorders`,
  `scope_render` — import it, so the root would have stayed a dependency of
  `urhpk/`. It imports nothing but stdlib, so `uv run urhpk/icom_net.py
  <radio-ip>` still runs it standalone. README and ARCHITECTURE.md name the
  new path.
- **`mrasz_api.py` and `puskas_standings.py`** postdate the ticket: the first
  moved, the second is an entry point and stayed.

`HUD_THEME_DIR` now reads `wiring.PROJECT_ROOT / "hud-theme"` rather than
climbing `__file__` a second time, so one module knows where the root is.
`PROJECT_ROOT` itself gained the extra `.parent`, checked by hand as the ticket
asked: the root is still refused as a round directory and `urhpk/` is not.

**Verified byte-for-byte.** The `test/` round rendered at 720p with every side
input, before the move and after it: same md5. Plus 707 unit tests and all 10
smoke tests, and each entry point launched by path from a round directory.

## Comments

Raised on the root `TODO` as "should we move the library python files from
project root to a subdirectory?".
