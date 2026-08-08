# Puskás URH Kupa – project context

## What is this?
Amateur radio contest (Puskás URH Kupa) toolset plus a general ON4KST bridge:
- `on4kst_irc_bridge.py` – general ON4KST↔IRC bridge; use with irssi or any IRC client
- `puskas_logger.py` – contest QSO logger; rig via `icom_net` (direct Ethernet, push updates)
  plus rotctld integration; exports EDI files
- `puskas_harvester.py` – pre-contest data collector; fetches all stations → `~/.puskas/puskas-seen-stations.json`
- `puskas_visualizer.py` – map and polar diagram from `~/.puskas/puskas-seen-stations.json`
- `hamlib_supervisor.py` – starts/stops rotctld based on USB device presence (inotify);
  used to manage rigctld too, until the rig moved to direct Ethernet (`icom_net`)
- `icom_net.py` – direct Ethernet CI-V client for Icom radios (IC-9700), bypassing rigctld; instant
  push freq/mode updates instead of polling, plus real spectrum-scope sweep capture
- `scope_preview.py` – standalone preview: renders a `.scope` recording into a waterfall video

### The other documents
| File | What belongs in it |
|---|---|
| **CLAUDE.md** (this file) | Rules, invariants and current architecture — what someone editing the code must not break |
| **README.md** | The public face: components table and quick start |
| **RECORDING.md** | How to actually record a round and produce a video; the video pipeline's behaviour and tuned constants, with real numbers |
| **FINDINGS.md** | Measurements, protocol archaeology and dead ends — the evidence behind the rules here |
| **hud-artwork-prompt.md** | The generation prompt and layout spec for the HUD artwork |

## Housekeeping reminders
- When adding or removing components, update the components table in **README.md**
- Keep the four documents above in their lanes. In particular: **research narrative
  and rejected approaches go to FINDINGS.md, not here**, and history that only
  explains how the code *used to* look goes nowhere — git keeps it. This file was
  pruned from 2157 lines once already for exactly that reason.

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
  second-best option. Prose-only requirements in CLAUDE.md are a last resort for things
  that genuinely cannot be tested (visual UX, hardware interactions).
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
  `datetime.now(timezone.utc)`) so tests can inject a fixed value via `_dt(h, m)`.
- **Tests don't sleep a guessed duration — they wait for the real condition**: the same
  "time is an input" rule applies to async/background-task synchronization, not just
  wall-clock timestamps. `await asyncio.sleep(0.1); assert X` (sleep a hand-picked
  duration, hope it was enough, then check) is both slow (every test pays the full
  guessed duration even when the real work finishes in microseconds) and fragile (too
  short → flaky on a loaded machine; too long → wastes time and can still be wrong).
  Fix: poll the actual predicate — `tests/helpers.py`'s `wait_until`/`wait_until_sync`
  return the instant the condition holds, with a generous `timeout` as a safety net for
  genuine failure only, not the expected wait. Prefer an even stronger fix where the
  output has a deterministic terminator: `on4kst_irc_bridge.py`'s IRC registration flow
  always ends in numeric 366, so tests `recv_until("366")` instead of draining on a
  "quiet for N ms" heuristic — zero guessing at all, not just a tighter guess. Only
  genuine negative assertions ("nothing arrives") still need a real bounded sleep, since
  there's no true condition to poll for proving an absence. Adopting this cut the
  suite from ~29s to ~3.5s and exposed one real race the old slack had been hiding.
- **No visual glitches**: the logger UI must look professional at all times. Transient
  incorrect states (e.g. a dup highlight flashing for one frame during a state transition)
  are bugs. The root cause is usually a final prompt_toolkit render that fires between a
  key handler updating `_state` and the next loop iteration clearing the screen. Fix:
  clear the buffer with `buf.set_document(Document(''))` before calling
  `get_app().exit(result=_REDRAW)` whenever leaving edit mode, so the final render sees
  an empty buffer and has nothing to mis-highlight.

## Credentials / locator
- Callsign and password: `~/.netrc` (`machine www.on4kst.info login ha5la password ...`)
- Callsign is read from `.netrc` at startup (uppercased), **not hardcoded**
- Grid locator is fetched from the server via `/SHow CONFig` after login, **not hardcoded**

