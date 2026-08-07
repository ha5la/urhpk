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

## Housekeeping reminders
- When adding or removing components, update the components table in **README.md**

## Development principles
- **Succinct code comments**: prefer explaining identifiers over comments (Robert C.
  Martin) — a well-named variable/function usually makes a comment unnecessary. When
  the *why* genuinely needs explaining (a hidden constraint, a non-obvious tradeoff, a
  bug's root cause), write a succinct comment, not an essay. Applies going forward;
  this file's existing long-form comments predate the rule and document real debugging
  history, so they're not being retroactively trimmed.
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
  expensive later. One session let three unrelated `contest_video.py` topics (webcam PiP,
  CW decoder tuning, WAV-metadata rig-state redesign) plus a `puskas_logger.py` macro edit
  accumulate uncommitted; splitting them afterward required reconstructing each topic's
  slice by hand, function-by-function, against a full end-state backup, since there was no
  intermediate git history left to split from.
- **Prove a regression test catches the bug — red before green**: write the test
  against the still-buggy code and watch it actually fail, *then* write the fix and
  watch the test pass. Don't just reason that a test "should" fail on the old code —
  a test that looks right but was never seen red is unverified, and writing it after
  the fix already exists risks unconsciously shaping the assertion around whatever the
  fix happens to produce. If a fix was already written before the test (e.g. the bug
  and its cause were understood in the same pass), the fallback is to temporarily
  revert the fix (or monkeypatch the specific buggy function back), confirm the test
  fails, then restore the fix and confirm it passes — strictly weaker than true
  test-first, but better than trusting an unverified test. Example: `contest_video.py`'s
  `_snap_to_cluster` regression test was confirmed via the fallback, by monkeypatching
  the old nearest-cluster logic back in and observing the assertion fail with the old
  (wrong) value.
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
  there's no true condition to poll for proving an absence.
  Cut the full test suite from ~29s to ~3.5s (pytest alone: ~26s → ~3.2s) — found while
  investigating why `pre-commit` felt slow; `tests/test_integration.py` (~18s) and
  `tests/test_irc_protocol.py` (~7s) were almost entirely blind `asyncio.sleep()` calls
  synchronizing against the bridge's background tasks. Removing the guesswork also
  surfaced one real, previously-masked race: `test_exclamation_not_forwarded_to_kst`
  snapshotted `kst_server.received` right after connecting, racing the connect-triggered
  `/SET HERE` send (a separate task) — the old 0.3s drain timeout had enough slack to
  hide it by accident. Fixed by explicitly `wait_until`-ing for `/SET HERE` before taking
  the baseline, rather than re-adding a sleep.
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

Problem this solves: `rigctld`/`rotctld` were started by hand, picking the device path
(`/dev/ttyUSBn`) from shell history. If the USB connection drops (cable wiggle, radio
power-cycle) and the kernel re-enumerates the device, the running daemon keeps the old,
now-dead file descriptor open — it does not notice the device came back on a new number.
The fix is not "give it a stable device name" alone (a udev `SYMLINK+=` rule or, as it
turns out, the distro's own `/dev/serial/by-id/` — see below — both already solve that
half); the daemon itself still needs to *restart* against the new device node, since it
never re-`open()`s a path once it's already got a fd.

```
uv run hamlib_supervisor.py
```
Run permanently (tmux, or a `systemd --user` unit) alongside the contest tools.

- **No custom udev rule needed.** `/dev/serial/by-id/` is populated automatically by the
  distro's own stock udev package for both devices here — verified directly on the
  actual hardware, not assumed:
  - IC-9700: `/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_IC-9700_13013358_A-if00-port0`.
    The radio actually exposes **two** separate CI-V USB-serial ports (`_A`/`_B`, real
    distinct USB devices under the hood, not two interfaces of one) — likely so a second
    CAT-speaking program can run without contending with rigctld. Port A is the one in
    use; confirmed live (`145.355 MHz FM`) with `rigctld -m 3081 -s 115200`.
  - Rotator (custom Arduino, Yaesu GS-232-compatible firmware):
    `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0` (CH340, `1a86:7523` — no serial
    number, but there's only one such device on this machine so plain VID/PID-based
    identity, which is all `/dev/serial/by-id` uses here, is already unambiguous).
    Confirmed live (real azimuth reading) with `rotctld -m 603 -s 9600` — **not** GS-232A
    (`-m 601`): that returned "Protocol error" on `get_pos` despite genuinely receiving
    bytes back (wrong response framing, not a dead link or wrong baud) — 603/GS-232B is
    the sketch's actual dialect.
  - If a device's `/dev/serial/by-id/` entry ever goes missing on a future machine (e.g.
    a USB-serial chip too generic for udev's built-in rules to name distinctly), fall
    back to a custom udev `SYMLINK+=` rule matched on `idVendor`/`idProduct` — see git
    history / prior discussion for the template; not needed for the current hardware.
- **inotify, not polling**: watches the parent directory of each configured device path
  (both land in `/dev/serial/by-id/` here, so one shared watch) for `IN_CREATE`/
  `IN_DELETE`/`IN_MOVED_TO`/`IN_MOVED_FROM`, implemented via `ctypes` directly against
  libc — no `inotify_simple`/`watchdog` dependency, matching `on4kst_irc_bridge.py`'s
  "pure stdlib" style. `reconcile_initial_state` handles the device-already-present-at-
  startup case explicitly, since inotify only reports *future* events.
- **Async/event-driven rig or rotator state (no polling `rigctld`/`rotctld` themselves)
  was investigated and is not available for this hardware on the installed Hamlib
  (4.6.2, confirmed also still absent in the latest release, 4.7.2)**: Hamlib's
  `async_data_supported` backend flag — the mechanism that lets rigctld consume Icom
  CI-V Transceive frames without polling, and even multicast them to network clients —
  is set for `ic7300.c`/`ic7610.c`/`ic785x.c`/etc. but not `ic9700.c` (checked directly
  against the Hamlib source at both version tags). Hamlib's rotator API
  (`include/hamlib/rotator.h`) has no equivalent concept at all, for any backend, at
  either version — this isn't a per-rig gap, the rotator subsystem never defined the
  hook. So `puskas_logger.py`'s rotator polling thread stays as it is (its rig polling
  has since been replaced entirely by `icom_net` push updates — rigctld is now only
  used by `on4kst_irc_bridge.py`); this script only removes polling from the
  *device-presence* problem, not from azimuth queries themselves.
- The IC-7300MK2 (an unrelated HF rig also on this machine) has no dedicated Hamlib
  backend in the installed 4.6.2 — `RIG_MODEL_IC7300MK2` was added in the 4.7 release
  series (confirmed: absent from `rigctl --list` here, present in Hamlib's 4.7.2 source
  and release notes). The plain IC-7300 model (`-m 3073`) was confirmed working against
  it live for basic CAT (freq/mode read) regardless — Puskás Kupa is VHF/UHF-only so
  this rig isn't part of this project's workflow either way, noted here only because it
  came up while investigating the IC-9700.

## icom_net.py – Direct Icom Ethernet CI-V client

Problem this solves: the IC-9700 is reachable over Ethernet (this network's radio is at
a fixed LAN IP), but `rigctld` only speaks CI-V over a serial/USB link — there is no
plain "CI-V over TCP" port on the radio itself. The only way to control it over Ethernet
is Icom's own network-remote-control protocol (the one RS-BA1 and wfview speak), which is
UDP-based, authenticated, and stateful. `icom_net.py` implements a minimal client for it
directly, in pure stdlib Python (no external deps, matching `on4kst_irc_bridge.py`'s
style). Originally a standalone first step; `puskas_logger.py` now uses it as its only
rig interface — instant, event-driven state, no rigctld involved.

