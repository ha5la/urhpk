# Puskás URH Kupa – project context

## What is this?
Amateur radio contest (Puskás URH Kupa) toolset plus a general ON4KST bridge:
- `puskas_log_analyzer.py` – contest log analyser, generates `puskas_stations.csv`
- `on4kst_irc_bridge.py` – general ON4KST↔IRC bridge; use with irssi or any IRC client

## Housekeeping reminders
- When adding or removing components, update the components table in **README.md**

## Development principles
- **Kent Beck's simplicity rule**: always implement the simplest thing that works.
  Prefer decremental development — remove code that isn't needed rather than keeping
  it "just in case". Dead code is technical debt.
- **Tests must always pass**: never commit with a failing test. The test suite is the
  safety net for refactoring and simplification.

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
- Bridge auto-joins the IRC client to `#on4kst` on connect — no client-side autojoin needed
- `/SET HERE` sent when first IRC client connects; `/UNSET HERE` when last disconnects;
  AWAY command from IRC client forwards the same
- User list updates (every 120 s) trigger IRC JOIN/PART events for member list accuracy
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
- No runtime dependency on `uv`; the bridge script is pure stdlib, run directly with `/usr/bin/python3`
- Bridge logs: `journalctl -u on4kst-irc-bridge -f`
- To change the service user without losing it on upgrade: `sudo systemctl edit on4kst-irc-bridge`

## Running
```
uv run on4kst_irc_bridge.py # IRC bridge (then connect irssi to localhost:6667)
```

## Testing
```
uv run pytest tests/ -v     # 80 tests: parsing, IRC protocol, integration
```
CI runs the same suite on every push via GitHub Actions.

## Repository
- `.gitignore` excludes generated files (`puskas_stations.csv`, `puskas_missed.csv`,
  `puskas_map.html`, `puskas_polar.png`) and scratch files (`*.json`, `*.url`, `*.txt`)
