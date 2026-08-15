# 18 — Differential render

Status: ready-for-agent

Blocked by: nothing in this tracker — by a condition. **Do not build this while
the render is being changed on purpose.** It answers "did this change move the
output", which is the right question for a refactor and the wrong one for a
feature.

Split out of **01**, which delivered everything else it asked for. 01's oracle
is the pair of text files, so the *video* is the half still unverified.

## The shape

`git worktree add` a reference ref, render the same fixture from both trees,
`md5sum`. No frame comparison, no tolerance, no golden images to maintain — the
old build is the oracle. Proven during the structure refactor: the same
3-minute cut of the August round rendered from a pre-refactor worktree and from
the working tree came out byte-identical, and `chapters.txt` and `.srt` matched
too.

Roughly 15 lines of shell. It is a script, not a pytest case: it takes minutes,
needs two worktrees, and its answer is only meaningful against a ref the caller
chooses.

## Measured cost

A full render of `test/` (169 s round, every side input) is **5m42s at 720p** on
four cores, ~14 min at 1080p. Doubled, since it renders twice.

## What it would also unlock

`tests/test_render_smoke.py` now passes both webcam clips and asserts where they
land, which turned out to cost ~1.5 s and need nothing from this ticket —
`sync_webcams` runs before `--no-video` returns. What it cannot reach is the
*drift fit*: `test/` is 169 s with no TX segment long enough to correlate
against, so the fixture only exercises the honest-no-match branch. Fitting a real
rate needs a long round, and checking that the fitted rate was applied to the
picture correctly needs a byte oracle — this ticket.

## Caveats to settle when it is built

**The bytes are pinned by the machine.** Same ffmpeg build, same core count.
That is fine for a local pre-round check and is why 01 concluded this cannot run
in CI.

**It cannot distinguish "moved" from "moved correctly".** A red result means
look, not revert.
