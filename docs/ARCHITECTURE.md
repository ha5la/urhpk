# Architecture — component by component

What each component does and, more importantly, the constraints an edit must
not break. PIPELINE.md is the high-level story these pieces serve; FINDINGS.md
has the measurements and dead ends behind the rules stated here; CLAUDE.md has
the development principles.

The project root holds the six programs a person runs, and nothing else in
Python; everything they import lives in `urhpk/`. An entry point stays at the
root because `sys.path[0]` is the *script's* directory, which is what makes
`urhpk.*` resolve when one is launched by path from a round directory.

## on4kst_irc_bridge.py

General ON4KST↔IRC bridge with optional Puskás URH Kupa sked support. The
quick start is in README.md.

- No external dependencies – pure stdlib asyncio
- Listens as a minimal IRC server on `127.0.0.1:6667`; designed for one IRC client
  but supports multiple simultaneous connections
- Public chat maps to `#on4kst`; `/CQ CALLSIGN` maps to IRC PM (PRIVMSG to nick)
- ON4KST connection is kept permanently and reconnects after drops (`RECONNECT_S = 30`)
- **TCP keepalives are mandatory on the KST socket** to detect silent drops (e.g. WiFi
  disconnect) without waiting for the OS default timeout (30+ min). Parameters set in
  `connect()`: `SO_KEEPALIVE=1`, `TCP_KEEPIDLE=30`, `TCP_KEEPINTVL=10`, `TCP_KEEPCNT=3`
  → dead connection detected by the OS within ~60 s, which raises `OSError` on the next
  read. `read_loop` catches `OSError`/`ConnectionResetError`/`BrokenPipeError` and breaks,
  letting `_run_kst` reconnect. Do not remove this error handling.
- Bridge auto-joins the IRC client to `#on4kst` on connect — no client-side autojoin needed
- `/SET HERE` sent when first IRC client connects; `/UNSET HERE` when last disconnects;
  AWAY command from IRC client forwards the same
- User list updates (every 120 s) trigger IRC JOIN/PART events for member list accuracy
- **ON4KST seen-stations**: every user list update is persisted to `~/.puskas/on4kst-seen-stations.json`
  (`{callsign: {wwls: [most_recent, ...], bands: []}}` — same format as `puskas-seen-stations.json` in `~/.puskas/`
  but `bands` is always empty since band is not known from ON4KST). The logger merges this file
  with `~/.puskas/puskas-seen-stations.json` to build its locator cache.
- IRC subset implemented: CAP negotiation, NICK/USER registration, PING/PONG,
  JOIN, PRIVMSG, AWAY, WHO (352), WHOIS (311/312/318/319), MODE (324/368/349/347), QUIT
- irssi channel sync (10 s) requires responses to `MODE #channel b/e/I`
  (368 ban-list end, 349 exception-list end, 347 invite-list end) — plain `MODE #channel`
  returns 324
- WHOIS shows distance and bearing (e.g. `1534 km 305°`) computed from own locator
  (fetched via `/SHow CONFig` at login) to the target's current KST locator
- Sked commands:
  - `/msg CALL sked` (IRC PM) → sends sked via `/CQ CALL …` on KST, echoes NOTICE to channel
  - Sked text: `"Hi CALL, sked? Puskás URH Kupa – 1534 km, 305° – 144.174 MHz USB (JN97MX). 73 HA5LA"`
  - Distance/bearing from live KST user list; QRG/mode queried fresh at sked-composition
    time (falling back to the poll cache) — see rig-state integration below
- Local commands (not forwarded to KST, response NOTICE goes to `#on4kst`):
  - `!scatter CALL` — real-time airplane scatter check via OpenSky Network API
  - `!list` — lists online stations by distance and bearing
  - `!help` — lists available commands
- Rig-state integration (optional, no-op when nothing listens on 4532): speaks the
  rigctld TCP dialect (`f`/`m`) to `localhost:4532`, but what normally serves that port
  now is **`puskas_logger.py`'s built-in rig server** (answering from its push-fresh
  `icom_net` cache), not Hamlib's rigctld — the port is an interface, either works.
  - **No poller, no cache, no persistent connection**: `fetch_rig_info()` is one short
    connect-query-close (`RIG_QUERY_TIMEOUT_S` = 0.5 s overall), run at the moment the
    data is needed — sked composition and `!help`'s rig line. Served from the logger's
    memory it costs microseconds; with no server listening, the localhost connect is
    refused instantly, so the offline case costs nothing and degrades to a sked without
    QRG. (There used to be a 5 s `_rig_poller` + cache + connect/disconnect NOTICEs —
    removed once query-on-demand made all three redundant.)

## puskas_harvester.py – Pre-contest station harvester

Run once before a round to build `~/.puskas/puskas-seen-stations.json`:
```
uv run puskas_harvester.py
```
- No external dependencies — pure stdlib
- Fetches event list from `bb.mrasz.hu`, filters for Puskás URH Kupa rounds with `isClaimed==true`
- Rounds are **sorted by `submitDeadline` oldest-first** before processing — the `_record`
  helper inserts locators at the front of `wwls`, so the last-processed (most recent) round's
  locator ends up first. Without this sort the API's newest-first order would put old locators
  at the front.
- Records **only log submitters** — partner callsigns/locators from uploaded logs are skipped
  because they are typed by someone else and prone to typos
- QSO records are still fetched per submitter to capture which bands they operated on
- Output: `~/.puskas/puskas-seen-stations.json` — `{callsign: {wwls: [most_recent, ...], bands}}`
  where `wwls` is a list of all known locators in reverse-chronological order (most recently
  observed in any Puskás round appears first)
- All API responses cached in `.puskas_cache/` via `mrasz_api`; delete it to force a fresh fetch

## urhpk/mrasz_api.py – the contest server, cached

`bb.mrasz.hu/nest` is read by two components, so the fetch and its cache live in one
place and the cache is shared.
- `cached_get(url, max_age=None, now=time.time)` — `max_age=None` never expires, which
  is correct **only** for a round the organiser has already evaluated. Anything still
  moving must pass a bound: the event list did not, and a clone first run in May still
  believed May was the newest round in August.
- The clock is an argument so a test pins it instead of touching mtimes.

## puskas_standings.py – the year so far, including the un-evaluated rounds

```
uv run puskas_standings.py [--year 2026] [--callsign HA5LA] [--category SO-BP] [--refresh]
```
The organiser's annual table (`/preliminary` on the `*-MERGED` event) covers only the
rounds it has finished evaluating, which in practice runs two to three months behind.
This rebuilds that table from the per-round results and carries it forward.

- **Aggregation is per category, not per callsign.** A station that changes category
  mid-year — out of `KEZDO` on its second licence anniversary — is credited in each
  category only with the rounds it spent there. Summing its rounds under one callsign
  reproduces neither total. `tests/fixtures/mrasz-2026-evaluated.json` pins the six
  evaluated 2026 rounds against the organiser's own published table: 42 totals, exact.
- **Two totals, side by side.** `naive` takes claimed scores at face value; `adj` scales
  each pending round by the station's historical retention. Where the two disagree about
  the order, the order is not yet knowable — and for 2026 after August they do disagree
  about first place.
- **Retention is `evaluated / claimed`, meaned over every evaluated round including
  previous years'** — the rate belongs to the operator, not the season, and one year is
  six samples. Below `MIN_SAMPLES` the field's pooled mean stands in, so a newcomer is
  not judged by one unlucky round. It matters: 2026-only retention and 2025+2026
  retention rank the top two BP stations differently.
- `--refresh` drops the hour-long cache on un-evaluated rounds; evaluated ones are
  cached forever because they cannot change.
- Not subject to the round-directory rule — it writes nothing and reads no round.

## hamlib_supervisor.py – rotctld USB-replug supervisor

Problem this solves: if the USB connection drops (cable wiggle, radio power-cycle)
and the kernel re-enumerates the device, a running `rotctld` keeps the old, now-dead
file descriptor open — it never re-`open()`s a path once it has a fd. A stable device
name alone doesn't fix that; the daemon has to be *restarted* against the new node.

```
uv run hamlib_supervisor.py
```
Run permanently (tmux, or a `systemd --user` unit) alongside the contest tools.

- **inotify, not polling**: watches the parent directory of each configured device
  path for `IN_CREATE`/`IN_DELETE`/`IN_MOVED_TO`/`IN_MOVED_FROM`, via `ctypes`
  against libc — no `inotify_simple`/`watchdog` dependency, matching
  `on4kst_irc_bridge.py`'s pure-stdlib style. `reconcile_initial_state` handles the
  device-already-present-at-startup case, since inotify only reports *future* events.
- **Device paths come from the distro's own `/dev/serial/by-id/`**, no custom udev
  rule (verified on this hardware):
  - IC-9700: `usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_IC-9700_13013358_A-if00-port0`
  - Rotator (Arduino, Yaesu GS-232-compatible): `usb-1a86_USB_Serial-if00-port0`
- **Rotator model is 603 (GS-232B)**, not 601. It only manages `rotctld` now — the
  rig moved to `icom_net.py`, which also removed the reason to want async rig state
  here. See FINDINGS.md for why async state isn't available from Hamlib at all.

## urhpk/icom_net.py – Direct Icom Ethernet CI-V client

