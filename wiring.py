"""Where the components meet: the files they share and the ports they meet on.

Every name here is a contract between at least two processes — one writes and
another reads, or one listens and another connects — so changing a value is
only safe if both ends move together. That is the reason they live here rather
than in whichever script happens to mention them first.
"""

from pathlib import Path

PUSKAS_DIR = Path.home() / ".puskas"

# harvester writes, logger reads
SEEN_STATIONS = PUSKAS_DIR / "puskas-seen-stations.json"
# bridge writes, logger reads
ON4KST_SEEN = PUSKAS_DIR / "on4kst-seen-stations.json"

RIG_SERVER_HOST = "localhost"
RIG_SERVER_PORT = 4532  # logger serves the rigctld dialect, bridge connects

ROTCTLD_HOST = "localhost"
ROTCTLD_PORT = 4533  # hamlib_supervisor starts rotctld here, logger connects
