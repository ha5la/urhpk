"""Where things live: the files components share, the ports they meet on, and
where a round's own files belong.

Every name here is a contract between at least two processes — one writes and
another reads, or one listens and another connects — so changing a value is
only safe if both ends move together. That is the reason they live here rather
than in whichever script happens to mention them first.
"""

import sys
from pathlib import Path

# This module sits in the project root, so no walking up is needed — and none
# is wanted: the answer must not depend on the current directory, which is the
# very thing being checked.
PROJECT_ROOT = Path(__file__).resolve().parent

PUSKAS_DIR = Path.home() / ".puskas"

# harvester writes, logger reads
SEEN_STATIONS = PUSKAS_DIR / "puskas-seen-stations.json"
# bridge writes, logger reads
ON4KST_SEEN = PUSKAS_DIR / "on4kst-seen-stations.json"

RIG_SERVER_HOST = "localhost"
RIG_SERVER_PORT = 4532  # logger serves the rigctld dialect, bridge connects

ROTCTLD_HOST = "localhost"
ROTCTLD_PORT = 4533  # hamlib_supervisor starts rotctld here, logger connects


def round_directory_error(cwd: Path, project_root: Path | None = None) -> str | None:
    """Why `cwd` cannot hold a round, or None if it can.

    A round writes an .edi log, three side-channel files and a video; several
    rounds' worth in one directory is a pile nobody can attribute afterwards.
    Only the project root is refused — anywhere else, including outside the
    project, is the operator's business.

    Pure, and both inputs are arguments, because a false positive here costs a
    contest round: this has to be testable against a pinned root rather than
    against wherever the suite happens to run.
    """
    root = PROJECT_ROOT if project_root is None else project_root
    if cwd.resolve() != root.resolve():
        return None
    return (
        f"Refusing to run in the project root ({root}).\n"
        "A round's files belong in their own directory, one round per directory.\n"
        "  mkdir -p 26szeptember && cd 26szeptember"
    )


def require_round_directory(cwd: Path | None = None) -> None:
    """Exit with an explanation if the current directory is the project root."""
    message = round_directory_error(cwd or Path.cwd())
    if message:
        print(message, file=sys.stderr)
        sys.exit(2)