The IC-9700 is reachable over Ethernet, but there is no plain "CI-V over TCP" port
on the radio: the only way in is Icom's own network-remote-control protocol (what
RS-BA1 and wfview speak) — UDP, authenticated, stateful. This is a minimal client
for it in pure stdlib Python. `puskas_logger.py` uses it as its **only** rig
interface; it is also usable standalone (`uv run urhpk/icom_net.py <radio-ip>`).

**Asynchronous throughout**: `connect()` and `close()` are coroutines, the two
receive loops are tasks over `loop.create_datagram_endpoint`, and there is no thread
and no lock. Sends stay synchronous — `transport.sendto()` never blocks — so
`send_cw`/`stop_cw`/`enable_scope` can still be called straight from a
key binding.

**Why it beats polling**: with `SET > CONNECTORS > CI-V > CI-V Transceive: ON` the
radio pushes frequency/mode changes the instant they happen, front-panel included.
Hamlib's IC-9700 backend doesn't use that, so the logger on rigctld only ever saw
state up to its poll interval stale. A direct client just listens.

- **Credentials** come from `~/.netrc` (`machine <radio-ip> login <user> password
  <pass>`), same convention as the bridge. Requires `SET > Network > Network
  Control: ON` and a LAN username/password set in the radio's own menu — this
  applies even on the same subnet; Icom's login step is not WAN-only.
- Username/password are **obfuscated on the wire** by a fixed substitution table
  keyed by `(character_value + position_index)` — not a cipher, just not cleartext.
