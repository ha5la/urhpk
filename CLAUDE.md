# Puskás URH Kupa – project context

## What is this?
Amateur radio contest (Puskás URH Kupa) toolset plus a general ON4KST bridge:
- `puskas_log_analyzer.py` – contest log analyser, generates `puskas_stations.csv`
- `puskas_kst.py` – ON4KST 144/432 MHz chat client, monitors online stations
- `on4kst_irc_bridge.py` – general ON4KST↔IRC bridge; use with irssi or any IRC client

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

## puskas_kst.py – architecture
- **Pure asyncio, no threads** (final refactor 2025-05-25)
- Dependency: `prompt_toolkit` (declared in uv script header)
- Login: sequential `await _read_until()` on `asyncio.StreamReader`
- Locator: `fetch_locator()` sends `/SHow CONFig` after login, parses the response line
  that contains the callsign for a Maidenhead locator pattern (`RE_LOCATOR`)
- Main loop: `asyncio.create_task(client.read_loop())` + `await session.prompt_async()`
- Socket refresh: `asyncio.wait_for(reader.read(), timeout)` fires `/SHow USer` every 120 s
- Output: `prompt_toolkit.patch_stdout` + `print_formatted_text(ANSI(...))` for colours —
  plain `print()` for uncoloured lines; the prompt is redrawn cleanly after every print
- TAB completion: `KSTCompleter` (prompt_toolkit `Completer`) — CSV known stations first,
  then currently online stations (union, deduped)
- Prompt format: `1917Z [online:80] HA5LA>` (or `… [away] HA5LA>` when away) —
  callable passed to `prompt_async`;
  `minute_ticker()` coroutine wakes at each UTC minute boundary and calls
  `session.app.invalidate()` to sync the timestamp;
  `client.first_userlist` (`asyncio.Event`) is awaited before showing the first prompt
  so the online count is present from the start
- Away tracking: `away_watcher()` coroutine checks idle time every 60 s;
  after `AWAY_SEC` (30 min) of no user input it sends `/UNSET HERE` and sets `_is_away`;
  the first command after being away sends `/SET HERE` to return;
  `/SET HERE` is also sent once immediately after login
- Message highlighting via `colored_chat()`:
  - Bold bright-yellow (`\033[1;93m`) — message addressed to MY_CALLSIGN
  - Bright cyan (`\033[96m`) — broadcast / no explicit recipient
  - Dim (`\033[2m`) — server notices
  - Plain — message addressed to someone else
- Sked messages: `sked_text()` always returns a string; for CSV-known stations it includes
  bands/distance/bearing; for unknown stations it falls back to a short generic message.
  Either way, `send`/`pm`/`s` work for any online callsign.

## Known history / why it evolved
- Original threading version: `_read_loop` + `_process_loop` + refresh thread →
  disconnected after a few seconds (race condition between login and read thread)
- Select rewrite: fixed disconnect but `sys.stdin.readline()` broke TAB completion
  (readline only active inside `input()`)
- Stdin-thread + SimpleQueue: restored TAB completion but `input()` from a non-main
  thread doesn't use readline on CPython
- Socket-thread + main-thread `input()`: readline worked but raw ANSI cursor math
  in `rprint()` caused prompt corruption on multiline server output
- Current: full asyncio + prompt_toolkit — no threads, no cursor math, clean output

## on4kst_irc_bridge.py – architecture
- **General** ON4KST↔IRC bridge; no contest-specific logic (Puskás sked message TBD)
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

irssi quick-start:
```
/server add -auto -network on4kst localhost 6667
/save
/connect on4kst
```

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
postinst enables and starts the service; prerm stops and disables it before upgrade/removal.

**Service details:**
- Unit file: `on4kst-irc-bridge.service` (checked into repo, installed to `/lib/systemd/system/`)
- Script installed to: `/usr/lib/on4kst-irc-bridge/on4kst_irc_bridge.py`
- Runs as `User=pi` — `~/.netrc` must exist for that user
- No runtime dependency on `uv`; the script is pure stdlib and run directly with `/usr/bin/python3`
- Logs: `journalctl -u on4kst-irc-bridge -f`
- To change the service user without losing it on upgrade: `sudo systemctl edit on4kst-irc-bridge`

## Running
```
uv run puskas_kst.py        # interactive prompt_toolkit client
uv run on4kst_irc_bridge.py # IRC bridge (then connect irssi to localhost:6667)
```
Prerequisite for puskas_kst.py: `puskas_stations.csv` must exist (run `puskas_log_analyzer.py` first).

## Testing
```
uv run pytest tests/ -v     # 50 tests: parsing, IRC protocol, integration
```
CI runs the same suite on every push via GitHub Actions.

## Repository
- `.gitignore` excludes generated files (`puskas_stations.csv`, `puskas_missed.csv`,
  `puskas_map.html`, `puskas_polar.png`) and scratch files (`*.json`, `*.url`, `*.txt`)
