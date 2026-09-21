"""The round's LAN audio capture: a growing WAV and its clock sidecar."""

from __future__ import annotations

import json
import wave

import pytest

from urhpk import recorders
from urhpk.lan_audio import LanAudioWriter

RATE = 16000
FRAME = b"\x01\x02" * 160  # 160 samples, one 10 ms datagram


class FakeBootClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def boot():
    return FakeBootClock()


def _sidecar(path):
    return [
        json.loads(line)
        for line in path.with_suffix(".wav.jsonl").read_text().splitlines()
    ]


def test_writes_a_playable_wav_of_what_arrived(tmp_path, boot):
    path = tmp_path / "round.wav"
    w = LanAudioWriter(path, RATE, boot_clock=boot)
    w.write(1, FRAME, 1_700_000_000.0)
    w.write(2, FRAME, 1_700_000_000.01)
    w.close()

    with wave.open(str(path)) as f:
        assert (f.getnchannels(), f.getsampwidth(), f.getframerate()) == (1, 2, RATE)
        assert f.getnframes() == 320
        assert f.readframes(320) == FRAME * 2


def test_checkpoints_the_first_datagram(tmp_path, boot):
    path = tmp_path / "round.wav"
    w = LanAudioWriter(path, RATE, boot_clock=boot)
    w.write(41, FRAME, 1_700_000_000.0)
    w.close()

    records = _sidecar(path)
    assert len(records) == 1
    assert records[0]["sample"] == 0
    assert records[0]["seq"] == 41
    assert records[0]["boot"] == 1000.0
    assert records[0]["wall"].startswith("2023-11-14T22:13:20")


def test_records_a_sequence_gap_at_its_sample_index(tmp_path, boot):
    path = tmp_path / "round.wav"
    w = LanAudioWriter(path, RATE, boot_clock=boot)
    w.write(1, FRAME, 1_700_000_000.0)
    w.write(4, FRAME, 1_700_000_000.03)  # 2 and 3 never arrived
    w.close()

    gaps = [r for r in _sidecar(path) if "gap" in r]
    assert len(gaps) == 1
    assert gaps[0]["gap"] == 2
    assert gaps[0]["sample"] == 160
    assert gaps[0]["seq"] == 4


def test_wrapping_sequence_number_is_not_a_gap(tmp_path, boot):
    path = tmp_path / "round.wav"
    w = LanAudioWriter(path, RATE, boot_clock=boot)
    w.write(0xFFFF, FRAME, 1_700_000_000.0)
    w.write(0x0000, FRAME, 1_700_000_000.01)
    w.close()

    assert [r for r in _sidecar(path) if "gap" in r] == []


def test_checkpoints_once_per_interval(tmp_path, boot):
    path = tmp_path / "round.wav"
    w = LanAudioWriter(path, RATE, checkpoint_s=60.0, boot_clock=boot)
    for i in range(5):
        boot.t = 1000.0 + i * 30.0
        w.write(i + 1, FRAME, 1_700_000_000.0 + i * 30.0)
    w.close()

    assert [r["boot"] for r in _sidecar(path)] == [1000.0, 1060.0, 1120.0]


def test_leaves_a_readable_wav_if_the_round_never_closes(tmp_path, boot):
    """The case this capture exists for: the logger dies mid-round. Every
    datagram refreshes the header's sizes, so all of it plays without repair
    -- ffprobe reads an unclosed file as exactly as long as the header says."""
    path = tmp_path / "round.wav"
    w = LanAudioWriter(path, RATE, checkpoint_s=60.0, boot_clock=boot)
    w.write(1, FRAME, 1_700_000_000.0)
    boot.t += 60.0
    w.write(2, FRAME, 1_700_000_060.0)
    w.write(3, FRAME, 1_700_000_060.01)  # after the last checkpoint

    with wave.open(str(path)) as f:
        assert f.getnframes() == 480


class TestTheRoundsRecorder:
    """recorders.py's side of it: armed by the round, fed by the session."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        recorders.lan_audio_close()
        yield
        recorders.lan_audio_close()

    def test_a_datagram_before_the_round_armed_it_leaves_nothing_behind(self, tmp_path):
        recorders.on_lan_audio(1, FRAME, 1_700_000_000.0)
        assert list(tmp_path.iterdir()) == []
        assert recorders.lan_audio_status() == "off"

    def test_the_file_is_opened_by_the_first_datagram(self, tmp_path):
        path = tmp_path / "round.wav"
        recorders.lan_audio_open(path, RATE)
        assert not path.exists()  # a round the radio never reaches leaves none

        recorders.on_lan_audio(1, FRAME, 1_700_000_000.0, now=500.0)
        assert recorders.lan_audio_status(now=500.0) == "recording"
        recorders.lan_audio_close()
        with wave.open(str(path)) as f:
            assert f.getnframes() == 160

    def test_a_stream_that_stopped_arriving_is_not_still_recording(self, tmp_path):
        """The lesson of the round the CW macros died in: a capture that has
        opened a file is not the same as a capture that is still being fed."""
        recorders.lan_audio_open(tmp_path / "round.wav", RATE)
        recorders.on_lan_audio(1, FRAME, 1_700_000_000.0, now=500.0)

        assert recorders.lan_audio_status(now=502.0) == "recording"
        assert recorders.lan_audio_status(now=520.0) == "stalled"

    def test_a_failed_write_stops_the_capture_and_not_the_round(self, tmp_path):
        recorders.lan_audio_open(tmp_path / "round.wav", RATE)
        recorders.on_lan_audio(1, FRAME, 1_700_000_000.0)
        recorders._lan_audio["writer"].close()  # as a full disk would

        recorders.on_lan_audio(2, FRAME, 1_700_000_000.01)

        assert recorders.lan_audio_status() == "failed"


class TestTheDiskWarning:
    def test_says_nothing_with_room_to_spare(self):
        assert recorders.low_disk_warning(20 * 1000**3) is None

    def test_names_the_gigabytes_left_when_the_disk_runs_low(self):
        assert recorders.low_disk_warning(3_200_000_000) == "DISK 3.2G"
