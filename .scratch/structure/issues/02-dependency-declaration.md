# 02 — One dependency declaration, or two

Status: needs-info

Blocks: 03

Today every runtime dependency is declared **twice**:

| | `pyproject.toml` (dev-dependencies) | script PEP 723 header |
|---|---|---|
| prompt_toolkit | `>=3.0` | unpinned |
| numpy | `>=1.26` | unpinned |
| pyte | `>=0.8` | unpinned |
| pillow | `>=10.0` | unpinned |

`uv.lock` locks the project side only. The scripts resolve independently at
run time. So the suite can pass against one numpy while a contest round runs
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
Cost: the standalone-script property goes. `run-recorded-contest-session.sh`
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
