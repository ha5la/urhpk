import io

from urhpk.progress import LOG_INTERVAL_S, stage_bar


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestStageBar:
    def test_redraws_smoothly_when_stderr_is_a_terminal(self):
        with stage_bar("x", 10, stream=_Tty()) as bar:
            assert bar.mininterval < 1.0

    def test_updates_rarely_when_stderr_is_redirected_to_a_log(self):
        with stage_bar("x", 10, stream=io.StringIO()) as bar:
            assert bar.mininterval == LOG_INTERVAL_S