- **BCD frequencies**: standard Icom 5-byte little-endian BCD, verified against the
  worked example in Icom's own CI-V documentation (144.300.000 Hz → `00 00 30 44
  01`) before touching hardware, which caught a base-16-vs-base-10 typo and a
  nibble-order bug via a red-before-green unit test.

**Invariants — do not "simplify" any of these.** Each was a real failure against the
real radio; the evidence is in FINDINGS.md.
- The first *tracked* packet starts at `seq=1`, not 2 — the are-you-there/ready
  handshake is a separate counter.
- `_ctrl_loop` (control-socket idle/reauth) must start **immediately after conninfo
  succeeds**, before any CI-V-socket work, or the radio drops the session mid-open.
- `close()` **cancels and awaits** its tasks before any goodbye packet: a query burst
  between two awaits is uninterruptible, so an un-awaited cancel can still put a meter
  query on the wire after the disconnect.
- CI-V "open" retries are spaced at `CIV_STALE_S`, deliberately far slower than
  wfview's 100 ms, or the radio's receive-sequence tracker desyncs.
- Every open attempt sends a real CI-V query alongside it: the radio never speaks
  first, so waiting for data before sending is a deadlock.
- No extra token renewal between register and conninfo — a real client sends none.
- `close()` sends a disconnect control packet (`type=0x05`, `seq=0`) on **every**
  socket it opened, not just the token deregister; without it the radio keeps the
  session and refuses new ones for over a minute. It is idempotent and safe on a
  half-connected session, so `connect()`'s failure path just calls it.
- **The radio holds exactly one session.** A second connect silently kills the
  first. So every consumer goes through the one session owner (`puskas_logger.py`
  during a round — see its rig server), and the CLI harness must never run while
  the logger is up.

**rigctld-parity commands**, as plain CI-V writes: `send_cw()` (0x17 + ASCII, 30-char
limit) and `stop_cw()` (0x17 + 0xFF) — byte layouts transcribed from Hamlib's own
`icom_send_morse`/`icom_stop_morse`. Nothing here sets the radio's clock: it keeps
itself right over NTP (FINDINGS.md), so the clock is read only, via `read_clock()`
— the one setting whose reply is two bytes rather than one, which is why it does
not go through `read_param()`.
The NTP client's own settings are readable the same way: `read_param()` for
0181 (Function), `read_ntp_server()` for 0182, whose reply is a 64-byte
space-padded string rather than one byte.
`on_civ_frame()` exposes every raw inbound CI-V frame, which is how a caller sees
ACKs (FB ok / FA rejected); note the radio echoes the client's own frames back, so
distinguish direction by the address bytes.

**Test coverage**: `tests/test_icom_net.py` covers the pure functions (passcode, BCD,
frame parsing) with no mocking; `tests/test_icom_net_integration.py` runs the full
`connect()` handshake against an in-process fake radio and injects an unsolicited
Transceive frame to assert push updates with no polling. What it *cannot* catch: anything about the real radio's own
session/sequence tracking, since a fake that mirrors the client's assumptions back
can't contradict them.

**Not implemented, deliberately**: audio streaming (conninfo requests
`rxenable=0`/`txenable=0`), PTT/transmit control, and general retransmit-*request*
compliance (resending a specific buffered packet on demand) — skippable on a clean
LAN, confirmed in steady-state traffic. What is *not* skippable is the initial seq
numbering and handshake ordering above.

### Scope (spectrum-scope waterfall) data

Goal: match the exact waterfall the radio's own display shows, rather than one
reconstructed from the recorded audio — audio can only ever show what the receiver
demodulated, not the RF passband the operator was watching.

- Sweeps are ordinary CI-V `0x27` frames on the **existing** CI-V socket (port
  50002) — not a separate stream and not separately negotiated. (There is no scope
  port; see FINDINGS.md, which is where that dead end is recorded.) Enabled with two
  plain CI-V writes, `27 10 01` (scope on) and `27 11 01` (data output on) —
  `enable_scope()`. Off by default since it is a much heavier stream than Transceive
  frames; opt in explicitly, as `--scope` does on the CLI.
- Pixel bytes are raw `0`–`160` linear scope units, 475 bins per sweep on this
  radio, whole sweep in one datagram. Frame layout and the Centre-mode
  centre/half-span arithmetic are in FINDINGS.md.
- **Reassembly is defensive**: `_apply_scope_frame` buffers pixels across
  `sequence` 1..`sequence_max` and only fires `on_scope()` on a complete sweep, even
  though this radio always sends `sequence_max == 1` over LAN. Essentially free, and
  it is what a multi-packet sweep would need;
  `test_parse_scope_frame_multi_sequence_reassembly` exercises the path with a
  synthetic two-packet sweep.
- **Recording format**: `write_scope_record` appends one binary record per sweep —
  `<f8 timestamp><u4 start_hz><u4 end_hz><u2 npixels><npixels raw bytes>`.
  Deliberately not JSONL like the telemetry/input logs: those are ~1 record/second
  and human-debuggability matters more than size, while sweeps arrive several times
  a second already byte-quantised. Verified byte-exact round-trip against real
  hardware. `icom_net.read_scope_records` is the format's single owner —
  `contest_video.py` reads it through that rather than reimplementing the parser.

### Meters

`enable_meters()` polls Po/SWR/Vd/Id (`CIV_METERS`) at `METER_POLL_S` (0.5 s) and
reports one snapshot per cycle via `on_meters`, rather than firing per reply — that
keeps the four values in a record genuinely simultaneous and lets a recorder write
one line per cycle instead of four. Off unless asked for, like `enable_scope`, and
re-armed on every reconnect since the poll task belongs to its session.

Polling is unavoidable here: the radio only reports meters when asked. **The
S-meter (`15 02`) is deliberately not polled** — `contest_video.py` derives signal
level from the scope recording's own centre bins, which costs no extra traffic and
is already captured.

**Store raw meter values and convert at render time**, so a better calibration is a
one-line change rather than a ruined recording. Curves and the measurements behind
them are in FINDINGS.md.

## contest_video.py – Annotated CW contest video

Turns a round's recording plus its EDI log into a YouTube-ready MP4: a scrolling
waterfall background, a DOOM-style HUD status bar, and picture-in-picture of the
logger's own terminal and the operator's webcam.

```
uv run contest_video.py RECORDING_DIR EDI_FILE [EDI_FILE ...] [-o OUT.mp4]
uv run contest_video.py [-o OUT.mp4]      # inputs taken from the round directory
```
The second form is all-or-nothing on the positionals: give neither and
`wiring.discover_round_inputs` fills every slot from the directory, give one and
nothing is discovered. Flags always win over what was found. The discovery
refuses ambiguity rather than resolving it — two `.cast` files is an error, not
the newer one — and it never globs plain `*.mp4`, because the renders live in
the round directory beside the webcam clips.

Dependencies: `numpy`, `pyte`, `pillow`, `tqdm` (`pyproject.toml`) +
`ffmpeg`/`ffprobe`.

Each long stage reports its own progress, because they run at very different
rates and a single bar over the lot would say nothing about when the render
finishes. The four bars are in the functions that own the loops — CW decode,
cast PiP, HUD, scope waterfall — and the final ffmpeg pass reports itself
through `-stats`. `main` puts stdout in line-buffered mode so that those
announcements do not sit in a 4 KB buffer while the unbuffered bars overtake
them in a redirected log.

**RECORDING.md is the companion document** — the full option list, the CW decoder's
tuned constants and the reasoning behind the QSO-timing heuristics live there, with
real numbers from real rounds. Keep it current; this section is only what someone
*editing the code* has to know.

### Where the code lives

`contest_video.py` itself is now only the compositor and the CLI: the ffmpeg
filtergraph, the intermediate clips, and `main`. Everything it orchestrates sits in
a module of its own, and the imports form a DAG — nothing below imports anything
above it.

| Module | What it owns |
|---|---|
| `urhpk/wav.py` | The recorder's WAV files: the IC-9700 title tag, reading a time range |
| `urhpk/cw_decode.py` | The signal chain (pitch → envelope → hysteresis → Morse) and the trust gate |
| `urhpk/timeline.py` | `Segment`, `Qso`, the EDI read, and wall clock ↔ audio time |
| `urhpk/webcam_sync.py` | The one stream with no trustworthy clock: its start, and its drift |
| `urhpk/webcam_face.py` | Where that stream is cropped: the face the PiP is framed on |
| `urhpk/rig_state.py` | Telemetry and input log; RX/TX + QRG/mode events; QSO time matching |
| `urhpk/qso_windows.py` | Where each QSO sits in the finished video |
| `urhpk/chapters.py` | YouTube chapters and SRT captions |
| `urhpk/cast_render.py` | The terminal PiP: an asciinema `.cast` replayed into frames |
| `urhpk/scope_render.py` | The spectrum-scope waterfall background |
| `urhpk/hud.py` | The HUD's data layer: what the bar shows at any moment |
| `urhpk/hud_draw.py` | The HUD's drawing layer: artwork, sprites, readouts |
| `urhpk/video_format.py` | Frame size and rate — the two facts all three renderers must agree on |
| `urhpk/progress.py` | The per-stage progress bar, and how often it redraws off a terminal |

The data/drawing split inside the HUD is the one worth preserving deliberately:
`hud.py` needs no art, no fonts and no ffmpeg, which is what makes it fully
unit-testable, and `hud_draw.py` knows nothing about where its numbers came from.

### Inputs

- A directory of WAV segments named `YYYYMMDD_HHMMSS...wav` (local time), split by
  the radio on every RX/TX switch. They are contiguous, so **the audio timeline is
  the sum of segment durations**; filename wall-clock is used only to line QSOs up
  against the audio. All segments must share one sample rate/format (`concat_audio`
  copies frames straight from each into the output WAV).
- **Each file measures ~5.6 ms longer than the wall time it occupies**, so that
  sum runs long by 4 s over a round's 759 segments unless
  `compensate_split_excess` trims it first — which every run does, before
  anything reads `audio_t`. It sets `eff_dur`, not `dur`, because `concat_audio`
  cuts each segment to `eff_dur`; trimming one without the other desynchronises
  the audio from the timeline describing it. A cut this small is why the
  assembly is hand-written rather than an ffmpeg concat — see FINDINGS.md, which
  also has the three measurements behind the constant.
- **Multiple EDI files merge into one timeline** — a round worked across bands
  writes one EDI per band but is still one physical recording. `merge_edi`
  concatenates and sorts by `dt`. `Qso` carries no band field; band only ever
  mattered for logging, not rendering.
- **UTC offset is derived, not hardcoded**: EDI times are UTC, WAV filenames local;
  `derive_utc_offset` rounds the span-midpoint difference to whole hours, so DST
  handles itself.
### Which source is the truth, field by field

The single most confusing thing in this codebase has been that two sources describe
the same radio and neither is wholly better. Settled, and not to be re-litigated
field by field in individual functions:

| field | source of truth | the other source's role |
|---|---|---|
| `ptt` | **WAV metadata, always** | telemetry has no ptt field, by design |
| `freq_hz`, `mode`, and the `band` derived from them | **telemetry** | WAV metadata is a genuine second observation, not a fallback |
| radio↔laptop clock offset | **`clock_offset_s` in telemetry** | nothing else measures it |

- **`ptt` is the WAV's alone, because a PTT transition is *what cuts the file*** —
  the fact and its timestamp are the same event, so nothing can beat it. The
  IC-9700 writes a `title` tag into every file with frequency, mode and RX/TX as of
  the instant it started recording; `wav.read_wav_title` parses the RIFF
  `LIST/INFO/INAM` chunk directly rather than shelling out to `ffprobe` per file
  (6500× faster; see FINDINGS.md). ptt also cannot legitimately change mid-segment.
  Telemetry carried ptt once (the July round still has it) and it was removed:
  re-deriving it from a poll is a worse copy of a lossless record.
- **freq/mode are the union of both sources as plain timestamped observations,
  latest-wins** (`rig_runs`). Neither is seeded from nor corrected by the other, and
  there is deliberately no branch on which generation of round is being read —
  the right behaviour falls out of the timestamps:
  - A WAV sample is *exactly* timed but exists only at segment boundaries.
  - Pre-icom_net telemetry was a 1 Hz sample, so a freshly-cut segment genuinely
    carries newer information and wins at that instant — which is why the WAV
    metadata is not merely a fallback for old rounds.
  - Post-icom_net telemetry is pushed the instant either value changes, so it is
    denser and lag-free and dominates on its own.
  - While the radio is disconnected telemetry stops producing samples entirely, but
    the radio's own recorder keeps cutting WAVs, so the WAV samples take over.
- **"Did it change?" is asked at kHz**, the resolution the QRG readout displays and
  the band lookup needs. The two sources do not agree below that on old rounds
  (WAV metadata has 10 Hz resolution, the old sampler re-parsed a kHz-rounded
  string), and comparing them exactly fragments `state_events`, `cw_decode`'s CW
  ranges and the HUD's frame reuse. Genuine retunes are kHz steps, so nothing real
  is lost. This replaced a `FREQ_MATCH_TOLERANCE_HZ = 500` whose justification
  needed archaeology to understand; see FINDINGS.md for that history.

### Two independent timing errors, only one of which is fixable

- **The radio's clock against this laptop's** — systematic across the whole round,
  and *measured*: `_clock_monitor_run` pins it to ±25 ms every `CLOCK_SAMPLE_S` and
  writes `{"t", "clock_offset_s"}`. Applied **piecewise** — each measurement holds
  until the next — because the physical process is a clock that free-runs and is
  then stepped by NTP, not one that drifts smoothly; interpolating would smooth
  across exactly the discontinuity that matters, and the monitor already signals a
  step by dropping to unknown. A round with no such records gets no correction.
- **The 1-second resolution of a WAV's own timestamp** — the radio names files on
  the SD card to the second, and the `title` tag is no finer, so each segment's
  placement in wall time is quantised. This is *not* fixable from the recording and
  no amount of clock correction touches it. It is random per segment, whereas the
  clock offset is systematic, which is why removing the offset is still worth doing:
  it removes a bias, it does not make the timeline sub-second accurate.
- **Telemetry (`--telemetry`) is optional and partial by source.** Records mention
  only what changed — `{"t", "freq_hz", "mode"}` from the rig, `{"t", "az"}` from
  the rotator — so every field carries forward across the records that don't
  mention it. Four generations of it exist on disk, which is why the reader is
  permissive and why "is telemetry present at all" is a real question:

  | round | telemetry |
  |---|---|
  | `urhob2026cw`, `urhob2026mix` (Jul 4) | **none** — WAVs only |
  | `2026-jul` (Jul 6) | one combined line per second, `az freq_hz mode ptt` |
  | `2026-aug` (Aug 3) | one combined line per second, `az freq_hz mode` |
  | `test`, `test2` (Aug 11–12) | change-driven, separate rig/rot/meters/clock records |

  Three rules that each cost a bug:
  - **`load_telemetry` must accept both stamp precisions**, whole seconds (the
    original 1 Hz sampler) and microseconds (the current writer). Note the failure
    mode if it stops: it swallows a bad line via `except ValueError: continue`, so a
    stricter format would *silently* drop every line of a new recording.
  - **An absent `az` key and an explicit `"az": null` mean opposite things** while
    both loading as `az=None`: silence is a rig record saying nothing about the
    rotator and must not end the carry-forward, whereas a null is the rotator going
    offline and must. Hence `TelemetrySample.az_offline`; filtering on
    `az is not None` alone was a real bug caught in review, which showed the last
    known bearing for the rest of the video. Same pattern for the meters
    (`meters_offline`).
  - **Frequencies are compared at kHz, never exactly** — old recordings carry a
    systematic sub-kHz disagreement against the WAV value that would otherwise look
    like a retune at the start of almost every segment. New recordings don't (the
    cause was our own rounding — see FINDINGS.md), but the rounding stays for the
    old ones, and kHz is what the QRG readout displays anyway. Mode has no such
    problem; it is an exact string match.

### Pipeline

Decode → intermediate clips → one ffmpeg pass. Each side stream is rendered to its
own clip first (`render_cast_video`, `render_scope_video`, `render_hud_video`) and
composited in a single `filter_complex`, in the same spirit as `concat_audio`'s
intermediate wav.

- **CW decode is per-segment** (each WAV is one over at one speed, so adaptive
  per-segment dit estimation is robust and yields absolute per-character timestamps
  for sync). `decode_segment` returns immediately for anything over `MAX_OVER_S`,
  since `gate_events` would reject it on duration alone — that alone roughly halved
  decode time. The pitch is auto-detected per segment; `--pitch` is only a fallback
  for when nothing is found at all.
- **What gets decoded is a telemetry-confirmed CW span, not a segment** — our
  recorder only splits on *our* PTT, so a segment's own recorded mode is only its
  mode at the instant the file was cut, and anything from a mid-over retune to two
  other stations working each other in CW happens inside one file. `decode_round`
  asks `cw_subranges` for the CW-mode spans within each segment and decodes those,
  with the duration gate disabled (`check_duration=False`): mode confirmation is
  stronger evidence than length, and a two-way exchange between others can
  legitimately run longer than one of our overs. Touching spans are joined first —
  a run also ends at every retune, and a one-second sliver has too few ON runs for
  `_estimate_dit`. The output is kept **out** of `s.events`, so `--skip-gaps` needs
  an explicit `cw_span_segs` exemption or `remap_audio_t` would trim away the very
  audio just recovered. A segment whose WAV carries no IC-9700 metadata has no
  known mode at all and keeps the mode-blind whole-file decode.
- **`--duration SECONDS` trims before the decode loop**, not after — decoding is the
  dominant cost, so a 10-minute preview of a 2-hour round decodes ~12× less audio.
  QSOs past the cutoff are dropped before chapters/SRT are built.
- Also writes `<out>.chapters.txt` (paste into the YouTube description; first
  chapter must be `0:00`, and anything within `MIN_CHAPTER_GAP_S` of the previous
  one is dropped) and `<out>.srt` (upload as captions, each cue capped to
  `CAPTION_DUR_S`). Both come from `qso_windows()`, so they agree with each other by
  construction.

### QSO timing

The EDI format stores time only to the minute, so it can never say when an over
actually began. `qso_windows()` snaps each QSO onto real audio structure —
`burst_starts` finds every burst of activity, `_tx_start` finds the operator's own
first transmission within it, `_snap_to_burst` takes the *latest* burst at or
before the anchor. `--input-log` supplies an exact anchor where available
(`match_qso_times` pairs EDI QSOs to logged `'qso'` events **by callsign in
chronological order**, never by minute — a hand-edited seed log is expected to move
timestamps across minute boundaries). Each rule here fixed a specific reported bug;
RECORDING.md has the cases, and the regression tests name them.

A timing change is verified from `<out>.chapters.txt` and `<out>.srt`, not from a
render: `main()` writes both from `qso_windows()` right after CW decode, seconds in
and well before `concat_audio` and the ffmpeg pass. Anything touching `qso_windows`,
`build_state_events` or `match_qso_times` shows up there. Render only for what text
cannot show — layout, PiP, waterfall.

### ffmpeg composition — where the real bugs live

- **An input's index is taken at the moment it is appended** (`add_input`), never
  from a separately maintained list. Those two drifted apart the moment a branch was
  inserted ahead of another, and every stream then read another stream's clip — the
  HUD drawn at the cast's position, the terminal squeezed into the face recess, the
  webcam stretched along the bottom. Every filter-graph string assertion still
  passed, because each branch was individually well-formed.
- **Composite order is background → scope → cast → HUD → webcam.** The HUD is a
  status bar: nothing may overlap it. The webcam goes on top of it, inside the face
  recess.
- **A round's webcam clips share that one recess, stacked in time order**
  (`sync_webcams` sorts them; each overlay is enabled from its own start). Between
  two clips the earlier one's `tpad`-cloned last frame is what stays on screen.
- **Each clip's crop is its own, and is a constant.** `webcam_face` scans the clip
  and the crop is centred on the median face; a clip with no scan (no detector
  installed) keeps the size-agnostic centred expression. The crop must not vary
  with time — a PiP that follows the operator's head is the visual glitch the
  logger's rule forbids, and the motion it would chase is 0.14 % of a round.
- **Every side stream needs `tpad=stop_mode=clone`** so a clip shorter than the
  round cannot end the shared filtergraph early and silently truncate the main
  video and audio. This is a real risk class with multi-input filtergraphs.
- **Sync differs per stream, and the difference is the point:**
  - cast and scope carry real absolute timestamps (asciinema's Unix epoch header;
    `time.time()` per sweep), so a plain `-itsoffset` places them exactly.
  - the HUD needs **no offset at all** — it is generated *from* the output timeline
    rather than captured against an independent clock, so its t=0 already is the
    output's.
  - only an independently recorded webcam has a second physical clock to reconcile,
    needing `setpts=PTS/(1-rate)` before an `fps=` resample as well as the offset
    (see FINDINGS.md). The logger's own Alt+V capture shares this machine's clock:
    `parse_webcam_precise_filename` reads the exact start off the filename.
  - the same laptop-clock drift measured from the webcam is applied to the cast,
    since asciinema stamped its header from that same clock.
- **A stream that began before the audio must be *entered partway in*, not clamped
  to t=0.** `stream_start` differs from `audio_time_for` for exactly this case, and
  `_stream_input_args` turns a negative start into an `-ss` seek into that input
  (ffmpeg has no meaning for a negative `-itsoffset`). This is the **normal** case:
  `run-recorded-round.sh` starts asciinema before the radio recorder, and
  the `.scope` recorder starts when the radio connects. Clamping instead showed up
  as the cast PiP's clock lagging the round by 25 s.
- **Filter-graph string assertions cannot catch how branches combine.** Three real
  bugs got through them. Verify a change by rendering an actual clip and decoding a
  frame back out.

### HUD (DOOM-style status bar)

A full-width opaque status bar modelled on DOOM's, replacing readouts that are
technically visible in the terminal PiP but far too small to read there — the
logger's screen renders at ~13px in the cast, so *importance* has no visual weight.
The HUD's rule is that the more important a value, the bigger it is drawn.

- **The DOOM mapping is the design**, not decoration: SCORE takes the health slot
  (biggest number, flashing and counting up as it lands), QSOS takes ammo, the
  **webcam takes the face slot** (a centre crop of the operator, exactly where
  DOOMguy's portrait sits), Vd/Id take armor, band/mode chips take the weapon-slot
  strip. QRG, RX/TX lamp, S-meter, compass, UTC/rate/ODX and the CW ticker fill the
  rest. Best DX is labelled **ODX**, the contest term.
- **The bar *is* the artwork** (`hud-theme/artwork.png`), which carries every panel,
  recess, static label and the compass rose; `hud-theme/theme.json` says where each
  value goes, in artwork pixels. The code draws only what changes: readouts, five
  sprites, and dimming. Nothing static is drawn, so a baked label cannot be printed
  twice. Two consequences to know: a baked label can't change (the meter's caption
  is a fixed "S" rather than switching to "PO" on transmit — the lamp beside it
  already says which), and the compass has no numeric azimuth (no recess for one;
  the needle is the reading).
- **Coordinates are data, so re-fitting to new artwork is an edit to `theme.json`,
  not to code.** `--hud-theme-check` draws every rect back onto the artwork, which
  is the only way to check a hand-edited theme; several recesses can't be
  auto-detected because their interiors are too close in brightness to the panel.
  `HUD_THEME_DIR` is project-relative — renders run from a contest directory.
- **`hud_art(theme, W, H)` prepares everything once per render** (crop and scale the
  bar, scale every rect, cut/key/pre-scale the sprites); a frame is then a copy plus
  values. `draw_hud_frame` is called ~24,000 times for a 2 h round.
- **`HUD_W`/`HUD_H` (1920x340) come from the artwork's own 5.65:1 aspect**, and the
  bar is scaled uniformly or not at all — squashing it turns the compass into an
  ellipse, which is the specific reason this artwork was chosen. A test asserts
  every supported resolution lands within 1% of that ratio. `hud_height()` forces an
  even pixel height (libx264 refuses odd; 720p rounds to an odd number) and is the
  single owner of that rounding, which `main()` and `render()` must agree on exactly.
- **Sprites are keyed off flat magenta with a hard threshold on `min(R,B) - G`**,
  which is large only for the background and at most zero for anything the sprites
  are made of. Not an exact-colour match and not a soft alpha ramp: the sheet is not
  flat `#FF00FF` in practice (only 145 pixels of the artwork are exactly the key
  colour), so an exact match keys almost nothing and a ramp leaves a pink halo.
  Keyed pixels are blacked as well as cleared, so resampling blends edges toward
  black — and the sprites already have black outlines.
