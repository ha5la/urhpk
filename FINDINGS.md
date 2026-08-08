# Findings — measurements, protocol archaeology and dead ends

Things that cost real time to discover and would cost real time to rediscover:
hardware measurements, protocol details reverse-engineered from packet captures,
and approaches that were tried and rejected *with evidence*.

ARCHITECTURE.md keeps the **rules** ("do not remove this error handling"); this
file keeps the **evidence** behind them. Narrative that only explains how the code
used to look is not here at all — git history keeps that.

## Hamlib

- **Async/event-driven rig or rotator state is not available for this hardware**,
  on the installed Hamlib (4.6.2) or the latest release (4.7.2). Hamlib's
  `async_data_supported` backend flag — what lets rigctld consume Icom CI-V
  Transceive frames without polling — is set for `ic7300.c`/`ic7610.c`/`ic785x.c`
  but *not* `ic9700.c`, checked directly against the Hamlib source at both version
  tags. The rotator API (`include/hamlib/rotator.h`) has no equivalent concept at
  all, for any backend, at either version — not a per-rig gap, the rotator
  subsystem never defined the hook. Hence: `puskas_logger.py` polls the rotator,
  and the rig went to `icom_net.py` instead.
- **Rotator model is 603 (GS-232B), not 601 (GS-232A).** 601 returned "Protocol
  error" on `get_pos` while genuinely receiving bytes back — wrong response
  framing, not a dead link or wrong baud.
- **The IC-7300MK2 has no dedicated backend in 4.6.2**; `RIG_MODEL_IC7300MK2`
  arrived in the 4.7 series (absent from `rigctl --list` here, present in 4.7.2's
  source and release notes). The plain IC-7300 model (`-m 3073`) was confirmed
  working against it for basic CAT regardless. Irrelevant to this project — Puskás
  Kupa is VHF/UHF only — noted only because it came up while investigating the
  IC-9700.
