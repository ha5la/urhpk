# Operator setup

How this operator's terminal is configured for a round. None of it is required
to run anything in this repo — it is the kind of knowledge that is expensive to
rediscover and worth writing down, but it is one person's setup, not the
project's.

## Getting notified of a private message

A sked request is easy to miss while concentrating on the log. irssi emits a
BEL for private messages and highlights; these three settings carry it through
tmux and SSH to the desktop.

### Taskbar blink (irssi → tmux → SSH terminal)

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

### Highlighting the irssi window itself (tmux)

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
