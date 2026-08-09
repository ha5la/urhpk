# 06 — Split puskas_logger.py (2,130 lines)

Status: resolved

Blocked by: 03

Candidate seams: the EDI logbook, the locator cache, the rig server, the
telemetry/input/scope recorders, CW macro expansion, and the prompt_toolkit UI.

The UI resists extraction and should probably stay whole: it is verified by
running rounds rather than by tests, and "no visual glitches" is a real
requirement that no unit test asserts.

## Answer

2,031 lines to 1,243, in five modules. What stayed is the UI and the radio
session, which is the whole of the ticket's own advice plus one addition: the
radio's single network session is what the logger *is* — the radio accepts one
connection and a second silently kills the first, so owning it is the reason
this process exists.

| Module | Lines | What it owns |
|---|---|---|
| `logbook.py` | 286 | QSOs, duplicates, scoring, the EDI export and crash-recovery read |
| `recorders.py` | 371 | The round's side-channel files: telemetry, input box, scope, webcam |
| `loc_cache.py` | 102 | Which locator a callsign uses, merged from three sources |
| `rotator.py` | 78 | The poll thread, the current bearing, "point there" |
| `rig_server.py` | 61 | The rigctld dialect on 4532 |

Three findings, and one seam declined:

- **`RE_LOC` was a fifth copy of the locator pattern.** `geo.py` already owned
  it; the logger spelled the same thing as `^...$` with `re.IGNORECASE`. Six
  call sites now ask `geo.is_locator`, and one of its tests asserts the two
  layers agree rather than merely matching today.
- **A third hand-parser of an EDI QSO line**, which the AST scan behind issue
  03 missed because it never mentions `PCall=`: the locator cache looked for
  `[QSORecords` and split field 9 itself. It goes through `edi.read` now.
  Proved equal on four real round logs, with one deliberate difference — a
  record whose date will not parse is dropped rather than seeded. That path had
  no tests at all; it has five now.
- **`band_summary` carried a fourth literal band tuple** and followed the
  scoring into the logbook.
- **CW macro expansion, declined.** The ticket lists it as a seam, but it is
  ten lines of pure string replacement plus two four-line wrappers over the
  radio session. A module there would be smaller than its own docstring, and
  the wrappers belong wherever the session lives.

`rig_server.serve` takes a `snapshot()` callable rather than reading `_rig`.
That is what let it leave: the module now says nothing about where the radio
state lives, which is also the thing to copy if the rest of the shared state
is ever pulled apart (see backlog 15, the thread map).

Verified by running the logger, not only by the suite. A pty harness answers
the startup prompts and the offline band/mode wizard, types a QSO, and sends
Ctrl-D; the toolbar, the live geo readout, the recent-QSO list, both input-log
event kinds and the saved EDI were all checked after each extraction. It found
what 584 unit tests could not: `main` had `loc_cache = loc_cache.load()`, a
local shadowing the module imported one commit earlier, raising
UnboundLocalError on the second call. Nothing in the suite calls `main()`.
Notes for making that harness permanent are on backlog 01.
