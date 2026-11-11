"""C12/Q3: civil dates, inclusive boundaries and independent follow-up."""

from datetime import date, timedelta

import pytest

from nurse.expiry_calculation import classify_dates

DAY = date(2026, 3, 29)


@pytest.mark.parametrize(
    "start,end,renewed,state,to_renew,proximity",
    [
        (None, None, False, "unknown", False, "none"),
        (None, -1, False, "unknown", False, "none"),
        (1, None, False, "unknown", False, "none"),
        (2, 1, False, "invalid", False, "none"),
        (-1, -2, False, "invalid", False, "none"),
        (1, 10, False, "future", False, "none"),
        (-20, -1, False, "expired", False, "none"),
        (0, 0, False, "valid", True, "near"),
        (-20, 0, False, "valid", True, "near"),
        (-20, 1, False, "valid", True, "near"),
        (-20, 2, False, "valid", True, "none"),
        (-20, 7, False, "valid", True, "none"),
        (-20, 10, False, "valid", True, "none"),
        (-20, 11, False, "valid", False, "none"),
        (-20, 0, True, "valid", False, "none"),
        (-20, 10, True, "valid", False, "none"),
        (-20, -1, True, "expired", False, "none"),
        (2, 1, True, "invalid", False, "none"),
    ],
)
def test_date_precedence_and_renewal_window(
    start, end, renewed, state, to_renew, proximity
):
    dates = [
        None if value is None else DAY + timedelta(days=value) for value in (start, end)
    ]
    assert classify_dates(*dates, DAY, renewed=renewed) == {
        "date_state": state,
        "to_renew": to_renew,
        "proximity": proximity,
    }


def test_default_followup_does_not_invent_a_renewal():
    assert classify_dates(DAY, DAY, DAY) == {
        "date_state": "valid",
        "to_renew": True,
        "proximity": "near",
    }


def test_leap_day_is_a_civil_date():
    leap_day = date(2028, 2, 29)
    assert classify_dates(leap_day, leap_day, leap_day)["to_renew"] is True
