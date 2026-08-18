# Findings — measurements, protocol archaeology and dead ends

Things that cost real time to discover and would cost real time to rediscover:
hardware measurements, protocol details reverse-engineered from packet captures,
and approaches that were tried and rejected *with evidence*.

ARCHITECTURE.md keeps the **rules** ("do not remove this error handling"); this
file keeps the **evidence** behind them. Narrative that only explains how the code
used to look is not here at all — git history keeps that.

## Scoring: a QSO is worth its kilometres rounded up

The organiser's evaluator scores `int(km) + 1`, where km is the great-circle
distance between the two locators' centres on a 6371 km sphere — the same
haversine `geo.py` computes. Measured against the server's own per-QSO `points`
field over every round cached at the time: 8290 of 8290 QSOs it scored agree, and
the 79 that disagree are ones it zeroed outright (INVALID, X-QSO), not distances
it computed differently. A QSO inside one's own square is 0 km and pays 1 point.

The logger wrote `int(km)`, which lost exactly one point per QSO: August 2026 was
claimed as 2318 (34 QSOs) + 915 (22 QSOs) where the server said 2352 + 937.
`tests/fixtures/mrasz-scored-qsos.json` is the evidence, 499 QSOs over 28 rounds
with each round's total as the server published it.

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
visibly working spectrum scope showed zero packets on 50004 (reproduced independently by
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
  radio itself measures it.** With the meter in series, the round's own `vd`
  readings fell to raw 22–41 — 10.2–10.7 V at the radio against 13.7 V at the
  supply, i.e. ~0.55 Ω of shunt plus leads — and at 25% power the radio hit
  undervoltage and switched off, twice, reproducibly. The current readings stay
  valid for calibration regardless: a (raw, amps) pair describes one instant however
  degraded the supply was at that instant, which is why a run that looked like a
  failure produced the fit above. What it does cost is the assumption that the
  receive baseline stays constant while being subtracted — part of why the lowest
  point fits worst.
- **Vd's own curve is confirmed by the same round.** Across 10–100% power the gap
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

### Dead end: the radio cannot say whether it is recording

The Voice Recorder is started by hand on the front panel, and forgetting to
press it costs the round's audio — the one thing the pipeline cannot
reconstruct. So: can the logger notice and warn?

No. The IC-9700's CI-V reference guide command table (checked in full, not
sampled) exposes the recorder only as **settings** — `1A 05 0242` TX REC Audio,
`0243` RX REC Condition, `0244` File Split, `0245` REC Operation, `0246` PTT
Auto REC, `0247` PRE-REC, `0248` Player Skip Time. There is no start command,
no stop command, and no readable in-progress status anywhere in the table.
Nothing adjacent substitutes either: `1C 00` is RX/TX only, `1A 0A` is the OVF
indicator, `1A 0B` is picture TX. The SD card itself is not reachable over the
LAN session at all.

Hence Alt+S: an acknowledgement the operator gives, not a measurement. It is a
checklist item with a red block attached, and that is the ceiling of what is
possible here.

What the settings *do* buy is worth having, because two of them silently ruin
the recording and neither is visible until render time, days later:

- **`File Split` (0244, default ON)** — ON is what makes the radio cut a new WAV
  on every RX/TX switch. That boundary is the entire basis of `qso_windows.py`'s
  QSO timing; with it OFF the round arrives as one file split only at 2 GB.
- **`RX REC Condition` (0243, default `Squelch Auto`)** — the default is the
  wrong one. `Squelch Auto` records only while the squelch is open, so every
  quiet stretch is missing, and (with File Split ON) it also cuts a segment on
  each squelch transition — so RX and TX no longer strictly alternate, which
  `qso_windows.py` and RECORDING.md both assume.

A wrong `RX REC Condition` is therefore one factory reset away at any time, and
costs a round's audio structure without any symptom during the round.

## The recorded audio

