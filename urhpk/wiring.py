"""Where things live: the files components share, the ports they meet on, and
where a round's own files belong.

Every name here is a contract between at least two processes — one writes and
another reads, or one listens and another connects — so changing a value is
only safe if both ends move together. That is the reason they live here rather
than in whichever script happens to mention them first.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

# This module sits one directory down from the project root, so one step up —
# and no searching: the answer must not depend on the current directory, which
# is the very thing being checked.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

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
    round: this has to be testable against a pinned root rather than
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


@dataclass(frozen=True)
class RoundInputs:
    """Everything contest_video.py needs, found by looking at the round."""

    recdir: str
    edi: list[str]
    telemetry: str | None
    input_log: str | None
    casts: list[str]
    scope: str | None
    webcams: list[str]


def _one(directory: Path, pattern: str) -> str | None:
    """The single file matching `pattern`, or None if the round has none."""
    found = sorted(directory.glob(pattern))
    if len(found) > 1:
        names = ", ".join(p.name for p in found)
        raise ValueError(f"more than one {pattern} in {directory}: {names}")
    return str(found[0]) if found else None


def discover_round_inputs(directory: Path) -> RoundInputs:
    """The round's own files, by the names the components that wrote them use."""
    recdir = directory / "recording"
    if not recdir.is_dir():
        raise ValueError(f"no recording/ in {directory} -- is this a round directory?")
    edi = sorted(directory.glob("*.edi"))
    if not edi:
        raise ValueError(f"no .edi log in {directory}")
    return RoundInputs(
        recdir=str(recdir),
        edi=[str(p) for p in edi],
        telemetry=_one(directory, "*-telemetry.jsonl"),
        input_log=_one(directory, "*-input.jsonl"),
        # One cast per logger session: a round the logger was restarted in
        # has several, named by their own UTC start, so sorted by name is
        # sorted by time -- as for the webcams below.
        casts=[str(p) for p in sorted(directory.glob("*.cast"))],
        scope=_one(directory, "*.scope"),
        # Never plain *.mp4: the renders live in the round directory too. Sorted
        # by name is sorted by capture time -- the prefix is one round's own, and
        # what follows it is a fixed-width UTC stamp.
        webcams=[str(p) for p in sorted(directory.glob("*-webcam-*.mp4"))],
    )


AV_SKEW_FILE = "webcam-av-skew"  # contest_video reads it, the operator writes it


def round_av_skew(directory: Path) -> float | None:
    """Seconds of webcam A/V skew this round measured for itself, or None.

    The one number about a round that none of the round's own recordings
    carries: how far the camera's pipeline ran behind PulseAudio's under that
    round's load (RECORDING.md has the clap that measures it). It belongs to
    the round rather than to the camera -- an idle machine and a round measured
    145 ms and ~480 ms on the same hardware -- so it lives beside the round's
    other files, where a re-render months later still finds it.

    A file that cannot be read as a number raises rather than falling back:
    the fallback renders two hours of video with the sync this file exists to
    correct, and says nothing."""
    path = directory / AV_SKEW_FILE
    if not path.is_file():
        return None
    text = path.read_text().strip()
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{path} ({AV_SKEW_FILE}) is not a number: {text!r}") from None