## on4kst_irc_bridge.py – architecture
- **General** ON4KST↔IRC bridge with optional Puskás URH Kupa sked support
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
  (`{call: {wwls: [most_recent, ...], bands: []}}` — same format as `puskas-seen-stations.json` in `~/.puskas/`
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

irssi quick-start:
```
/server add -auto -network on4kst localhost 6667
/save
/connect on4kst
```

### Taskbar blink on private message (irssi + tmux over SSH)

irssi emits a BEL character for incoming PMs; the chain is:
irssi → tmux → SSH terminal → taskbar flash.

**irssi** (`/set beep_msg_level` still works; `bell_beeps` was removed in 2016):
```
/set beep_msg_level MSGS HILIGHT
/save
```

**tmux** (`~/.tmux.conf` on the Pi) — by default tmux swallows BEL and shows `!`
in the status bar; this passes it through to the outer terminal instead:
```
set -g bell-action any
set -g visual-bell off
```
Reload: `tmux source ~/.tmux.conf`

**Terminal emulator on the laptop** — most set the WM_URGENT hint on BEL,
which causes the taskbar entry to flash:

| Terminal | Setting |
|---|---|
| gnome-terminal | Preferences → Profile → Command → *Urgent on bell* |
| Konsole | Settings → Edit Profile → Scrolling → Bell → *Flash taskbar entry* |
| xterm | `XTerm*bellIsUrgent: true` in `~/.Xresources`, then `xrdb -merge ~/.Xresources` |
| kitty | `enable_audio_bell yes` (WM handles the urgent hint automatically) |

### Highlighting the irssi window itself on private message (tmux)

The taskbar flash above only helps when looking away from the terminal — sked
requests were noticed late even while the tmux session was on-screen, just on
the logger window instead of irssi's. tmux can highlight the *window* itself
in its own status bar the moment the same BEL (already sent for PMs/highlights,
see above) arrives on a window that isn't currently focused:
```
set -g monitor-bell on
set -g window-status-bell-style fg=black,bg=red
```
Reload: `tmux source ~/.tmux.conf`. Complements (doesn't replace) the
taskbar-flash chain above — this one catches it even without ever leaving the
tmux session.

## File layout

The whole stack (bridge, logger, harvester, visualizer, hamlib supervisor) runs on the
same laptop during the contest — no separate always-on host. File locations follow a
simple rule: **global databases live in `~`, per-session files live in CWD**.
- `~/.puskas/puskas-seen-stations.json` — harvested Puskás station database (all rounds, accumulates)
- `~/.puskas/on4kst-seen-stations.json` — ON4KST session database (written by the bridge)
- `.puskas_cache/` — API response cache (CWD, delete to force a fresh fetch)
- `*.edi` — contest QSO logs (CWD, one file per band per session)

Run the contest tools from a contest directory:
```
mkdir ~/contest-2026 && cd ~/contest-2026
uv run puskas_harvester.py     # fetch ~/.puskas/puskas-seen-stations.json
./run-recorded-contest-session.sh   # right before the round: irssi + logger (recorded),
                                     # hamlib_supervisor.py + bridge in a background window
uv run puskas_visualizer.py    # generate map/polar from ~/.puskas/puskas-seen-stations.json + my-logs/
```

## puskas_harvester.py – Pre-contest station harvester

Run once before the contest to build `~/.puskas/puskas-seen-stations.json`:
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
- Output: `~/.puskas/puskas-seen-stations.json` — `{call: {wwls: [most_recent, ...], bands}}`
  where `wwls` is a list of all known locators in reverse-chronological order (most recently
  observed in any Puskás round appears first)
- All API responses cached in `.puskas_cache/`; delete it to force a fresh fetch

## puskas_visualizer.py – Map and polar diagram

```
uv run puskas_visualizer.py [CALLSIGN LOCATOR]
```
- Loads `~/.puskas/puskas-seen-stations.json` (built by harvester)
- Loads own log EDI files from `my-logs/` for callsign, locator, and worked-station marking
- Generates `puskas_map.html` (interactive Folium map) and `puskas_polar.png` (polar scatter)
- Missed stations (in seen_stations but not worked) shown in red on map
- Dependencies: `folium`, `matplotlib`, `numpy`

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

## icom_net.py – Direct Icom Ethernet CI-V client

The IC-9700 is reachable over Ethernet, but there is no plain "CI-V over TCP" port
on the radio: the only way in is Icom's own network-remote-control protocol (what
RS-BA1 and wfview speak) — UDP, authenticated, stateful. This is a minimal client
for it in pure stdlib Python. `puskas_logger.py` uses it as its **only** rig
interface; it is also usable standalone (`uv run icom_net.py <radio-ip>`).

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
- `IcomNetRig` uses `threading.RLock`, not `Lock` — the CI-V thread re-enters it.
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
  during a contest — see its rig server), and the CLI harness must never run while
  the logger is up.

**rigctld-parity commands**, as plain CI-V writes: `send_cw()` (0x17 + ASCII, 30-char
limit), `stop_cw()` (0x17 + 0xFF), `set_clock()` (0x1A 0x05, IC-9700 parameters 0184
UTC-offset / 0180 time / 0179 date, packed BCD) — byte layouts transcribed from
Hamlib's own `icom_send_morse`/`icom_stop_morse`/`icom_set_clock`. `set_clock` was
verified against the radio: all three commands ACKed and the read-back matched.
`on_civ_frame()` exposes every raw inbound CI-V frame, which is how a caller sees
ACKs (FB ok / FA rejected); note the radio echoes the client's own frames back, so
distinguish direction by the address bytes.

