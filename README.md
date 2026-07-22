# Puskás URH Kupa

[![Tests](https://github.com/ha5la/urhpk/actions/workflows/test.yml/badge.svg)](https://github.com/ha5la/urhpk/actions/workflows/test.yml)
[![Coverage](https://img.shields.io/badge/coverage-report-blue)](https://ha5la.github.io/urhpk/)

Amateur radio contest toolset for the Puskás URH Kupa, plus a general-purpose
[ON4KST](https://www.on4kst.info/) chat ↔ IRC bridge.

## Components

| File | Purpose |
|---|---|
| `on4kst_irc_bridge.py` | ON4KST ↔ IRC bridge; connect any IRC client to ON4KST chat |
| `puskas_logger.py` | Contest QSO logger with rigctld integration; exports EDI files |
| `puskas_harvester.py` | Pre-contest data collector; fetches all stations → `puskas-seen-stations.json` |
| `puskas_visualizer.py` | Map and polar diagram from `puskas-seen-stations.json` |
| `contest_video.py` | Annotated CW contest video from a timestamped recording + EDI log |
| `hamlib_supervisor.py` | Starts/stops rigctld and rotctld based on USB device presence (inotify, no polling) |

## Quick start — IRC bridge

```
uv run on4kst_irc_bridge.py
```

Then in irssi:

```
/server add -auto -network on4kst localhost 6667
/save
/connect on4kst
```

Public ON4KST chat appears in `#on4kst`. Private messages arrive as IRC PMs.

Credentials are read from `~/.netrc` (`machine www.on4kst.info login <call> password <pass>`).

## Testing

```
uv run pytest tests/ -v
```

## Developer notes

See [CLAUDE.md](CLAUDE.md) for architecture details, design decisions, and how to run a contest session.
