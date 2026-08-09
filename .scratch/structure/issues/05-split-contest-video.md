# 05 — Split contest_video.py (3,995 lines)

Status: resolved

Blocked by: 03

Too big to hold in one head, independent of any duplication. Candidate seams,
in rough order of how cleanly they cut:

- CW decode (pitch detection, envelope, gating, trust gate)
- audio/timeline sync (offsets, anchors, drift fitting)
- the HUD (theme, recesses, needle, meters, ticker)
- the cast PiP renderer (pyte replay to frames)
- the scope waterfall
- ffmpeg filtergraph assembly

The constraint an edit must not break: how filter branches *combine* is not
covered by unit tests, so each extraction needs a real render checked against
a decoded frame, not just a green suite.

## Answer

3,932 lines to 874, in twelve modules. The imports already formed a DAG before
anything moved — measured with an AST scan of which section referenced which —
so the extraction order was the topological one, and no cycle had to be broken.

| Module | Lines | What it owns |
|---|---|---|
| `wav.py` | 87 | The recorder's WAV files: the title tag, reading a time range |
| `cw_decode.py` | 445 | The signal chain and the trust gate |
| `timeline.py` | 211 | `Segment`, `Qso`, the EDI read, wall clock ↔ audio time |
| `webcam_sync.py` | 353 | The one stream with no trustworthy clock |
| `rig_state.py` | 271 | Telemetry, input log, state events, QSO time matching |
| `qso_windows.py` | 164 | Where each QSO sits in the video |
| `chapters.py` | 72 | YouTube chapters and SRT captions |
| `cast_render.py` | 302 | The terminal PiP |
| `scope_render.py` | 157 | The waterfall background |
| `hud.py` | 522 | The HUD's data layer |
| `hud_draw.py` | 672 | The HUD's drawing layer |
| `video_format.py` | 13 | Frame size and rate |

Four findings the move itself produced, none of them mechanical:

- **`wav.py` was not on the list of seams** and had to exist. Two callers
  needed to read a range of samples out of a segment — the decoder, for a
  sub-range of a long segment, and the webcam drift fit — and putting it in
  either would have made the other import a private name from a module about
  something else. The IC-9700's title tag went with it, so the module is now
  the boundary to the recorder's format, the role `edi.py` plays for EDI.
- **A section headed "ASS generation" contained no ASS code.** The subtitle
  overlay it named had been replaced by the HUD; what was actually in there
  was QSO windows and the ticker, which went to two different places.
- **The webcam's knowledge was filed under two headings** — its start time was
  read in the rig-state section while the cross-correlation that corrects it
  sat 200 lines earlier. One subject, and now one module.
- **The HUD's data/drawing seam was described but not real.** Its banner
  comment claimed the split already existed; in fact the data layer reached
  forward into the font block for `HUD_MATRIX_COLS`, because the scroll clock
  is computed from the glyph width. The geometry moved back to the data layer
  and the glyph table stayed with the drawing, which is what let the two
  halves actually come apart.

The tests split the same way — 3,211 lines into twelve files, 565 tests before
and 565 after. What stayed in `test_contest_video.py` is what could not move:
how the ffmpeg filter branches combine.

Verified against reality, as the ticket required, on the real August round
(`26augusztus/`, both EDI logs, telemetry, input log, cast and webcam):

- a 3-minute 720p render with every branch on is **byte-identical** before and
  after the split — same md5, so the filtergraph combines exactly as it did
- the cast PiP and HUD intermediates are byte-identical too
- the full round's `chapters.txt` (56 QSOs) and `.srt` (56 cues) are identical
- `--hud-demo` renders a pixel-identical PNG (difference bbox `None`)

`ffmpeg` filtergraph assembly stayed in `contest_video.py` rather than becoming
a module of its own. It is what the file *is* now, together with `main`, and
splitting the compositor from the CLI that configures it would have separated
two halves of one decision.