**Test coverage**: `tests/test_icom_net.py` covers the pure functions (passcode, BCD,
frame parsing) with no mocking; `tests/test_icom_net_integration.py` runs the full
`connect()` handshake against an in-process fake radio and injects an unsolicited
Transceive frame to assert push updates with no polling — that is what caught the
`RLock` deadlock in CI. What it *cannot* catch: anything about the real radio's own
session/sequence tracking, since a fake that mirrors the client's assumptions back
can't contradict them.

**Not implemented, deliberately**: audio streaming (conninfo requests
`rxenable=0`/`txenable=0`), PTT/transmit control, and general retransmit-*request*
compliance (resending a specific buffered packet on demand) — skippable on a clean
LAN, confirmed in steady-state traffic. What is *not* skippable is the initial seq
numbering and handshake ordering above.

### Scope (spectrum waterfall) data

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
  `contest_video.py` and `scope_preview.py` both read it through that, neither
  reimplements the parser.

### Meters

`enable_meters()` polls Po/SWR/Vd/Id (`CIV_METERS`) at `METER_POLL_S` (0.5 s) and
reports one snapshot per cycle via `on_meters`, rather than firing per reply — that
keeps the four values in a record genuinely simultaneous and lets a recorder write
one line per cycle instead of four. Off unless asked for, like `enable_scope`, and
re-armed on every reconnect since the poll thread belongs to its session.

Polling is unavoidable here: the radio only reports meters when asked. **The
S-meter (`15 02`) is deliberately not polled** — `contest_video.py` derives signal
level from the scope recording's own centre bins, which costs no extra traffic and
is already captured.

**Store raw meter values and convert at render time**, so a better calibration is a
one-line change rather than a ruined recording. Curves and the measurements behind
them are in FINDINGS.md.

## contest_video.py – Annotated CW contest video

Turns a contest recording plus its EDI log into a YouTube-ready MP4: a scrolling
waterfall background, a DOOM-style HUD status bar, and picture-in-picture of the
logger's own terminal session and the operator's webcam.

```
uv run contest_video.py RECORDING_DIR EDI_FILE [EDI_FILE ...] [-o OUT.mp4]
```
Dependencies: `numpy`, `pyte`, `pillow` (uv script header) + `ffmpeg`/`ffprobe`.

**RECORDING.md is the companion document** — the full option list, the CW decoder's
tuned constants and the reasoning behind the QSO-timing heuristics live there, with
real numbers from real sessions. Keep it current; this section is only what someone
*editing the code* has to know.

### Inputs

- A directory of WAV segments named `YYYYMMDD_HHMMSS...wav` (local time), split by
  the radio on every RX/TX switch. They are contiguous, so **the audio timeline is
  the sum of segment durations**; filename wall-clock is used only to line QSOs up
  against the audio. All segments must share one sample rate/format (they are
  concatenated with `ffmpeg -f concat -c copy`).
- **Multiple EDI files merge into one timeline** — a session worked across bands
  writes one EDI per band but is still one physical recording. `merge_edi`
  concatenates and sorts by `dt`. `Qso` carries no band field; band only ever
  mattered for logging, not rendering.
- **UTC offset is derived, not hardcoded**: EDI times are UTC, WAV filenames local;
  `derive_utc_offset` rounds the span-midpoint difference to whole hours, so DST
  handles itself.
- **WAV metadata is ground truth for RX/TX and the starting QRG/mode.** The IC-9700
  writes a `title` tag into every file with frequency, mode and RX/TX as of the
  instant it started recording — no polling lag at all. `_read_wav_title` parses the
  RIFF `LIST/INFO/INAM` chunk directly rather than shelling out to `ffprobe` per
  file (6500× faster; see FINDINGS.md). `ptt` therefore needs no telemetry: it
  cannot legitimately change mid-segment, since a real transition is exactly what
  cuts a new file. freq/mode *can* change inside a long listening segment, which is
  what telemetry sub-divides (`build_state_events`), seeded from the WAV value.
