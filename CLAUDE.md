# Puskás URH Kupa – project context

A toolset for one amateur radio contest (Puskás URH Kupa), plus a general-purpose
ON4KST↔IRC bridge. One operator, one laptop, one radio: everything runs on the
same machine during a round, and the whole point of the project is that the round
itself is captured well enough to be reconstructed afterwards as a video.

**Read PIPELINE.md first** — it is the story the components serve, and most
questions about "why does this exist" are answered by where a thing sits in it.

## The documents

Three live at the root because something insists: GitHub renders README.md,
the harness reads CLAUDE.md, `docs/agents/domain.md` pins CONTEXT.md. The rest
are in `docs/`, and are referred to by bare filename everywhere.

| File | What belongs in it |
|---|---|
| **CLAUDE.md** (this file) | How to work here: development principles, conventions, house rules |
| **CONTEXT.md** | What each word means, and nothing else — the project's glossary |
| **README.md** | The public face: components table, quick starts |
| **docs/PIPELINE.md** | The end-to-end story: harvest → run the round → render → publish |
| **docs/ARCHITECTURE.md** | Component by component: what each does and the constraints an edit must not break |
| **docs/RECORDING.md** | Practical how-to for recording a round and producing a video, with real numbers |
| **docs/FINDINGS.md** | Measurements, protocol archaeology and dead ends — the evidence behind the rules |
| **docs/operator-setup.md** | This operator's terminal configuration — settings, not project |
| **hud-theme/artwork-prompt.md** | Generation prompt and layout spec for the HUD artwork — beside the artwork it generates, and travels with a copied theme |

## Housekeeping

- When adding or removing a component, update **README.md**'s components table
  and **PIPELINE.md** if it changes the story.
- **Keep the documents in their lanes.** Component detail goes to
  ARCHITECTURE.md, research narrative and rejected approaches to FINDINGS.md, and
  history that only explains how the code *used to* look goes nowhere — git keeps
  it. CLAUDE.md was 2157 lines once, for exactly those reasons.
- **A term defined in CONTEXT.md is used, not redefined.** The other documents and
  the code spell it that way and move on; if a definition needs changing, it changes
  in CONTEXT.md. Synonyms are pollution here, not style — three words for one
  concept (azimuth = bearing = rotator angle) cost more than the repetition they
  avoid.

## Development principles

TDD, red before green, pinned time, comment discipline and commit discipline are
in the global CLAUDE.md and are not restated here. What this project adds:

- **Concurrency is asyncio, not threads.** Everything concurrent here waits on I/O
  or on a timer; nothing waits on the CPU, and prompt_toolkit already runs a loop —
  `Application.run()` ends in `asyncio.run`. Threads buy nothing against a socket
  and cost the one hazard a single-threaded loop cannot have: a deadlock.
  `on4kst_irc_bridge.py` is the worked example, 32 coroutines and no locks. When
  something must happen while something else waits, write a coroutine and a task.
  The escape hatch is a genuinely blocking library call with no async form —
  `asyncio.to_thread` at the boundary, holding no lock — and a thread that needs a
  lock is a design that needs rethinking instead. FINDINGS.md has the audit this
  rule came from, including the deadlock it found on the round's normal exit path.
- **What "verify against reality" means here**: how ffmpeg filter branches
  *combine*, how a drawn frame actually looks and how a real radio's session state
  machine reacts are all invisible to the unit suite. Render the clip and decode a
  frame; capture the packets; measure against the real recording.
- **Async tests poll, they don't sleep**: `tests/helpers.py` has
  `wait_until`/`wait_until_sync`. Where a deterministic terminator exists, wait on
  that instead — `on4kst_irc_bridge.py`'s IRC registration always ends in numeric
  366, so its tests `recv_until("366")`.
- **No visual glitches**: the logger UI must look professional at all times. Transient
  incorrect states (e.g. a dup highlight flashing for one frame during a state transition)
  are bugs.

## Conventions

**Credentials** live in `~/.netrc`, never in the repo and never hardcoded:
`machine www.on4kst.info login ha5la password …` for the chat, and a
`machine <radio-ip>` entry for the radio's LAN login. The callsign is read from
there at startup (uppercased); the grid locator is fetched from the ON4KST server
via `/SHow CONFig` after login.

**File layout — global databases live in `~`, per-round files in the CWD.** A
contest directory holds exactly one round. `puskas_logger.py` and
`contest_video.py` enforce it by refusing to start in the project root, where a
mistaken launch lands and several rounds' files cannot be told apart afterwards.
`--hud-demo` and `--hud-theme-check` are exempt — they write one PNG and exit,
and iterating the HUD's layout from the root is how they are meant to be used.

| Path | What |
|---|---|
| `~/.puskas/puskas-seen-stations.json` | harvested station database (all rounds, accumulates) |
| `~/.puskas/on4kst-seen-stations.json` | ON4KST session database (written by the bridge) |
| `.puskas_cache/` | API response cache (CWD; delete to force a fresh fetch) |
| `*.edi`, `*.jsonl`, `*.scope`, `*.cast`, `*.mp4` | one round's own files (CWD) |

**Running**: this is one `uv` project, not a set of standalone scripts.
Dependencies are declared once in `pyproject.toml` and locked in `uv.lock` — no
per-script PEP 723 headers, and no virtualenv to activate. Every component runs
either as `uv run <script>.py` or directly by its `#!/usr/bin/env -S uv run`
shebang — `uv` finds the project by walking up from the current directory, so a
script launched from a round directory inside the project works, and one
launched from outside the project does not. PIPELINE.md has the order they are
actually used in.

## Testing

Enforced by `pre-commit`, not by a checklist here — one-time setup per clone:
`uv run pre-commit install`. What runs is defined in `.pre-commit-config.yaml`,
the only source of truth; CI runs the same config rather than a separately
maintained list of steps.

**Ruff policy**: both `ruff check` and `ruff format` run via pre-commit. Aligned
assignment style is not preserved — `ruff format` collapses it, accepted as worth
avoiding the diff noise of realigning a block whenever one name's length changes.
E501 (line length) and E701 (single-line `if …: return` in lookup functions) are
suppressed for `ruff check`.

Run `uv run ruff check .` over the **whole project**, not just changed files —
scoping it to one file has already missed a CI failure.

## Repository

`.gitignore` excludes scratch files (`*.json`, `*.url`, `*.txt`).

Commits go straight to `master` — one operator, one line of history. Don't
open a branch unless asked.

## Agent skills

### Issue tracker

Issues live as markdown files under `.scratch/<feature-slug>/` in this repo. See
`docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, unrenamed: `needs-triage`, `needs-info`,
`ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` and `docs/adr/` at the repo root, created lazily.
See `docs/agents/domain.md`.
