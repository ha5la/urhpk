# Puskás URH Kupa – project context

A toolset for one amateur radio contest (Puskás URH Kupa), plus a general-purpose
ON4KST↔IRC bridge. One operator, one laptop, one radio: everything runs on the
same machine during a round, and the whole point of the project is that the round
itself is captured well enough to be reconstructed afterwards as a video.

**Read PIPELINE.md first** — it is the story the components serve, and most
questions about "why does this exist" are answered by where a thing sits in it.

## The documents

| File | What belongs in it |
|---|---|
| **CLAUDE.md** (this file) | How to work here: development principles, conventions, house rules |
| **PIPELINE.md** | The end-to-end story: harvest → run the round → render → publish |
| **ARCHITECTURE.md** | Component by component: what each does and the constraints an edit must not break |
| **RECORDING.md** | Practical how-to for recording a round and producing a video, with real numbers |
| **FINDINGS.md** | Measurements, protocol archaeology and dead ends — the evidence behind the rules |
| **README.md** | The public face: components table, quick starts |
| **hud-artwork-prompt.md** | Generation prompt and layout spec for the HUD artwork |

## Housekeeping

- When adding or removing a component, update **README.md**'s components table
  and **PIPELINE.md** if it changes the story.
- **Keep the documents in their lanes.** Component detail goes to
  ARCHITECTURE.md, research narrative and rejected approaches to FINDINGS.md, and
  history that only explains how the code *used to* look goes nowhere — git keeps
  it. CLAUDE.md was 2157 lines once, for exactly those reasons.

## Development principles

- **Succinct code comments**: prefer explaining identifiers over comments (Robert C.
  Martin) — a well-named variable/function usually makes a comment unnecessary. When
  the *why* genuinely needs explaining (a hidden constraint, a non-obvious tradeoff, a
  bug's root cause), write a succinct comment, not an essay.
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
- **No visual glitches**: the logger UI must look professional at all times. Transient
  incorrect states (e.g. a dup highlight flashing for one frame during a state transition)
  are bugs.

## Conventions

**Credentials** live in `~/.netrc`, never in the repo and never hardcoded:
`machine www.on4kst.info login ha5la password …` for the chat, and a
`machine <radio-ip>` entry for the radio's LAN login. The callsign is read from
there at startup (uppercased); the grid locator is fetched from the ON4KST server
via `/SHow CONFig` after login.

**File layout — global databases live in `~`, per-session files in the CWD.** The
whole stack runs on one laptop during a round, and a contest directory holds
exactly one round:

| Path | What |
|---|---|
| `~/.puskas/puskas-seen-stations.json` | harvested station database (all rounds, accumulates) |
| `~/.puskas/on4kst-seen-stations.json` | ON4KST session database (written by the bridge) |
| `.puskas_cache/` | API response cache (CWD; delete to force a fresh fetch) |
| `*.edi`, `*.jsonl`, `*.scope`, `*.cast`, `*.mp4` | one round's own files (CWD) |

**Running**: every component is a `uv run` script with its dependencies declared
in its own header — there is no shared requirements file and no virtualenv to
activate. PIPELINE.md has the order they are actually used in; each also runs
standalone outside a contest round.

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

`.gitignore` excludes generated files (`puskas_map.html`, `puskas_polar.png`) and
scratch files (`*.json`, `*.url`, `*.txt`).
