#!/bin/sh
# systemd sleep hook: fire a best-effort chrony resync right after resume.
# Install:
#   sudo mkdir -p /etc/systemd/system-sleep
#   sudo cp chrony-resync-on-wake.sh /etc/systemd/system-sleep/
#   sudo chown root:root /etc/systemd/system-sleep/chrony-resync-on-wake.sh
#   sudo chmod 755 /etc/systemd/system-sleep/chrony-resync-on-wake.sh
#
# Complements chrony-resync-on-network-up.sh, which fires on the more
# precise "network is actually usable" signal instead of resume itself.
#
# Backgrounded so this synchronous, time-budgeted resume hook returns
# immediately. Called as "pre"/"post"; only post-resume matters.

is_resume_event() { [ "$1" = "post" ]; }

if is_resume_event "$1"; then
    (chronyc burst 4/4 >/dev/null 2>&1) &
fi