- **Chips and the S-meter are baked lit and dimmed back** (`HUD_UNLIT_DIM`) rather
  than being a second unlit asset that would have to stay stylistically in sync. The
  meter sprite box holds the LED strip *itself*, with its frame left to the artwork,
  so the lit fraction is simply `lit/segments` of the width and the cut lands
  between LEDs.
- **Both compass needles pivot on the ball at their base, not their bounding box's
  centre** — the pivot is stored per sprite in `theme.json`, and `_paste_needle`
  pads the sprite into a square canvas centred on it. Getting this wrong makes the
  needle orbit rather than point. The hollow needle (bearing to the station being
  worked, from its EDI locator) is drawn **on top of** the solid one (where the
  rotator actually points), because the two coinciding is the normal case and
  underneath its outline would be invisible — "on target" would look identical to
  "no target known". Validated on the real August round: rotator 310.0° against a
  computed target bearing of 310.38°, two independent sources agreeing.
- **The needle sweeps between samples rather than stepping to them.** The poller
  reports whole degrees about once a second, so a slew arrives as closely-spaced
  samples that interpolate into one continuous turn; a gap longer than
  `HUD_AZ_INTERP_S` is a stationary rotator, not slow movement, so the azimuth holds
  there. Interpolation takes the short way round the circle. Azimuth is deliberately
  its own time series (`hud_az_marks`) rather than a field of the per-run rig state:
  a run is however long freq/mode hold for, so one median for all of it made a real
  27-second 250°→31° slew render as a single jump at the run boundary.
- **The S-meter comes from the `.scope` recording's own centre bins**, not from
  CI-V's polled `15 02`. 475 bins across a 1 MHz span makes one bin ~2.1 kHz, close
  enough to an SSB passband to be a real reading rather than a proxy; `hud_s_marks`
  takes a *max* over the centre bins so a signal in one bin isn't diluted. Not
  retroactive: no round recorded so far has a `.scope` file, so the meter
  reads empty until the next one.
