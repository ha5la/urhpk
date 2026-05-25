# Puskás URH Kupa – projekt kontextus

## Mi ez?
Amatőr rádió verseny (Puskás URH Kupa) segédeszköz, két szkripttel:
- `puskas_log_analyzer.py` – versenynapló elemző, generálja a `puskas_stations.csv`-t
- `puskas_kst.py` – ON4KST 144/432 MHz chat kliens, figyeli az online állomásokat

## Hívójel / lokátor
- `MY_CALLSIGN = "HA5LA"`, `MY_LOCATOR = "JN97TF"`
- Belépési adatok: `~/.netrc` (`machine www.on4kst.info login ha5la password ...`)

## puskas_kst.py – architektúra
- **Pure asyncio, no threads** (2025-05-25 refactor from threading → select → asyncio+prompt_toolkit)
- Login: sequential `await _read_until()` using `asyncio.StreamReader`
- Locator: fetched from `/SHow CONFig` after login, not hardcoded
- Callsign: taken from `.netrc` (uppercase), not hardcoded
- Main loop: `asyncio.create_task(client.read_loop())` + `await session.prompt_async()`
- Socket refresh: `asyncio.wait_for(reader.read(), timeout)` fires `/SHow USer` every 120s
- Output: `prompt_toolkit.patch_stdout` — plain `print()` in socket coroutine, prompt redrawn cleanly
- TAB completion: `KSTCompleter` (prompt_toolkit) — CSV known stations + currently online
- Prompt: `1917Z [online:80] HA5LA>` callable; `minute_ticker()` coroutine syncs to UTC minute boundary via `session.app.invalidate()`; `first_userlist` asyncio.Event ensures count shown from first prompt
- Message highlighting: bold-yellow = addressed to me, bright-cyan = broadcast, dim = server notices

## Ismert problémák / történet
- Eredeti threading verzió: `_read_loop` + `_process_loop` + refresh thread –
  néhány mp után disconnect; valószínűleg race condition a login után
- Select-es átírás 2025-05-25: megszüntette a disconnectet, de `sys.stdin.readline()`
  miatt elveszett a tab-kiegészítés (readline csak `input()` híváskor aktív)
- Stdin-thread + SimpleQueue fix 2025-05-25: tab-kiegészítés visszaállítva,
  minimális threading (csak stdin, nincs shared state a socketen)

## Futtatás
```
uv run puskas_kst.py
```
Előfeltétel: `puskas_stations.csv` létezzen (futtasd előbb `puskas_log_analyzer.py`-t).