- **Telemetry (`--telemetry`) is optional and partial by source.** Records mention
  only what changed — `{"t", "freq_hz", "mode"}` from the rig, `{"t", "az"}` from
  the rotator — so every field carries forward across the records that don't
  mention it. Three rules that each cost a bug:
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
  - **Frequencies are compared with `FREQ_MATCH_TOLERANCE_HZ` (500), never exactly**
    — old recordings carry a systematic sub-kHz disagreement against the WAV value
    that would otherwise look like a retune at the start of almost every segment.
    New recordings don't (the cause was our own rounding — see FINDINGS.md), but the
    tolerance stays for the old ones. Mode has no such problem; it is an exact
    string match.

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
- **`decode_long_segment` recovers CW hidden inside a long segment** — our recorder
  only splits on *our* PTT, so listening to two other stations work each other stays
  one long file. It decodes the telemetry-confirmed CW sub-ranges within it, with
  the duration gate disabled (`check_duration=False`): mode confirmation is stronger
  evidence than length, and a two-way exchange between others can legitimately run
  longer than one of our overs. Its output is kept **out** of `s.events`, so
  `--skip-gaps` needs an explicit `long_cw_segs` exemption or `remap_audio_t` would
  trim away the very audio just recovered.
- **`--duration SECONDS` trims before the decode loop**, not after — decoding is the
  dominant cost, so a 10-minute preview of a 2-hour session decodes ~12× less audio.
  QSOs past the cutoff are dropped before chapters/SRT are built.
- Also writes `<out>.chapters.txt` (paste into the YouTube description; first
  chapter must be `0:00`, and anything within `MIN_CHAPTER_GAP_S` of the previous
  one is dropped) and `<out>.srt` (upload as captions, each cue capped to
  `CAPTION_DUR_S`). Both come from `qso_windows()`, so they agree with each other by
  construction.

### QSO timing

The EDI format stores time only to the minute, so it can never say when an over
actually began. `qso_windows()` snaps each QSO onto real audio structure —
`cluster_starts` finds every burst of activity, `_tx_start` finds the operator's own
first transmission within it, `_snap_to_cluster` takes the *latest* burst at or
before the anchor. `--input-log` supplies an exact anchor where available
(`match_qso_times` pairs EDI QSOs to logged `'qso'` events **by call in
chronological order**, never by minute — a hand-edited seed log is expected to move
timestamps across minute boundaries). `--seed-input-log` writes that hand-editable
skeleton from the EDI for a recording made before input logs existed. Each rule here
fixed a specific reported bug; RECORDING.md has the cases, and the regression tests
name them.

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
- **Every side stream needs `tpad=stop_mode=clone`** so a clip shorter than the
  session cannot end the shared filtergraph early and silently truncate the main
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
  `run-recorded-contest-session.sh` starts asciinema before the radio recorder, and
  the `.scope` recorder starts when the radio connects. Clamping instead showed up
  as the cast PiP's clock lagging the session by 25 s.
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
  `HUD_THEME_DIR` is script-relative — renders run from a contest directory.
- **`hud_art(theme, W, H)` prepares everything once per render** (crop and scale the
  bar, scale every rect, cut/key/pre-scale the sprites); a frame is then a copy plus
  values. `draw_hud_frame` is called ~24,000 times for a 2 h session.
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
  `HUD_AZ_INTERP_S` is a stationary rotator, not slow movement, so the bearing holds
  there. Interpolation takes the short way round the circle. Azimuth is deliberately
  its own time series (`hud_az_marks`) rather than a field of the per-run rig state:
  a run is however long freq/mode hold for, so one median for all of it made a real
  27-second 250°→31° slew render as a single jump at the run boundary.
- **The S-meter comes from the `.scope` recording's own centre bins**, not from
  CI-V's polled `15 02`. 475 bins across a 1 MHz span makes one bin ~2.1 kHz, close
  enough to an SSB passband to be a real reading rather than a proxy; `hud_s_marks`
  takes a *max* over the centre bins so a signal in one bin isn't diluted. Not
  retroactive: no contest round recorded so far has a `.scope` file, so the meter
  reads empty until the next one.
- **PWR renders placeholders rather than hiding** when Vd/Id are absent, which is
  every recording to date — panels appearing and disappearing between recordings
  would leave the bar looking broken.
- **Every readout is fitted to its own recess** (`_seven_seg`'s shrink loop), since
  nothing here has a fixed width — a five-digit score used to spill clean across the
  gutter into the QSOS panel. One test asserts the gutter comes out byte-identical
  to the artwork; another generalises it to the whole bar, so any readout painting
  over a baked label fails.
- **Numerals are DSEG7** (Debian `fonts-dseg`, SIL OFL), with **unlit segments drawn
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
  needles to the nearest degree). Without that quantisation the scope-derived signal
  level alone forces a fresh draw ~30 times a second.
- **The data layer is pure and fully unit-tested** — everything up to
  `draw_hud_frame` is functions over the recording's own sources, needing no art, no
  fonts and no ffmpeg. `HudTimeline.at(t)` returns a `HudState` for any video time.
- **Two single-frame preview modes**, because iterating layout against a full render
  is absurd: `--hud-demo OUT.png` needs no recording at all (this is what to check
  artwork against), `--hud-preview OUT.png --hud-preview-t SECONDS` builds real
  state from a real recording. `recdir`/`edi` are optional in argparse purely so
  `--hud-demo` can run standalone. `--no-hud` keeps the pre-HUD look, corner webcam
  PiP included.
