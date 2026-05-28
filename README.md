# Puskás URH Kupa

[![Tests](https://github.com/ha5la/urhpk/actions/workflows/test.yml/badge.svg)](https://github.com/ha5la/urhpk/actions/workflows/test.yml)
[![Release](https://github.com/ha5la/urhpk/actions/workflows/release.yml/badge.svg)](https://github.com/ha5la/urhpk/actions/workflows/release.yml)

Amateur radio contest toolset for the Puskás URH Kupa, plus a general-purpose
[ON4KST](https://www.on4kst.info/) chat ↔ IRC bridge.

## Components

| File | Purpose |
|---|---|
| `on4kst_irc_bridge.py` | ON4KST ↔ IRC bridge; connect any IRC client to ON4KST chat |
| `puskas_log_analyzer.py` | Contest log analyser, generates `puskas_stations.csv` |

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

## Raspberry Pi deployment

The bridge runs as a systemd service on a Raspberry Pi and is distributed as a `.deb` package.

To release a new version:

```
git tag v1.2.3
git push origin v1.2.3
```

GitHub Actions builds and attaches `on4kst-irc-bridge_1.2.3_all.deb` to the release automatically.

## Testing

```
uv run pytest tests/ -v
```

## Developer notes

See [CLAUDE.md](CLAUDE.md) for architecture details, design decisions, and deployment instructions.
