"""The suite's own runtime ceiling -- see .scratch/backlog/issues/02-pytest-budget.md.

The hook that enforces it lives in conftest.py; everything decidable is in the
pure function tested here, so the decision needs no subprocess pytest run of its
own to check.
"""

from tests.conftest import DEFAULT_MARKEXPR, budget_message


class TestBudgetMessage:
    def test_reports_the_elapsed_time_and_the_budget_when_over(self):
        message = budget_message(
            13.2, [], ci=False, markexpr=DEFAULT_MARKEXPR, budget=12.0
        )
        assert message is not None
        assert "13.2" in message
        assert "12" in message

    def test_names_the_ten_slowest_tests_worst_first(self):
        durations = [(f"test_{i}", float(i)) for i in range(20)]
        message = budget_message(
            13.2, durations, ci=False, markexpr=DEFAULT_MARKEXPR, budget=12.0
        )
        named = [line for line in message.splitlines() if "test_" in line]
        assert [line.split()[-1] for line in named] == [
            f"test_{i}" for i in range(19, 9, -1)
        ]

    def test_is_silent_in_ci_however_slow_the_run_was(self):
        assert (
            budget_message(30.0, [], ci=True, markexpr=DEFAULT_MARKEXPR, budget=12.0)
            is None
        )

    def test_is_silent_for_a_selection_other_than_the_default_suite(self):
        assert budget_message(30.0, [], ci=False, markexpr="smoke", budget=12.0) is None

    def test_is_silent_when_the_run_fits_the_budget(self):
        assert (
            budget_message(11.9, [], ci=False, markexpr=DEFAULT_MARKEXPR, budget=12.0)
            is None
        )