- **The artwork's generation prompt is `hud-artwork-prompt.md`** — what the software
  draws (and therefore what the artwork must leave empty), why the sprite sheet sits
  on flat magenta, and why a coordinate table baked into the image was rejected (an
  image generator cannot measure its own output raster, so such a table would be
  confabulated while looking authoritative). The artwork's "text" is drawn, not
  typeset, and its labels were good enough to keep — so the HUD has no label face of
  its own at all.

## Uploading a rendered video to YouTube
`contest_video.py` only renders the mp4 + `.chapters.txt` + `.srt` — it does not upload.
Uploading is a deliberate separate manual step, run after reviewing the render, using
[`youtubeuploader`](https://github.com/porjo/youtubeuploader) (a Go binary, installed at
`~/.local/bin/youtubeuploader`):

```
youtubeuploader \
  -filename out.mp4 \
  -title "Puskás URH Kupa 2026-07 — HA5LA" \
  -description "$(cat out.mp4.chapters.txt)" \
  -caption out.mp4.srt \
  -secrets ~/.config/youtubeuploader/client_secrets.json \
  -cache ~/.config/youtubeuploader/request.token
```
- **OAuth credentials are intentionally global, not project-specific** —
  `~/.config/youtubeuploader/` holds one client secret + cached token shared across every
  project on this machine that uploads to this YouTube channel, not just `urhpk`.
- Video lands **private** by default (both the flag default and Google's own forced-private
  restriction on new/unverified API projects) — this is the review gate: check it on YouTube,
  then flip to Public/Unlisted by hand in YouTube Studio. Nothing in this repo auto-publishes.
- The OAuth consent screen is left in "Testing" mode (no Google verification review needed
  for personal single-channel use) — the tradeoff is the refresh token expires after 7 days,
  requiring a re-click through the browser consent screen. Irrelevant in practice since
  contests are monthly.

## Recording the logger session (for contest_video.py --cast)

Record the logger's own tmux pane with [asciinema](https://asciinema.org/)
(`asciinema rec YYMMDD-CALL.cast`, started before and stopped after the
`puskas_logger.py` session) — not the irssi pane, and not a screen-capture
tool like `recordmydesktop`. The console UI is plain text, so a graphical
screen recording would just be lossy video of something that's already
exactly representable as text; `asciinema`'s cast v2 format is a timestamped
stream of terminal output plus a header carrying the exact real-world UTC
start time (see `parse_cast_header`), which is exactly what
`render_cast_video` needs to replay it losslessly and sync it into the
video's timeline. Plain `script(1)` capture was considered and rejected for
the same reason recordmydesktop was: no per-event timestamps, so it can't be
replayed frame-accurately or synced to the audio at all.

**`run-recorded-contest-session.sh` is the entrypoint** — run right before a
contest round begins, nothing before that. It wraps exactly this recording
command in one `tmux new-session`, so starting/stopping the tmux session is
also what starts/stops everything else for the round:
- Window 0 (recorded): irssi | `puskas_logger.py`, side by side
  (`select-layout even-horizontal`) — this is the layout `contest_video.py
  --cast` expects.
- Window 1 (`bg`, **not** recorded — created with `new-window -d`, so the
  client's attached window never leaves window 0 and none of this appears
  in the cast): `hamlib_supervisor.py` on top, `on4kst_irc_bridge.py` split
  below it. Both are here rather than in a `systemd --user` unit
  specifically because they should only run for the duration of a contest
  round, not persistently — killing the tmux session (end of round) tears
  down both along with everything else, no separate stop step. Attach with
  `tmux attach -t <session>` (or `tmux select-window -t bg`) to check on
  either — `on4kst_irc_bridge.py` prints `[KST] Connecting …` / `[KST]
  Connection lost …` / `[KST] Reconnecting in N s …` etc. directly to
  stdout, so KST connect/drop events are visible there live, not just
  inferable from IRC-side symptoms.

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
- **Live rig status**: QRG and contest-clock update every second in the bottom toolbar.
  A band/mode change on the radio must be visible immediately in the prompt — never require
  Enter to see the updated state.
