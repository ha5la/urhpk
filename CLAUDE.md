# Puskás URH Kupa – project context

## What is this?
Amateur radio contest (Puskás URH Kupa) toolset plus a general ON4KST bridge:
- `on4kst_irc_bridge.py` – general ON4KST↔IRC bridge; use with irssi or any IRC client
- `puskas_logger.py` – contest QSO logger with rigctld + rotctld integration; exports EDI files
- `puskas_harvester.py` – pre-contest data collector; fetches all stations → `~/.puskas/puskas-seen-stations.json`
- `puskas_visualizer.py` – map and polar diagram from `~/.puskas/puskas-seen-stations.json`

## Housekeeping reminders
- When adding or removing components, update the components table in **README.md**

## Development principles
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
  - Distance/bearing from live KST user list; QRG/mode from rigctld cache
- Local commands (not forwarded to KST, response NOTICE goes to `#on4kst`):
  - `!scatter CALL` — real-time airplane scatter check via OpenSky Network API
  - `!list` — lists online stations by distance and bearing
  - `!help` — lists available commands
- rigctld integration (optional, no-op when rigctld not running):
  - Background poller (`_rig_poller`) queries `RIGCTLD_HOST:RIGCTLD_PORT` every `RIGCTLD_POLL_S` (5 s)
  - Caches latest `(rig_qrg, rig_mode)` on the `Bridge` object; sked reads the cache — zero latency
  - Connect/disconnect events shown as NOTICE to own nick (irssi status window), not the channel
  - To start rigctld: `rigctld -m MODEL -r /dev/ttyUSB0` (see Hamlib docs for MODEL number)

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

## Raspberry Pi deployment
The bridge runs permanently on a Raspberry Pi (Debian trixie, Python 3.13) with irssi
in tmux. It is distributed as a `.deb` package built by GitHub Actions.

**To release a new version:**
```
git tag v1.2.3
git push origin v1.2.3
```
The `release.yml` workflow builds `on4kst-irc-bridge_1.2.3_all.deb` and attaches it to
a GitHub Release automatically.

**To install / upgrade on the Pi:**
```
wget https://github.com/ha5la/urhpk/releases/latest/download/on4kst-irc-bridge_VERSION_all.deb
sudo dpkg -i on4kst-irc-bridge_VERSION_all.deb
```
postinst enables and starts both services; prerm stops irssi first, then the bridge, before upgrade/removal.

**Service details:**
- `on4kst-irc-bridge.service` — the bridge; script at `/usr/lib/on4kst-irc-bridge/on4kst_irc_bridge.py`
- `irssi.service` — runs irssi in a tmux session (`tmux new-session -d -s irssi irssi`); `Type=oneshot RemainAfterExit=yes` because tmux daemonizes
- Both unit files are checked into the repo and installed to `/lib/systemd/system/`
- Both run as `User=pi` — `~/.netrc` must exist for that user
- No runtime dependency on `uv` for the bridge; the bridge script is pure stdlib, run directly with `/usr/bin/python3`
- Bridge logs: `journalctl -u on4kst-irc-bridge -f`
- To change the service user without losing it on upgrade: `sudo systemctl edit on4kst-irc-bridge`

**Contest tools on the Pi:**
The package also installs `puskas_harvester.py`, `puskas_logger.py`, and
`puskas_visualizer.py` to `/usr/lib/on4kst-irc-bridge/`, with wrapper scripts in
`/usr/local/bin/` (`puskas-harvester`, `puskas-logger`, `puskas-visualizer`).
These require `uv` on the Pi — install once with:
```
curl -LsSf https://astral.sh/uv/install.sh | sh
```
File locations follow a simple rule: **global databases live in `~`, per-session files live in CWD**.
- `~/.puskas/puskas-seen-stations.json` — harvested Puskás station database (all rounds, accumulates)
- `~/.puskas/on4kst-seen-stations.json` — ON4KST session database (written by the bridge service)
- `.puskas_cache/` — API response cache (CWD, delete to force a fresh fetch)
- `*.edi` — contest QSO logs (CWD, one file per band per session)