- **Why this is worth it over polling**: CI-V has a "Transceive" mode (`SET > CONNECTORS
  > CI-V > CI-V Transceive: ON`) where the radio pushes frequency/mode changes
  unsolicited the instant they happen (front-panel or otherwise), rather than waiting to
  be asked. `rigctld`'s Hamlib backend for the IC-9700 doesn't take advantage of this
  (see `hamlib_supervisor.py`'s notes above on `async_data_supported`), so
  `puskas_logger.py` on rigctld only ever saw rig state up to its 1 s poll interval
  stale. A direct client can just listen — no polling loop at all.
- **Protocol reverse-engineered from two independent implementations**, not from any
  official Icom spec (there isn't a public one): wfview (C++,
  gitlab.com/eliggett/wfview) and kappanhang (Go, github.com/nonoo/kappanhang, credited
  by wfview's own source as its basis). Every packet layout in this file is transcribed
  byte-for-byte from `packettypes.h` and the actual field-assignment code in
  `icomudphandler.cpp`, cross-checked between the two, not guessed from prose
  descriptions — several real discrepancies between what secondhand descriptions imply
  and what the code (and the radio) actually require turned up during implementation
  (see below), so treat any *future* protocol change here the same way: verify against
  source or a real packet capture, don't infer from memory.
- **Credentials**: read from `~/.netrc` (`machine <radio-ip> login <user> password
  <pass>`), same convention as `on4kst_irc_bridge.py`. Requires `SET > Network > Network
  Control: ON` and a LAN username/password set in the radio's own `SET > Network` menu
  first — this applies even for a same-subnet, non-relay connection; Icom's login step
  isn't WAN-only.
- **Username/password are obfuscated on the wire**, not sent plaintext: a fixed
  substitution table keyed by `(character_value + position_index)`, byte-identical
  between wfview and kappanhang (`passcode()`, credited by kappanhang to W6EL). Not a
  real cipher, just enough that credentials aren't sitting in cleartext UDP frames.
- **BCD frequency encoding**: standard Icom 5-byte little-endian BCD
  (`bcd_encode_freq`/`bcd_decode_freq`), verified against the worked example in Icom's
  own CI-V documentation (144.300.000 Hz → `00 00 30 44 01`) before ever touching real
  hardware — this caught a base-16-vs-base-10 typo (`int(x, 16-6)` instead of `int(x,
  16)`) and a nibble-order bug in the decoder, both via a red-before-green unit test
  against that known example, per this project's testing philosophy.
- **Three real bugs found only by testing against the actual radio** — the protocol
  research (byte layouts, packet field meanings) was solid, but none of these were
  discoverable from source reading or the fake-radio integration test alone, since a
  mock server just mirrors back whatever assumptions were baked into it:
  1. **Outer transport `seq` numbering**: the are-you-there(seq=0)/are-you-ready(seq=1)
     handshake is *not* part of the same counter as subsequent tracked packets
     (login/token/conninfo) — the first tracked packet also starts at seq=1, not 2 as a
     naive "keep incrementing" reading would suggest. Confirmed directly: sending login
     at seq=2 got a real retransmit-request packet back (type=0x01, "resend seq=1") from
     the radio, and switching to seq=1 got an immediate, correct login response. The
     radio validates this strictly — it is not a "nice to have, ignorable on a clean
     LAN" part of the protocol the way general retransmit-*request compliance* is (see
     next point).
  2. **`threading.Lock` self-deadlock**: `IcomNetRig._apply_update` held `self._lock`
     while also reading `self.band`, whose own property getter re-acquires the same
     lock — silently wedges the one background thread that processes all incoming CI-V
     data, forever, on the very first update. Symptom was confusing: `freq_hz` updated
     once and then froze, `mode` never updated at all, no exception, no crash — the
     thread was just gone. Root-caused with a small standalone debug script logging
     `freq_hz`/`mode`/`band` every 100ms after connect(), which showed exactly one
     update then silence. Fixed by switching to `threading.RLock()`.
  3. **The control socket must be kept alive with idle packets *during* the CI-V-socket
     open handshake, not just after everything is fully connected**: the original
     structure did login→token→conninfo→(then, still synchronously) CI-V rendezvous→CI-V
     open-retry-loop, with the control socket's keepalive thread only starting at the
     very end. On real hardware this left the control socket silent for several seconds
     while the CI-V open was being retried, and the radio silently dropped the
     just-registered conninfo session — CI-V data never arrived, even though the
     low-level are-you-there/ready responder (session-independent) kept answering the
     whole time, which made this look like a CI-V-specific problem rather than a control
     socket one. Fixed by starting `_ctrl_loop` (idle every ~100ms, reauth every 60s)
     immediately after conninfo succeeds, before doing any CI-V-socket work at all.
- **The CI-V "open" retry cadence is deliberately much slower than wfview's own
  documented behavior, and for a specific, tested reason**: wfview's watchdog resends
  the open request every 100ms once no CI-V data has been seen for 2s. Implementing that
  literally — a new tracked `seq` on every 100ms retry, since this client doesn't
  implement retransmit-*request* compliance (resending a specific requested packet on
  demand) — made the radio's own receive-sequence tracker lose sync entirely: it started
  sending retransmit requests for a huge, nonsensical `seq` range (`0xff82`–`0xffff`)
  that grew with every retry, and CI-V data never flowed. Spacing retries at `CIV_STALE_S`
  (2s) instead — giving each attempt time to land before the next is sent — fixed it.
  Diagnosed by tracing raw packet hex from the client's own retry loop, not by capturing
  wfview traffic (that capture only became necessary for the next bug).
- **The real deadlock, found only by comparing a live wfview packet capture against our
  own traffic**: this client wasn't sending anything past the "open" request until it
  had first *received* real CI-V data, on the theory that receiving data was the signal
  the stream was live. But the radio never sends CI-V data unprompted just because the
  stream is open — confirmed from a clean wfview session capture (`dumpcap`, no sudo
  needed since the invoking user is in the `wireshark` group — `dumpcap` has
  `cap_net_raw`/`cap_net_admin` via file capabilities, unlike plain `tcpdump` which
  needed `sudo`) against this same radio: wfview's own first real CI-V traffic is a
  *client-initiated* query (`FE FE 00 E1 19 00 FD`, a broadcast "read transceiver
  address"), not anything radio-initiated. Two sides both waiting to hear from the other
  first is a deadlock. Fixed by sending a real CI-V query (`cmd 0x03`, read frequency)
  alongside every open attempt, rather than gating that first query on already having
  received data — matches wfview's actual behavior of speaking first.
- **wfview's own outer-`seq` sequence has no second `token(0x05)` renewal between
  register and conninfo**, contrary to an earlier assumption in this file's own history:
  captured directly from the wfview trace, outer `seq` goes login=1, token(register)=2,
  conninfo=3, with nothing in between. This client originally sent an extra
  token(`0x05`) request there (seq=3) before conninfo (pushing conninfo to seq=4) — removed
  to match the real, working sequence exactly, on the theory (consistent with bug #1
  above) that this radio's session state machine cares about more than raw seq
  continuity: an extra tracked packet a real client never sends is exactly the kind of
  thing that could desync it, even if the seq numbers themselves stay technically
  sequential.
- **A capture-driven debugging session needs a cooldown between real-hardware
  attempts**: repeated rapid reconnect attempts against the same radio during this
  debugging session caused failures to shift to different, earlier steps across
  successive runs (login one time, conninfo the next) with no code change in between —
  strong evidence of session-slot exhaustion from prior attempts that were never cleanly
  logged out (the process just exited on error, abandoning the UDP session from the
  radio's perspective). Two fixes: `IcomNetRig.connect()` now sends a best-effort
  token-deregister (`requesttype=0x01`) and closes sockets on any failure partway
  through, so a failed attempt doesn't linger; and empirically, a real fresh attempt
  needs on the order of tens of seconds of quiet since the last one before the radio's
  own session state reliably resets.
- **Closing a session takes a real goodbye — the deregister alone is not one.** `close()`
  used to send the CI-V stream-close plus the token deregister and stop there. Captured
  from a live wfview shutdown (same `dumpcap` technique as the scope research; wfview was
  closed via a WM_DELETE_WINDOW so its normal destructors actually ran, unlike a SIGTERM,
  which skips them), a real client also sends a **disconnect control packet — `type=0x05`,
  16 bytes, `seq=0` — on every socket it opened**, CI-V first and the control socket after
  the deregister is acknowledged. Without it the radio keeps the session on its books:
  measured against the real IC-9700, it went on pinging the dead sockets at ~50
  packets/s for over a minute while refusing new sessions the whole time (wfview's own log
  says `Busy: 1`; our client's are-you-there simply goes unanswered). With it, traffic
  stops within the same second and a fresh `connect()` succeeds at +0.0 s, measured
  back-to-back. `close()` is idempotent and safe on a half-connected session, so
  `connect()`'s failure path just calls it rather than a separate partial teardown.
- **Scope data output is *not* session-scoped, contrary to what this file said**: a fresh
  session gets `27 00` waveform frames immediately, before it enables anything — the
  radio remembers `27 11 01` across sessions. Harmless (the logger re-enables anyway), but
  it means a leftover session's flood is the radio streaming a *real* scope, not an
  artifact of the abandoned handshake.
- **Test coverage**: `tests/test_icom_net.py` covers the pure, hardware-independent
  functions (passcode scrambling, BCD codec, CI-V frame parsing) with no mocking needed.
  `tests/test_icom_net_integration.py` runs the full `connect()` handshake against an
  in-process fake UDP radio (`FakeIcomRadio`) implementing just enough of the real
  protocol to exercise every branch, then injects an unsolicited CI-V Transceive frame
  and asserts `IcomNetRig.freq_hz`/`.mode`/`.band` update with no polling — this is what
  caught the `RLock` deadlock (bug #2 above) in CI, well before real-hardware testing.
  Note what this test suite *can't* catch: the sequencing/timing bugs (#1, #3, and the
  retry-cadence and deadlock findings above) are all about how a real radio's own
  session/sequence tracking behaves, which a fake server that just mirrors back
  whatever the client sends can't reproduce — those needed the real IC-9700.
- **The radio holds only ONE live network session — a second connect is not refused, it
  silently kills the first**: verified live with two concurrent `IcomNetRig` sessions —
  the second came up fine while the first's CI-V data stream died the moment the second
  started connecting, and did not recover even after the second closed (its own reopen
  retries notwithstanding). Consequences: every consumer of radio state must go through
  the one session owner (`puskas_logger.py` during contests — it serves other local
  consumers, see its rig-server notes), and the `icom_net.py` CLI harness must never be
  run while the logger is up — the two would fight over the session in a reconnect loop.
- **rigctld-parity commands** — the full rigctld surface `puskas_logger.py` uses beyond
  freq/mode reads, as plain CI-V writes on the existing CI-V socket: `send_cw()` (0x17 +
  ASCII, 30-char limit), `stop_cw()` (0x17 + 0xFF), `set_clock()` (0x1A 0x05, IC-9700
  parameters 0184 UTC-offset / 0180 time / 0179 date, packed BCD) — byte layouts
  transcribed from Hamlib's `icom_send_morse`/`icom_stop_morse`/`icom_set_clock`
  (`rigs/icom/icom.c`) and `ic9700_clock_cmds` (`rigs/icom/ic7300.c`), the exact code
  paths rigctld's `b`/`0xBB`/`\set_clock` take. `set_clock` verified against the real
  radio: all three set commands ACKed (FB) and the time/date read back over CI-V matched
  the set value exactly. `on_civ_frame()` exposes every raw inbound CI-V frame — this is
  how a caller observes ACKs (FB ok / FA rejected), which the logger's clock-sync
  success/failure notice will need. Note: the radio echoes the client's own outbound
  frames back on the CI-V stream, so a raw-frame listener sees both directions —
  distinguish by the to/from address bytes (`E0 A2` = radio→controller).
- **Not yet implemented / explicitly out of scope for this first pass**: audio
  streaming (conninfo currently requests `rxenable=0`/`txenable=0` — CI-V status only),
  PTT/transmit control, and general-purpose retransmit-*request* compliance (resending a
  specific buffered packet on demand — the project's research concluded, and
  real-hardware testing confirmed for steady-state traffic, that this is skippable on a
  clean LAN; what *isn't* skippable, per the bugs above, is getting the initial seq
  numbering and handshake ordering exactly right). Wired into `puskas_logger.py` as its
  only rig interface (see that section's radio notes); also still usable standalone via
  the CLI test harness (`uv run icom_net.py <radio-ip>`).

### Scope (spectrum waterfall) data

Goal: match the exact waterfall the radio's own front-panel display shows in
`contest_video.py`'s rendered background, instead of a waterfall reconstructed from the
recorded audio (`showspectrum` — see that section above) — the audio-based waterfall can
only ever show what the receiver actually demodulated, not the true RF passband the
operator was watching, including signals never within earshot.

- **The obvious assumption — a separate "scope port" (`ScopeLANPort`, default 50004,
  present as a real key in wfview's own config) — is wrong for Icom radios, confirmed
  two independent ways before writing any code**: first empirically, by running a real
  wfview session against this radio with `dumpcap` capturing 100% of UDP traffic to/from
  it (not filtered to one port) while wfview's window visibly showed a live spectrum
  matching the radio's own screen — zero packets ever appeared on port 50004, and the
  user independently reproduced the same result with Wireshark, additionally noting the
  radio's own `SET > Network` menu only exposes three configurable LAN ports (Control/
  CI-V/Audio), no fourth scope port at all. Second, from wfview's own source: `conninfo_
  packet` (the packet that negotiates ports with the radio at login) has exactly two
  port fields, `civport` and `audioport` — there is no third port field anywhere in that
  struct, so the radio is structurally never told about a separate scope socket.
  `ScopeLANPort`/50004 in wfview's config turned out to be a vestige of its *Yaesu*
  support (`src/radio/yaesuudpcontrol.cpp`/`yaesucommander.cpp` are its only consumers)
  — dead weight for any Icom session, misleading precisely because it's a real,
  populated config key that looks Icom-relevant.
- **Real mechanism: scope sweeps are ordinary CI-V command `0x27` frames on the same
  CI-V socket already used for freq/mode** (port 50002) — not a separate stream, not
  separately negotiated. Turned on with two plain CI-V "set" commands sent after the
  CI-V socket is open, exactly like any other CI-V write: `27 10 01` (scope on), `27 11
  01` (scope data output on) — `IcomNetRig.enable_scope()`. Off by default (`connect()`
  never calls it) since it's a much heavier stream than freq/mode transceive frames —
  opt in explicitly, matching `--scope` on the CLI harness.
- **Frame layout — confirmed by decoding a real captured sweep byte-for-byte, not just
  from source reading**, which caught a real ordering mistake: after the `27 00 00`
  scope-wave-data marker, `sequence`/`sequence_max` (single BCD bytes — the IC-9700 over
  LAN always sends `sequence == sequence_max == 1`, the whole 475-pixel sweep in one UDP
  datagram, unlike the old multi-packet serial-CI-V framing), then for `sequence == 1`:
  `mode`(1 byte, `0x00`=Center) → `start_freq`(5-byte BCD) → `end_freq`(5-byte BCD) →
  `out_of_range`(1 byte) → pixel bytes. An initial reading with `out_of_range` placed
  *before* the two frequency fields instead of after decoded a real captured frame to a
  nonsensical ~1.45 MHz "center frequency"; swapping to oor-after-frequencies decoded
  the same bytes to 145.11–146.11 MHz — sane 2M-band, 1 MHz-span, matching what the
  radio was actually showing. In Center mode the transmitted fields are center-frequency
  and half-span, not edges — `start_hz = raw_start - raw_end; end_hz = start_hz + 2 *
  raw_end` recovers the true sweep edges. Pixel bytes are each a raw `0`–`160` linear
  scope unit (`SpectrumAmpMax`), not dBm — no rescaling needed, already a
  ready-to-colormap waterfall row, one byte per bin, 475 bins for the IC-9700.
- **Reassembly is defensive, not just a pass-through**: `IcomNetRig._apply_scope_frame`
  buffers pixels across `sequence` 1..`sequence_max` and only fires `on_scope()` /
  updates `scope_pixels` once a sweep is complete, even though the IC-9700's own LAN
  behavior makes this a same-frame no-op in practice (sequence_max is always 1) — this
  is what a hypothetical multi-packet sweep (older serial-CI-V-style framing, or a
  different rig model) would need, and it's essentially free to support since the state
  machine is trivial; `tests/test_icom_net.py`'s
  `test_parse_scope_frame_multi_sequence_reassembly` exercises that path with a
  synthetic two-packet sweep since real hardware here never takes it.
- **Test fixture is a real captured frame, not hand-built**: `tests/test_icom_net.py`'s
  `_REAL_SCOPE_SWEEP_HEX` is the literal bytes of one CI-V scope-wave datagram, extracted
  from a `dumpcap` capture with a ~30-line pure-stdlib pcap parser (Ethernet+IP+UDP
  header math, no `dpkt`/`scapy` — neither was installed and the format is simple enough
  not to need them) rather than transcribed by hand from a hex dump — manual hex
  transcription of a 497-byte payload turned out to be genuinely error-prone in practice
  (a mis-typed nibble slipped into an early draft of this exact fixture and was only
  caught by re-deriving the hex programmatically and diffing) and a real captured frame
  exercises the actual byte layout, not just whatever assumptions were baked into a
  synthetic one.
- **Recording format for `contest_video.py`'s eventual use**: `write_scope_record`
  appends one binary record per sweep — `<f8 timestamp><u4 start_hz><u4 end_hz><u2
  npixels><npixels raw bytes>` — to a plain file, no JSON. Deliberately not JSONL like
  the telemetry/input-log recorders: those are ~1 record/second and human-debuggability
  matters more than size; scope sweeps arrive several times a second, each already
  byte-quantized (0-160 per pixel, nothing to gain from text encoding), so JSON overhead
  would balloon a multi-hour recording for no benefit — same reasoning as the WAV
  recorder storing raw samples rather than one JSON number per sample. Verified against
  real hardware: `uv run icom_net.py <radio-ip> --scope out.scope` produces a file whose
  records read back byte-exact (timestamp/frequency-range/pixel-count/pixel-values all
  round-tripped correctly) against ~33 real sweeps over a 15 s session.
- **Consumed by `contest_video.py`'s `--scope`** (see that section below) and previewable
  standalone via `scope_preview.py` (`uv run scope_preview.py RECORDING.scope -o out.mp4`
  — a quick non-integrated look at a recording's waterfall, useful before committing to a
  full contest_video.py render). Both read the format via `icom_net.read_scope_records`,
  the format's single owner — `contest_video.py` doesn't reimplement the parser.

### Meters and the rest of the CI-V surface

`enable_meters()` polls Po/SWR/Vd/Id (`CIV_METERS`) at `METER_POLL_S` (0.5 s) and reports
one snapshot per cycle via `on_meters`, rather than firing per reply — that keeps the four
values in a record genuinely simultaneous and lets a recorder write one line per cycle
instead of four. Off unless asked for, like `enable_scope`, and re-armed on every reconnect
since the poll thread belongs to its session. The **S-meter (`15 02`) is deliberately not
polled**: `contest_video.py` derives signal level from the scope recording's own centre
bins, which costs no extra traffic and is already captured.

`puskas_logger.py` writes raw readings into the event telemetry on change
(`_telemetry_meter_record`). Unlike the old 1 Hz rig sampler this genuinely has to be
sampled — the radio only reports meters when asked — so there is no lag-free source being
needlessly re-sampled. While receiving, Po/SWR/Id sit at a flat zero and Vd barely moves,
so change-only keeps an idle hour nearly silent.

The findings behind all of it, from three `dumpcap` captures of a live **wfview** session against this radio
(same technique as the scope research above — wfview is a known-good client, so capturing
it answers "how does a working implementation do this" without needing our own session,
and crucially without taking the radio's single session slot away from it). Nothing here
is wired into any tool yet; recorded so the measurements don't have to be repeated.

- **Only two things are ever pushed unsolicited: freq/mode Transceive frames and scope
  sweeps (`27 00`, ~29/s). Everything else is polled** — wfview queries the S-meter
  (`15 02`) at **~19 Hz**, plus PTT (`1c 00`), OVF (`15 07`), VFO frequency/mode
  (`25`/`26`) and assorted settings at 1–3 Hz. So a live S-meter needs a poller; there is
  no push option to find. Affordability is settled by the same capture: the radio sustains
  ~30 queries/s alongside 29 scope sweeps/s and an audio stream without trouble.
- **Every outbound frame appears twice in a capture** — the radio echoes the client's own
  frames back on the CI-V stream (see the raw-frame note above), so a naive frame counter
  reads exactly 2x. Every query/reply pair in these captures came out at precisely 2:1,
  which is what confirmed the echo rather than some retransmit behaviour.
- **Meter calibration, measured against real instruments** (raw values are 0–255, sent as
  4-digit BCD across two bytes):
  | Meter | RX | TX | Verdict |
  |---|---|---|---|
  | `15 02` S | 0–6 quiet, 150 on a strong signal | not polled | S9 = raw 120 |
  | `15 11` Po | not polled | 213–214 | 100% — matches the 0/143/213 curve |
  | `15 12` SWR | 0 | 27–29 | ≈1.3 via 0/48/80/120 → 1.0/1.5/2.0/3.0 |
  | `15 15` Vd | **152** | — | **13.66 V** vs 13.78 V on a multimeter (0.9% — curve transfers) |
  | `15 16` Id | 0 | 169–172 | **the 7300 curve does not transfer** (see below) |
- **Vd reads on receive**, which is the case that matters for portable battery ops —
  watching a pack sag across a round beats a momentary key-down dip. Id is PA drain only
  and reads a literal 0 on RX.
- **Id's scale, measured: a straight line through the origin at 0.0741 A per raw unit**
  (~17.9 A full scale, against Icom's 25 A). Taken with a multimeter in series with the
  supply, PA drain = total current less the measured 1.18 A receive baseline. The low
  cluster (raw 55-64, 3.9-4.7 A) alone gives 0.0726 and adding a 100%-power anchor at
  raw 171 gives 0.0741 — two nearly independent estimates a factor of three apart in
  current, agreeing to 2%, which is what makes "linear through zero" believable rather
  than merely fitted. The low cluster cannot determine a slope alone: it spans nine raw
  units, so a free (non-origin) fit through it returns a physically absurd −1.5 A
  intercept purely from the short lever arm. Residuals within ±5.3%, worst at the lowest
  point.
- **A cheap multimeter's burden voltage is enough to brown the radio out, and the radio
  itself measures it.** With the meter in series the session's own `vd` readings fell to
  raw 22-41 — 10.2-10.7 V at the radio against 13.7 V at the supply, i.e. ~0.55 Ω of
  shunt plus lead resistance — and at 25% power the radio hit undervoltage and switched
  off, twice, reproducibly. The current readings are *still valid* for calibration
  regardless: Id measures drain current, and a (raw, amps) pair describes one instant
  however degraded the supply was at that instant — which is why a run that looked like
  a failure produced the fit above. What the brownout does cost is the assumption that
  the receive baseline stays constant while it is being subtracted, which is part of why
  the lowest reading is the worst-fitting one.
- **Vd's own curve is confirmed by the same session.** Across 10-100% power the gap
  between supply voltage and converted Vd grows 0.37 → 0.81 V, and that gap is *linear
  in current*: 54 mΩ of series resistance with a 0.10 V intercept. A wrong Vd curve would
  have shown curvature or a nonsense intercept, so Icom's points stand. The 54 mΩ is
  itself worth knowing — 0.69 V lost between supply and PA at full power, which for
  portable battery operation is the difference between holding up and browning out on
  key-down. The bench supply itself barely sags (13.78 → 13.70 V), so that loss is cable,
  connector and internal, not the source.
- **Superseded — Id's scale was previously unresolved:** The IC-7300 curve (0/97/146/241 →
  0/10/15/25 A) gives 17.6 A for raw 171, but the PSU's own current meter read 14 A total
  during that same transmission, of which ~2 A is the radio's RX baseline — so PA drain
  was ~12 A. One TX point can anchor a linear scale near the operating point but can't
  resolve the curve's shape. **Consequence for the recorder: store raw meter values and
  convert at render time**, so a better calibration is a one-line change rather than a
  ruined recording.
- **The S-meter slot switches to Po on transmit, in the protocol and not just the
  display**: `15 02` stops being queried entirely during TX and `15 11` starts. A consumer
  therefore needs PTT to label the reading — which `contest_video.py` already has losslessly
  from the WAV metadata.
- **`25 00`/`25 01` is VFO A/B of the selected receiver, not main/sub.** A capture showed
  144.800 FM and 144.1735 USB simultaneously, which looks like dual receive but isn't —
  confirmed from the radio itself, where A and B move together when the band changes.
  Reading the *other band's* receiver (the IC-9700's real dual-watch, e.g. monitoring 70cm
  while working 2m) needs a command we haven't identified; `07 D0`/`07 D1` switches the
  selection, but that is intrusive for a passive monitor. wfview has a sub-band display
  (`SubPlotCeiling`/`SubWFLength` in its config), so one capture with dual watch actually
  enabled would reveal it.
- **Scope commands carry a main/sub selector, and `parse_scope_frame` would drop the sub
  band's sweeps.** wfview sends every scope *settings* command twice, once ending `00` and
  once `01` (`27 14 00`/`27 14 01`, and likewise `27 15`, `27 19`, `27 1a`). By the same
  pattern `frame[4]` of a `27 00` waveform frame is that selector — but `parse_scope_frame`
  asserts it is `0x00`, so sub-band sweeps would be silently discarded. Not a live problem
  (all 439 waveform frames captured were main, and the sub scope's data output was never
  enabled), just a limit to know about before anyone tries.

## contest_video.py – Annotated CW contest video

Turns a CW contest recording plus its EDI log into a YouTube-ready MP4 with a
scrolling audio waterfall, a live CW-decode ticker, an RX/TX + rig badge, and
optionally a picture-in-picture of the logger's own terminal session (see
`--cast` below) and/or a webcam. Built for reuse across future contests
recorded the same way.

```
uv run contest_video.py RECORDING_DIR EDI_FILE [EDI_FILE ...] [-o OUT.mp4]
```
- Dependencies: `numpy`, `pyte`, `pillow` (uv script header) + `ffmpeg`/`ffprobe` on PATH
- **Input**: a directory of WAV segments named `YYYYMMDD_HHMMSS...wav` (local
  time), split on RX/TX switches, plus the EDI log for the same round. The
  recorder splits continuously, so segments are contiguous — the audio timeline
  is the sum of segment durations; filename wall-clock is used only to line QSOs
  against the audio. Segments must share one sample rate/format (concatenated
  with `ffmpeg -f concat -c copy`).
- **Multiple EDI files merge into one timeline**: a session worked across
  several bands (e.g. 2M + 70CM) writes one EDI per band, but it's still a
  single physical recording. `edi` takes `nargs='+'`; `merge_edi` parses each
  file and concatenates+sorts by `dt` into one chronological QSO list. `Qso`
  itself carries no band field — the pipeline never needed one, since a QSO's
  band only mattered for logging, not for rendering.
- **CW decode is per-segment**: each WAV is one over at one speed, so a
  complex-demodulate envelope decoder with per-segment adaptive dit estimation
  is robust and yields absolute per-character timestamps for sync.
  `decode_segment` skips segments longer than `MAX_OVER_S` before doing any
  signal processing, since `gate_events` would reject them on duration alone
  regardless — this alone roughly halved total decode time on real recordings.
- **The demodulation pitch is auto-detected per segment** (`_detect_pitch`),
  not assumed to be a single `--pitch` (default 600 Hz) for the whole
  session — that argument is now only a fallback for the rare case nothing
  is found at all (e.g. true silence). Found from real received-signal
  segments: one RX segment's true tone was ~1296 Hz against the 600 Hz
  default, a 695 Hz gap entirely outside the envelope lowpass's passband
  (`LOWPASS_CUTOFF_HZ=120`) — not a decode-quality problem but a near-total
  loss of the actual signal before decoding even started (measured SNR
  near 0). The operator's own TX sidetone auto-detects to within ~1 Hz of
  600 Hz regardless (verified across several real TX segments from two
  different QSOs), so always auto-detecting is strictly better than only
  doing it conditionally.
- **Envelope filter/threshold constants** (`LOWPASS_CUTOFF_HZ`, `LOWPASS_NTAPS`,
  `THR_HI_FRAC`/`THR_LO_FRAC`): a windowed-sinc lowpass (`_lowpass_kernel`) plus
  hysteresis thresholding (`_hysteresis_on`), verified against real recordings to
  raise SNR for moderate-offset interference (~150 Hz+) with no effect on
  genuine nearby QSOs. See RECORDING.md's "CW decoder behaviour" section for
  the full before/after numbers and the hard limit on closer-in interference.
- **RX (received signal) decodes far worse than TX (the operator's own
  sidetone) at the same SNR, and needed its own fix.** Diagnosed against a
  real RX segment with known ground truth (`20260706_160342A.wav`, the
  user transcribed it by ear as `TU CFM 5NN TT3 TT3 JN86SR K`): despite a
  33 dB SNR (well above `MIN_SNR_DB`), the raw decode was gibberish. SNR
  measures average loudness, not the cleanliness of individual element
  edges — dumping the exact hysteresis run durations showed many on/off
  runs a fraction of a dit long (10-40 ms against a ~55 ms dit), fragmenting
  single dits/dahs into several pieces. The operator's own TX sidetone is
  a clean, locally-generated tone with none of this; a real received signal
  picks up QSB/AGC/near-threshold noise the sidetone never has to deal
  with. `_debounce_on` merges any on/off run shorter than
  `DEBOUNCE_DIT_FRAC` (0.5) of the segment's own *preliminary* dit estimate
  into its neighbour, run in `decode_segment` between hysteresis and the
  final (real) dit estimate — two passes, since the debounce threshold
  itself needs a dit estimate to scale against. Deliberately relative to
  the segment's own dit, not a fixed time: a fixed 30 ms threshold (tuned
  against this one file) silently ate *all* decode at 45 WPM in the
  existing synthesized-WPM regression test, where a dit is only ~27 ms —
  caught by that test, not by the real-data check, which is exactly why
  both exist. `THR_HI_FRAC`/`THR_LO_FRAC` were also lowered (0.5/0.3 →
  0.35/0.15) as part of the same tuning pass, both found via a grid search
  scored by edit distance to the known ground truth text. Net effect on
  the real July recording's first 20 minutes: 187 characters from 13
  trusted overs → 500 characters from 30, with no regressions in the
  existing decoder test suite (12-60 WPM) or on previously-good TX segments.
- **Trust gate** (`gate_events`): the long "listening / calling CQ" stretches
  between QSOs carry overlapping signals and noise at the CW pitch that decode to
  gibberish. A segment's decode is shown only if it is short (`< MAX_OVER_S`),
  loud enough (`>= MIN_SNR_DB`), word-shaped (`_quality >= MIN_QUALITY`), and not
  a chopped steady carrier (`_dominance <= MAX_DOMINANCE`, only checked at all
  once there's `>= MIN_CHARS_FOR_DOMINANCE` characters — see below). This keeps
  every real over and rejects the noise. Tune these constants, not the decoder,
  if a future recording gates too aggressively/loosely.
  - `MAX_OVER_S` is 35s (was 30s): raised after a real, correctly
    transcribable 32.5-second exchange (a full report + locator handoff)
    was being skipped before decoding even started. No clean statistical
    gap here the way there is for `FREQ_MATCH_TOLERANCE_HZ` — real segment
    durations form a continuum from 30s past 100s — so this is a modest,
    evidence-backed nudge for one confirmed case, not a broad guess; the
    other three gates still guard genuinely long listening periods that
    happen to land in the 30-35s range.
  - `MIN_CHARS_FOR_DOMINANCE` (5): any 2-character decode has dominance
    `>= 0.5` by construction (the two characters either match, giving 1.0,
    or don't, giving exactly 1/2 — never less), so `MAX_DOMINANCE=0.4` was
    structurally impossible to pass for *any* two-letter contest word
    ("TU", "R", "K"...), independent of content. Found from real,
    correctly-decoded "TU" and "73 EE" being silently dropped from the
    ticker. Below this length, `_dominance` just returns `0.0` — the
    "chopped carrier" pattern it guards against only shows up over many
    characters in practice anyway.
- **Long segments can still hide a real CW exchange between *other*
  stations**: our own recorder only splits a new WAV file on our own PTT,
  so a segment where we just listened to someone else's whole exchange --
  e.g. two stations negotiating a CW frequency over voice, working each
  other in CW, then moving on -- stays one long file, and `decode_segment`
  never even attempts it once it exceeds `MAX_OVER_S`. `decode_long_segment`
  recovers this: it finds telemetry-confirmed CW-mode sub-ranges within the
  segment (`cw_subranges`, from `build_state_events` -- exactly the same
  sub-division already used for the rig/rotator badge), extracts just that
  audio (`_read_wav_range`), and decodes it with `_decode_samples` (the
  actual pipeline, factored out of `decode_segment` so both can share it).
  The sub-range's own duration is deliberately *not* checked against
  `MAX_OVER_S` (`gate_events(..., check_duration=False)`) -- a real two-way
  exchange between other stations can easily run longer than one of our own
  overs, and the duration gate's only purpose was rejecting segments whose
  *unexplained* length made them suspicious; telemetry mode confirmation is
  already stronger evidence than length that this specific span is genuine
  CW, not noise. SNR/quality/dominance still apply. Verified against a real
  reported case (`20260706_163045A.wav`, 305s: FM voice → CW → SSB → FM →
  CW, the two stations negotiating a frequency and working each other) --
  recovered readable text from both CW windows, where before there was
  nothing at all. One known limitation: the two stations may key at
  noticeably different speeds, but dit-length is estimated once across the
  whole sub-range, which can degrade accuracy for whichever side differs
  most from that single estimate. `main()` loads WAV metadata and telemetry
  *before* decoding now (previously after), since finding these sub-ranges
  needs `state_events` up front. A second, easy-to-miss bug this exposed:
  `remap_audio_t`'s `--skip-gaps` trimming decides whether to shrink a
  segment to `GAP_KEEP_S` based on whether it has any `s.events` -- but a
  long segment's recovered content is deliberately kept *out* of `s.events`
  (to keep per-span burst-flushing correct in the ticker, see below), so
  without an explicit exemption `--skip-gaps` would trim exactly the
  segment whose audio was just recovered, and `concat_audio`'s `outpoint`
  would cut that audio out of the rendered file entirely. `remap_audio_t`
  now takes a `long_cw_segs` set (`id(seg)` for segments decode_long_segment
  found content in) to exempt them.
- **The ticker merges normal per-segment decodes with recovered long-segment
  spans into one chronological list before building the transcript**,
  flushing wherever the real gap since the previous chunk exceeds
  `MAX_OVER_S` -- the same threshold used everywhere else to tell a genuine
  over from a genuine gap -- rather than the old per-segment bookkeeping.
  This matters because a single long segment can contain *two* unrelated
  recovered exchanges (e.g. we followed one QSO, then later another, without
  ever transmitting in between): dumping both into that segment's own event
  list the way normal segments work would show them as one continuous,
  un-flushed burst, which is why they're kept separate from `s.events` and
  passed to `build_ass` as `long_cw_spans` instead.
  This same change independently fixed a second real bug, found watching
  an actual rendered video: a fresh CW QSO's ticker still showed a CW QSO
  decoded over four minutes earlier. Between the two, the operator worked
  several SSB/FM contacts, each individually short (`dur <= MAX_OVER_S`) --
  so no *single* segment in between ever looked like a "genuine gap" to
  the old per-segment `prev_was_gap` bookkeeping, which only checked
  whether the one immediately-preceding segment was long, regardless of
  how much real time had actually passed across several short ones
  combined. Keying the flush decision on the real time gap since the last
  *included* chunk fixes this the same way, with no special-casing needed.
- **UTC offset is derived**, not hardcoded: EDI times are UTC, WAV filenames are
  local; `derive_utc_offset` rounds the span-midpoint difference to whole hours,
  so DST is handled automatically.
- **Rendering is one ffmpeg pass**: the RX/TX badge and CW ticker (the only two
  overlays `build_ass` still produces — see "Terminal-session PiP" below for why
  the rest were removed) are one ASS subtitle file burned over an `showspectrum`
  waterfall (dimmed to ~0.42 luma so text stays readable). No frame-by-frame
  rendering. The waterfall fills the frame within the first ~80 s, then stays
  full. The terminal-session and webcam PiPs (also below) are composited in the
  same `filter_complex` graph, in the same ffmpeg invocation.
- The video keeps the recording's full length.
- **`--duration SECONDS` for a chronological preview cut**: trims to the first
  `SECONDS` of real session time — a straight, uncut trim (not a curated
  highlight reel; that was considered and rejected as much more machinery for
  a first cut). `trim_to_duration` runs *before* the CW-decode loop, not
  after, and drops segments past the cutoff outright rather than decoding the
  full session and discarding most of the result — the main cost of this
  pipeline is CW decoding, so a 10-minute preview of a 2-hour session decodes
  roughly 12x less audio. QSOs past the cutoff are filtered out of the merged
  list before chapters/SRT are built so nothing gets a chapter/caption with no
  time left in the clip to show it in.
- **`--webcam PATH` for a picture-in-picture selfie/webcam overlay**, bottom-
  right corner, muted (radio audio is the only soundtrack — the cam mic would
  just add room noise/echo of the operator's own on-air voice), mirrored with
  `hflip` since a phone's front camera records un-mirrored relative to what
  the operator saw in the viewfinder while recording. Sync is the interesting
  part: the webcam is a *different device* with its own clock convention, not
  necessarily the WAV recorder's — in the first real use of this feature the
  WAV recorder happened to stamp filenames in plain UTC while the phone
  stamped its own in local wall time, two different offsets for the same
  session. So the webcam's start position in the output timeline is derived,
  not assumed: `sync_webcam_start` wraps the whole clip as a synthetic
  one-segment "recording" and reuses `derive_utc_offset`'s own span-midpoint
  match against the *full* QSO list (never a `--duration`-trimmed subset —
  a short preview's QSO span is too narrow an anchor for reliable hour
  rounding) to find the webcam's own offset, then maps its true start onto
  the main timeline via `audio_time_for`. In `render()`, `-itsoffset` delays
  the whole cam stream's presentation timestamps so its own frame 0 lands
  exactly at that computed start — no input seeking needed, since the cam's
  own t=0 already *is* the first frame we want. `tpad=stop_mode=clone`
  clones the cam's last frame indefinitely so a clip a little shorter than
  the session (as in that first real case) can never end the shared
  ffmpeg filtergraph early and silently truncate the main waterfall/audio —
  a real risk class with multi-input filtergraphs, not a hypothetical.
  **The PiP's own video is explicitly resampled to `RENDER_FPS` before
  scaling** (`fps={RENDER_FPS}` in the `[1:v]` filter chain) — for a real
  reported bug: sync was correct at the start of a video but the audio
  read as over a second late by the end. A phone recording's video stream
  can claim a constant frame rate (`ffprobe`: `r_frame_rate` 30/1) while
  its own per-frame timestamps are genuinely variable — confirmed directly
  against the real webcam file by reading every packet's own `pts_time`:
  not one big pause but 3,444 scattered micro frame-drops across the ~2h
  recording (typical thermal/buffer pressure on a long phone capture),
  summing to exactly 0.753s of real elapsed time its raw frame count alone
  doesn't account for (218,052 frames at a flat 30fps only spans 7268.4s;
  the container's real, PTS-accurate duration is 7269.12s). Without an
  explicit `fps=` filter, the PiP branch was effectively laid out by frame
  count rather than by each frame's own true timestamp, so it silently ran
  very slightly fast relative to the audio-driven main timeline the whole
  way through. `fps=` resamples using the decoder's true per-frame PTS as
  its reference, duplicating frames onto a clean, constant `RENDER_FPS`
  grid that absorbs every one of those scattered drops — eliminating the
  drift instead of just reducing it.
  **This alone did not fix a second, separate reported drift** ("video
  ahead of audio" by 1:48 into a ~2h session, confirmed by ear and
  independently by uploading to YouTube): the phone and the radio recorder
  are two *independent* devices, and their clocks simply don't tick at
  exactly the same *rate* — a small, real crystal-oscillator mismatch,
  unrelated to the frame-drop issue above (that one was already verified
  fixed via a labeled-frame re-composite test showing sub-3ms accuracy a
  full hour in). `sync_webcam_start`/`derive_utc_offset` only ever correct
  a whole-hour *offset* (timezone/DST, by design — see below); any
  sub-hour clock *rate* skew between the two devices was passing straight
  through uncorrected the whole time, invisible to every test that only
  checked "does the render faithfully apply whatever webcam_start it was
  given" rather than "is webcam_start itself correct". Diagnosed by ear:
  the webcam has its own audio track (unused in the final output — see
  below — but still on disk), and the operator's own voice reaches both
  the phone's mic and the radio's mic at the same real-world instant.
  Extracting the speech onset from both (a simple RMS envelope threshold)
  and comparing against the assumed `webcam_start` showed a **growing**
  gap: sampling confident anchors across the real session found the needed
  correction climbing smoothly from ~0s near the start to ~+3.2s near the
  end — a linear drift, not a constant one, and not something a single
  offset (however well chosen) can correct for the whole video at once.
  `refine_webcam_start` fits this directly: for several of the operator's
  own TX segments (`s.ptt`, at least 1.5s, sampled evenly across the
  *whole* session so the fit has real time range to constrain the rate —
  an earlier version that only took the first few anchors clustered them
  in the first few minutes and got a near-meaningless rate estimate) it
  reads that segment's own radio audio straight from its WAV file, and
  cross-correlates it (`_find_offset_correction`, via `_rms_envelope` — a
  coarse amplitude-rhythm signature, robust to the very different
  frequency/timbre of two different microphones/paths capturing the same
  speech, unlike correlating raw waveform samples directly) against a
  padded window of the webcam's own audio extracted around where the
  coarse sync predicts it should be. Only anchors above `min_confidence`
  (0.3 — real data showed a clean gap between spurious matches at
  0.08-0.29 and genuine ones at 0.34-0.77) are kept, and a degree-1
  least-squares fit (`np.polyfit`) across their `(audio_t, correction)`
  pairs gives both the corrected intercept (folded into `webcam_start`,
  same meaning as before) and the rate. Applying a linear rate needs more
  than `-itsoffset` (a constant shift): `render()` applies
  `setpts=PTS/(1-webcam_rate)` on the PiP branch, *before* the `fps=`
  resampling above (so the resampling itself operates on the
  already-corrected timeline) — stretching or compressing the PiP's own
  presentation timestamps just enough to compensate. Verified against the
  real recording: at the exact reported drift point, the coarse-only
  mapping was off by 2.73s; the rate-corrected mapping was off by 0.07s.
  **`--webcam-offset SECONDS` is a manual fallback**, added in the same
  pass: a fixed correction added to the coarse `webcam_start`, bypassing
  cross-correlation entirely (no rate compensation) — for a webcam clip
  with no audio track, or wherever cross-correlation can't find a
  confident match.
  **For a logger-recorded webcam (Alt+V, same machine — see
  puskas_logger.py's own Webcam capture notes), none of the above
  cross-correlation/rate-fitting machinery is needed at all**: since Alt+V's
  stop handler now renames the file with a µs-precise timestamp,
  `parse_webcam_precise_filename` reads it straight from the filename —
  preferred over `webcam_start_from_log` (the `*-webcam.log` sidecar, same
  precision but depends on that file surviving alongside the video) and
  `webcam_start_wall` (the logged `webcam_start` event, ~1s early), in that
  order, both kept as fallbacks for recordings made before the rename
  existed. The independent-device cross-correlation/drift-rate path above is
  only ever reached for the phone-clip case, where there genuinely are two
  unrelated clocks to reconcile.
- **`--cast PATH` for a terminal-session picture-in-picture**, replacing what
  used to be separate QSO panels, a running-score header, a UTC clock, and a
  typewriter overlay of what was typed. `PATH` is an asciinema (cast v2)
  recording of the tmux session running irssi + `puskas_logger.py` during the
  contest (see "Recording the logger session" below for how to make one) — the
  logger's own screen already shows the callsign/band/mode/QSO list/timestamp
  live, so reconstructing any of that as a separate overlay was pure
  duplication once this became possible. The only thing the terminal session
  *can't* show is RX/TX: `puskas_logger` has no way to know the rig's real PTT
  state until the WAV recordings are downloaded from the SD card after the
  contest and their IC-9700 metadata is read back — that's what the badge
  (above) is still for.
  - **Rendering the cast is its own pipeline stage** (`render_cast_video`),
    producing a standalone intermediate mp4 before the main `render()` call,
    in the same spirit as `concat_audio`'s intermediate wav — not rendered
    frame-by-frame inline with the main waterfall/ASS pass, since replaying
    terminal escape codes into pixels is a different kind of work entirely.
    It uses `pyte.Screen`/`pyte.ByteStream` to replay the cast's terminal
    escape codes into a `pyte` screen buffer, and Pillow (`ImageFont`/
    `ImageDraw`) to draw each character cell onto a canvas which is piped
    straight into `ffmpeg` as raw video frames (`-f rawvideo -pix_fmt rgb24`)
    — no intermediate PNG files per frame.
  - **Only redraw rows pyte marks dirty, not the whole canvas every
    frame**: a first implementation redrew every row on every tick and took
    123.8s to render a 76.9s clip (0.62x realtime — impractical for a full
    contest-length session, which would have taken hours). `pyte.Screen`
    already tracks exactly which rows changed since the last time it was
    read (`screen.dirty`); redrawing only those onto a canvas that persists
    across frames (rather than rebuilding from scratch) cut this to 25.6s
    for the same clip (~3x realtime, ~40min for a full 2h session) — a 5x
    speedup, verified on the same input before/after the change.
  - **Line height must come from the font's own metrics, not a rule of
    thumb**: `lh = int(CAST_FONT_SIZE * 1.2)` (a common monospace
    line-height approximation) undershot DejaVu Sans Mono 13pt's real
    `ascent + descent` (17px vs. the approximation's 15px) by enough that
    descenders (e.g. underscores) got clipped by the *next* row's own
    background-clearing rectangle on redraw — found from the user directly
    reporting "some characters are off, seems to be some buffer garbage" in
    a rendered preview. Root-caused by comparing the direct pre-encode
    canvas against the same frame decoded back out of the rendered mp4
    (ruling out video compression as the cause), then checking
    `font.getmetrics()` against the row height actually in use. Fixed with
    `lh = ascent + descent`; verified via a before/after pixel-diff of the
    exact same frame (39 differing pixels with the old formula, 0 with the
    fixed one) before writing the permanent regression test
    (`test_draw_cast_row_descender_survives_the_row_belows_own_redraw`).
  - **A second, separate "buffer garbage" artifact the user spotted (stale
    startup-screen text still showing behind the contest screen) is a real
    renderer bug, fixed by `_CastScreen`/`_CastStream`** — subclasses of
    `pyte.Screen`/`ByteStream` that implement three CSI sequences stock pyte
    silently drops. The cast is recorded with the logger running inside
    **tmux** (two panes: irssi + logger), and tmux clears/scrolls a *single*
    pane by setting left/right margins (DECSLRM, `CSI Pl;Pr s`) and then
    scrolling within them (SU `CSI Ps S` / SD `CSI Ps T`). pyte implements
    none of the three (verified: no `S`/`T`/`s` in its CSI dispatch table, and
    `?69h`/DECLRMM is ignored), so the pane was never actually cleared — when
    the logger cleared its screen (`\x1b[2J\x1b[H`, which tmux translates into
    a per-pane `\x1b[97;191s\x1b[51S`) and redrew shorter content, the old
    tail stayed on screen. `_CastScreen.scroll_up`/`scroll_down` mirror pyte's
    own `index`/`reverse_index` but operate cell-by-cell within *both* the
    top/bottom margins (pyte's `self.margins`) and the left/right margins
    (`self.margins_lr`, new), so only the pane's own columns shift; the other
    pane and the `│` separator are untouched. `set_left_right_margins`
    distinguishes DECSLRM (2 params) from a bare `CSI s` = SCOSC save-cursor
    (<2 params). `_CastStream.csi` adds the three missing final bytes to the
    dispatch table. **This corrects an earlier diagnosis** that called the
    same garbage a genuine *source* artifact ("the logger omits
    erase-to-end-of-line; any correct terminal would show it too") and left it
    as-is — wrong on both counts: `asciinema play` (a correct emulator that
    *does* honour SU+DECSLRM) always showed the cast clean, and the erase is
    tmux's, not something the logger owes. Verified end-to-end on the real
    July cast: the transition frame that showed `no QSOs yet5LA-2M.edi (34
    QSOs)…` and `JN97TFS]:` now renders `no QSOs yet` and `JN97TF` cleanly
    (regression tests `test_stock_pyte_leaves_stale_pane_content` /
    `test_cast_screen_clears_only_the_pane_columns`). The dirty-row redraw
    optimization is unaffected — `_scroll` adds the rows it shifts to
    `screen.dirty`, so the incremental canvas still picks them up.
    (A recording made *outside* tmux, or of a single pane, never emits these
    sequences — the logger's own `\x1b[2J\x1b[H` is handled by stock pyte
    fine — so this only bites tmux-captured casts, which is the documented
    recording method.)
  - **Layout**: `CAST_PIP_WIDTH_FRAC`/`CAST_PIP_X_FRAC`/`CAST_PIP_Y_FRAC`
    position the cast as a large PiP — the dominant visual element, not a
    small inset, since the terminal session is most of what there is to
    watch — occupying most of the frame below the RX/TX badge (top-left)
    and above the CW ticker (bottom-center), with the small webcam PiP (if
    used) in the bottom-right corner, clear of the cast box horizontally
    regardless of its vertical extent. The fractions are sized against
    `render_cast_video`'s *real* output aspect ratio (~1.69 for a 191x52
    DejaVu Sans Mono 13pt terminal — a first mockup used ~1.91, since it
    was rendered before the descender-clipping line-height fix above
    shortened rows; using the stale aspect left too little room below the
    box and would have visually covered the ticker text). Verified
    end-to-end (not just the two unit-tested bugs above) with a full
    synthetic render — a silent WAV session plus `hello.cast` — checking an
    actual decoded frame from the output mp4 to confirm the PiP box clears
    both the badge above and the ticker's reserved space below at both
    1080p and 720p.
  - **Sync is exact, unlike the webcam**: an asciinema cast file's header
    `timestamp` field is a Unix epoch — real, absolute UTC with no
    filename-parsing or whole-hour-rounding ambiguity the way
    `parse_webcam_wall` has for a phone clip (see `sync_webcam_start`
    above). `parse_cast_header` reads it directly; `main()` computes the
    cast's start position in the output timeline with a single
    `audio_time_for(cast_wall + timedelta(hours=offset_h), segs)` call — no
    `stream_start(cast_wall + timedelta(hours=offset_h), segs)` call — no
    `refine_webcam_start`-style cross-correlation needed, since there's no
    second physical clock to drift against.
  - **A stream that began before the audio must be entered partway in, not
    clamped to t=0.** `stream_start` is `audio_time_for` except for exactly
    this case: `audio_time_for` clamps any wall time before the first WAV
    segment to `segs[0].audio_t`, which pins the cast's own frame 0 to video
    t=0 and makes everything in the PiP read late by the whole cast-to-WAV
    gap — observed as the cast PiP's clock lagging the session by 25 s.
    This is the **normal** case, not an edge case:
    `run-recorded-contest-session.sh` starts asciinema before the radio
    recorder is switched on, so every cast made through the documented
    entrypoint begins ahead of the audio; the logger's `.scope` recorder
    (starting when the radio connects) is in the same position, which is why
    `--scope` goes through `stream_start` too. `render()` turns the negative
    value into an `-ss` seek *into* that input
    (`_stream_input_args`) rather than a negative `-itsoffset`, which ffmpeg
    has no meaningful interpretation for — the frames before t=0 are simply
    ones the output never shows. Verified against real ffmpeg rather than
    just asserted on the command string: overlaying a timer-labelled clip
    with `-ss 25` puts that clip's own t=25 at output t=0 and advances 1:1
    from there (t=3 → 28, t=7 → 32), confirmed by pixel-matching the
    composited PiP region back against the source clip's frames. The
    `enable=` gates clamp to `max(start, 0.0)`, since a negative threshold
    is trivially always true. One knowingly-accepted imprecision: with `-ss`,
    the cast branch's `setpts` drift correction measures from the seek point
    rather than the cast's true frame 0, losing `seek × cast_rate` of
    correction — ~11 ms for a 25 s seek at the real measured drift rate, a
    third of a frame at `RENDER_FPS`. For the same reason,
    `render()`'s cast branch has no `setpts` rate-correction term: the cast
    mp4 is `render_cast_video`'s own synthetic, constant-framerate output,
    not an independent recording device, so only a plain `fps=RENDER_FPS`
    resample onto the shared clock is needed (the `-itsoffset` position is
    already exact). `tpad=stop_mode=clone` still applies, for the same
    reason as the webcam branch: a cast shorter than the full session can't
    be allowed to end the shared filtergraph early.
- **Ticker clears in gaps, doesn't linger**: a ticker event's display end is capped
  to `TICKER_HOLD_S` (3 s) after its last character, even if the next real
  character is minutes away across a listening gap. Without this cap the last
  decoded text stayed on screen for the entire gap, showing stale info.
- **Ticker/panel/chapter/caption timing all come from real audio structure, not
  the EDI clock**: the EDI contest format only stores QSO time to the *minute*
  (no seconds field exists in the format at all), so `parse_edi`'s `qso.dt` is
  always truncated toward zero seconds — using it directly to decide when to
  flush the ticker or switch panels could land seconds *into* the next real
  over, appending that over's opening characters onto the previous QSO's
  leftover ticker transcript instead of starting fresh (this happened; the
  regression test is `test_ticker_does_not_leak_across_a_genuine_gap`).
  `cluster_starts(segs)` instead finds, purely from the decoded WAV segments,
  every real over that immediately follows a genuine listening gap (a segment
  with no trusted events and `dur > MAX_OVER_S`) — that is the true start of a
  fresh burst of on-air activity, sub-second precise, independent of any
  clock. The ticker flushes exactly there (see the `build_ass` ticker loop).
  `qso_windows()` snaps each QSO's approximate EDI-derived position onto a
  cluster start via `_snap_to_cluster` — the *latest* cluster at or before
  that approximate time, **not the nearest one**. A QSO's own over always
  starts before it gets logged, so "nearest" can jump ahead to the *next*
  contact's burst if the current QSO took a while (calling, retries) to
  complete — this was a second real bug the user caught by spotting that a
  QSO's panel showed the *following* contact's actual start time (regression
  test: `test_qso_window_snaps_to_own_burst_not_the_next_ones`). Since
  `build_chapters`/`build_srt` are built from `qso_windows()`'s windows too,
  chapters and captions inherit both fixes.
  A third real bug: when a QSO's approximate time is *before every* detected
  cluster (e.g. an early QSO, or any QSO on a mostly-voice recording where
  little or no CW ever gets decoded), `_snap_to_cluster` used to fall back to
  the *first* cluster in the whole recording — pulling an early QSO's panel
  minutes into the future (regression test:
  `test_qso_window_before_any_cluster_uses_approx_time`). It now falls back
  to the raw approximate time itself in that case.
  A fourth: `cluster_starts` originally required `s.events` (successful CW
  decode) to mark a burst start, so it was blind to every voice-mode over —
  there's no CW there to decode no matter how good the decoder is. On a
  mostly-voice recording this left almost no burst to snap to at all
  (regression test: `test_cluster_starts_counts_voice_segments_too`). It now
  keys on segment duration alone (`dur <= MAX_OVER_S`), since a WAV segment
  boundary is a precise real-world RX/TX transition regardless of content —
  voice and CW alike. Verified against the real "mix" recording: clusters
  went from 5 to 27 across the 51-minute session.
  **A tempting further step, rejected after testing**: making *every*
  real-over segment a candidate (not just the first one per coalesced
  burst) looked appealing for pinpointing exactly which segment within a
  burst a voice QSO started on, but it regressed the CW round's
  independently-verified precision — a single QSO's own multi-over exchange
  spans several segments, and "latest candidate at or before the logged
  time" then lands on a *later point within that same QSO's own exchange*
  rather than its true start (confirmed: QSO 2's panel shifted from the
  verified-correct 520.03s to a wrong 579.14s). Coalescing to one candidate
  per burst is what makes "latest cluster" mean "start of *this* exchange"
  rather than "some segment inside it" — don't remove it.
  **Resolved with a heuristic, not telemetry**: a burst can begin with the
  operator listening (RX) before their own initiating transmission, so the
  burst's first segment isn't always where a QSO really starts (e.g. the
  recording starting mid-listen, before any TX). `_tx_start` finds the real
  start within a burst without needing PTT data at all: RX and TX strictly
  alternate (the recorder splits on every switch), and a TX segment — a
  brief call or report — is consistently shorter than the RX segment either
  side of it. So whichever alternating phase (even/odd position in the
  burst) has the shorter median duration is TX, and its first occurrence is
  the real start (regression test:
  `test_cluster_starts_skips_leading_rx_to_find_the_tx_start`, built from
  the exact real durations the user identified by ear: RX 26.11s, TX 2.13s,
  RX 5.54s, TX 5.41s). Verified to leave the CW round byte-for-byte
  unchanged — every one of its bursts already happened to start on TX, so
  there was nothing to correct there; the heuristic only ever moves a snap
  point *later* within its own burst, never earlier or into a different one.
  **Known unsolved case, from the user directly**: this breaks down while
  calling CQ — a stretch of many brief TX calls with only short listening
  gaps in between has no single "real" start, and an earlier fruitless call
  looks identical to the one that finally got answered. No fix attempted;
  falls back to the burst's first segment when the two phases aren't
  distinguishable (equal medians, or fewer than one of each).
  There is deliberately no more `LEAD` pre-show constant: once panel timing is
  snapped to the real over, showing it exactly when the over starts *is* the
  natural lead (the over itself takes several seconds), so an artificial
  pre-show margin is no longer needed and was removed.
- **Rig/rotator overlay: WAV metadata is ground truth, telemetry is a
  refinement.** Shows a top-left `● TX`/`● RX` badge plus a QRG/mode/bearing
  line (`144.174 MHz  CW  ROT 135°`) underneath. This went through two
  designs before landing here, both driven by real bugs spotted from
  watching an actual rendered preview:
  - **Design 1 (telemetry-only)**: one `SegState` per `Segment`,
    majority-voted from whichever 1 Hz `puskas_logger` telemetry samples
    fell inside the segment's span. Wrong in two ways: freq_hz/mode
    genuinely can change *within* one long idle/listening segment (nothing
    to split the WAV on there), so a majority vote could let an early
    stable reading outvote a later stretch of continuously-tuned,
    individually-unique readings for minutes; and even ptt itself could lag
    the true transition by up to a second, since a 1 Hz poll isn't
    synced to the WAV split at all.
  - **Design 2 (telemetry, split into runs)**: fixed both problems from
    within telemetry alone -- sub-dividing freq_hz/mode into runs *within*
    a segment, and taking ptt from the segment's *last* known telemetry
    reading (most likely to have caught up) rather than the first or a
    vote. Both real, working fixes -- but then the user discovered
    something better while inspecting a WAV file directly.
  - **Design 3 (current): the WAV files themselves carry this ground
    truth.** IC-9700 "Voice Recorder" mode embeds a `title` metadata tag in
    every WAV file it writes, e.g. `IC-9700 Voice Recorder Data
    144.299.84 USB    ----.---.-- ------ -- TX 2026-07-06 16:00:37` --
    frequency, mode, and RX/TX, straight from the rig at the exact instant
    it started recording that file, with *no polling lag at all* (unlike
    telemetry, which is a separate, unsynced 1 Hz poll). `parse_wav_title`
    parses this (mode aliases USB/LSB/AM/DSB/SAM normalized to `SSB`,
    matching `puskas_logger._mode_str`); `read_wav_metadata` populates
    `Segment.freq_hz`/`.mode`/`.ptt` from it. `_read_wav_title` reads the
    RIFF `LIST/INFO/INAM` chunk directly rather than shelling out to
    `ffprobe` per file -- measured 707 files at ~112s via `ffprobe` vs.
    ~0.02s reading raw chunk headers (~6500x), since ffprobe's per-process
    spawn cost dominates at this file count even though the work itself is
    trivial.
  - **ptt no longer needs telemetry at all**: unlike freq/mode it cannot
    legitimately change mid-segment (a real transition is exactly what
    causes the recorder to cut a new WAV file), so `s.ptt` alone is
    authoritative for a segment's whole span -- Design 2's "last sample"
    fix is now dead code, deleted rather than kept as a fallback.
    `puskas_logger.py` no longer queries or records ptt in telemetry at
    all (see below) -- it would just be reconstructing, with more latency,
    something the WAV file already has losslessly.
  - **freq_hz/mode still benefit from telemetry**, though, for exactly
    Design 1's original reason: the WAV metadata is fixed at
    file-creation time, so on a long segment with no PTT activity at all,
    it only captures the *starting* frequency/mode. `build_state_events`
    seeds each segment's first run from the WAV value and lets telemetry
    sub-divide it further wherever a later sample shows a genuine change.
  - **The WAV value and telemetry's own reading don't agree to the exact
    Hz even when nothing changed** -- a second real bug, found immediately
    after switching to Design 3, from comparing the two sources directly.
    Checked against the real July round: a systematic disagreement of
    160/250/300/310 Hz (depending on band) appears on nearly every
    segment's very first telemetry sample, which without a tolerance
    looked like a spurious retune at the start of almost every segment.
    Genuine retunes in the same data are >=1000 Hz (mostly round kHz
    steps, as a human tuning by hand would produce) -- a clean gap, zero
    occurrences between 310 Hz and 1000 Hz -- so
    `FREQ_MATCH_TOLERANCE_HZ = 500` safely separates the two. Mode has no
    such problem (exact string match; "SSB" vs "CW" isn't a rounding
    question).
    **The cause was later identified, and it was not the two sources
    disagreeing at all**: the 1 Hz sampler built its `freq_hz` by re-parsing
    the logger's own toolbar string (`f"{freq_hz / 1e6:.3f}"`), quantizing to
    the nearest kHz — 144299840 → `"144.300"` → 144300000, exactly the
    reported 160 Hz, and a bound of 500 Hz explains the observed clean gap
    below 1000 Hz precisely. Telemetry now records the radio's own exact Hz
    (see puskas_logger's Telemetry recorder section), so new recordings don't
    have the disagreement; `FREQ_MATCH_TOLERANCE_HZ` stays for the older ones,
    which do.
  - A segment with no WAV metadata at all (freq_hz/mode/ptt all `None` --
    not an IC-9700 recording, or a parse failure) is skipped rather than
    guessed at from telemetry alone. `az` has no equivalent in the WAV
    metadata at all and is purely telemetry's own — the median of whichever
    samples fall in a run, **falling back to the last azimuth seen before
    that run began** when the run holds none of its own; only a run with no
    az anywhere before it falls back to `ROT ---`, matching the logger's own
    toolbar. The carry-forward is what makes change-only telemetry work: the
    rotator poller writes a line only when the azimuth actually moves, so a
    rotator parked on one bearing for a whole QSO leaves no sample inside
    that run at all, and a median of nothing would render `ROT ---` despite
    the rotator being online and pointing somewhere known. az is a step
    function — it holds until the next event, with no staleness *time*
    horizon (deliberately: a horizon would be a tunable constant with no data
    behind it, and freq/mode already carry forward unboundedly from their WAV
    seed the same way). What ends it instead is an explicit event: **the
    rotator going offline writes one `{"az": null}` line, and that null is a
    real mark carrying `None`, not a record to skip.** `TelemetrySample`
    therefore carries `az_offline` alongside `az`, since an *absent* `az` key
    (a rig event — silence about the rotator) and an explicit `"az": null`
    (the rotator died) both load as `az=None` while meaning opposite things:
    silence must not end the carry-forward, a null must. Filtering `az_marks`
    on `az is not None` alone was a real bug caught in review — it discarded
    the offline marker and showed the last known bearing for the rest of the
    video (regression test:
    `test_explicit_az_null_ends_the_carry_forward`, with
    `test_a_rig_event_does_not_count_as_an_az_reading` guarding the mirror
    case). This also matters for the *old* dense recordings, which wrote
    `"az": null` on every line while rotctld was offline — the whole July
    round is 9313 such lines, so treating them as marks is what keeps it
    correctly showing `ROT ---` throughout. It also fixed a pre-existing bug on the *dense* old
    recordings, where a run shorter than the 1 Hz sample spacing could
    contain no sample by luck: replaying the real August round found 184
    such runs that used to show `ROT ---`, with run boundaries and freq/mode
    byte-identical before and after.
  - **`load_telemetry` accepts both stamp precisions** — whole seconds (the
    original 1 Hz sampler) and microseconds (the current change-driven
    writer). Note the failure mode if it didn't: `load_telemetry` swallows a
    bad line via `except ValueError: continue`, so a strict whole-second
    format would *silently* drop every line of a new recording rather than
    erroring.
  - `--telemetry PATH` is therefore now *optional* for the whole badge --
    it only adds `az`/bearing and the within-segment freq/mode refinement.
    The RX/TX + starting QRG/mode badge works from the WAV files alone.
- **YouTube chapters + SRT for seeking without scrubbing**: alongside the mp4,
  `main()` writes `<out>.chapters.txt` (paste into the YouTube description) and
  `<out>.srt` (upload as a captions track) — both built from `qso_windows()`, the
  same start/end used for the on-screen QSO panels. YouTube requires the first
  chapter at `0:00` and each chapter at least `MIN_CHAPTER_GAP_S` (10 s) apart, so
  `build_chapters` always emits a leading `0:00 Start` and drops any QSO whose
  chapter would land closer than that to the previous one — those QSOs still get
  an SRT cue, just no separate chapter marker. SRT cues are capped to
  `CAPTION_DUR_S` (8 s) each so they read as short captions rather than
  persisting on screen until the next QSO.
- **`--input-log PATH` for exact chapter/caption timing**, when available.
  `PATH` is a `puskas_logger *-input.jsonl` file (see below) — optional, so
  older recordings without one still render normally, falling back to the
  EDI-minute + cluster-snap timing described above. `load_input_log` parses
  two event kinds sharing the file, into one `InputLogEvent` list (`kind` is
  `'text'` or `'qso'`); only `'qso'` events are used by `contest_video.py`
  itself now — the `'text'` keystroke stream used to drive an on-screen
  typewriter overlay, but that overlay was removed once the terminal-session
  PIP (see below) started showing the actual logger UI live, keystrokes and
  all, making a reconstructed text overlay redundant. `load_input_log` still
  parses both kinds (rather than dropping `'text'` events at load time)
  because the file format is shared with nothing else that would need
  changing, and a future overlay could still want them.
  - **`'qso'` events → exact QSO/chapter/caption timing.** `match_qso_times` pairs
    each `Qso` (from the EDI, minute-precision) to its `'qso'` event by
    **call, in chronological order within that call** — not by exact
    minute, even though `puskas_logger` derives both `q.dt` and the event's
    `t` from the same captured `now` and so *could* match exactly by
    `(call, minute-truncated time)`. That was the first implementation, and
    it was wrong: it silently breaks the moment a hand-crafted log (seeded
    from the EDI via `--seed-input-log`, then hand-tuned against the audio —
    see below) has an edited timestamp cross a minute boundary from what the
    EDI happened to record, which is exactly the kind of edit the feature
    exists to make possible. Call+order has no such trap — a `--duration`
    cut only ever removes a *suffix* in time, so the surviving occurrences
    of any call are still a prefix of the full sequence, and "next unused"
    stays correct regardless of what the edited timestamps say.
    `qso_windows` then feeds that exact time into `_snap_to_cluster` in
    place of the EDI's coarse `q.dt` wherever a match exists. The snap
    itself is still necessary even with an exact timestamp — the moment the
    operator hits Enter is the *end* of data entry, at or after the real
    over, not its start — but an exact anchor removes the EDI's
    minute-level slop that could otherwise point the snap at the wrong
    neighbouring burst, which is what caused visibly wrong QSO timing in
    the first video generated with this feature. Falls back to the plain
    EDI `q.dt` per-QSO wherever unmatched (no input log, an older
    recording, or a `--duration` cut that excludes the matching event).
  - **Only a QSO's *start* is ever a heuristic — its end doesn't need to
    be.** `qso_windows` used to close a QSO's window exactly when the
    *next* QSO's window opened (or at `total` for the last QSO) — but
    `qso_times` gives an exact, real end for a QSO wherever known: the
    moment the operator hit Enter. `windows[i][1]` is `qso_times[i]`
    (mapped to video time) wherever available, so the chapter/caption
    boundary lands the moment the QSO is actually finished, rather than
    running until the next QSO's own over starts. Falls back to the old
    "next QSO's start" (or `total` for the last QSO) wherever
    `qso_times[i]` is `None` for that particular QSO — no better
    information exists then. (This used to also gate a QSO panel's on-screen
    lifetime and a running score in the header — see git history before the
    terminal-session PIP replaced both; `qso_windows`' timing itself is
    unchanged, only what consumes it.)
  - **Two (or more) QSOs sharing one burst is a second, separate timing bug
    `qso_times` exposed**: the same station worked on multiple modes
    back-to-back with no real listening gap between them (e.g. SSB, then
    FM, then CW with the same callsign, all within a couple of minutes) is
    *one* burst as far as `cluster_starts` is concerned — there's no audio
    structure to tell the individual overs apart at all. Snapping every one
    of those QSOs' anchors onto that single shared cluster start collapsed
    all their windows onto the same instant, which showed up as overlapping
    chapters/captions. `qso_windows` now tracks the previously resolved
    cluster: when a QSO's anchor resolves (via `_snap_to_cluster`) to the
    *same* cluster as the previous QSO **and** an exact `qso_times` entry is
    available, it starts exactly where the *previous* QSO's own window ended
    (its real, known finish) instead of the shared cluster start — not
    audio-structure-precise either, but real, and leaves no overlap and no
    gap between the two. Without `qso_times` for that QSO, falls back to the
    original squeeze behaviour.
  - **`--seed-input-log OUT.jsonl`**: writes one `'qso'` event per QSO from
    the EDI(s) (`t` is just `q.dt` with seconds zeroed) and exits without
    rendering — for a recording made before this feature existed, so there's
    no automatically-generated `*-input.jsonl` to fall back on. Edit each
    `t` against the audio, then pass the result back in as `--input-log` for
    exact chapter/caption timing with no cluster-snapping guesswork involved
    for those QSOs. This is what `match_qso_times`'s call+order (not
    call+minute) matching exists for — a seed's timestamps are expected to
    move freely across minute boundaries once hand-edited.
- **`--scope PATH` for a real-spectrum waterfall background**, replacing (wherever the
  recording covers) the audio-derived `showspectrum` reconstruction with the radio's own
  actual scope sweeps — see icom_net.py's own "Scope (spectrum waterfall) data" section
  above for where `PATH` (a `.scope` recording) comes from and the wire format.
  - **Sync is exact, the same way `--cast` is**: each sweep's timestamp in the recording
    is real `time.time()`, so `render()` positions the rendered scope clip with a plain
    `-itsoffset`, no drift-rate correction needed (unlike the webcam's independent,
    drifting camera clock).
  - **Drawn as a full-frame layer *under* the subtitles pass**, not as a PiP on top of it
    like `--cast`/`--webcam` — it needs to actually replace the background, and the
    ticker/badge text still needs to render on top of it. `render_scope_video` first
    renders the `.scope` file into its own standalone clip (same
    `render_cast_video`-style separate-stage-then-composite pattern used throughout this
    file), scrolling one row per *real* sweep — held static between sweeps to fill real
    elapsed time at `RENDER_FPS`, not resampled to a constant per-sweep rate — so the
    clip's own duration equals the recording's real elapsed capture time. `render()` then
    overlays that clip onto the existing `showspectrum` layer with
    `enable='between(t,scope_start,scope_end)'` — the same gating idiom already proven
    for `--cast`/`--webcam`'s own start gate, reused here for the *end* gate too, rather
    than introducing a second mechanism (`eof_action=pass`) for what's really the same
    class of problem. Falling back to the audio-based waterfall for any stretch the
    recording doesn't cover (not started yet, stopped early, or outside a `--duration`
    cut) comes for free from this: the old layer is simply never covered there.
  - **Input-index bookkeeping generalized, not special-cased**: `render()` used to assume
    cast was always input 1 (if present) and webcam was 2-if-cast-else-1. Adding a third
    optional video input made that assumption break, so all three (scope, cast, webcam)
    now get their input index computed up front from which ones are actually present,
    in a fixed order (scope, cast, webcam), and each branch references its own
    precomputed index rather than guessing it from what came before.
  - **The on-screen frequency-range label** (top-right, e.g. `144.100-146.100 MHz`) is a
    separate ASS overlay (`build_ass`'s `scope_periods` parameter, new `ScopeFreq`
    style), not baked into the rendered waterfall image — added because the operator can
    QSY or change span mid-recording, which the label needs to track.
    `scope_freq_periods` walks the recording's own sweeps and emits one period per
    stretch the frequency range stayed constant. **A real bug here was only caught by
    rendering an actual small end-to-end video and inspecting real frames, not by either
    layer's own unit tests**: an early version extended the *last* period's end to the
    video's full duration, reasoning that the label "shouldn't vanish" if the recording
    stopped before the session did — but the scope *background* itself doesn't persist
    past its own last sweep either (same `between(scope_start,scope_end)` gate above), so
    that extension left the label showing a stale frequency range over what had already
    fallen back to the audio-spectrum background. `scope_freq_periods`'s own unit tests
    (checking the function in isolation) and `render()`'s filter_complex string-matching
    tests (checking the overlay chain in isolation) both looked correct on their own —
    the bug only existed in how the two independently-correct pieces combined, which
    string/unit assertions can't see and only an actual rendered frame exposed. Fixed by
    simply not extending past the last real sweep at all — matches `scope_end` exactly,
    since both are derived from the same underlying last-sweep timestamp.
  - **The real end-to-end smoke test** that caught the above bug used fully synthetic
    fixtures (a short synthesized WAV, a one-QSO EDI, and a hand-built `.scope` file with
    a deliberately fast, visually-distinctive drifting "signal" — not a real radio
    capture), specifically so it could be constructed with exact, known-in-advance timing
    (session start, QSY point, scope start/end) to check against, and run in seconds
    rather than needing a real multi-minute recording. This is a one-off manual
    verification technique worth reusing for future contest_video.py changes that are
    hard to unit-test in isolation (anything about how the ffmpeg filter graph's pieces
    actually combine visually) — not (yet) a permanent addition to `tests/`, since it
    shells out to real ffmpeg and takes real wall-clock time, unlike the rest of the
    suite.

### HUD (DOOM-style status bar) — data layer + preview

A full-width opaque status bar modelled on DOOM's, replacing readouts that are
technically visible in the terminal PiP but too small to read: the logger's own screen
renders at ~13px in the cast, so *importance* has no visual weight there. The HUD's rule
is that the more important a value, the bigger it is drawn.

- **The DOOM mapping is the design**, not decoration: SCORE takes the health slot (the
  biggest number, flashing and counting up as it lands), QSOS takes ammo, the **webcam
  takes the face slot** (a square centre-crop of the operator, exactly where DOOMguy's
  portrait sits), Vd/Id take armor, and band/mode chips take the weapon-slot strip.
  Everything else — QRG, RX/TX lamp, S-meter bar, compass, UTC/rate/best-DX, CW ticker —
  fills the remaining panels.
- **The drawing layer is itself split into chrome and values.** `draw_hud_chrome` paints
  panel frames, recesses, the compass rose and *every* static label, and runs **only when
  no `--hud-background` is given** — with artwork none of it runs, because the artwork
  already carries all of it. Anything whose text changes at render time stays in
  `draw_hud_frame`. Getting this wrong is not cosmetic: before the split, the stats
  captions were drawn in the value path, so with real artwork every one of them would have
  been printed twice, slightly offset. A consequence worth knowing: a baked label can't
  change, which is why the meter's caption is a fixed "S" and no longer switches to "PO"
  on transmit — the RX/TX lamp beside it already says which is being shown.
- **Band/mode chips are drawn (and baked) lit, and the inactive ones dimmed**
  (`_dim_region`, `HUD_CHIP_DIM`) rather than existing as a lit/unlit pair of assets that
  would have to be kept stylistically in sync. `_chip_rects` is shared by the drawing and
  dimming passes so the two can never disagree about where a chip is.
- **Split into a data layer and a drawing layer, deliberately.** Everything up to
  `draw_hud_frame` is pure functions over the recording's own sources, so it needs no
  art, no fonts and no ffmpeg and is fully unit-tested; `HudTimeline.at(t)` returns a
  `HudState` for any video time. Geometry lives in one table (`HUD_SLOTS`, scaled by
  `hud_layout`), so replacing the placeholder background with finished artwork is an edit
  to coordinates, not to drawing code.
- **`HudTimeline` looks everything up by bisect, never by scanning.** A two-hour render
  queries it ~216,000 times; a linear scan per frame over hundreds of segments or
  thousands of decoded characters would dominate the entire pass.
- **The compass carries two needles**: solid = where the rotator actually points
  (telemetry `az`), hollow = bearing to the station being worked (computed from their
  EDI locator). The swing of one onto the other is a QSO's whole story in one glyph.
  Validated on the real August round — at a sampled instant the rotator read 310.0° and
  the target bearing computed to 310.38°, i.e. two independent sources agreeing.
- **The S-meter comes from the `.scope` recording's own centre bins**, not from CI-V's
  `15 02` (which is polled-only — see icom_net's meter notes). The IC-9700's scope runs in
  Centre mode and 475 bins across a 1 MHz span makes one bin ~2.1 kHz, close enough to an
  SSB passband that this is a real reading rather than a proxy; `hud_s_marks` takes a
  *max* over the centre bins, not a mean, so a signal in one bin isn't diluted by the
  quiet ones beside it. **This is not retroactive in practice**: no contest round recorded
  so far has a `.scope` file at all (the only one in the tree is a 65 s bench capture from
  a different day), so the meter reads empty until the next round.
- **The PWR panel renders placeholders rather than hiding itself** when Vd/Id are absent,
  which is every recording to date — a panel that appears and disappears between
  recordings would shift the whole layout.
- **Every readout is fitted to its own panel (`_fit_font` for text, `_seven_seg`'s own
  shrink loop for segment digits)**, because nothing on this bar has a fixed width: the
  score gains a digit partway through a contest and a callsign-shaped ticker line is wider
  than a report. Found by rendering the demo frame and looking at it — a fixed point size
  spilled five-digit scores clean across the gutter into the QSOS panel. The regression
  test asserts no lit pixels in that gutter, and was confirmed red by monkeypatching
  `_fit_font` back to a plain fixed-size lookup.
- **Numerals are real segment glyphs from DSEG7 (Debian `fonts-dseg`, SIL OFL)** — every
  value on the bar is numeric, so the alphabetic DSEG14 the stats rows briefly used is
  gone: once their captions became chrome (see the chrome/values split above) there was
  nothing left to render that needed letters. An earlier version
  drew all seven segments as polygons by hand to avoid depending on a font at all; the
  package turned out to be packaged for Debian and already installed, the glyphs are better
  than hand-rolled ones, and it deleted ~120 lines of geometry. There is precedent for the
  hardcoded path — `CAST_FONT_PATH` already points at DejaVu the same way.
- **Unlit segments are drawn too, very dim (`HUD_SEG_DIM`, 0.12)** — that is what makes an
  LED panel read as a panel rather than as numerals floating on black. Keep the value low:
  at 0.16 the ghost behind a `1` (which lights only its two right-hand bars) read as a
  digit being *clipped by the panel edge* rather than as an unlit cell, which is why an
  intermediate version dropped the layer entirely before it was tuned and restored.
  `_all_segments` doubles as the positioning reference, a subtler need: a value containing
  `-` (the `--.-` placeholder) has a glyph box only as tall as the middle segment, so
  anchoring on the value's own box floats the dashes well above where the digits they
  replace sit.
- **The artwork's generation prompt lives in `hud-artwork-prompt.md`**, along with what the
  software draws (and therefore what the artwork must leave empty), why the sprite sheet
  sits on flat magenta, and why a coordinate table baked into the image was rejected — an
  image generator cannot measure its own output raster, so such a table would be
  confabulated while looking authoritative. Sprite extraction is instead automatic:
  non-magenta is sprite, magenta is not, so bounding boxes come from connected-component
  detection and identity from left-to-right order.
- **The artwork has no font to match — its "text" is drawn, not typeset** (confirmed with
  the generator directly). So the typography is ours to choose and the artwork can only
  ever be *chrome*: request future iterations as texture and bevels with no text at all.
  `HUD_FONT_PATH`/`HUD_FONT_BOLD` are the HUD's own label face, deliberately separate from
  the cast renderer's `CAST_FONT_PATH`, so swapping in a chunky pixel font (DOOM's own
  labels are pixel art, not type) is a one-line change that can't disturb the terminal PiP.
  Debian's `fonts-dotgothic16` (OFL, TrueType, 16x16-bitmap-derived) is the candidate;
  the authentic recommendations — DooM Font, Small Fonts, Px437 IBM VGA8 — are dafont or
  font-pack downloads whose licences would need checking before living in this repo.
- **The CW ticker is a 5x7 dot-matrix display** (`_FONT_5X7`, `_draw_matrix_text`), every
  dot drawn with the same lit/`HUD_SEG_DIM` treatment as the segment panels, so an idle
  ticker still reads as a display. The glyph table is written out in the source rather than
  taken from a font file because the set is tiny and *fully determined* — `MORSE` can only
  decode to 44 characters plus space, which a test asserts — and at 5x7 each row is
  directly readable where it is typed. A generated character sheet was considered and
  rejected: rendering long specific character sequences is what image generators are worst
  at, so 36 glyphs would be 36 chances to be wrong with no way to fix one without
  regenerating everything. Glyphs were verified by rendering the whole set as a sheet and
  reading it, since a mistyped row is a plausible-looking letter rather than an error.
- **The ticker shrinks to `HUD_TICKER_CHARS` (16)** in a fixed right-aligned slot, down
  from the full-width 84-character overlay — the value of a ticker is "something is
  arriving right now", not a readable backlog. `build_ass` and the HUD now share one
  source for it (`ticker_chunks` / `ticker_stream` / `ticker_texts`, extracted from
  `build_ass`) rather than each deriving the transcript independently.
- **PWR and STATS are half-height so the ticker spans underneath them**, which is what the
  artwork does. The first layout gave the ticker a full-height column of its own, leaving
  it far too narrow for 16 characters to be legible while PWR and STATS were stretched
  vertically to fill space they didn't need. Folding the V/A units onto the same line as
  their values is what freed the height. The unbalanced left/right halves are the one
  thing the artwork gets wrong on its own terms, and the one place to diverge from it;
  everything else follows it closely, down to the portrait face recess.
- **Best DX is labelled ODX**, the contest term, not "BEST".
- **Two preview modes, both single-frame PNG**, because iterating layout against a full
  render is absurd: `--hud-demo OUT.png` needs no recording at all and draws the mockup's
  dummy values (this is what to check artwork against); `--hud-preview OUT.png
  --hud-preview-t SECONDS` builds real state from an actual recording. `recdir`/`edi` are
  `nargs="?"`/`nargs="*"` purely so `--hud-demo` can run standalone; every other mode
  still errors without them.
- **Vertical budget is the real constraint, and the first artwork missed it.** The
  ChatGPT mockup came out at 4.4:1, i.e. 435px at 1920 wide — 40% of a 1080p frame, which
  collides with the cast PiP (73% width x 1.69 aspect = 830px tall). The workable budget
  is ~260px (24%, 7.4:1), at which the cast PiP only has to shrink from 0.73 to ~0.70
  width. `HUD_W`/`HUD_H` are 1920x260 for that reason.
- **Not yet done**: `render_hud_video` (the per-frame pass, to follow `render_cast_video`'s
  PIL-to-ffmpeg-pipe pattern), compositing into `render()`'s filter graph, moving the
  webcam into the face slot, and the logger-side meter recorder that would fill the PWR
  panel.

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
flushed per sweep. ~30 sweeps/s on real hardware (~18 MB per 2 h session).

**Telemetry recorder** (`*-telemetry.jsonl`, always on, **one JSON line per
actual change**, microsecond stamps): records are *partial* by source --
`{"t", "freq_hz", "mode"}` written from the `icom_net` push callback,
`{"t", "az"}` written by the rotctld poller. A field a line doesn't mention
simply didn't change; `contest_video.py`'s `build_state_events` carries each
one forward across the events that don't mention it.
- **Was a 1 Hz sampler of `current_rig()`/`current_rot()`**, which made sense
  while the rig sat behind rigctld and had to be polled anyway. Since the
  switch to `icom_net`, freq/mode arrive as CI-V Transceive pushes and
  `IcomNetRig._apply_update` already returns early on a no-op update — so the
  callback *is* the change stream, and re-sampling it on a timer put back onto
  a lag-free source exactly the latency `icom_net` exists to remove, blurred
  any retune shorter than the poll interval, and wrote overwhelmingly
  duplicate lines: on the real July round only **616 of 9313** lines carried
  anything new.
- **`freq_hz` is now the radio's own exact value**, not the toolbar's
  kHz-rounded `qrg` string the sampler re-parsed. That rounding is the entire
  source of `contest_video.py`'s long-documented 160/250/300/310 Hz "WAV vs
  telemetry disagreement" — this logger was quantizing, not two sources
  reading the rig differently.
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
- Microsecond precision (unlike telemetry's whole-second stamps) matters here:
  the video overlay built from this file is a "typewriter" effect keyed to
  exactly when each character was typed.
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
  `contest_video.py`'s `match_qso_times` match them up exactly (see below);
  it's the fix for "weird QSO timing" in a preview, where the EDI's
  minute-only precision let `_snap_to_cluster` occasionally pick the wrong
  neighbouring burst.

**Webcam capture** (`YYMMDD-CALL-webcam.mp4` while recording, renamed on stop — see
below — Alt+V to start/stop, off by default):
- Replaces an earlier approach that recorded the operator with a phone propped up
  separately, requiring `contest_video.py` to sync two *independent* device clocks
  after the fact (see its own webcam-sync notes) -- error-prone even with audio
  cross-correlation, since a phone's own clock has no relationship to the radio
  recorder's. Capturing on the *same machine* that runs the logger removes that
  problem at the source: start/stop goes through the same `_log_input_event`/
  `datetime.now(timezone.utc)` already used for QSOs and keystrokes, so the
  recording's real start time is known exactly, with no separate clock to
  reconcile at all.
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
  `_webcam_precise_name`), e.g. `260706-HA5LA-webcam.mp4` ->
  `260706-HA5LA-webcam-20260706T160037.123456Z.mp4`. Investigated and rejected two
  alternatives first, both for real, measured reasons: `-metadata creation_time=...`
  can't be passed to ffmpeg up front, since the true frame-0 wallclock isn't known
  until the camera has actually opened (~1s, variable, after the process spawns) --
  and tagging it in *after* capture (`ffmpeg -c copy -metadata ...`) was tested
  against a real ~2h/3GB capture and does work (15s, stream-copy, no re-encode), but
  needs a full second copy of the file on disk simultaneously -- too risky right at
  the point in a session (end of a multi-hour recording) where free disk space is
  most likely to be tight. A rename needs none of that: verified on a real 3GB file
  at 0.006s, a directory-entry update on the same filesystem, independent of file
  size. `contest_video.py`'s `parse_webcam_precise_filename` now prefers this over
  parsing the `*-webcam.log` sidecar (`webcam_start_from_log`, kept as a fallback for
  recordings made before this existed, or if the rename didn't happen for some
  reason) -- same precision either way, but self-contained in the filename, no
  dependency on the `.log` file surviving alongside the video.
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
