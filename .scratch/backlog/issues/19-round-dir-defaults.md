# 19 — Defaults discovered from the round directory

Status: ready-for-agent

Split out of **07**, which is about the render's progress and shares no code with
this: one adds `tqdm` to four loops, the other adds a pure function and an
argparse path.

A render is invoked today with seven paths spelled out (PIPELINE.md §3 has the
line). That is enough friction that the operator has never run
`contest_video.py` by hand. A round directory holds exactly one round and its
files follow one convention, so the arguments are derivable from the directory
itself.

## What is discoverable

| slot | glob | required |
|---|---|---|
| recdir | `recording/` | yes |
| edi | `*.edi` | yes, all of them — multiple bands is the merge case |
| `--telemetry` | `*-telemetry.jsonl` | no |
| `--input-log` | `*-input.jsonl` | no |
| `--cast` | `*.cast` | no |
| `--scope` | `*.scope` | no |
| `--webcam` | `*-webcam-*.mp4` | no, all matches in timestamp order |

**The webcam glob must not be `*.mp4`.** Outputs share the directory — `out.mp4`,
`contest_video.hud.mp4` and finished renders all sit beside the inputs.

## The shape

`discover_round_inputs(dir) -> RoundInputs`, pure, in `urhpk/wiring.py`, tested
in `tests/test_wiring.py`. The globs are already a cross-process contract —
`puskas_logger.py` writes the two `.jsonl`s and the `.scope`, `run-recorded-round.sh`
writes the `.cast`, `logbook.py` writes the `.edi`, `contest_video.py` reads all
of them — which is exactly what `wiring.py` says it is for. Keeping it there also
means the tests need nothing from `contest_video.py`'s argparse.

**Fires only when there are zero positional arguments.** Explicit flags still
override their own slot. Today's fully-explicit invocations are untouched, and
there is no half-discovered state to reason about.

**It refuses to guess.** A missing required slot is an error; an absent optional
slot is skipped, which is what no flag already means; an *ambiguous* optional slot
is an error listing the candidates. Picking the newer of two `.cast` files by
mtime would cost a three-hour render to find out.

**It announces what it found** — the resolved slots printed before the run, which
is what makes the refusal-to-guess trustworthy — and then proceeds. No prompt:
the unattended redirected runs must not block.

`-o` keeps defaulting to `contest_video.mp4`. Deriving `<edi-stem>.mp4` would
read better and would silently overwrite finished renders in `2026-aug/` and
`test/`.

`require_round_directory` is unchanged. Discovery's own "no `recording/` here"
error is the same condition from the other side, and that guard's docstring
refuses only the project root on purpose — widening it risks a false positive
that costs a round.

## Rejected: fixed filenames instead of globs

Dropping the date-and-callsign prefix (`260803-HA5LA-telemetry.jsonl` →
`telemetry.jsonl`) looks like it would remove the globs. It does not. EDI is
multi-file by design and webcam is multi-file with a timestamp that
`webcam_sync.py:50`'s `_WEBCAM_PRECISE_RE` *parses* to place the clip — both stay
globs whatever they are called, and a file named `webcam-<stamp>.mp4` stops
matching that regex. The remaining five turn `_one("*-telemetry.jsonl")` into
`Path("telemetry.jsonl")` against the same helper: no lines saved, against four
writer edits and migration of six existing round directories.

If the redundant prefix is still worth removing, it is a `puskas_logger` ticket —
a behavioural change to what the logger writes, with `.edi` exempt, since that is
the one file that leaves the directory for bb.mrasz.hu.
