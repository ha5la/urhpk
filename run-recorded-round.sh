#!/bin/sh

d=$(dirname "$0")

# Preflight: contest_video.py's audio/webcam/cast sync assumes this
# machine's system clock rate is trustworthy for the whole round. Real
# case: systemd-timesyncd restarted ~2 min before a contest round started
# (journalctl showed "Initial clock synchronization" right at that
# restart), resetting its learned frequency-drift compensation from
# scratch -- unlike chrony, it has no persistent drift file. The clock
# then drifted ~2.5s/hour against the radio recorder for the whole ~2h
# round while it relearned, visible in the rendered video as the
# webcam/cast PiPs sliding out of sync with the audio. A plain
# "synchronized: yes" check doesn't catch this -- it flips true on the
# very first poll, well before frequency compensation has converged.
# Service uptime is a better proxy: warn if the active time-sync service
# has been running for under 10 minutes. Non-blocking -- the contest
# window is fixed, this is just visibility, not a gate.
svc_found=""
for svc in chrony chronyd systemd-timesyncd; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        svc_found=$svc
        started=$(systemctl show "$svc" -p ActiveEnterTimestamp --value 2>/dev/null)
        up_s=$(($(date +%s) - $(date -d "$started" +%s 2>/dev/null || echo 0)))
        if [ "$up_s" -ge 0 ] && [ "$up_s" -lt 600 ]; then
            echo "WARNING: $svc has only been running for ${up_s}s -- its clock-drift" >&2
            echo "  compensation may not have converged yet. webcam/cast PiP sync in" >&2
            echo "  contest_video.py may drift over a long round -- see CLAUDE.md" >&2
            echo "  ('systemd-timesyncd restart' note). Not blocking; just be aware." >&2
        fi
        break
    fi
done
if [ -z "$svc_found" ]; then
    echo "WARNING: no active chrony/systemd-timesyncd -- system clock may not be" >&2
    echo "  NTP-disciplined at all; webcam/cast PiP sync may drift." >&2
fi

# irssi is fully parameterized -- no reliance on a saved '/server add -auto'
# config on this machine: wait for the bridge (started in the bg window) to
# bind 6667, then connect straight to it. The bridge renames the nick and
# auto-joins #on4kst on its own, so no other client config is needed.
irssi_cmd="while ! nc -z 127.0.0.1 6667; do sleep 0.2; done; exec irssi -c 127.0.0.1 -p 6667"

filename="$(date -uIseconds).cast"
exec asciinema rec "${filename}" -c "tmux new-session '$irssi_cmd'\; split-window $d/puskas_logger.py\; select-layout even-horizontal\; new-window -d -n bg $d/hamlib_supervisor.py\; split-window -t bg $d/on4kst_irc_bridge.py"
