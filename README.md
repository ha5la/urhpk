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
| `contest_video.py` | Annotated CW contest video from a timestamped recording + EDI log |
| `hamlib_supervisor.py` | Starts/stops rotctld based on USB device presence (inotify, no polling) |
| `icom_net.py` | Direct Ethernet CI-V client for Icom radios (IC-9700 etc.), bypassing rigctld; instant push freq/mode updates, plus real spectrum-scope capture |
| `scope_preview.py` | Standalone preview: renders an `icom_net.py --scope` recording into a waterfall video |
| `run-recorded-contest-session.sh` | The contest-round entrypoint — recorded irssi + logger, plus rig/rotator supervision and the bridge in a background window |
| `sync-clock.sh` | Forces an immediate chrony resync (offset + drift rate) right before a round starts |

## Quick start — contest session

```
uv run puskas_harvester.py          # once, before the round
./sync-clock.sh                     # right before the round starts
./run-recorded-contest-session.sh   # right before the round starts
```

See [CLAUDE.md](CLAUDE.md) for what `run-recorded-contest-session.sh` actually starts.

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

### Getting notified of a private message

A sked request is easy to miss while concentrating on the log. irssi emits a
BEL for private messages and highlights; these three settings carry it through
tmux and SSH to the desktop.

#### Taskbar blink (irssi → tmux → SSH terminal)

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

#### Highlighting the irssi window itself (tmux)

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
| [PIPELINE.md](PIPELINE.md) | The end-to-end story of a contest round, harvest to upload |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Component by component, and the constraints each carries |
| [RECORDING.md](RECORDING.md) | Recording a round and producing the video, with real numbers |
| [FINDINGS.md](FINDINGS.md) | Hardware measurements, protocol archaeology, dead ends |
| [CLAUDE.md](CLAUDE.md) | Development principles and house rules |
