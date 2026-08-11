# 02 — One dependency declaration, or two

Status: resolved

Blocks: 03

Today every runtime dependency is declared **twice**:

| | `pyproject.toml` (dev-dependencies) | script PEP 723 header |
|---|---|---|
| prompt_toolkit | `>=3.0` | unpinned |
| numpy | `>=1.26` | unpinned |
| pyte | `>=0.8` | unpinned |
| pillow | `>=10.0` | unpinned |

`uv.lock` locks the project side only. The scripts resolve independently at
run time. So the suite can pass against one numpy while a round runs
against another, and nothing would report the difference — the same divergence
this effort is removing from the code, one level down.

This has to be settled before the shared library, because it decides what the
library *is*: a sibling module (imported because `sys.path[0]` is the script's
own directory — verified: a sibling import works from an arbitrary CWD under a
`uv run --script` shebang) or a package the project installs.

## Options

**(a) Full uv project.** Runtime deps move to `[project] dependencies`, scripts
lose their PEP 723 headers, `uv.lock` covers everything. One declaration, one
resolution, one lock.
Cost: the standalone-script property goes. `run-recorded-round.sh`
launches the logger as `$d/puskas_logger.py` from a contest directory; that
keeps working while the directory is inside the repo (uv walks up to find
`pyproject.toml`), but a script copied elsewhere no longer runs. CLAUDE.md
currently advertises the opposite ("no shared requirements file"), so that
convention changes too.

**(b) Keep PEP 723, strip the duplicates from pyproject.** Minimal edit.
Does not actually work: the suite imports `puskas_logger`, which imports
`prompt_toolkit`, so the test environment genuinely needs the runtime deps.
Removing them from pyproject breaks the tests. Listed only to record why it
was rejected.

**(c) Status quo**, with the duplication documented as intentional.

## Recommendation

**(a)**, but it is the user's call — it trades a property the project has
advertised since it was one script. The deciding fact is that the drift is
already possible today and would be invisible when it bites, which is during a
round.

## Answer

**(a) — full uv project.** Approved by the user.

Runtime deps moved to `[project] dependencies`; dev-dependencies keep only the
test tooling; PEP 723 headers removed from all six scripts; shebangs are now
`#!/usr/bin/env -S uv run`.

Verified rather than assumed, in a scratch project with a round subdirectory:

- a script launched by shebang from a round subdir finds the project (uv walks
  up from the CWD), installs from the lock, and runs
- `sys.path[0]` is still the script's own directory, so issue 03's shared
  library can be a plain sibling module with no packaging
- launched from outside the project it fails with `ModuleNotFoundError` — the
  accepted cost, and acceptable because a round directory is always inside the
  project

Unification also settled a discrepancy nobody had noticed: `contest_video.py`
asked for `requires-python = ">=3.11"` while every other script and
`pyproject.toml` said `>=3.12`.
