#!/bin/sh
# Force an immediate, verified clock sync -- run this right before a round
# starts (chrony must already be running; see CLAUDE.md's laptop-
# clock-drift note for why chrony over systemd-timesyncd).
#
# chrony's normal poll ramp (starts at 32s, grows to minutes) is fine for
# steady-state operation, but after any gap -- laptop just woke up,
# reconnected to a new network, hasn't been online in a while -- it can
# take a while to gather enough fresh samples to trust the *rate*
# correction, not just the offset (a one-off "clock looks right" check,
# e.g. time.is in a browser, cannot see this). `chronyc burst` forces
# several fresh measurements right now instead of waiting for the next
# scheduled poll; `chronyc makestep` immediately applies a step if the
# offset still warrants one. Both need root -- chronyd's command socket
# denies them to a regular user; read-only queries like tracking/sources
# don't need it.

if ! systemctl is-active --quiet chrony; then
    echo "chrony is not running -- start it first: sudo systemctl start chrony" >&2
    exit 1
fi

echo "Requesting fresh measurements from all sources ..."
sudo chronyc burst 4/4
sleep 12

echo "Forcing an immediate step if the offset still warrants one ..."
sudo chronyc makestep

echo
echo "=== chronyc tracking ==="
chronyc tracking
echo
echo "=== chronyc sources ==="
chronyc sources
echo
echo "Frequency/Skew above describe how well the *rate* is understood, not"
echo "just whether the offset looks right now -- that's the number that"
echo "actually matters for a multi-hour recording. If Skew is still wide"
echo "(tens of ppm), the correction is still converging; normal shortly"
echo "after a gap, and it keeps improving as the round runs. If there's no"
echo "network at all, this can't help -- burst/makestep have nothing to"
echo "measure against, and chrony just keeps running on its last known"
echo "(driftfile-persisted) frequency correction."