The question that opened this: the radio can stream audio over the LAN
(`conninfo`'s `rxenable` at 0x70, which `icom_net.py` sets to 0), so the laptop
could capture the round itself. That would put the audio on the laptop's clock
and survive a forgotten REC press — at the cost of holes wherever the logger
isn't running. Measuring what the SD card actually does answered most of it.

### Dead end: the audio stream carries no PTT

The audio packet is a 24-byte header — `len`/`type`/`seq`/`sentid`/`rcvdid` at
0x00–0x0f, then `ident`/`sendseq`/`unused`/`datalen` at 0x10–0x17 — followed by
raw PCM (wfview `packettypes.h`, `audio_packet`). No rig state of any kind. The
only PTT inference a LAN capture allows is *level*: the receiver is muted during
one's own over, which is indistinguishable from a quiet band. So the RX/TX
structure the pipeline is built on can only come from the SD card's file splits,
whatever else the audio is captured for.

The same header does carry a per-packet `seq`, so a dropped datagram is provable
and exactly sized rather than inferred from arrival times. `conninfo` also has
`rxcodec`/`rxsample` at 0x72/0x74: the codec is the client's choice, not the
radio's.

### Dual watch: the sub band is a second set of WAVs

Verified 2026-08-17 with a deliberate test recording (`dualwatchtest/`), dual
watch on, several TX periods, main band swapped mid-recording:

- **The sub band records to its own files**, `…B.wav` beside `…A.wav`, both
  starting at the instant REC was pressed. Every WAV of every round before this
  one is an `A` file (1,793 of them across four rounds).
- **The B file never splits on its own.** The sub band never transmits, so File
  Split has no RX/TX transition to cut on: 9 A files against 1 continuous B file
  over the same 57 s. It ends only when the sub band is switched off, and a
  fresh B file begins when it is switched back on — confirmed deliberately in
  the harmonic recording below, where the toggle left one 783.7 s B file, an
  8.3 s hole, and a second B file. So B covers the sub band's *on* periods and
  nothing else, and anything using it as a reference has to handle a partial
  round.
- Both sets are **16 kHz mono, 16-bit**.

So `contest_video.py` globbing all `*.wav` would interleave two incompatible
series and break the strict RX/TX alternation `qso_windows.py` assumes.

### Dead end: the title tag's second slot is not the sub band

Every IC-9700 title tag carries a second, dashed-out frequency/mode slot:

    IC-9700 Voice Recorder Data   144.489.10 CW     ----.---.-- ------ -- RX 2026-08-03 15:59:13

It stays empty on a B file recorded mid-dual-watch, so it is not the sub band
(satellite/duplex or the other VFO, unidentified). Band identity comes from the
`A`/`B` filename suffix; `_WAV_TITLE_RE` skipping the slot loses nothing.

### The recorded passband stops at 4 kHz

Measured on the test recording's two FM files — squelch noise is broadband, which
makes it the right probe for a passband edge. Welch spectrum, 4096-point, dB
relative to peak:

| | 2 kHz | 3 kHz | 3.5 kHz | 4 kHz | 6 kHz | 7.9 kHz |
|---|---|---|---|---|---|---|
| B (438.525 FM) | −20.8 | −41.1 | −56.7 | −84.0 | −99.8 | −100.5 |
| A (144.800 FM) | −18.4 | −33.2 | −47.3 | −82.4 | −99.1 | −98.7 |

A brick wall at 3.5–4 kHz, flat at the 16-bit quantisation floor above it. The
SD card's 16 kHz already oversamples the content 2×, so a LAN capture at 48 kHz
carries six times the samples and no more information. Confirmed rather than
argued: a 30 s LPCM 48 kHz capture off the LAN (`lan_audio_probe.py`) reproduces
the same table to within ~1 dB — −17.9 at 2 kHz, −33.4 at 3 kHz, −46.4 at
3.5 kHz, −81.8 at 4 kHz — and sits flat at −104 dB from 6 kHz all the way to
20 kHz. Both paths tap the same bandlimited AF stage, so **16 kHz mono is the
right rate for a LAN capture** and 48 kHz is six times the disk for the
quantisation floor.

### The LAN audio clock does not drift against the laptop

Ten minutes at 16 kHz, 30,000 datagrams, **not one gap in the sequence** — which
is what "retransmit-request compliance is skippable on a clean LAN" looks like
measured rather than assumed.

Fitting cumulative sample count against `CLOCK_BOOTTIME` arrival over all 30,000
gives **15999.99911 Hz, −0.056 ±0.035 ppm** — 0.4 ms over a two-hour round.
Arrival jitter is 1.04 ms rms, 8.5 ms worst case. For scale, the webcam drift
this file documents elsewhere is ~3.2 s over 2 h, several hundred ppm: the
radio's audio stream is four orders of magnitude steadier, so a LAN capture does
*not* inherit the two-crystals problem that `refine_webcam_start` exists to fit.

Wall clock and `CLOCK_BOOTTIME` diverged by at most 0.05 ms across the same run,
so nothing stepped and chrony's slewing is below the noise. Recording both is
still what makes that statement possible rather than assumed.

**And the samples are continuous too.** The rate above measures packet *pacing*:
datagrams per second times samples per datagram, which a radio slipping the odd
sample while pacing off a network timer would satisfy exactly, with no gap in
the sequence numbers either. Settled by correlating the content rather than by
capturing longer — five minutes of LAN capture against the SD card's own
recording of the same minutes (`--continuity`), both 16 kHz LPCM off the same AF
stage:

    coarse alignment: -10.073s, correlation 0.996
    34 of 34 windows matched above 0.5
      lag 0..0 samples, spread 0
      correlation 0.977..0.997

Constant lag over 4.8 million samples — **not one gained or lost**. The detector
is shown finding a single planted 62.5 µs slip in `tests/test_lan_audio_probe.py`,
because a run of "CONTINUOUS" from a detector never seen to fire says nothing.

**This correlation is itself the offset measurement** the LAN capture was wanted
for: it places the SD card's radio-clock timeline against a laptop-clocked
capture to the sample, where filenames quantise to a whole second.

### The send buffer is ~10 ms, not the 100-200 ms wfview reports

That 100-200 ms is wfview's own client-side playout buffer (`rxSetup.latency`),
not the radio's. This client has none. Two measurements:

**Packet cadence, from any capture.** Every datagram carries 320 samples — 20.0
ms of audio at 16 kHz — and they arrive every 20.0000 ms, with 99.1% landing
within 5 ms of the earliest and a worst case of 10.7 ms. The radio streams in
real time and queues nothing; only a *constant* delay could hide here, since it
would move every packet equally.

**Timing a CI-V mode change against the audio** (`--calibrate`, which transmits
nothing). The radio echoes our own frames back, so the echo dates the command to
within the return leg — measured round trip 2.8 ms median, 1.0 ms best, so ~1.4
ms each way. The demodulator's noise character changes at that instant and
appears in the audio however long the pipeline is:

| | median | range |
|---|---|---|
| → USB | −12.8 ms | −14.8 … −11.5 |
| → FM | +5.4 ms | +4.3 … +7.1 |

Each cluster is tight to ±1.5 ms, and the two differ by **18.2 ms** — the FM and
SSB chains have different group delay. **The absolute delay is not resolved by
this**, though: a pure delay cannot be negative, and the echo is only ~1.4 ms
behind the radio receiving the frame. Switching demodulator evidently changes
audio the radio had computed but not yet sent, so the edge does not mark a fixed
point in the pipeline. It bounds the delay at order 10 ms and no better. An
absolute figure needs an event arriving through the *antenna* at an instant the
laptop already knows, which this station cannot produce without transmitting.

**For joining the SD card to the laptop's clock, most of it cancels anyway**: the
SD card records the same demodulated AF that the LAN stream carries, so the
demodulator's share is common to both and only packetisation plus network
separates them — tens of ms against a filename quantised to the whole second.

**Do not measure audio on 144.800.** It is APRS: stations key up at random, and a
burst is a far larger step in any level detector than the thing being measured.
The first calibration runs there produced one 300 ms outlier per direction and
nothing else went wrong; on a quiet frequency all 24 transitions came back
clean.

**The estimator matters more than the capture length here.** The first,
30-second capture appeared to show +300 ppm, from `len(samples)/span`: it counts
the last datagram's samples against a span that ends when that datagram
*arrived*, and rests the entire answer on two jittery endpoints. The least
squares fit reads −11 ±15 ppm on that same file.

### The B file as a ruler for the A timeline

A's segment boundaries are quantised to a whole second by the filename, and
FINDINGS' clock section calls that the pipeline's floor. A continuous B file
spans the same interval with no gaps at all, so it can in principle measure the
dead time between A segments that no filename resolves.

The total is not the statistic. `len(B) − Σdur(A)` came out at **−0.067 s** on
the test recording — negative, i.e. physically impossible for a sum of gaps,
because B's start and end are themselves pinned only to ±1 s and that slop
swamps the signal. The slop is constant across a recording while per-gap jitter
accumulates, which is what separates them:

1. Fit one constant gap `g = (len(B) − Σdur(A)) / n_gaps`.
2. Predict each A file's start, `p_i = Σ_{j<i}(dur_j + g)`.
3. Residual against the filename, `r_i = p_i − (filename_i − filename_0)`.
4. **`max(r) − min(r) < 1.0 s` ⇒ consistent** — one `g` plus one unknown offset
   explains every filename and the entire disagreement is quantisation. Wider,
   and no single `g` fits: the split timing is genuinely variable and the ruler
   bounds the error rather than removing it.

The 57 s test gives `g = −0.008 s` and residuals spanning **1.017 s** over 8
gaps — fractionally over the line, inconclusive. All of which was superseded
before it was needed: the filenames turned out not to be the only way to place an
A segment inside B.

### The sub band hears the main band's harmonic, and that is the ruler

Recorded 2026-08-17 (`dualwatch-harmonic/`): main 144.900 FM, sub 434.700 FM —
an exact 3× harmonic — 53 A files with 26 TX periods over 13 minutes.

The harmonic is received plainly, and the signature is **not** a level dip. It is
**FM capture**: the transmitter takes over the sub receiver, the squelch hiss
disappears and what remains is the operator's own modulation. B's 50 ms envelope
across one 16.75 s over:

    35.25  -13.5 dB   hiss, the steady ~-15 dB baseline
    35.50  -42.9 dB   carrier captures, hiss gone
    36.00   -7.8 dB   speech
    39.00  -50.5 dB   pause between words
    52.00  -53.3 dB   still keyed
    52.25  -16.6 dB   hiss returns

So a level threshold fragments on every syllable. The detector that works keys on
the hiss itself — energy in 2800–3900 Hz, 1 ms hop, thresholded 8 dB below its
own median, majority-smoothed over 201 ms, runs merged across gaps under 1 s.
Transitions land inside a single 50 ms frame.

**The measurement.** 22 of 26 TX periods matched, 19 clean after dropping
mismatches. Regressing B's exact onsets against A's concatenated durations, with
one unknown constant offset:

| model | residual rms | spread |
|---|---|---|
| per segment boundary | **5.4 ms** | **19.6 ms** |
| per elapsed second | 37.5 ms | 113.9 ms |

- **Concatenating A's durations runs ahead of real time by 5.77 ms per segment
  boundary** — a fixed cost per split, not a clock-rate difference, which is what
  the 7× gap between the two models says. A round with a few hundred splits
  accumulates seconds of it.
- **Corrected, the scatter is 5.4 ms rms and 19.6 ms spread** — roughly two
  orders of magnitude below the ±0.5 s the section above calls the floor. That
  floor applies to *filenames*; it is not a property of the recording.
- Detected durations sit a steady −33 to −42 ms against A's own. That is the
  detector's edge bias, constant, and does not enter the result above.

The recording also answers what a mid-round sub-band toggle does, since one was
done deliberately near the end: B closes on switch-off and a new B file opens on
switch-on, leaving an explicit hole rather than a stretched or padded file.

### Every recorded file measures ~5.6 ms long, and it accumulates

The per-boundary figure above needs no harmonic to confirm. Comparing a round's
summed segment durations against the span its own filenames cover measures the
same thing directly, and on the two full rounds — where ±1 s of endpoint
quantisation spread over 700+ boundaries is worth ~1.3 ms — it agrees:

| | segments | excess | per boundary |
|---|---|---|---|
| 2026-aug | 759 | +4.36 s | +5.75 ms |
| 2026-jul | 707 | +3.79 s | +5.37 ms |
| harmonic cross-correlation | 20 TX | — | +5.55 ms |
| harmonic hiss-edge detector | 19 TX | — | +5.77 ms |

Pooled over the two rounds' 1,464 boundaries: **5.57 ms**. The short recordings
scatter (16.5 ms at n=56) purely because the endpoint quantisation divides by a
small number — not evidence of a different value.

**It is not duplicated audio.** Cross-correlating the head of each file against
the tail of its predecessor finds no match at any of 52 boundaries, so the file
does not re-record the moment before it. Each file simply reports more time than
it occupied; the mechanism is unidentified. `1A 05 0247` PRE-REC is the obvious
suspect from the settings table but the missing overlap argues against it.

**Left uncorrected it accumulates**, since it is per file rather than per second.
`audio_time_for` re-anchors on each segment's own filename, so point events —
QSOs, telemetry — never feel it. Anything *continuous* laid against the assembled
audio does: the cast, the scope and the webcam all play at real rate over a
timeline running 4 s long by the end of a round. That is larger than the ~3.2 s
this file attributes to two crystals in the webcam drift section, and was inside
every measurement that produced that figure.

`compensate_split_excess` (`urhpk/timeline.py`) trims `SPLIT_EXCESS_S` from every
segment but the last, via `eff_dur`. Applied to the two rounds it leaves a
residual of ±0.14 s — at the floor of what the filenames can resolve.

**The cut has to be made sample by sample.** Handing it to ffmpeg's concat
demuxer as an `outpoint` per file does not work: that trims on whole demuxed
packets — 1024 samples, 64 ms at the recorder's 16 kHz. Asking it for
5.57 ms removed 0.21 s of the intended 4.22 s across the August round, and 4 ms
of 111 ms across a 20-segment slice, unchanged by re-encoding instead of
`-c copy`. The timeline meanwhile assumed the full 4.22 s had gone, so
everything drawn on it — the RX/TX badge most visibly — ran early by a margin
growing to 4 s. `concat_audio` writes the WAV itself now: 759 segments in 1.1 s,
and the assembled round lands 5.7 ms from its timeline, that being one sample of
rounding per boundary.

Caveats: one session, 19 points, and an unknown share of the 5.4 ms is the
detector rather than the radio. The technique also needs the sub band parked on
an exact harmonic of the main band with squelch open and `RX REC Condition` on
`Always` — a calibration setup, not something a round would be operated in.

**Unrelated to any of this**: an earlier observation of the same station's speech
on 144.860 and 435.650 at once has no harmonic relation (3 × 144.860 = 434.580)
and the higher frequency was the cleaner of the two. Identical audio, different
quality, unrelated frequencies is a cross-band linked repeater pair, not anything
happening inside the radio.

## The radio's clock

Every timestamp in the pipeline is joined on the assumption that the radio's
clock and the laptop's agree: the WAV filenames carry the radio's clock, the EDI
carries the laptop's. The radio was believed to be the weak half, drifting, with
a manual CI-V clock-set as its unreliable patch. Measurement says otherwise on
both counts, and the patch is gone.

**Seeing the radio's seconds at all.** The radio reports only HH:MM — `get_clock`
and CI-V `1A 05 0180` alike always read `:00` seconds — so the seconds could only
be read off the radio's own menu by eye. They can be measured instead: poll
`1A 05 0180` at 20 Hz and record the laptop time at which the reported minute
*increments*. That instant is the radio's `:00`, located to a few tens of ms, and
it is what every claim below rests on.

**A time-set zeroes the radio's seconds — it is not ignored, ever.** The
documented quirk was that the radio ignores the seconds field, and the folklore
was that a set "silently doesn't take" when the clock is only 2-3 s off. What a
set actually does is zero the seconds counter at the moment the frame lands.
Sending `16:14` — the very minute the radio already displayed — at laptop `:30`
moved its rollover from `:00` to `:30.016`. So a set fired on a `:00` boundary
is accurate to the frame's flight time, a few tens of ms, and always was. The
old belief is what you get from verifying with `get_clock`, whose seconds field
is zero regardless of the truth.

**The radio has an NTP client, and it was pointed nowhere.** Scanning the
numbered settings either side of the clock parameters found it:

| Setting | Meaning | Factory value |
|---|---|---|
| `1A 05 0181` | NTP Function, ON/OFF, persistent | `01` |
| `1A 05 0182` | NTP server address, 64-byte space-padded ASCII | `time.nist.gov` |
| `1A 05 0183` | the other NTP flag (reads `01`, unchanged by writes) | `01` |

`time.nist.gov` is unreachable: the radio's only link is the direct cable to the
laptop, with no route off it. So NTP was enabled and failing silently for the
project's whole history — which is the entire explanation for the drift the
manual sync existed to patch.

**Pointing it at the laptop fixes it, continuously.** With `chronyd` serving the
radio's subnet and `0182` written to the laptop's address, a clock left
deliberately 30 s wrong corrected itself to **+0.014 s** with nothing pressed;
`sudo chronyc clients` shows `icom9700` as a querying row. An off→on toggle of
`0181` forces an immediate poll, which is worth knowing because the radio does
not otherwise re-poll promptly after a settings change.

The failure mode this leaves is the same one the Voice Recorder settings have: a
factory reset restores `time.nist.gov`, and nothing during a round would say so.
Hence the logger's clock monitor, which measures the offset by the rollover trick
above rather than trusting the settings, shows it in the toolbar and records it to
telemetry. Measured drift with NTP working: four samples over 18 minutes spanned
19-49 ms, with no trend — below what one poll of the monitor can even resolve.

**...and `contest_video.py` then ignored every one of those records.** The offset
is measured to ±25 ms and written ~24 times over a two-hour round, but
`load_telemetry` never parsed the `clock_offset_s` key and nothing at render time
referenced it. So every video the project has produced places its WAV-derived times
(all the audio, hence the HUD, the chapters and the CW ticker) against its
laptop-clock sources with an uncorrected offset whose size was sitting on disk the
whole time.

**Rejected: correlating polled PTT against WAV boundaries to measure the same
offset.** Superficially attractive — a PTT transition is timestamped by the radio
(the WAV) and by the laptop (telemetry), so the pair measures the difference. Two
findings kill it:

- **PTT cannot be had unsolicited.** Per the capture work above, the only
  unsolicited pushes are freq/mode Transceive frames and scope sweeps; wfview polls
  `1c 00` at 1–3 Hz. So the laptop-side stamp carries poll jitter that has to be
  compensated rather than measured.
- **The radio-side stamp is quantised to a whole second** — filename and `title`
  tag alike — so each correlation observes the radio's clock to only ±0.5 s. Even
  averaged over hundreds of transitions that lands in the same tens-of-milliseconds
  range as the rollover burst, in exchange for a new CI-V polling path.

That second point is a general limit, not an artifact of this approach: **the WAV
timeline cannot be made sub-second accurate at all.** Corroborating evidence from
the August round — inter-segment gaps computed from filenames range from −1.58 s to
+1.0 s, and a negative gap is physically impossible, so at least 1.5 s of that
spread is quantisation rather than signal. The clock offset is worth removing
anyway because it is *systematic* across every segment while the quantisation is
random per segment; removing it removes a bias, it does not buy sub-second
accuracy.

Telemetry-side PTT keeps one live use unrelated to clocks: the S-meter slot
switches to Po on transmit (see the meter findings), so anything consuming the
meters needs PTT to label the reading.

## contest_video.py

### The webcam drift diagnosis

Two separate problems, found in sequence. The first is only reachable by an
**independently recorded** clip (a phone); the second reaches the logger's own
Alt+V capture too, since sharing the machine's clock fixes where a capture
*starts*, not the rate of the sample clock it is being compared against.

1. **Variable frame timing under a constant-rate label.** A phone clip claimed
   `r_frame_rate` 30/1 while its per-frame timestamps were genuinely variable —
   confirmed by reading every packet's own `pts_time`: not one pause but 3,444
   scattered micro frame-drops across ~2 h (thermal/buffer pressure), summing to
   exactly 0.753 s that the raw frame count doesn't account for (218,052 frames at a
   flat 30 fps spans 7268.4 s; the container's PTS-accurate duration is 7269.12 s).
   Without an explicit `fps=` filter the branch was laid out by frame count rather
   than by true timestamps, so it ran slightly fast the whole way through.
2. **Two independent crystals don't tick at the same rate.** Reported as "video
   ahead of audio by 1:48 into a 2 h round", confirmed by ear and by uploading to
   YouTube. The whole-hour offset correction passed sub-hour *rate* skew straight
   through, invisible to every test that only checked "does the render apply
   whatever `webcam_start` it was given" rather than "is `webcam_start` correct".
   Diagnosed by ear, then measured: the operator's voice reaches both microphones at
   the same real instant, so extracting speech onset from both and comparing against
   the assumed start showed a *growing* gap — ~0 s near the start, ~+3.2 s near the
   end. A linear drift, which no single offset can correct. A same-machine Alt+V
   round measured the same way grew to ~+5 s — but that was before the radio's
   clock went on NTP (see "The radio's clock"), so an unknown part of it was
   time-of-day drift rather than crystal rate, and the figure is an upper bound
   until a long post-NTP round re-measures it.

`refine_webcam_start` fits both: anchors sampled evenly across the *whole* round
(an earlier version took the first few, clustered them in the opening minutes and
got a near-meaningless rate), cross-correlated via an RMS envelope rather than raw
samples (a coarse amplitude-rhythm signature survives two very different
microphones capturing the same speech), keeping only anchors above `min_confidence`
0.3 — real data showed a clean gap between spurious matches at 0.08–0.29 and
genuine ones at 0.34–0.77. Verified: at the exact reported drift point the
coarse-only mapping was off by 2.73 s, the rate-corrected one by 0.07 s.

**Rejected: tagging the true start into the mp4's container metadata after
capture.** It works (tested on a real ~2 h/3 GB file: a 15 s stream-copy remux) but
needs a full second copy of the file on disk at exactly the point in a round when
free space is tightest. The rename that replaced it took 0.006 s on that same file —
a directory-entry update, independent of size.

### Where the face actually is

The PiP took a centred crop, which assumes the operator sits in the middle of a
frame. YuNet over the whole August round (2 h Alt+V capture, 1280×720, sampled
every 5 s, 1440 samples, 98.8 % hit rate, longest miss run 10 s) says otherwise:
the face centre's median is **x = 782 of 1280** — 0.61 of the width — with p5→p95
spanning 689→938. Vertically it barely moves (68 px of 720 across p5→p95).

The cause is feedback, not habit. The July round was shot on a phone front
camera the operator could watch while recording; the Alt+V laptop capture that
replaced it shows no preview, and every future round is the second kind.

How far the face centre strays from the crop centre (half-width 353 px):

| | > ½ half-width | > 0.8 | worst |
|---|---|---|---|
| centred crop | 36.8 % of the round | 7.8 % | 491 px — outside the crop; the operator was partly out of the PiP |
| crop on the median face | 4.2 % | 0.14 % | 350 px |

**Rejected: tracking the face.** That 0.14 % is a single 10 s excursion in two
hours — the operator reaching past the camera — so a tracker would spend
smoothing constants, a lost-face policy and `sendcmd` keyframing on it, and buy
a jitter failure mode in exchange.

Re-detecting on the **rendered** 245×250 PiP, which is the measurement that
counts, the static crop leaves the face 18.5 px from the recess centre at the
median and 57.1 px at p95, against the centred crop's 48.9 and 93.0. The
detector also finds a face in 1426 of 1440 rendered frames rather than 1393:
the centred crop loses it outright 33 times more often.

**Rejected: zooming in on the face.** The detector's box is 46 % of the frame
height, so the whole head with hair and headset is ~64 % — the existing
full-height crop already fills ~60 % of the recess, which is the framing a zoom
would have been aiming for. Only x moves.

Cost is not a reason to be clever: 6.2 min to decode a 2 h clip at one sample
per 5 s plus 1.2 min of detection (49 ms/frame at 640×360), against a ~3 h
render — 4 %. Hence no cache and no sidecar. Keyframe-only decoding would cost
1 min instead, but a keyframe every 25 s is 288 samples for the median to stand
on rather than 1,440.

### The webcam capture mode, and why 4:3 is the wrong shape

Found while asking why the August clip was 1280×720 at 10 fps and the test
captures 640×480: `_webcam_capture_cmd` passed no `-input_format`,
`-video_size` or `-framerate`, so ffmpeg inherited whatever mode the last
application had left the camera in. Nobody had chosen either.

This camera's own table (`v4l2-ctl --list-formats-ext`) explains the 10 fps —
uncompressed YUYV at 720p is USB-bandwidth-capped:

| format | 1280×720 | 848×480 | 640×480 |
|---|---|---|---|
| YUYV | 10 fps | 20 fps | 30 fps |
| MJPG | 30 fps | 30 fps | 30 fps |

**The 4:3 modes are a horizontal crop of the sensor, not a taller view of it.**
Measured by capturing one frame in each mode and feature-matching them against
the 720p frame (ORB + `estimateAffinePartial2D`):

| mode | horizontal field | vertical field |
|---|---|---|
| 848×480 | 98.2 % of 720p | 98.9 % |
| 640×360 | 99.6 % | 99.6 % |
| 640×480 | **75.9 %** | 101.2 % |

That 24 % is spent on exactly the axis the PiP's face framing pans along, which
rules 640×480 out despite it being the one 4:3 mode that reaches 30 fps
uncompressed.

Sustained rate and cost, measured with the logger's own encoder settings over
30 s each (a 20 s sample is contaminated by camera warmup and reads ~27 fps):

| mode | measured | disk | crop into the 245×250 recess |
|---|---|---|---|
| 640×360 | 30.0 fps | 600 MiB/h | 353×360 → 1.44× downscale |
| **848×480** | 30.0 fps | 960 MiB/h | 470×480 → 1.92× |
| 1280×720 | 30.0 fps | 2280 MiB/h | 706×720 → 2.88× |

848×480 is what is pinned: the full sensor field, 30 fps to match `RENDER_FPS`,
and under half the disk August spent to record a third of the frame rate
(4,055 MiB for that round).

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
  123.8 s for a 76.9 s clip (0.62× realtime — hours for a full round); redrawing
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
  it. The tolerance is gone: the change test now rounds both sources to kHz, which
  is the resolution the QRG readout displays and the band lookup needs, so the
  disagreement cannot express itself at all and no constant has to encode its size.
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

## Concurrency: how tangled the threads are (August 2026)

A one-time audit, prompted by the question of how easily this could deadlock by
accident. Snapshot of one point in time — recorded as evidence for the
"concurrency is asyncio, not threads" principle in CLAUDE.md, not as a table to
keep current.

Only `puskas_logger.py` is threaded. `contest_video.py` and its modules have no
threads at all (they wait on ffmpeg subprocesses); `on4kst_irc_bridge.py` is
pure asyncio; `hamlib_supervisor.py` is single-threaded too, blocking on an
inotify fd and running rotctld as a subprocess. (An earlier draft of this
section credited it with two threads. That was a scan matching `.start()`,
which caught `Daemon.start()` — a subprocess, not a thread.)

**Eight threads in the logger's process during a round**, plus transients:

| Thread | What it is waiting for |
|---|---|
| main | the keyboard, via prompt_toolkit |
| `_radio_thread` | the radio session to go stale, then reconnect |
| `icom_net._ctrl_loop` | UDP on the control socket |
| `icom_net._civ_loop` | UDP on the CI-V socket |
| `icom_net._meter_loop` | a 0.5 s timer |
| `rotator.poll_thread` | rotctld, then a 1 s timer |
| `rig_server.serve` | a TCP accept |
| `_toolbar_watcher` | a 0.1 s timer |

Transient: one thread per rig-server client, one per `Alt+R` rotator command,
one for the startup clock sync.

**Seven locks**: `_rig_lock`, `_rot_lock`, `_scope_rec_lock`, `_telem_lock`,
`_clock_sync_lock`, and `icom_net`'s `_lock` (an RLock) and `_send_lock`.

### The structure is better than the count suggests

An AST scan for lock nesting found **none**: no thread anywhere holds one lock
while acquiring another. A cycle between two threads therefore cannot form, and
the classic lock-ordering deadlock is structurally impossible. Every `join()`
and `Event.wait()` also carries a timeout, so no thread waits on another
indefinitely.

That leaves exactly one shape of deadlock available: a thread re-entering a
lock it already holds. Both instances found were real.

- `icom_net._lock` is an **RLock** because `_apply_update` holds it while
  reading `self.band`, whose getter takes it again. Already known, already
  fixed, and the comment says so.
- **The signal handler was the other, and it was live.** A Python signal
  handler runs on the main thread wherever that thread is; the main thread is
  the UI, which is constantly inside `current_rig()`'s critical section; the
  handler's teardown took the same plain lock. SIGTERM is how a round *normally*
  ends, so this ran every round. Reproduced by firing 20,000 signals at a main
  thread spinning in `current_rig()`: the process hangs and cannot be killed by
  a further SIGTERM, because that signal's handler is the thing stuck. Fixed by
  taking the lock off `_radio`, which never needed it.

One near-miss that is *not* a deadlock, checked rather than assumed: the same
handler writes to the input log, which the UI thread also writes. CPython's
`BufferedWriter` raises `RuntimeError: reentrant call` instead of blocking, and
`log_input_event` swallows it — so the cost is one missing log line, not a hang.

### Whether the threads are needed at all

They are not. Every one of the eight is waiting on I/O or on a timer; **nothing
in the logger is CPU-bound**. Three facts settle it:

- **prompt_toolkit is asyncio-native and the logger already runs an event
  loop.** `Application.run()` documents itself as running the application "in a
  fresh asyncio event loop" and ends in `asyncio.run(coro)`. `prompt_async()`
  exists. So this is not a question of adopting new machinery — the loop is
  already there, and the threads run *alongside* it.
- **The pattern is already proven in this codebase.** `on4kst_irc_bridge.py` is
  32 coroutines, zero threads, with an integration-test harness that drives it.
- **Each thread has a direct asyncio equivalent**, and two get simpler rather
  than merely different: `rig_server`'s accept loop plus its thread-per-client
  become one `asyncio.start_server`, and the two UDP receive loops become
  `create_datagram_endpoint`, which is the case asyncio exists for.

`_toolbar_watcher` is the argument in miniature. It exists only because
prompt_toolkit's own `refresh_interval` redraws unconditionally, and it already
carries a scar from being a thread: `get_app()` returns a `DummyApplication`
inside it, because a thread gets a fresh contextvars Context, so `invalidate()`
was a silent no-op until the app was captured explicitly. A task inherits the
context and the trap does not exist.

**The honest cost**: threads isolate a blocking mistake to one thread, while an
event loop propagates it to the UI. That is a real trade, and it is the reason
to keep the escape hatch (`asyncio.to_thread`) rather than to ban threads
outright. Note though that the UI already blocks on a mutex today, and the file
writes inside critical sections are already on whatever thread reaches them.

### What it became (August 2026)

Eight threads and seven locks to **zero of either**. Measured on a running
logger (pty harness, radio absent): two OS threads, the event loop and
prompt_toolkit's own input reader. The logger calls `asyncio.to_thread` nowhere
— the one remaining use of the escape hatch anywhere is the bridge's
`_persist_seen` file write. Nothing waits on the radio, the rotator, the rig
server or a timer in a thread any more.

Three things worth keeping:

- **The signal handler stopped being a hazard by construction.** It is
  `loop.add_signal_handler` now, so the teardown is an ordinary loop callback
  rather than code injected between two bytecodes of whatever the main thread
  was in the middle of. The self-deadlock above needed both halves; removing
  the lock fixed it, and this removes the shape.
- **Cancel is not enough on its own — cancel *and await* is.** The meter poller
  sends four queries with no `await` between them, and that burst is
  uninterruptible. `close()` awaits the cancelled tasks before the first
  goodbye packet, which is a stronger guarantee than the `join(timeout=1.0)` it
  replaced.
- **The webcam stays on `subprocess.Popen`, deliberately.** Converting it to
  `asyncio.create_subprocess_exec` looked like the last step, but on Python
  3.12 the default child watcher is `ThreadedChildWatcher`, which spawns a
  thread per child — the conversion would have *added* the thread it was
  supposed to remove. The cost of leaving it is a `proc.wait(timeout=5.0)` on
  the loop when a recording is stopped, once per recording.