- **PWR renders placeholders rather than hiding** when Vd/Id are absent, which is
  every recording to date — panels appearing and disappearing between recordings
  would leave the bar looking broken.
- **Every readout is fitted to its own recess** (`_seven_seg`'s shrink loop), since
  nothing here has a fixed width — a five-digit score used to spill clean across the
  gutter into the QSOS panel. One test asserts the gutter comes out byte-identical
  to the artwork; another generalises it to the whole bar, so any readout painting
  over a baked label fails.
- **Numerals are DSEG7**, vendored into `hud-theme/` beside the artwork (SIL OFL,
  licence alongside) rather than taken from a system font package, with **unlit segments drawn
  very dim** (`HUD_SEG_DIM`, 0.12) — that is what makes an LED panel read as a panel
  rather than numerals floating on black. Keep the value low: at 0.16 the ghost
  behind a `1` read as a digit clipped by the panel edge. `_all_segments` doubles as
  the positioning reference, since a value containing `-` has a glyph box only as
  tall as the middle segment.
- **The CW ticker is a 5x7 dot-matrix display** (`_FONT_5X7`, `_draw_matrix_text`),
  the glyph table written out in source because the set is tiny and *fully
  determined* — `MORSE` can only decode to 44 characters plus space, which a test
  asserts. A generated character sheet was rejected: rendering long specific
  character sequences is what image generators are worst at, and one wrong glyph
  would mean regenerating everything.
- **The ticker scrolls on a clock, which is why it needs no clearing rule at all.**
  A character enters at the right edge and leaves `HUD_TICKER_SPAN_S` later;
  staleness is structurally impossible rather than guarded against. It scrolls a
  whole dot column at a time (a physical panel has no sub-dot positions), so
  `HudTimeline.at` returns (column offset, character) pairs.
- **Spacing is the display's, timing is the scroll's.** Characters sit exactly one
  cell apart within an over; what varies with the keying is how fast the strip moves
  (`_ticker_scroll`) — each character is a pin that was in the right-hand cell when
  it was keyed. Placing characters *at* their keying time spaced them raggedly by
  fractions of a cell, because Morse characters differ wildly in air time (a `T` is
  one dit, a `0` nineteen). Word gaps need no room of their own, since the decoder
  emits them as real `' '`. Past `HUD_TICKER_BURST_S` real elapsed time takes over
  again, which is what drains the display between overs.
- **`HUD_TICKER_CHARS` (15) is measured, not eyeballed**: at 1080p the slot is
  446x35, so seven dot rows cap the pitch at 5px and 15 cells fill 445 of those 446
  pixels; 16 would drop the pitch to 4.
- **`HudTimeline` looks everything up by bisect, never by scanning** — a two-hour
  render queries it ~216,000 times.
- **Frames are reused whenever nothing visible changed** (`hud_frame_key`):
  everything the drawing depends on except `t`, with continuously-varying values
  quantised to the resolution they are actually *drawn* at (18 meter segments,
  needles to the nearest degree, chip brightness to 1/100). Without that quantisation
  the scope-derived signal level alone forces a fresh draw ~30 times a second.
- **The band/mode chips are lamps, and read telemetry** (`hud_chip_marks`): they ramp
  up in `HUD_CHIP_RISE_S` and glow down over the longer `HUD_CHIP_DECAY_S`, from a
  time series of their own rather than from `SegState` -- the same split, for the same
  reason, as the compass and `hud_az_marks`. Only the transitions animate: a steady
  glow would change every frame and cost the whole of the reuse above, where the
  August round's 118 transitions add at most ~1,000 draws to its 26,557. An explicit
  `rig_offline` telemetry line puts both chips out; a line merely silent about the rig
  carries them forward.
- **The data layer is pure and fully unit-tested** — everything up to
  `draw_hud_frame` is functions over the recording's own sources, needing no art, no
  fonts and no ffmpeg. `HudTimeline.at(t)` returns a `HudState` for any video time.
- **Two single-frame preview modes**, because iterating layout against a full render
  is absurd: `--hud-demo OUT.png` needs no recording at all (this is what to check
  artwork against), `--hud-preview OUT.png --hud-preview-t SECONDS` builds real
  state from a real recording. `recdir`/`edi` are optional in argparse purely so
  `--hud-demo` can run standalone.
- **The artwork's generation prompt is `hud-theme/artwork-prompt.md`**, beside the
  artwork it generates — what the software
  draws (and therefore what the artwork must leave empty), why the sprite sheet sits
  on flat magenta, and why a coordinate table baked into the image was rejected (an
  image generator cannot measure its own output raster, so such a table would be
  confabulated while looking authoritative). The artwork's "text" is drawn, not
  typeset, and its labels were good enough to keep — so the HUD has no label face of
  its own at all.

## puskas_logger.py – UX requirements (non-negotiable)

These requirements must be preserved across all future changes:

- **Dynamic prompt**: the prompt prefix is `{band} {mode}  RX ► ` (e.g.
  `2M SSB  RX ► `), computed by a callable so it updates whenever the toolbar
  redraws (see "Toolbar redraws only on change" below). It always reflects the
  current rig state (or manual override), giving the operator live context for
  what band/mode will be used if Enter is pressed now. It mirrors the `TX ►`
  line printed above it.
- **TX line is reprinted on band/mode change**: the TX line (`TX ► MYCALL  RST  NR
  LOCATOR`) is a static `print()` rendered once per loop iteration, not part of the
  prompt_toolkit UI. RST depends on mode and NR depends on band, so both go stale if
  the rig changes while the prompt is waiting. Fix: `_toolbar()` detects band/mode
  changes and calls `get_app().exit(result=_REDRAW)` — safe because `_toolbar()` only
  ever runs on the event-loop thread (see below). This exits `session.prompt()`,
  re-prints the TX line with fresh values, and re-enters the prompt within about a
  second (bounded by `_toolbar_watcher`'s 10Hz poll of `current_rig()`).
  **Do not move RST or NR into the prompt prefix** — they are TX fields; mixing them
  into `RX ►` was tried and rejected as confusing.
- **Live rig status**: QRG and round clock update every second in the bottom toolbar.
  A band/mode change on the radio must be visible immediately in the prompt — never require
  Enter to see the updated state.
- **`■ NTP` is the settings half of the same question, and not redundant with
  `CLK`**: read at connect like the recorder settings. A radio whose server
  address has been reset still keeps good time for hours, drifting far too
  slowly to see, so `CLK` stays quiet while the sync is already gone -- the
  address is wrong the moment it is read. The address to expect is
  `IcomNetRig.local_addr`, the CI-V socket's own local address: whatever the
  radio reaches us on is by construction where its NTP queries must go, so
  there is no configured IP to fall out of date. `Alt+S` re-reads recorder and
  NTP settings together (`_settings_check_run`) and puts both in the one notice
  slot -- the chips stay separate so a lit one still says which subsystem.
- **The `CLK` chip is a measurement, not a setting read**: the radio syncs itself
  from this laptop over NTP (FINDINGS.md), and `_clock_monitor_run` checks that it
  worked rather than that it is configured. Since the radio reports only HH:MM, the
  offset exists to be read at exactly one instant a minute -- the rollover -- and
  the offset is the midpoint of the two replies that bracket it. Only the *first*
  rollover is hunted for at 1 Hz; after that the next one is predictable, so
  `_clock_window_wait` sleeps until just before it and a 20 Hz burst pins it to
  ±25 ms. Measured on the real radio: 53 queries in 200 s against ~200 for a
  continuous 1 Hz poll, and the burst's value differed from the same session's
  1 Hz acquire by 0.17 s -- the bracket error the burst removes. A burst that
  sees no rollover means the radio's clock stepped, so the offset drops to
  unknown and the next pass re-acquires.
  `CLK —` until the first rollover, yellow past `CLOCK_WARN_S`. The monitor outlives
  any one radio session (unlike the meter poller, which belongs to its session) and
  drops the reading to `None` while the radio is offline, because a stale offset
  would go on claiming the clocks agree.
- **Toolbar redraws only on change, not on a fixed timer**: `session.prompt()` used to
  pass `refresh_interval=0.1` (10Hz), which called `_toolbar()` — and therefore redrew
  the screen — unconditionally 10x/s, even though almost every tick produced
  byte-for-byte identical output (the clock only changes once a second; rig/rotator/
  webcam state changes far less often). Under `--cast` (asciinema recording of this
  terminal, see `contest_video.py`) every redraw is a recorded terminal-output event,
  so this meant ~10 recorded events/s for the whole round, nearly all redundant.
  `_toolbar_signature()` is a pure (no side effects) tuple of everything `_toolbar()`
  reads; `_toolbar_watcher(app)` polls it at the same 10Hz cadence (so a real
  second-boundary is still caught within ~100ms — why 10Hz was chosen over 1Hz in the
  first place) but only calls `app.invalidate()` when the signature actually differs
  from the last poll, cutting typical redraw frequency to roughly once a second.
  `app` here is `session.app`, captured directly once (right after constructing
  `session`) rather than fetched via `get_app()` inside the watcher — the watcher is
  created before `prompt_async()` runs, so the application is not the current one in
  its context yet. `_toolbar_watcher` never mutates state and never calls `_toolbar()`
  itself, so the band/mode-change `_REDRAW` logic above still only ever executes
  inside the real `_toolbar()` call that a triggered redraw causes.
- **Dup warning before Enter**: as soon as the callsign token is recognisable, the entire
  input line background turns red (`DynamicStyle({'': 'bg:ansired fg:white'})`) and the
  right prompt shows a red `DUP` label followed by the geo info (distance + bearing + arrow)
  if known. The operator must not need to press Enter to discover a duplicate. The dup check
  must re-evaluate when the band changes on the radio — the `icom_net` session pushes
  band/mode changes the instant they happen, so the style (redrawn on the next
  change-triggered toolbar redraw, see above) always reflects the current band. The dup style is suppressed during edit mode
  (`_state['edit_idx'] is not None`) to avoid false positives.
- **Band always visible in log**: every QSO row must show its band. RST columns are
  **left-aligned** in 3 chars (`:<3`) so `↑` and `↓` attach directly to the first digit
  and padding appears to the right (e.g. `↑59  021 ↓59  028` / `↑599 023 ↓599 030`).
  Right-alignment was tried and rejected — it created a visual gap between the marker and
  the digits (`↑ 59`). The `↑` prefix labels the sent exchange and `↓` labels the
  received exchange; both appear in every log row so TX and RX fields cannot be confused.
- **Rig read at Enter time**: band and mode for a new QSO are captured by a fresh
  `current_rig()` call immediately after Enter, never from the stale snapshot taken when
  the prompt was first drawn.
- **The radio task must never die**: `_radio_task` wraps each session in `try/except`
  and reconnects after drops (`RADIO_RECONNECT_S` cooldown — the radio refuses new
  sessions for a while after an uncleanly-dropped one), so a transient radio/network
  error cannot kill rig state permanently. Liveness comes from `last_rx_age()`
  (no CI-V-socket traffic for `RADIO_STALE_S` = session dead), not from polling.
- **The radio session is closed on every exit path, signals included**: a round
  ends by killing the tmux session (SIGHUP), which used to skip teardown entirely and
  leave exactly the abandoned session `icom_net.close`'s notes describe — the radio then
  streamed to a dead socket and refused the restarted logger for a minute or more, which
  is the "can't reconnect after exiting the logger" symptom. Three pieces, all needed:
  `_install_signal_handlers` handles SIGTERM/SIGHUP (teardown, then `os._exit` —
  EDI/telemetry/input/scope all flush as they are written, so nothing is lost by not
  unwinding), via `loop.add_signal_handler` so the teardown runs as an ordinary loop
  callback rather than between two bytecodes of whatever the main thread was doing;
  and `_round`'s `finally` covers Ctrl-D and a crash. Both go through
  `_radio_stop`, which **cancels the radio task and
  awaits it** — the task's own `finally` closes whatever session it had reached,
  including one still inside `connect()`, which is not in `_radio["rig"]` yet and so
  cannot be closed by clearing that slot. Found from a real capture after a
  kill-and-restart cycle, where every session had said goodbye correctly except one
  still-connecting one, which the radio then pinged for ~70 s.
  Verified end-to-end: three back-to-back logger runs, each killed with `tmux
  kill-session` and restarted one second later, all connected immediately and left zero
  packets on the wire.
  The `__main__` block ends in `os._exit(0)` for a related reason found the same way: a
  vanished terminal can leave prompt_toolkit's input thread blocked, and interpreter
  shutdown joins it, so the process hangs — deaf to SIGTERM, since a main thread that has
  already returned no longer runs Python signal handlers — while still holding the rig
  server port. One such process was found alive and unkillable-by-SIGTERM during this
  work; `kill -9` was the only way out.
- **Backspace stops at column 0**: pressing Backspace when the input buffer is empty does
  nothing. Edit mode is entered with the Up arrow key only.
- **Edit mode via Up/Down**: Up/Down navigate to earlier/later QSOs in edit mode.
  Escape exits edit mode. All three actions use `get_app().exit(result=_REDRAW)` to
  force a full screen redraw — this is the only way to scroll the printed QSO list while
  the prompt is active.
- **Scrolling edit view**: when editing, `_print_recent` shows a centered window (height
  determined by terminal size, same formula as normal mode) with the focused QSO highlighted
  as `> …` (bold) instead of `  …`. QSOs are shown both above and below the focused row so
  the operator can see surrounding context and is not misled into thinking QSOs outside the
  window have been deleted.
- **Edit preserves immutable fields**: dt, band, mode, nr_s, rst_s are kept from the
  original QSO; only the received side (callsign, rst_r, nr_r, loc) can change. Band and mode
  come from the original QSO, not the current rig state — this is intentional. Escape in
  edit mode triggers `_REDRAW` so the highlight clears immediately.
- **Edit mode isolates from rig changes**: while `_state['edit_idx'] is not None`,
  band/mode changes on the rig are recorded in `_rig` but do **not** trigger a REDRAW
  (which would clear the operator's half-entered input). The prompt prefix shows the
  edited QSO's own `q.band`/`q.mode`, not `current_rig()`. When the rig's current
  band or mode differs from the QSO under edit, the toolbar prepends a yellow
  `RIG→BAND MODE │` indicator so the operator is visually notified without their
  input being interrupted.
- **Header band summary is compact**: format is `{band}:{count}q/{pts}pt` (e.g.
  `2M:12q/4321pt  70CM:3q/891pt`) so the full three-band line fits within the 80-character
  header width (`W = 80`, matching the CW legend line). Points = sum of `dist_km` for
  non-dup QSOs (matches EDI `CQSOP`).
- **My-exchange line**: printed in bold bright green between `_print_header` and
  `_print_recent` in `run()`. Format: `TX ► MYCALL  RST  NR  LOCATOR` (e.g.
  `TX ► HA5LA  59  010  JN97TF`). RST is `599` in CW mode, `59` otherwise.
  Stays accurate because a band/mode change triggers a full REDRAW (see above).
- **QSO list fills the terminal**: `_print_recent` receives `n = max(3, rows - 9)` where
  `rows = os.get_terminal_size().lines` (falls back to 24). The constant 9 accounts for the
  fixed header lines (blank, two bars, summary, legend, my-exchange, separator, prompt, toolbar).
- **CW abort on first Escape**: Escape must abort an in-progress CW transmission on the
  very first keypress with no perceptible delay. prompt_toolkit's default `ttimeoutlen`
  of 0.5 s causes a half-second lag — set it to `0.05` s via `pre_run` on every
  `session.prompt()` call. Escape must also call `_cw_stop()` before checking
  `buf.complete_state`, so it fires even when a completion menu is open.
- **CW number abbreviation**: the `<NUMBER>` placeholder in CW macro templates must
  substitute `0→T` and `9→N` (e.g. serial 014 → `T14`). This is standard contest CW.
- **Toolbar layout**: bottom toolbar shows QRG (e.g. `144.174 MHz`) when rig is online, or
  `offline`, plus `ROT: 045°` (current rotator azimuth) or `ROT: ---` when rotctld is
  offline, plus a colour-coded UTC clock. Clock background is **green** during the round
  window (first Monday of each month, 18:00–20:00 CET/CEST) and **red** at all other times.
  Band and mode are intentionally absent from the toolbar — they live in the prompt prefix.
- **Alt+B / Alt+M**: cycle band / mode through `_BANDS`/`_MODES` tuples when rig is offline.
- **Alt+R**: point the rotator at the bearing of the currently selected station. In edit mode
  (Up/Down to navigate) the bearing comes from the focused QSO's locator; in normal mode it
  comes from the first known locator of the callsign being typed. Silently no-ops when rotctld
  is offline or no bearing is available.
  When the rig is online these keys are **denied**: `_state['warn_until']` is set to
  `time.monotonic() + 2.0` and the toolbar flashes a yellow `rig online — Alt+B/M ignored`
  message until it expires. The rig is always the primary source; `_rig_manual` is only
  consulted by `current_rig()` when `_rig["online"]` is False.
- **Alt shortcuts are Alt+letter, never Alt+function-key**: `kb.add("escape", "<letter>")`
  is unambiguous on the wire — a bare ESC followed by one printable byte, which
  prompt_toolkit resolves against the single `"escape"` binding. A function key already
  sends its own ESC-prefixed sequence (F3 is often `ESC O R`), so Alt+F3 arrives as a
  nested double-ESC that parses ambiguously: an `escape, "f3"` CW-macro binding corrupted
  the terminal screen mid-round — everything but the input line vanished, and only a
  restart brought it back. New shortcuts follow the Alt+B/M/R/S/V mnemonic pattern.
- **Bearing arrows**: every bearing value (in the QSO list and in the rprompt) is followed
  by a Unicode direction arrow from `_BEARING_ARROWS = "↑↗→↘↓↙←↖"`, selected by octant.
  `_bearing_arrow(degrees)` must exist in `puskas_logger` — it was once missing and the
  silent `except Exception: pass` in `_rprompt` caused the entire geo display to vanish
  without any error.
- **Locator is mandatory**: every QSO must have a valid Maidenhead locator (contest rule).
  `parse_input` enforces this on live input. `load_from_edi` enforces it too — records
  without a valid locator in field[9] are silently dropped. Do not add optional handling for
  missing locators; the invariant is that `q.loc` is always a valid, non-empty string.

## puskas_logger.py – Contest QSO Logger

Purpose-built for Puskás URH Kupa rules. Requires `prompt_toolkit` (declared in `pyproject.toml`).

```
uv run puskas_logger.py
```

### Where the code lives

`puskas_logger.py` is the UI and the radio session — the two things that cannot
usefully be pulled apart from it. The UI is verified by running rounds rather
than by tests ("no visual glitches" is a real requirement no unit test asserts),
and the radio's single network session is what the logger exists to own.

| Module | What it owns |
|---|---|
| `urhpk/logbook.py` | QSOs, duplicates, scoring, and the EDI export and crash-recovery read |
| `urhpk/loc_cache.py` | Which locator a callsign uses, merged from three sources |
| `urhpk/recorders.py` | The round's side-channel files: telemetry, input box, scope, webcam |
| `urhpk/rotator.py` | The rotator poll task, the current bearing, "point there" |
| `urhpk/rig_server.py` | The rigctld dialect on 4532, answering from a state snapshot |

`rig_server.serve` takes a `snapshot()` callable rather than reading the
logger's `_rig`, which is what lets it live outside the file holding that state.

**Locator cache** — built at startup by merging four sources in priority order (highest first):

| Priority | Source | How |
|---|---|---|
| 1 (highest) | QSOs entered this round | `loc_cache.remember` called after each logged/edited QSO |
| 2 | Recovered EDI files (crash recovery) | `loc_cache.remember` called for each recovered QSO in `main()` |
| 3 | `my-logs/*.edi` historical logs | `loc_cache._from_my_logs()`, via `edi.read`, always merged |
| 4 | `~/.puskas/on4kst-seen-stations.json` | merged second |
| 5 (lowest) | `~/.puskas/puskas-seen-stations.json` | merged last |

`loc_cache.merge_sources(*sources)` takes sources highest-priority-first; each locator
appears once at the position of its highest-priority source. `loc_cache.remember(cache,
callsign, loc)` inserts `loc` at the front of `cache[callsign]` (most recently used first).
No API calls during a round.

**Crash recovery**: at startup, scans `*.edi` / `*.EDI` (case-insensitive) in the current
directory. If found, shows a summary and offers to resume — all QSOs, serials, and dup state
are rebuilt from the EDI records. EDI files are the sole persistence format (no round file).
Files are saved as lowercase `YYMMDD-CALL-BAND.edi`, and `load_from_edi` matches
case-insensitively and deduplicates by stem — a directory holding both spellings of one
log must never load it twice.

**Input format**: `CALL RST NR LOC` (locator is mandatory)
```
HA7NS 59 015 JN97WM    → SSB with locator
HA7NS 599 014 JN97WM   → CW with locator
```

**UX shortcuts**:
- Tab-complete callsigns (prefix-match from locator cache)
- Tab-complete locators after NR: shows all known locators for the callsign in
  reverse-chronological order (most recently used first)
- Space after callsign → auto-fills RST (59 or 599); if there is a recent cross-mode
  QSO (same callsign, same band, different mode, within **5 minutes**) the predicted received
  NR (`last_nr_r + 1`) is also filled (`_predict_nr` with injectable `now` parameter).
  When NR is predicted no trailing space is appended — the operator's next Space press
  both separates NR from locator and triggers locator autocomplete (single clean separator).
  When NR is not predicted, a trailing space after RST is added so the operator can type
  NR directly without pressing Space again.
- Space after NR → if one locator known: inserts it directly; if multiple: opens dropdown
- Right-prompt shows bearing and distance in green (e.g. `JN97WM  1234 km  225° ↙`) as soon
  as a known callsign is typed; when the callsign is a DUP both the red `DUP` label and the
  green geo info are shown together — geo is never suppressed
- Right-prompt also shows, in **bright** red (`ansibrightred`), the band/mode combos
  already worked with this callsign this round (e.g. `2M:SSB,CW 70CM:CW`), grouped by
  band — `LogBook.worked_combos(callsign)` checks all 9 (3 bands × 3 modes). Red because
  these are the combos that would be dups; naturally empty (and so hidden) for a
  brand-new callsign with nothing worked yet. **Must be `ansibrightred`, not plain
  `ansired`**: when the current band/mode is itself a dup the whole input line
  background turns `ansired` (see the dup style below) and that background reaches the
  rprompt, so plain-red text would be red-on-red and invisible there — the brighter red
  stays legible on both the default dark background and the dup background. Coexists
  with the `DUP` label — if the current band/mode is itself a dup, both show together,
  same as geo info. (This replaced an earlier version that showed the *open* combos in
  yellow — the operator wanted to see what's already in the log, not what's missing.)
- Backspace stops at column 0 (does nothing on empty input); edit mode via Up arrow only
- Up/Down → navigate log in edit mode; window scrolls to keep focused row centred
- Escape → exits edit mode (screen redraws immediately) and/or aborts CW transmission
- Alt+R → point rotator at bearing of selected/typed station (no-op when rotctld offline)
- Alt+S → confirm the radio's Voice Recorder is running (see **Recording warnings** below)
- Alt+V → start/stop webcam recording (see **Webcam capture** below); toolbar shows a red
  `● REC` indicator the whole time it's running, plus a transient confirmation message

**CW macros** (F1–F8, requires the radio online — sent via `icom_net`'s `send_cw`,
CI-V 0x17, the radio's own message keyer):
| Key | Template |
|-----|----------|
| F1  | `CQ <MYCALL> <MYCALL> TEST` |
| F2  | `<MYCALL>` |
| F3  | `5NN <NUMBER> <LOCATOR>` |
| F4  | `TU` |
| F5  | `<HISCALL>` |
| F6  | `DE <MYCALL>` |
| F7  | `?` |
| F8  | `282 282 SSB` |

`<HISCALL>` is the first token in the input buffer at key-press time.
`<NUMBER>` uses CW abbreviations: `0→T`, `9→N` (e.g. 014 → `T14`).
Macros silently no-op when the radio is offline. Escape aborts via `stop_cw` (0x17+0xFF).

**Band and mode without a radio**: `_rig_manual` is what `current_rig()` answers with
whenever the rig is not online, and it is never empty — so a QSO can always be logged.
It holds `START_BAND`/`START_MODE` until a radio has been seen, then whatever band and
mode the last live session was on, so a mid-round disconnect goes on logging where the
operator actually is rather than dropping back to the starting band. **Alt+B / Alt+M**
cycle it while offline, and are ignored (with a toolbar warning) while the rig is online.

**The round waits for the radio's first verdict** (`_await_radio`): the login handshake
takes several round trips, so at the instant a round starts the rig is always still
offline. `_radio["probed"]` is set when the first connect attempt resolves — either the
rig reported freq and mode, or the attempt failed — and the UI is not drawn until then.
Without it the toolbar shows `START_BAND` for a radio that turns out to be on another
band, and a QSO logged in that window is filed under the wrong one. Bounded by
`RADIO_CONNECT_TIMEOUT_S`, since a failed attempt resolves it too.

**rotctld integration** (optional, no-op when rotctld not running):
- Background poller (`rotator.poll`) queries `ROTCTLD_HOST:ROTCTLD_PORT` (4533) every
  `ROTCTLD_POLL_S` (1 s) using the `p` command (returns azimuth and elevation)
- Current azimuth shown in toolbar as `ROT: 045°` when online, `ROT: ---` when offline
- **Alt+R** sends `P az 0` to rotctld to slew the rotator; fires as a background task
- To start rotctld: `rotctld -m MODEL -r /dev/ttyUSB0` (see Hamlib docs for MODEL number)

**Rig server** (port 4532, always on): serves the rigctld TCP dialect (`f` → freq in
Hz, `m` → raw dial mode + passband, offline → `RPRT -1`) from the push-fresh `_rig`
cache — this is how `on4kst_irc_bridge.py` gets QRG/mode now that rigctld is gone,
with zero bridge protocol changes (the port is an interface: a real rigctld could
still serve it when the logger isn't running). Serves the *raw* mode (`USB`), not the
contest-normalized one (`SSB`), matching real rigctld byte-for-byte. Binds localhost
only; silently serves nothing if the port is already taken.

**Scope recorder** (`YYMMDD-CALL.scope`, always on once the radio connects): records
the radio's own sweeps via `enable_scope()`/`on_scope` to the `.scope` format
`contest_video.py --scope` consumes. Lives in the logger because the radio holds only
one network session (see the icom_net.py section above) — the CLI harness recorder can't run
alongside the logger. Re-enabled on every reconnect (scope data output is
session-scoped on the radio's side); file is lazily opened on the first sweep and
flushed per sweep. Measured on real hardware: 29.4 sweeps/s at 493 bytes each
(18-byte header + 475 pixels) = ~14.5 kB/s, so **~105 MB per 2 h round**.

**Telemetry recorder** (`*-telemetry.jsonl`, always on, **one JSON line per
actual change**, microsecond stamps): records are *partial* by source --
`{"t", "freq_hz", "mode"}` written from the `icom_net` push callback,
`{"t", "az"}` written by the rotctld poller. A field a line doesn't mention
simply didn't change; `contest_video.py`'s `build_state_events` carries each
one forward across the events that don't mention it.
- **Change-driven, not sampled**: the `icom_net` push callback *is* the change
  stream (`_apply_update` already returns early on a no-op), so re-sampling it
  on a timer would put back exactly the latency `icom_net` exists to remove,
  blur any retune shorter than the interval, and write overwhelmingly duplicate
  lines. **`freq_hz` is the radio's own exact value**, never a re-parse of the
  toolbar's kHz-rounded string — that rounding was the whole of the old "WAV vs
  telemetry" frequency disagreement (see FINDINGS.md).
- **`az` stays polled, but change-gated**: it has no push source at all, since
  Hamlib's rotator API never defined the async hook for any backend (see
  `hamlib_supervisor.py`'s notes). Plain inequality, **no deadband** — the
  rotator reports whole degrees, so there is no sub-degree jitter to suppress;
  checked against the real August round, where every one of the 756 azimuth
  changes was >= 1.0°.
- **Offline transitions are events too, but only on a real transition**: the
  radio going offline writes one `{"freq_hz": null, "mode": null}` line, gated
  on having been online — `_radio_task`'s loop also spins while the radio has
  simply never been reachable, and a null line every `RADIO_RECONNECT_S` would
  say nothing new.
- Opened in `main()` **before** the radio and rotator tasks start, since both
  write to it the moment they have something and the radio's first push lands
  within a second of connecting. No lock: both writers are tasks on the one
  event loop, so an append cannot interleave with another.
- **`clock_offset_s` is the one record written on a timer rather than on a
  change**, every few minutes: how far the radio's clock leads this laptop's. It
  is a measurement, not a state -- its noise floor is half the monitor's burst
  interval, so change-gating it would gate on noise, and a flat line is exactly
  the reassurance wanted afterwards when a WAV and the EDI seem to disagree.
- No `ptt` field: it used to be queried and recorded here too, but the WAV
  recordings' own IC-9700 metadata already carries it straight from the rig
  with zero polling lag (see `contest_video.py`'s `read_wav_metadata`) -- this
  was in practice reconstructing, with more latency, something already recorded
  losslessly elsewhere, so it was removed rather than kept for redundancy.

**Input-box logging** (`*-input.jsonl`, always on, feeds `contest_video.py --input-log`):
- Event-triggered, not polled: `session.default_buffer.on_text_changed` fires
  `_on_buffer_changed`, which appends `{"t": <UTC with microseconds>, "event":
  "text", "text": <full current buffer>}` to `YYMMDD-CALL-input.jsonl` on every
  keystroke. Sampling on any interval would blur or entirely miss fast typing,
  and the buffer only changes on a keypress in the first place, so there's
  nothing to poll.
- Microsecond precision matters here: it timestamps individual keystrokes.
  (`contest_video.py` parses the `'text'` events but no longer draws anything
  from them — the terminal PiP shows the real typing — so they exist for a
  future consumer, not a current one.)
- **A second event kind, `"event": "qso"`, is written from the "New QSO"
  block** in `run()` — one line per QSO actually appended to the log:
  `{"t": ..., "event": "qso", "call", "band", "mode", "nr_s", "dup"}`. This is
  deliberately *not* inferred from the `"text"` stream (Enter-submit,
  Ctrl+U/unix-line-discard, and Escape-abort all just clear the buffer the
  same way — see the long comment above `recorders.input_log_open` for why that's
  unreliable). It's written from the one place in the code that unambiguously
  knows a QSO was logged, right next to `lb.add(qso)`. `now = datetime.now
  (timezone.utc)` is captured **once** and used for both `qso.dt = now.replace
  (second=0, microsecond=0)` and this event's `t` — not two separate
  `datetime.now()` calls — so the two are *always* related by exact minute
  truncation with no possible race at a minute boundary. This is what lets
  `contest_video.py`'s `match_qso_times` line them up exactly; it is the fix for
  "weird QSO timing" in a preview, where the EDI's minute-only precision let
  `_snap_to_burst` occasionally pick the wrong neighbouring burst.

**Recording warnings** — the toolbar carries a block per recording that ought to be
running, because a round that was not recorded cannot be re-run:
- **`SD ✗` (red) / `SD ●`** — the radio's Voice Recorder, toggled by Alt+S. This
  one is the operator's word, not a measurement: the IC-9700's CI-V command table
  has no start/stop and no in-progress status for the recorder (FINDINGS.md), so
  the block starts red every round and only Alt+S clears it.
- **`■ REC SET` (yellow)** — the recorder's *settings* are readable, and two of them
  decide whether the segments are usable: `File Split` must be ON (it is what makes
  a segment boundary an RX/TX transition, which `qso_windows.py` depends on) and
  `RX REC Condition` must be `Always`, not the radio's own default `Squelch Auto`.
  Read once per radio connect via `icom_net`'s `read_param`; Alt+S re-reads and
  spells out which one is wrong. The connect-time check deliberately posts no
  notice — the logger's pane is ~95 columns (half of a 191-column terminal, see
  `run-recorded-round.sh`) and a notice would crowd the clock off it, so
  nothing appears in this toolbar unbidden.
- **`● REC` / `NO WEBCAM` (yellow) / `WEBCAM DIED` (red)** — from
  `recorders.webcam_status()`. `webcam_reap()`, called by `_toolbar_watcher`, is what
  notices an ffmpeg that exited on its own; without it a capture that died at 18:20
  would keep showing `● REC` for the rest of the round.

**Glyphs in this toolbar are limited to what DejaVu Sans Mono has** — ● ■ ⚠ ✓ ✗ —
because the pane is replayed into the video by `cast_render.py`, which draws it with
that font (`CAST_FONT_PATH`). Emoji like 📷 or 💾 look right in the terminal and come
out as tofu boxes in the rendered video; they are also double-width in some terminals,
which shifts every column after them. (Unicode has no SD-card character at all, so
`SD ✗`/`SD ●` is the closest thing available regardless.)

**Webcam capture** (`YYMMDD-CALL-webcam.mp4`, renamed ~1 s in — see below — Alt+V to
start/stop, off by default):
- Capturing on the *same machine* that runs the logger is the whole point: a
  phone propped up separately has its own clock, which `contest_video.py` then
  has to reconcile by audio cross-correlation (see FINDINGS.md). Here the start
  time is simply known.
- `_webcam_capture_cmd` builds the `ffmpeg -f v4l2 ... -f pulse ...` command (Linux
  video4linux2 + PulseAudio); `WEBCAM_DEVICE`/`WEBCAM_AUDIO_SOURCE` constants at the
  top of the file are the only things that need adjusting for a given machine (find
  with `v4l2-ctl --list-devices` / `pactl list short sources`). `-preset ultrafast`
  keeps the encode cheap enough to run alongside the radio, the rotator and the UI for a
  multi-hour round without competing for CPU.
- Stop sends `SIGINT` (not a hard kill) so ffmpeg finalizes the mp4 properly; a
  5 s `wait()` with a `terminate()` fallback guards against ffmpeg hanging. Also
  triggered automatically on exit (`_webcam_stop_if_running`, both the normal
  Ctrl-D path and the crash-handler path in `main()`) so a still-running capture
  is never left orphaned or its output file unfinalized.
- **Renamed with a µs-precise timestamp** (`webcam_finalize_name` +
  `_webcam_precise_name`), e.g. `260706-HA5LA-webcam-20260706T160037.123456Z.mp4`.
  The true frame-0 wallclock isn't known until the camera has actually opened
  (~1 s after spawn, variable), so it cannot be passed to ffmpeg up front; tagging
  it into the container afterwards works but needs a second copy of a multi-GB file
  on disk exactly when space is tightest (see FINDINGS.md). A rename is a
  directory-entry update, independent of size. `contest_video.py` reads it with
  `parse_webcam_precise_filename` and has no `*-webcam.log` path at all: the log
  is the logger's own input to the rename, not a sidecar the video depends on.
- **The rename happens while the capture runs**, on the toolbar's 10 Hz tick, as
  soon as the log carries frame 0 — not at stop, so a power cut or a `kill -9`
  (where nothing gets to run at the end) still leaves the timestamp on the file.
  ffmpeg writes on through it: an open fd follows the inode, not the name, and
  the moov atom it seeks back to finalize lands in the renamed file (verified by
  renaming a live capture mid-run, then decoding a frame from after that point).
  `_webcam_finish` calls it once more for a capture that died between ticks.
- **The `*-webcam.log` is renamed with the mp4**, to the same stem. Not
  cosmetic: a second Alt+V capture in the same round reopens `<prefix>-webcam.log`
  in append mode, and `frame_zero_utc` takes the *first* `Input #0` start it
  finds — a log left behind under the un-stamped name would stamp the second
  recording with the first one's start time.
- Logs `"event": "webcam_start"` / `"webcam_stop"` to the same `*-input.jsonl` as
  everything else (see **Input-box logging** above) rather than a separate file —
  one more consumer of the same already-precise event log, not a new format.

**Contest rules**:
- Reads band/QRG/mode from the radio via `icom_net` (push updates, no polling); falls
  back to Alt+B/Alt+M if the radio is offline
- RST defaults: `59` for SSB/FM, `599` for CW
- Serial auto-increments per band; all QSOs (including dups) get a serial
- Dup check key: `(callsign, band, mode)` — 9 valid combos per station (3 bands × 3 modes)
- Dup QSOs shown in red and EDI-flagged `D`
- Auto-saves EDI after every QSO; files named `YYMMDD-CALL-BAND.EDI` in current directory

**Commands**: `!undo`, `!help`  
Ctrl-D → final save and exit

EDI export: one file per band, `[REG1TEST;1]` format compatible with bb.mrasz.hu submission.