Run the contest tools from a contest directory:
```
mkdir ~/contest-2026 && cd ~/contest-2026
puskas-harvester     # fetch ~/.puskas/puskas-seen-stations.json
puskas-logger        # log QSOs; writes *.EDI here
puskas-visualizer    # generate map/polar from ~/.puskas/puskas-seen-stations.json + my-logs/
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

## contest_video.py – Annotated CW contest video

Turns a CW contest recording plus its EDI log into a YouTube-ready MP4 with a
scrolling audio waterfall, a live CW-decode ticker, and a panel showing the
current QSO. Built for reuse across future contests recorded the same way.

```
uv run contest_video.py RECORDING_DIR EDI_FILE [EDI_FILE ...] [-o OUT.mp4]
```
- Dependencies: `numpy` (uv script header) + `ffmpeg`/`ffprobe` on PATH
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
- **Rendering is one ffmpeg pass**: everything (ticker, QSO panels, header) is an
  ASS subtitle file burned over an `showspectrum` waterfall (dimmed to ~0.42 luma
  so text stays readable). No frame-by-frame rendering. The waterfall fills the
  frame within the first ~80 s, then stays full.
- The video keeps the recording's full length.
- **`--duration SECONDS` for a chronological preview cut**: trims to the first
  `SECONDS` of real session time — a straight, uncut trim (not a curated
  highlight reel; that was considered and rejected as much more machinery for
  a first cut). `trim_to_duration` runs *before* the CW-decode loop, not
  after, and drops segments past the cutoff outright rather than decoding the
  full session and discarding most of the result — the main cost of this
  pipeline is CW decoding, so a 10-minute preview of a 2-hour session decodes
  roughly 12x less audio. QSOs past the cutoff are filtered out of the merged
  list before `build_ass`/chapters/SRT so nothing shows a QSO panel with no
  time left to display it in.
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
  - A segment with no WAV metadata at all (freq_hz/mode/ptt all `None` --
    not an IC-9700 recording, or a parse failure) is skipped rather than
    guessed at from telemetry alone. `az` has no equivalent in the WAV
    metadata at all and is purely telemetry's own (median per run); missing
    `az` falls back to `ROT ---`, matching the logger's own toolbar.
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
- **`--input-log PATH` for a live "typewriter" overlay**, and for exact QSO-panel
  timing when available. `PATH` is a `puskas_logger *-input.jsonl` file
  (see below) — optional, so older recordings without one still render
  normally, falling back to the EDI-minute + cluster-snap timing described
  above. `load_input_log` parses two event kinds sharing the file, into one
  `InputLogEvent` list (`kind` is `'text'` or `'qso'`):
  - **`'text'` events → the typewriter overlay**, bottom-center, styled
    bright green like the logger's own TX line since both mark the
    operator's own action rather than something heard on the air. Unlike
    the CW ticker or QSO panels, this needs no burst-snapping heuristic at
    all: every record is the operator's own keystroke, already exact
    ground truth, so `build_input_events` just maps each timestamp straight
    through `audio_time_for` and shows that state verbatim until the next
    keystroke changes it (`'qso'` events are ignored here — they don't
    change what's on screen). Empty-text states (buffer cleared by
    Enter/Ctrl+U/Escape) are dropped rather than rendered, so nothing shows
    while the input line is genuinely idle — same "no visual glitches"
    principle as the logger's own UI.
  - **`'qso'` events → exact QSO-panel timing.** `match_qso_times` pairs
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
    be.** `qso_windows` used to close a QSO's panel exactly when the
    *next* QSO's panel opened (or at `total` for the last QSO) — but
    `qso_times` gives an exact, real end for a QSO wherever known: the
    moment the operator hit Enter. Reported directly from watching a
    rendered preview: a QSO's panel was staying up long after that QSO was
    actually done, and the running score (below) was crediting points the
    instant a panel *appeared* rather than once the contact was actually
    complete. Now `windows[i][1]` is `qso_times[i]` (mapped to video time)
    wherever available, so the panel clears the moment the QSO is actually
    finished, leaving a real gap with nothing shown if the next QSO's own
    over hasn't started yet. Falls back to the old "next QSO's start" (or
    `total` for the last QSO) wherever `qso_times[i]` is `None` for that
    particular QSO — no better information exists then.
  - **Two (or more) QSOs sharing one burst is a second, separate timing bug
    `qso_times` exposed**: the same station worked on multiple modes
    back-to-back with no real listening gap between them (e.g. SSB, then
    FM, then CW with the same callsign, all within a couple of minutes) is
    *one* burst as far as `cluster_starts` is concerned — there's no audio
    structure to tell the individual overs apart at all. Snapping every one
    of those QSOs' anchors onto that single shared cluster start collapsed
    all their panels onto the same instant; the pre-existing
    minimum-1-second window then papered over the collision by showing two
    panels on screen simultaneously for that one second, and the earlier
    QSO's panel vanished before its own real submit time. `qso_windows` now
    tracks the previously resolved cluster: when a QSO's anchor resolves
    (via `_snap_to_cluster`) to the *same* cluster as the previous QSO **and**
    an exact `qso_times` entry is available, it starts exactly where the
    *previous* QSO's own window ended (its real, known finish) instead of
    the shared cluster start — not audio-structure-precise either, but
    real, and leaves no overlap and no gap between the two. Without
    `qso_times` for that QSO, falls back to the original squeeze behaviour.
  - **`--seed-input-log OUT.jsonl`**: writes one `'qso'` event per QSO from
    the EDI(s) (`t` is just `q.dt` with seconds zeroed) and exits without
    rendering — for a recording made before this feature existed, so there's
    no automatically-generated `*-input.jsonl` to fall back on. Edit each
    `t` against the audio, then pass the result back in as `--input-log` for
    exact QSO-panel timing with no cluster-snapping guesswork involved for
    those QSOs. This is what `match_qso_times`'s call+order (not
    call+minute) matching exists for — a seed's timestamps are expected to
    move freely across minute boundaries once hand-edited.
- **Running score in the header**: `running_score(qsos)` returns
  `(qso_count, cumulative_points)` after each QSO — every QSO counts
  toward `qso_count` including dups (still logged, just worth nothing,
  matching `puskas_logger`'s own `_band_summary` and the EDI's `CQSOP`),
  only non-dup QSOs add to points. `build_ass`'s header shows
  `{mycall} {mywwl} {contest}   NQ Mpts` once there's a first QSO to show
  a score for, updating in step with each QSO's own *finish* — see the
  `qso_windows` bullet above — not when its panel first appears, for the
  same reason as the panel-clearing fix: crediting points for a contact
  the instant its over starts, before it's actually complete, is wrong.
  Where a QSO's finish isn't known exactly (no `qso_times` entry for it),
  its score trigger falls back to its own panel's *start* instead — using
  `windows[i][1]` there would just be "next QSO's start" (or `total` for
  the last QSO, leaving no room at all to display that QSO's own score
  before the clip ends).

## puskas_logger.py – UX requirements (non-negotiable)

These requirements must be preserved across all future changes:

- **Dynamic prompt**: the prompt prefix is `{band} {mode}  RX ► ` (e.g.
  `2M SSB  RX ► `), computed by a callable so it updates every `refresh_interval`
  second. It always reflects the current rig state (or manual override), giving the
  operator live context for what band/mode will be used if Enter is pressed now.
  It mirrors the `TX ►` line printed above it.
- **TX line is reprinted on band/mode change**: the TX line (`TX ► MYCALL  RST  NR
  LOCATOR`) is a static `print()` rendered once per loop iteration, not part of the
  prompt_toolkit UI. RST depends on mode and NR depends on band, so both go stale if
  the rig changes while the prompt is waiting. Fix: `_toolbar()` detects band/mode
  changes and calls `get_app().exit(result=_REDRAW)` — safe because `_toolbar()` runs
  on the event-loop thread every `refresh_interval`. This exits `session.prompt()`,
  re-prints the TX line with fresh values, and re-enters the prompt within one second.
  **Do not move RST or NR into the prompt prefix** — they are TX fields; mixing them
  into `RX ►` was tried and rejected as confusing.
- **Live rig status**: QRG and contest-clock update every second in the bottom toolbar.
  A band/mode change on the radio must be visible immediately in the prompt — never require
  Enter to see the updated state.
- **Dup warning before Enter**: as soon as the callsign token is recognisable, the entire
  input line background turns red (`DynamicStyle({'': 'bg:ansired fg:white'})`) and the
  right prompt shows a red `DUP` label followed by the geo info (distance + bearing + arrow)
  if known. The operator must not need to press Enter to discover a duplicate. The dup check
  must re-evaluate when the band changes on the radio — `RIGCTLD_POLL_S = 1` keeps cached
  rig state fresh so the style (redrawn every second via `refresh_interval`) always reflects
  the current band. The dup style is suppressed during edit mode
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
- **Rig thread must never die**: `_rig_thread` wraps its loop body in `try/except` so a
  transient rigctld error cannot kill the thread.
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
- Backspace stops at column 0 (does nothing on empty input); edit mode via Up arrow only
- Up/Down → navigate log in edit mode; window scrolls to keep focused row centred
- Escape → exits edit mode (screen redraws immediately) and/or aborts CW transmission
- Alt+R → point rotator at bearing of selected/typed station (no-op when rotctld offline)

**CW macros** (F1–F8, requires rigctld):
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
Macros silently no-op when rigctld is offline.

**Offline setup wizard**: if rigctld is not running at startup and no manual band/mode
override is set, the logger shows an interactive prompt asking for band (`2M/70CM/23CM`)
then mode (`SSB/CW/FM`) before entering the main loop. Ctrl-D exits cleanly.
Mid-session rig disconnect uses `_rig_manual` values as fallback (set by the wizard or
**Alt+B / Alt+M** during the session), so the wizard only appears once per session.

**rotctld integration** (optional, no-op when rotctld not running):
- Background poller (`_rot_thread`) queries `ROTCTLD_HOST:ROTCTLD_PORT` (4533) every
  `RIGCTLD_POLL_S` (1 s) using the `p` command (returns azimuth and elevation)
- Current azimuth shown in toolbar as `ROT: 045°` when online, `ROT: ---` when offline
- **Alt+R** sends `P az 0` to rotctld to slew the rotator; fires in a background thread
- To start rotctld: `rotctld -m MODEL -r /dev/ttyUSB0` (see Hamlib docs for MODEL number)

**Telemetry recorder** (`*-telemetry.jsonl`, always on, one JSON line per
second): `{"t", "freq_hz", "mode", "az"}` -- band/mode/QRG from rigctld,
bearing from rotctld. No `ptt` field: it used to be queried and recorded
here too, but the WAV recordings' own IC-9700 metadata already carries it
straight from the rig with zero polling lag (see `contest_video.py`'s
`read_wav_metadata`) -- this was in practice reconstructing, with more
latency, something already recorded losslessly elsewhere, so it was removed
rather than kept for redundancy.

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

**Contest rules**:
- Reads band/QRG/mode from rigctld; falls back to Alt+B/Alt+M (or `!band`/`!mode`) if rig offline
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
uv run on4kst_irc_bridge.py   # IRC bridge (then connect irssi to localhost:6667)
uv run puskas_harvester.py    # build ~/.puskas/puskas-seen-stations.json before a contest
uv run puskas_logger.py       # log QSOs during the contest
uv run puskas_visualizer.py   # generate map and polar after the contest
```

## Testing
```
uv run pytest tests/ -v     # 231 tests: parsing, IRC protocol, logger, harvester, integration
uv run ruff check .         # linting: E/F/W/I rules; E501 and E701 intentionally ignored
```
CI runs both on every push via GitHub Actions (`test.yml`).

**Ruff policy**: `ruff check` only — no `ruff format`. The formatter strips intentional
aligned-assignment style (e.g. `RIGCTLD_HOST   = "localhost"`) that aids readability in
the configuration and dataclass sections. E501 (line length) and E701 (single-line
`if …: return` in lookup functions like `_mode_str`) are suppressed globally.

## Repository
- `.gitignore` excludes generated files (`puskas_map.html`, `puskas_polar.png`) and scratch
  files (`*.json`, `*.url`, `*.txt`)
