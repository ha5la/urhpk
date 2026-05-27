# Puskás URH Kupa – project context

## What is this?
Amateur radio contest (Puskás URH Kupa) toolset plus a general ON4KST bridge:
- `puskas_log_analyzer.py` – contest log analyser, generates `puskas_stations.csv`
- `puskas_kst.py` – ON4KST 144/432 MHz chat client, monitors online stations
- `on4kst_irc_bridge.py` – general ON4KST↔IRC bridge; use with irssi or any IRC client

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
- Public chat maps to `#on4kst`; `/CQ CALLSIGN` maps to IRC private messages
- ON4KST connection is kept permanently and reconnects after drops (`RECONNECT_S = 30`)
- Messages received while no IRC client is connected are buffered (`HISTORY_MAX = 500`)
  and replayed with original timestamps when a client reconnects
- `/SET HERE` sent when first IRC client connects; `/UNSET HERE` when last disconnects
- User list updates (every 120 s) trigger IRC JOIN/PART events for member list accuracy

irssi quick-start:
```
/server add -auto -network on4kst localhost 6667
/channel add -auto #on4kst on4kst
/save
/connect on4kst
```

## Running
```
uv run puskas_kst.py        # interactive prompt_toolkit client
uv run on4kst_irc_bridge.py # IRC bridge (then connect irssi to localhost:6667)
```
Prerequisite for puskas_kst.py: `puskas_stations.csv` must exist (run `puskas_log_analyzer.py` first).

## Repository
- `.gitignore` excludes generated files (`puskas_stations.csv`, `puskas_missed.csv`,
  `puskas_map.html`, `puskas_polar.png`) and scratch files (`*.json`, `*.url`, `*.txt`)