- **Toolbar redraws only on change, not on a fixed timer**: `session.prompt()` used to
  pass `refresh_interval=0.1` (10Hz), which called `_toolbar()` — and therefore redrew
  the screen — unconditionally 10x/s, even though almost every tick produced
  byte-for-byte identical output (the clock only changes once a second; rig/rotator/
  webcam state changes far less often). Under `--cast` (asciinema recording of this
  session, see `contest_video.py`) every redraw is a recorded terminal-output event,
  so this meant ~10 recorded events/s for the whole contest, nearly all redundant.
  `_toolbar_signature()` is a pure (no side effects) tuple of everything `_toolbar()`
  reads; `_toolbar_watcher(app)` polls it at the same 10Hz cadence (so a real
  second-boundary is still caught within ~100ms — why 10Hz was chosen over 1Hz in the
  first place) but only calls `app.invalidate()` when the signature actually differs
  from the last poll, cutting typical redraw frequency to roughly once a second.
  `app` here is `session.app`, captured directly once (right after constructing
  `session`) rather than fetched via `get_app()` inside the watcher thread — verified
  experimentally that `get_app()` from a plain `threading.Thread` sees a fresh,
  isolated contextvars context and returns a `DummyApplication` whose `invalidate()`
  is a silent no-op, so the redraw would simply never happen. Holding the real
  `Application` object directly sidesteps this: `Application.invalidate()` is
  documented as thread-safe (`loop.call_soon_threadsafe` internally) and works
  correctly called this way, confirmed with a standalone `PromptSession` test before
  wiring it into the real code. `_toolbar_watcher` never mutates state and never calls
  `_toolbar()` itself, so the band/mode-change `_REDRAW` logic above still only ever
  executes on the event-loop thread, inside the real `_toolbar()` call that a
  triggered redraw causes.
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
- **Radio thread must never die**: `_radio_thread` wraps each session in `try/except`
  and reconnects after drops (`RADIO_RECONNECT_S` cooldown — the radio refuses new
  sessions for a while after an uncleanly-dropped one), so a transient radio/network
  error cannot kill rig state permanently. Liveness comes from `last_rx_age()`
  (no CI-V-socket traffic for `RADIO_STALE_S` = session dead), not from polling.
- **The radio session is closed on every exit path, signals included**: a contest round
  ends by killing the tmux session (SIGHUP), which used to skip teardown entirely and
  leave exactly the abandoned session `icom_net.close`'s notes describe — the radio then
  streamed to a dead socket and refused the restarted logger for a minute or more, which
  is the "can't reconnect after exiting the logger" symptom. Three pieces, all needed:
  `_install_signal_handlers` handles SIGTERM/SIGHUP (teardown, then `os._exit` —
  EDI/telemetry/input/scope all flush as they are written, so nothing is lost by not
  unwinding); `main()`'s `finally` covers Ctrl-D, a crash, and the early return from the
  offline wizard, and is the single owner of teardown (`run()` no longer does its own);
  and `_shutdown` stops `_radio_thread` from treating the closed session as a drop and
  opening a fresh one on the way out. `_radio_close_if_connected` also **joins the radio
  thread**: a session still inside `connect()` is not in `_radio["rig"]` yet, so closing
  that slot cannot reach it, and only the thread that opened it can close it. Found from
  a real capture after a kill-and-restart cycle, where every session had said goodbye
  correctly except one still-connecting one, which the radio then pinged for ~70 s.
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
  original QSO; only the received side (call, rst_r, nr_r, loc) can change. Band and mode
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
  offline, plus a colour-coded UTC clock. Clock background is **green** during the contest
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

Purpose-built for Puskás URH Kupa rules. Requires `prompt_toolkit` (declared in uv script header).

```
uv run puskas_logger.py
```

**Locator cache** — built at startup by merging four sources in priority order (highest first):

| Priority | Source | How |
|---|---|---|
| 1 (highest) | QSOs entered this session | `_update_loc_cache` called after each logged/edited QSO |
| 2 | Recovered EDI files (crash recovery) | `_update_loc_cache` called for each recovered QSO in `main()` |
| 3 | `my-logs/*.edi` historical logs | `_parse_edi_files()` always merged via `_merge_loc_sources` |
| 4 | `~/.puskas/on4kst-seen-stations.json` | merged second |
| 5 (lowest) | `~/.puskas/puskas-seen-stations.json` | merged last |

`_merge_loc_sources(*sources)` takes sources highest-priority-first; each locator
appears once at the position of its highest-priority source. `_update_loc_cache(cache,
call, loc)` inserts `loc` at the front of `cache[call]` (most recently used first).
No API calls during contest.

**Crash recovery**: at startup, scans `*.edi` / `*.EDI` (case-insensitive) in the current
directory. If found, shows a summary and offers to resume — all QSOs, serials, and dup state
are rebuilt from the EDI records. EDI files are the sole persistence format (no session file).
Files are saved as lowercase `YYMMDD-CALL-BAND.edi`; `write_edi` automatically removes any
stale uppercase `.EDI` sibling of the same name (migration from pre-v1.6 saves).
`load_from_edi` deduplicates by stem (case-insensitive) as a safety backstop.

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
  QSO (same call, same band, different mode, within **5 minutes**) the predicted received
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
  band — `LogBook.worked_combos(call)` checks all 9 (3 bands × 3 modes). Red because
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

