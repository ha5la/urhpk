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

- **Succinct code comments**: the first question is not "is this short enough" but
  **what would it cost to omit this entirely** — usually nothing. Then: can a clearer
  identifier carry it instead (Robert C. Martin)? Only what survives both gets a
  comment, and then a sentence, not an essay. Three kinds are worth zero and should
  not be written: **history** ("this used to be X" — git keeps it), **restating the
  standard**, and **justifying a duplication or workaround** — that last one is a
  load-bearing excuse, it goes stale silently because nothing tests a justification,
  and it ends up arguing against a fix that has become free.
- **Kent Beck's simplicity rule**: always implement the simplest thing that works.
  Prefer decremental development — remove code that isn't needed rather than keeping
  it "just in case". Dead code is technical debt.
- **Tests over markdown for requirements**: requirements are best expressed as tests —
  they are executable, unambiguous, and cannot go stale silently. Markdown is the
  second-best option. Prose-only requirements are a last resort for things that
  genuinely cannot be tested (visual UX, hardware interactions).
- **Tests must always pass**: never commit with a failing test. The test suite is the
  safety net for refactoring and simplification.
- **Commit each finished topic before starting the next**: don't let unrelated changes
  from different features pile up in one working tree — it makes a clean commit split
  expensive later. One session let four unrelated topics pile up, and splitting them
  afterwards meant reconstructing each slice by hand against a full end-state backup,
  with no intermediate history left to split from.
- **Prove a regression test catches the bug — red before green**: write the test
  against the still-buggy code and watch it actually fail, *then* write the fix and
  watch the test pass. Don't just reason that a test "should" fail on the old code —
  a test that looks right but was never seen red is unverified, and writing it after
  the fix already exists risks unconsciously shaping the assertion around whatever the
  fix happens to produce. If a fix was already written before the test (e.g. the bug
  and its cause were understood in the same pass), the fallback is to temporarily
  revert the fix (or monkeypatch the specific buggy function back), confirm the test
  fails, then restore the fix and confirm it passes — strictly weaker than true
  test-first, but better than trusting an unverified test.
- **Tests use pinned timestamps**: `datetime.now()` in tests undermines reproducibility.
  Time is an input — pin it like any other. Production code that needs the current time
  accepts an optional `now: datetime | None = None` parameter (defaulting to
  `datetime.now(timezone.utc)`) so tests can inject a fixed value.
- **Tests don't sleep a guessed duration — they wait for the real condition**: the same
  "time is an input" rule applies to async/background-task synchronization, not just
  wall-clock timestamps. `await asyncio.sleep(0.1); assert X` is both slow (every test
  pays the full guessed duration) and fragile (too short → flaky on a loaded machine).
  Fix: poll the actual predicate — `tests/helpers.py`'s `wait_until`/`wait_until_sync`
  return the instant the condition holds, with a generous `timeout` as a safety net for
  genuine failure only, not the expected wait. Prefer an even stronger fix where the
  output has a deterministic terminator: `on4kst_irc_bridge.py`'s IRC registration flow
  always ends in numeric 366, so tests `recv_until("366")` — zero guessing at all. Only
  genuine negative assertions ("nothing arrives") still need a real bounded sleep, since
  there's no true condition to poll for proving an absence. Adopting this cut the suite
  from ~29s to ~3.5s and exposed one real race the old slack had been hiding.
- **Verify against reality, not just against assertions.** Several classes of bug here
  are structurally invisible to unit tests: how ffmpeg filter branches *combine*, how a
  drawn frame actually looks, how a real radio's session state machine reacts. Render
  the clip and decode a frame; capture the packets; measure against the real recording.
  Every one of those has caught a bug that a green suite did not.
- **Concurrency is asyncio, not threads.** Everything concurrent here waits on I/O
  or on a timer; nothing waits on the CPU. A single event loop expresses all of it,
  and prompt_toolkit already runs one — `Application.run()` ends in `asyncio.run`,
  so the logger has an event loop whether or not we use it. Threads buy nothing
  against a socket and cost the one hazard a single-threaded loop cannot have: a
  deadlock. `on4kst_irc_bridge.py` is the worked example, 32 coroutines and no
  locks. When something must happen while something else waits, write a coroutine
  and a task. The escape hatch is a genuinely blocking library call with no async
  form — `asyncio.to_thread` at the boundary, holding no lock — and a thread that
  needs a lock is a design that needs rethinking instead. FINDINGS.md has the audit
  this rule came from, including the deadlock it found on the round's normal exit
  path.
- **No visual glitches**: the logger UI must look professional at all times. Transient
  incorrect states (e.g. a dup highlight flashing for one frame during a state transition)
  are bugs.

## Conventions

**Credentials** live in `~/.netrc`, never in the repo and never hardcoded:
`machine www.on4kst.info login ha5la password …` for the chat, and a
`machine <radio-ip>` entry for the radio's LAN login. The callsign is read from
there at startup (uppercased); the grid locator is fetched from the ON4KST server
via `/SHow CONFig` after login.

**File layout — global databases live in `~`, per-round files in the CWD.** The
whole stack runs on one laptop during a round, and a contest directory holds
exactly one round. `puskas_logger.py` and `contest_video.py` enforce it: both
refuse to start in the project root, since that is where a mistaken launch
lands and several rounds' files in one directory cannot be told apart
afterwards. `--hud-demo` and `--hud-theme-check` are exempt — they write one
PNG and exit, and iterating the HUD's layout from the root is how they are
meant to be used.

| Path | What |
|---|---|
| `~/.puskas/puskas-seen-stations.json` | harvested station database (all rounds, accumulates) |
| `~/.puskas/on4kst-seen-stations.json` | ON4KST session database (written by the bridge) |
| `.puskas_cache/` | API response cache (CWD; delete to force a fresh fetch) |
| `*.edi`, `*.jsonl`, `*.scope`, `*.cast`, `*.mp4` | one round's own files (CWD) |

**Running**: this is one `uv` project, not a set of standalone scripts.
Dependencies are declared once in `pyproject.toml` and locked in `uv.lock`;
there is still no virtualenv to activate. Every component runs either as
`uv run <script>.py` or directly by its `#!/usr/bin/env -S uv run` shebang —
`uv` finds the project by walking up from the current directory, so a script
launched from a round directory inside the project works, and one launched
from outside the project does not. PIPELINE.md has the order they are actually
used in.

The scripts carried their own PEP 723 headers until the same four packages
were *also* listed in `pyproject.toml` with a different version policy and
only that side locked — so the test suite could resolve differently from a
contest round, and nothing would say so.

## Testing

Enforced by `pre-commit`, not by a checklist here — one-time setup per clone:
`uv run pre-commit install`. What runs is defined in `.pre-commit-config.yaml`,
the only source of truth; CI runs the same config rather than a separately
maintained list of steps.

**Ruff policy**: both `ruff check` and `ruff format` run via pre-commit. Aligned
assignment style is not preserved — `ruff format` collapses it, and that is
accepted as worth avoiding the diff noise of realigning a whole block whenever
one name's length changes. E501 (line length) and E701 (single-line `if …: return`
in lookup functions) are suppressed for `ruff check`.

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