- **No custom udev rule is needed**; the distro's stock udev names both devices
  under `/dev/serial/by-id/`, verified on the actual hardware. If a future machine
  ever lacks an entry (a USB-serial chip too generic for udev's built-in rules),
  fall back to a `SYMLINK+=` rule matched on `idVendor`/`idProduct`.
- The IC-9700 exposes **two** CI-V USB-serial ports (`_A`/`_B`, genuinely distinct
  USB devices), likely so a second CAT-speaking program can run without contending
  with rigctld. Port A is the one that was in use.

## Icom network protocol (`icom_net.py`)

Transcribed byte-for-byte from wfview (`packettypes.h`, `icomudphandler.cpp`) and
kappanhang, cross-checked between the two — there is no public Icom spec. Verify
any future protocol change the same way: against source or a real capture, never
from memory or from prose descriptions, several of which turned out to disagree
with what the radio actually requires.

### Bugs that only real hardware could find

The protocol research was sound; none of these were visible from source reading or
from the in-process fake radio, which can only mirror back the assumptions baked
into it.

1. **Outer transport `seq` numbering.** The are-you-there(0)/are-you-ready(1)
   handshake is not part of the same counter as the tracked packets that follow:
   the first tracked packet also starts at seq=1, not 2. Sending login at seq=2 got
   a real retransmit-request back (type=0x01, "resend seq=1"); seq=1 got an
   immediate correct login response. The radio validates this strictly.
2. **`threading.Lock` self-deadlock.** `_apply_update` held `self._lock` while
   reading `self.band`, whose getter re-acquires it — wedging the single thread
   that processes all inbound CI-V data, on the first update, silently. Symptom:
   `freq_hz` updated exactly once then froze, `mode` never updated, no exception.
   Root-caused with a standalone script logging state every 100 ms after connect.
   Fixed with `RLock`; caught in CI afterwards by the fake-radio integration test.
3. **The control socket must be kept alive *during* the CI-V open handshake**, not
   only once everything is connected. The original ordering left it silent for
   several seconds while the CI-V open retried, and the radio dropped the
   just-registered conninfo session — while the session-independent
   are-you-there responder kept answering, which made it look like a CI-V problem.
4. **The CI-V "open" retry cadence must be slow.** wfview resends every 100 ms;
   doing that literally (a new tracked seq per retry, since this client doesn't
   implement retransmit-request compliance) made the radio's receive-sequence
   tracker lose sync entirely — it began requesting retransmits for a nonsensical
   `0xff82`–`0xffff` range that grew with every retry. Spacing retries at
   `CIV_STALE_S` fixed it.
5. **The client must speak first.** This client waited for CI-V data before sending
   anything past the open request; the radio never sends data unprompted just
   because the stream is open. Both sides waiting is a deadlock. A clean wfview
   capture showed its first real CI-V traffic is a *client-initiated* query
   (`FE FE 00 E1 19 00 FD`). Fixed by sending a real query alongside every open
   attempt.
6. **wfview sends no second token renewal between register and conninfo.** Captured
   outer `seq` is login=1, token(register)=2, conninfo=3, nothing in between. This
   client had an extra token request there; removed to match exactly, on the same
   theory as (1) — an extra tracked packet a real client never sends is exactly
   what could desync the radio's session state machine.
7. **A deregister is not a goodbye.** A real client also sends a **disconnect
   control packet (`type=0x05`, 16 bytes, `seq=0`) on every socket it opened** —
   captured from a wfview shutdown via WM_DELETE_WINDOW, so its destructors
   actually ran. Without it the radio kept the session on its books: measured, it
   pinged the dead sockets at ~50 packets/s for over a minute while refusing new
   sessions (wfview's own log says `Busy: 1`). With it, traffic stops within the
   same second and a fresh connect succeeds at +0.0 s.
- **Capture-driven debugging needs a cooldown.** Repeated rapid reconnects made
  failures shift to different, earlier steps across runs with no code change —
  session-slot exhaustion from attempts that were never cleanly logged out. A real
  fresh attempt needs on the order of tens of seconds of quiet.
- **The radio holds exactly one live session, and a second connect is not refused
  — it silently kills the first.** Verified with two concurrent sessions: the
  second came up fine while the first's CI-V stream died the moment the second
  started connecting, and did not recover even after the second closed.

### Dead end: there is no separate scope port

`ScopeLANPort` (default 50004) is a real, populated key in wfview's config and is
**irrelevant to Icom radios** — it is a vestige of wfview's *Yaesu* support
(`yaesuudpcontrol.cpp`/`yaesucommander.cpp` are its only consumers), which is
exactly what made it misleading. Confirmed two independent ways before writing any
code: a `dumpcap` capture of all UDP traffic during a live wfview session with a
visibly working spectrum showed zero packets on 50004 (reproduced independently by
the user in Wireshark, who also noted the radio's own `SET > Network` menu exposes
only three ports); and wfview's `conninfo_packet` has exactly two port fields,
`civport` and `audioport`, so the radio is structurally never told about a third
socket.

Scope sweeps are ordinary CI-V `0x27` frames on the CI-V socket already in use.

### Scope frame layout

Confirmed by decoding a real captured sweep byte-for-byte, which caught an ordering
mistake: after the `27 00 00` marker come `sequence`/`sequence_max` (single BCD
bytes), then for `sequence == 1`: `mode`(1) → `start_freq`(5-byte BCD) →
`end_freq`(5) → `out_of_range`(1) → pixels. Placing `out_of_range` *before* the two
frequency fields decoded a real frame to a nonsensical ~1.45 MHz centre; the
correct order decoded the same bytes to 145.11–146.11 MHz, matching what the radio
was showing.

In Centre mode the transmitted fields are centre-frequency and half-span, not
edges: `start_hz = raw_start - raw_end; end_hz = start_hz + 2 * raw_end`.

Pixels are raw `0`–`160` linear scope units (`SpectrumAmpMax`), not dBm — already a
ready-to-colormap waterfall row, 475 bins on the IC-9700, the whole sweep in one
datagram over LAN (`sequence == sequence_max == 1` always).

**Scope data output is not session-scoped**: a fresh session gets `27 00` frames
immediately, before enabling anything — the radio remembers `27 11 01` across
sessions. So a leftover session's flood is a real scope stream, not an artifact.

**`frame[4]` is a main/sub selector**, and `parse_scope_frame` asserts it is `0x00`,
so sub-band sweeps would be silently discarded. wfview sends every scope *settings*
command twice, once ending `00` and once `01`. Not a live problem (all 439 captured
waveform frames were main), just a limit to know about.

### Test fixture provenance

`tests/test_icom_net.py`'s `_REAL_SCOPE_SWEEP_HEX` is the literal bytes of one real
captured datagram, extracted with a ~30-line pure-stdlib pcap parser rather than
transcribed by hand — a mis-typed nibble did slip into an early draft of exactly
this fixture and was only caught by re-deriving the hex programmatically and
diffing.

`dumpcap` needs no sudo here (the user is in the `wireshark` group and it carries
`cap_net_raw`/`cap_net_admin`), unlike plain `tcpdump`.

## Meter calibration

Raw values are 0–255, sent as 4-digit BCD across two bytes. **Store raw values and
convert at render time** — that is the consequence of the Id story below: a better
calibration then costs one line rather than a ruined recording.

| Meter | RX | TX | Verdict |
|---|---|---|---|
| `15 02` S | 0–6 quiet, 150 on a strong signal | not polled | S9 = raw 120 |
| `15 11` Po | not polled | 213–214 | 100% — matches the 0/143/213 curve |
| `15 12` SWR | 0 | 27–29 | ≈1.3 via 0/48/80/120 → 1.0/1.5/2.0/3.0 |
| `15 15` Vd | **152** | — | **13.66 V** vs 13.78 V on a multimeter (0.9%) |
| `15 16` Id | 0 | 169–172 | the IC-7300 curve does **not** transfer |

- **Vd reads on receive**, which is the case that matters for portable battery ops:
  watching a pack sag across a round beats a momentary key-down dip. Id is PA drain
  only and reads a literal 0 on RX.
- **Id is a straight line through the origin at 0.0741 A per raw unit** (~17.9 A
  full scale, against Icom's 25 A). Measured with a multimeter in series, PA drain =
  total less the measured 1.18 A receive baseline. The low cluster (raw 55–64,
  3.9–4.7 A) alone gives 0.0726 and adding a 100%-power anchor at raw 171 gives
  0.0741 — two nearly independent estimates a factor of three apart in current,
  agreeing to 2%, which is what makes "linear through zero" believable rather than
  merely fitted. Residuals within ±5.3%, worst at the lowest point. The low cluster
  cannot determine a slope alone: it spans nine raw units, so a free (non-origin)
  fit through it returns a physically absurd −1.5 A intercept purely from the short
  lever arm.
- **A cheap multimeter's burden voltage is enough to brown the radio out, and the
  radio itself measures it.** With the meter in series, the session's own `vd`
  readings fell to raw 22–41 — 10.2–10.7 V at the radio against 13.7 V at the
  supply, i.e. ~0.55 Ω of shunt plus leads — and at 25% power the radio hit
  undervoltage and switched off, twice, reproducibly. The current readings stay
  valid for calibration regardless: a (raw, amps) pair describes one instant however
  degraded the supply was at that instant, which is why a run that looked like a
  failure produced the fit above. What it does cost is the assumption that the
  receive baseline stays constant while being subtracted — part of why the lowest
  point fits worst.
- **Vd's own curve is confirmed by the same session.** Across 10–100% power the gap
  between supply voltage and converted Vd grows 0.37 → 0.81 V, and that gap is
  *linear in current*: 54 mΩ of series resistance with a 0.10 V intercept. A wrong
  curve would have shown curvature or a nonsense intercept. The 54 mΩ is worth
  knowing on its own — 0.69 V lost between supply and PA at full power, which for
  portable battery operation is the difference between holding up and browning out
  on key-down. The bench supply itself barely sags (13.78 → 13.70 V), so that loss
  is cable, connector and internal.

## What the radio pushes vs what must be polled

From three `dumpcap` captures of a live wfview session — capturing a known-good
client answers "how does a working implementation do this" without taking the
radio's single session slot.

- **Only two things are ever pushed unsolicited**: freq/mode Transceive frames and
  scope sweeps (`27 00`, ~29/s). Everything else is polled — wfview queries the
  S-meter (`15 02`) at **~19 Hz**, plus PTT (`1c 00`), OVF (`15 07`), VFO
  freq/mode (`25`/`26`) and assorted settings at 1–3 Hz. A live S-meter needs a
  poller; there is no push option to find. Affordability is settled by the same
  capture: the radio sustains ~30 queries/s alongside 29 sweeps/s and an audio
  stream without trouble.
- **Every outbound frame appears twice in a capture** — the radio echoes the
  client's own frames back on the CI-V stream. Every query/reply pair came out at
  precisely 2:1, which is what confirmed an echo rather than retransmission.
  Distinguish direction by the to/from address bytes (`E0 A2` = radio→controller).
- **The S-meter slot switches to Po on transmit**, in the protocol and not just on
  the display: `15 02` stops being queried during TX and `15 11` starts. A consumer
  therefore needs PTT to label the reading.
- **`25 00`/`25 01` is VFO A/B of the selected receiver, not main/sub.** A capture
  showed 144.800 FM and 144.1735 USB simultaneously, which looks like dual receive
  but isn't — confirmed on the radio, where A and B move together on a band change.
  Reading the *other band's* receiver (the real dual-watch) needs a command we
  haven't identified; `07 D0`/`07 D1` switches the selection but is intrusive for a
  passive monitor. wfview has a sub-band display, so one capture with dual watch
  enabled would reveal it.

## contest_video.py

### The webcam drift diagnosis

Only relevant to an **independently recorded** clip (a phone). The logger's own
Alt+V capture shares the machine's clock and needs none of this.

Two separate problems, found in sequence:

1. **Variable frame timing under a constant-rate label.** A phone clip claimed
   `r_frame_rate` 30/1 while its per-frame timestamps were genuinely variable —
   confirmed by reading every packet's own `pts_time`: not one pause but 3,444
   scattered micro frame-drops across ~2 h (thermal/buffer pressure), summing to
   exactly 0.753 s that the raw frame count doesn't account for (218,052 frames at a
   flat 30 fps spans 7268.4 s; the container's PTS-accurate duration is 7269.12 s).
   Without an explicit `fps=` filter the branch was laid out by frame count rather
   than by true timestamps, so it ran slightly fast the whole way through.
2. **Two independent crystals don't tick at the same rate.** Reported as "video
   ahead of audio by 1:48 into a 2 h session", confirmed by ear and by uploading to
   YouTube. The whole-hour offset correction passed sub-hour *rate* skew straight
   through, invisible to every test that only checked "does the render apply
   whatever `webcam_start` it was given" rather than "is `webcam_start` correct".
   Diagnosed by ear, then measured: the operator's voice reaches both microphones at
   the same real instant, so extracting speech onset from both and comparing against
   the assumed start showed a *growing* gap — ~0 s near the start, ~+3.2 s near the
   end. A linear drift, which no single offset can correct.

`refine_webcam_start` fits both: anchors sampled evenly across the *whole* session
(an earlier version took the first few, clustered them in the opening minutes and
got a near-meaningless rate), cross-correlated via an RMS envelope rather than raw
samples (a coarse amplitude-rhythm signature survives two very different
microphones capturing the same speech), keeping only anchors above `min_confidence`
0.3 — real data showed a clean gap between spurious matches at 0.08–0.29 and
genuine ones at 0.34–0.77. Verified: at the exact reported drift point the
coarse-only mapping was off by 2.73 s, the rate-corrected one by 0.07 s.

**Rejected: tagging the true start into the mp4's container metadata after
capture.** It works (tested on a real ~2 h/3 GB file: a 15 s stream-copy remux) but
needs a full second copy of the file on disk at exactly the point in a session when
free space is tightest. The rename that replaced it took 0.006 s on that same file —
a directory-entry update, independent of size.

### Terminal PiP (pyte + tmux)

- **Stock pyte silently drops three CSI sequences tmux needs.** tmux clears or
  scrolls a *single pane* by setting left/right margins (DECSLRM, `CSI Pl;Pr s`) and
  scrolling within them (SU `CSI Ps S` / SD `CSI Ps T`). pyte implements none of the
  three (no `S`/`T`/`s` in its CSI dispatch table; `?69h`/DECLRMM ignored), so a
  pane was never actually cleared and stale text stayed behind newer, shorter
  content. `_CastScreen`/`_CastStream` implement them, honouring both the top/bottom
  margins and the new left/right ones so only the pane's own columns shift.
  `set_left_right_margins` distinguishes DECSLRM (2 params) from a bare `CSI s` =
  SCOSC save-cursor (<2 params).
  **This corrected an earlier, wrong diagnosis** that called the same garbage a
  source artifact ("the logger omits erase-to-end-of-line; any correct terminal
  would show it too"). Wrong on both counts: `asciinema play` always showed the cast
  clean, and the erase is tmux's, not the logger's. A cast recorded *outside* tmux
  never emits these sequences.
- **Line height must come from the font's own metrics.** `int(size * 1.2)` — a
  common monospace approximation — undershot DejaVu Sans Mono 13pt's real
  `ascent + descent` by 2px, enough that descenders were clipped by the next row's
  own background-clearing rectangle. Root-caused by comparing the pre-encode canvas
  against the same frame decoded back out of the mp4 (ruling out compression), then
  checking `font.getmetrics()`. Verified by pixel-diffing the same frame: 39
  differing pixels before, 0 after.
- **Redraw only the rows pyte marks dirty.** Redrawing every row every frame took
  123.8 s for a 76.9 s clip (0.62× realtime — hours for a full session); redrawing
  only `screen.dirty` onto a persistent canvas cut it to 25.6 s (~3× realtime), a 5×
  speedup measured on the same input.

### Measurements worth not repeating

- **`ffprobe` per file is 6500× slower than reading the RIFF chunk directly** for
  WAV metadata: 707 files took ~112 s via `ffprobe` against ~0.02 s reading
  `LIST/INFO/INAM` headers. Process-spawn cost dominates at this file count even
  though the work itself is trivial.
- **The WAV/telemetry frequency disagreement was our own rounding.** A systematic
  160/250/300/310 Hz gap (by band) appeared on nearly every segment's first
  telemetry sample, which without a tolerance looked like a spurious retune at the
  start of almost every segment; genuine retunes in the same data are ≥1000 Hz, a
  clean gap with zero occurrences between 310 Hz and 1000 Hz, hence
  `FREQ_MATCH_TOLERANCE_HZ = 500`. The cause was later identified: the old 1 Hz
  sampler built `freq_hz` by re-parsing the logger's own toolbar string
  (`f"{freq_hz / 1e6:.3f}"`), quantising to the nearest kHz — 144299840 →
  `"144.300"` → 144300000, exactly the reported 160 Hz. New recordings don't have
  it; the tolerance stays for the old ones.
- **Telemetry was overwhelmingly duplicate before it became change-only**: on the
  real July round only **616 of 9313** lines carried anything new.
- **Rejected: making every real-over segment a snap candidate** for QSO timing,
  instead of one per coalesced burst. Tempting for pinning down which segment inside
  a burst a voice QSO started on, but it regressed the CW round's independently
  verified precision — QSO 2's panel moved from the verified-correct 520.03 s to a
  wrong 579.14 s, because a single QSO's exchange spans several segments and "latest
  candidate at or before the logged time" then lands *inside* that same exchange
  rather than at its start.
- **Unsolved: the CQ-calling case.** `_tx_start` finds a burst's real start from the
  fact that RX and TX strictly alternate and TX is the shorter phase. That breaks
  down during a stretch of many brief CQ calls with short listening gaps: there is
  no single "real" start, and an earlier fruitless call looks identical to the one
  that finally got answered. Falls back to the burst's first segment.