**Offline setup wizard**: if the radio is not reachable at startup and no manual band/mode
override is set, the logger shows an interactive prompt asking for band (`2M/70CM/23CM`)
then mode (`SSB/CW/FM`) before entering the main loop. Ctrl-D exits cleanly.
Mid-session rig disconnect uses `_rig_manual` values as fallback (set by the wizard or
**Alt+B / Alt+M** during the session), so the wizard only appears once per session.

**rotctld integration** (optional, no-op when rotctld not running):
- Background poller (`_rot_thread`) queries `ROTCTLD_HOST:ROTCTLD_PORT` (4533) every
  `ROTCTLD_POLL_S` (1 s) using the `p` command (returns azimuth and elevation)
- Current azimuth shown in toolbar as `ROT: 045°` when online, `ROT: ---` when offline
- **Alt+R** sends `P az 0` to rotctld to slew the rotator; fires in a background thread
- To start rotctld: `rotctld -m MODEL -r /dev/ttyUSB0` (see Hamlib docs for MODEL number)

**Rig server** (port 4532, always on): serves the rigctld TCP dialect (`f` → freq in
Hz, `m` → raw dial mode + passband, offline → `RPRT -1`) from the push-fresh `_rig`
cache — this is how `on4kst_irc_bridge.py` gets QRG/mode now that rigctld is gone,
with zero bridge protocol changes (the port is an interface: a real rigctld could
still serve it when the logger isn't running). Serves the *raw* mode (`USB`), not the
contest-normalized one (`SSB`), matching real rigctld byte-for-byte. Binds localhost
only; silently serves nothing if the port is already taken.

**Scope recorder** (`YYMMDD-CALL.scope`, always on once the radio connects): records
the radio's own spectrum sweeps via `enable_scope()`/`on_scope` to the `.scope` format
`contest_video.py --scope` consumes. Lives in the logger because the radio holds only
one network session (see icom_net's notes) — the CLI harness recorder can't run
alongside the logger. Re-enabled on every reconnect (scope data output is
session-scoped on the radio's side); file is lazily opened on the first sweep and
flushed per sweep. Measured on real hardware: 29.4 sweeps/s at 493 bytes each
(18-byte header + 475 pixels) = ~14.5 kB/s, so **~105 MB per 2 h session**.

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
  on having been online — `_radio_thread`'s loop also spins while the radio has
  simply never been reachable, and a null line every `RADIO_RECONNECT_S` would
  say nothing new.
- Opened in `main()` **before** the radio/rotator threads start, since both
  write to it the moment they have something and the radio's first push lands
  within a second of connecting. `_telemetry_write` takes a lock: the radio's
  CI-V receive thread and the rotator poller are genuinely concurrent writers.
- No `ptt` field: it used to be queried and recorded here too, but the WAV
  recordings' own IC-9700 metadata already carries it straight from the rig
  with zero polling lag (see `contest_video.py`'s `read_wav_metadata`) -- this
  was in practice reconstructing, with more latency, something already recorded
  losslessly elsewhere, so it was removed rather than kept for redundancy.

**Input-box logging** (`*-input.jsonl`, always on, feeds `contest_video.py --input-log`):
- Event-triggered, not polled: `session.default_buffer.on_text_changed` fires
  `_on_buffer_changed`, which appends `{"t": <UTC with microseconds>, "event":
  "text", "text": <full current buffer>}` to `YYMMDD-CALL-input.jsonl` on every
  keystroke. A 1 Hz poll like the telemetry recorder would blur or entirely
  miss fast typing, and the buffer only changes on a keypress in the first
  place, so there's nothing to poll.
- Microsecond precision matters here: it timestamps individual keystrokes.
  (`contest_video.py` parses the `'text'` events but no longer draws anything
  from them — the terminal PiP shows the real typing — so they exist for a
  future consumer, not a current one.)
- **A second event kind, `"event": "qso"`, is written from the "New QSO"
  block** in `run()` — one line per QSO actually appended to the log:
  `{"t": ..., "event": "qso", "call", "band", "mode", "nr_s", "dup"}`. This is
  deliberately *not* inferred from the `"text"` stream (Enter-submit,
  Ctrl+U/unix-line-discard, and Escape-abort all just clear the buffer the
  same way — see the long comment above `_input_log_open` for why that's
  unreliable). It's written from the one place in the code that unambiguously
  knows a QSO was logged, right next to `lb.add(qso)`. `now = datetime.now
  (timezone.utc)` is captured **once** and used for both `qso.dt = now.replace
  (second=0, microsecond=0)` and this event's `t` — not two separate
  `datetime.now()` calls — so the two are *always* related by exact minute
  truncation with no possible race at a minute boundary. This is what lets
  `contest_video.py`'s `match_qso_times` line them up exactly; it is the fix for
  "weird QSO timing" in a preview, where the EDI's minute-only precision let
  `_snap_to_cluster` occasionally pick the wrong neighbouring burst.

**Webcam capture** (`YYMMDD-CALL-webcam.mp4` while recording, renamed on stop — see
below — Alt+V to start/stop, off by default):
- Capturing on the *same machine* that runs the logger is the whole point: a
  phone propped up separately has its own clock, which `contest_video.py` then
  has to reconcile by audio cross-correlation (see FINDINGS.md). Here the start
  time is simply known.
- `_webcam_capture_cmd` builds the `ffmpeg -f v4l2 ... -f pulse ...` command (Linux
  video4linux2 + PulseAudio); `WEBCAM_DEVICE`/`WEBCAM_AUDIO_SOURCE` constants at the
  top of the file are the only things that need adjusting for a given machine (find
  with `v4l2-ctl --list-devices` / `pactl list short sources`). `-preset ultrafast`
  keeps the encode cheap enough to run alongside the radio/rotator threads and the UI for a
  multi-hour session without competing for CPU.
- Stop sends `SIGINT` (not a hard kill) so ffmpeg finalizes the mp4 properly; a
  5 s `wait()` with a `terminate()` fallback guards against ffmpeg hanging. Also
  triggered automatically on exit (`_webcam_stop_if_running`, both the normal
  Ctrl-D path and the crash-handler path in `main()`) so a still-running capture
  is never left orphaned or its output file unfinalized.
- **Renamed on stop with a µs-precise timestamp** (`_webcam_precise_start` +
  `_webcam_precise_name`), e.g. `260706-HA5LA-webcam-20260706T160037.123456Z.mp4`.
  The true frame-0 wallclock isn't known until the camera has actually opened
  (~1 s after spawn, variable), so it cannot be passed to ffmpeg up front; tagging
  it into the container afterwards works but needs a second copy of a multi-GB file
  on disk exactly when space is tightest (see FINDINGS.md). A rename is a
  directory-entry update, independent of size. `contest_video.py`'s
  `parse_webcam_precise_filename` prefers this over the `*-webcam.log` sidecar
  (same precision, but depends on that file surviving alongside the video).
- Logs `"event": "webcam_start"` / `"webcam_stop"` to the same `*-input.jsonl` as
  everything else (see **Input-box logging** above) rather than a separate file —
  one more consumer of the same already-precise event log, not a new format.

**Contest rules**:
- Reads band/QRG/mode from the radio via `icom_net` (push updates, no polling); falls
  back to Alt+B/Alt+M (or `!band`/`!mode`) if the radio is offline
- RST defaults: `59` for SSB/FM, `599` for CW
- Serial auto-increments per band; all QSOs (including dups) get a serial
- Dup check key: `(callsign, band, mode)` — 9 valid combos per station (3 bands × 3 modes)
- Dup QSOs shown in red and EDI-flagged `D`
- Auto-saves EDI after every QSO; files named `YYMMDD-CALL-BAND.EDI` in current directory

**Commands**: `!undo`, `!help` (`!band`/`!mode` still accepted but Alt+B/Alt+M preferred)  
Ctrl-D → final save and exit

EDI export: one file per band, `[REG1TEST;1]` format compatible with bb.mrasz.hu submission.

## Running
```
uv run puskas_harvester.py          # build ~/.puskas/puskas-seen-stations.json before a contest
./run-recorded-contest-session.sh   # the contest round itself — see "Recording the logger
                                     # session" above for what this actually starts
uv run puskas_visualizer.py         # generate map and polar after the contest
```
Each piece also runs standalone when not tied to a contest round (general ON4KST chat
outside a round, debugging one component, etc.):
```
uv run on4kst_irc_bridge.py   # IRC bridge (then connect irssi to localhost:6667)
uv run puskas_logger.py       # log QSOs
uv run hamlib_supervisor.py   # starts/stops rotctld on USB replug
```

## Testing
Enforced by `pre-commit`, not a checklist here to follow by hand — one-time setup per
clone: `uv run pre-commit install`. What actually runs is defined in
`.pre-commit-config.yaml`, the only source of truth for it; CI (`test.yml`) runs the
same config rather than a separately maintained list of steps.

**Ruff policy**: both `ruff check` and `ruff format` run via pre-commit. Aligned-assignment
style (e.g. `RIGCTLD_HOST   = "localhost"`) is no longer preserved — `ruff format` collapses
it to single-space, and that's accepted as worth it to avoid diff noise from realigning a
whole block whenever one name's length changes. E501 (line length) and E701 (single-line
`if …: return` in lookup functions like `_mode_str`) are still suppressed for `ruff check`.

## Repository
- `.gitignore` excludes generated files (`puskas_map.html`, `puskas_polar.png`) and scratch
  files (`*.json`, `*.url`, `*.txt`)
