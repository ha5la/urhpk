# Puskás URH Kupa

[![Tests](https://github.com/ha5la/urhpk/actions/workflows/test.yml/badge.svg)](https://github.com/ha5la/urhpk/actions/workflows/test.yml)
[![Coverage](https://img.shields.io/badge/coverage-report-blue)](https://ha5la.github.io/urhpk/)

Amateur radio contest toolset for the Puskás URH Kupa, plus a general-purpose
[ON4KST](https://www.on4kst.info/) chat ↔ IRC bridge.

## Components

| File | Purpose |
|---|---|
| `on4kst_irc_bridge.py` | ON4KST ↔ IRC bridge; connect any IRC client to ON4KST chat |
| `puskas_logger.py` | Contest QSO logger; rig control via `icom_net` (direct Ethernet, push updates) + rotctld; exports EDI files |
| `puskas_harvester.py` | Pre-contest data collector; fetches all stations → `puskas-seen-stations.json` |
| `puskas_standings.py` | Where the year stands, carried forward over the rounds the organiser has not evaluated yet |
| `contest_video.py` | Annotated CW contest video from a timestamped recording + EDI log |
| `hamlib_supervisor.py` | Starts/stops rotctld based on USB device presence (inotify, no polling) |
| `icom_net.py` | Direct Ethernet CI-V client for Icom radios (IC-9700 etc.), bypassing rigctld; instant push freq/mode updates, plus real spectrum-scope capture |
| `run-recorded-round.sh` | The round entrypoint — recorded irssi + logger, plus rig/rotator supervision and the bridge in a background window |
| `sync-clock.sh` | Forces an immediate chrony resync (offset + drift rate) right before a round starts |

## Quick start — one round

```
uv run puskas_harvester.py          # once, before the round
./sync-clock.sh                     # right before the round starts
./run-recorded-round.sh             # right before the round starts
```

See [docs/PIPELINE.md](docs/PIPELINE.md) for the round from harvest to upload,
and [CLAUDE.md](CLAUDE.md) for what `run-recorded-round.sh` actually
starts.

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

Credentials are read from `~/.netrc` (`machine www.on4kst.info login <callsign> password <pass>`).

A sked request is easy to miss while concentrating on the log;
[docs/operator-setup.md](docs/operator-setup.md) has the irssi/tmux/terminal
settings that carry a private-message bell through to the desktop.

## Testing

Enforced by `pre-commit`, not a manual step — one-time setup per clone:

```
uv run pre-commit install
```

Runs automatically on every commit after that (see `.pre-commit-config.yaml`).
To run everything ad hoc: `uv run pre-commit run --all-files`.

## Documentation

| File | What's in it |
|---|---|
| [docs/PIPELINE.md](docs/PIPELINE.md) | The end-to-end story of a round, harvest to upload |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Component by component, and the constraints each carries |
| [docs/RECORDING.md](docs/RECORDING.md) | Recording a round and producing the video, with real numbers |
| [docs/FINDINGS.md](docs/FINDINGS.md) | Hardware measurements, protocol archaeology, dead ends |
| [docs/operator-setup.md](docs/operator-setup.md) | One operator's terminal setup — PM notifications through tmux and SSH |
| [CLAUDE.md](CLAUDE.md) | Development principles and house rules |
| [CONTEXT.md](CONTEXT.md) | The project's glossary: what each word means |
