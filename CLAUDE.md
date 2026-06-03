# Puskás URH Kupa – project context

## What is this?
Amateur radio contest (Puskás URH Kupa) toolset plus a general ON4KST bridge:
- `on4kst_irc_bridge.py` – general ON4KST↔IRC bridge; use with irssi or any IRC client
- `puskas_logger.py` – contest QSO logger with rigctld integration; exports EDI files
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
- **Band always visible in log**: every QSO row must show its band. RST columns must be
  3 chars wide so CW (599) and SSB/FM (59) rows stay aligned.
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
  `offline`, plus a colour-coded UTC clock. Clock background is **green** during the contest
  window (first Monday of each month, 18:00–20:00 CET/CEST) and **red** at all other times.
  Band and mode are intentionally absent from the toolbar — they live in the prompt prefix.
- **Alt+B / Alt+M**: cycle band / mode through `_BANDS`/`_MODES` tuples when rig is offline.
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

**CW macros** (F1–F7, requires rigctld):
| Key | Template |
|-----|----------|
| F1  | `CQ <MYCALL> <MYCALL> TEST` |
| F2  | `<MYCALL>` |
| F3  | `<HISCALL> DE <MYCALL> 5NN <NUMBER> <NUMBER> <LOCATOR>` |
| Alt+F3 | `5NN <NUMBER> <NUMBER>` (short exchange for scatter/time pressure) |
| F4  | `TU 73 EE` |
| F5  | `<HISCALL>` |
| F6  | `DE <MYCALL>` |
| F7  | `?` |
| F8  | `272 272 SSB` |

`<HISCALL>` is the first token in the input buffer at key-press time.
`<NUMBER>` uses CW abbreviations: `0→T`, `9→N` (e.g. 014 → `T14`).
Macros silently no-op when rigctld is offline.

**Offline setup wizard**: if rigctld is not running at startup and no manual band/mode
override is set, the logger shows an interactive prompt asking for band (`2M/70CM/23CM`)
then mode (`SSB/CW/FM`) before entering the main loop. Ctrl-D exits cleanly.
Mid-session rig disconnect uses `_rig_manual` values as fallback (set by the wizard or
**Alt+B / Alt+M** during the session), so the wizard only appears once per session.

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
uv run pytest tests/ -v     # 205 tests: parsing, IRC protocol, logger, harvester, integration
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
