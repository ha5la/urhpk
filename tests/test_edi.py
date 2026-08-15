"""Tests for the EDI format module."""

import pytest

from urhpk import edi


class TestModeFromRadio:
    """A log knows three modes; a radio reports a dozen.

    This lived in three places before: the logger's own normalizer, a partial
    copy in contest_video that folded only the SSB aliases, and the EDI code
    tables. The partial copy is why a `CW-R` WAV title used to reach the video
    unfolded while the logger folded it.
    """

    @pytest.mark.parametrize("raw", ["USB", "LSB", "AM", "DSB", "SAM"])
    def test_ssb_family(self, raw):
        assert edi.mode_from_radio(raw) == "SSB"

    @pytest.mark.parametrize("raw", ["CW", "CWR", "CW-R"])
    def test_cw_family(self, raw):
        assert edi.mode_from_radio(raw) == "CW"

    @pytest.mark.parametrize("raw", ["FM", "FMN", "WFM", "NFM"])
    def test_fm_family(self, raw):
        assert edi.mode_from_radio(raw) == "FM"

    def test_case_insensitive(self):
        assert edi.mode_from_radio("cw-r") == "CW"

    def test_unknown_passes_through(self):
        """Not guessed at: a mode we don't know shows up in the log rather
        than being silently filed as SSB."""
        assert edi.mode_from_radio("DV") == "DV"
        assert edi.mode_from_radio("RTTY") == "RTTY"

    def test_empty_defaults_to_ssb(self):
        assert edi.mode_from_radio("") == "SSB"
        assert edi.mode_from_radio(None) == "SSB"


class TestCodeTables:
    def test_round_trip(self):
        for code, mode in edi.MODE_BY_CODE.items():
            assert edi.CODE_BY_MODE[mode] == code
        for header, band in edi.BAND_BY_HEADER.items():
            assert edi.HEADER_BY_BAND[band] == header

    def test_every_logged_mode_is_a_family_output(self):
        """The tables and the normalizer have to agree on the vocabulary."""
        for mode in edi.MODE_BY_CODE.values():
            assert edi.mode_from_radio(mode) == mode
