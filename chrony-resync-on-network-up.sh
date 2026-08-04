#!/bin/sh
# NetworkManager dispatcher hook: resync chrony once a connection is
# actually usable (IP configured, route present) -- catches any
# reconnect, not just sleep/resume. See CLAUDE.md's laptop-clock-drift
# note for the incident this was written for.
# Install:
#   sudo cp chrony-resync-on-network-up.sh /etc/NetworkManager/dispatcher.d/50-chrony-resync
#   sudo chown root:root /etc/NetworkManager/dispatcher.d/50-chrony-resync
#   sudo chmod 755 /etc/NetworkManager/dispatcher.d/50-chrony-resync
#
# NetworkManager runs dispatcher scripts as root already.

connection_action="$2"

is_connection_up_event() {
    [ "$1" = "up" ] || [ "$1" = "vpn-up" ]
}

if is_connection_up_event "$connection_action"; then
    chronyc burst 4/4 >/dev/null 2>&1
fi
