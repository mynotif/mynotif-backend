"""#283 AC1: civil Paris milestones, current-day catch-up and strict preferences."""

from datetime import date, datetime
from importlib import import_module
from zoneinfo import ZoneInfo

import pytest

PARIS = ZoneInfo("Europe/Paris")


def calculation():
    try:
        return import_module("nurse.expiry_reminder_calculation")
    except ModuleNotFoundError as exc:
        if exc.name != "nurse.expiry_reminder_calculation":
            raise
        pytest.fail("#283 requires the additive reminder calculation module")


def instant(value):
    return datetime.fromisoformat(value)


@pytest.mark.parametrize(
    ("lead", "prealert"),
    [
        (7, date(2026, 3, 31)),
        (8, date(2026, 3, 30)),
        (9, date(2026, 3, 29)),
        (10, date(2026, 3, 28)),
    ],
)
def test_ac1_each_preference_has_one_prealert_and_one_j1(lead, prealert):
    rows = calculation().reminder_milestones(date(2026, 3, 1), date(2026, 4, 7), lead)
    assert rows == (
        {"kind": "prealert", "due_date": prealert, "eligible_date": prealert},
        {"kind": "j1", "due_date": date(2026, 4, 6), "eligible_date": date(2026, 4, 6)},
    )


@pytest.mark.parametrize(
    ("start", "end", "renewed"),
    [
        (None, date(2026, 4, 7), False),
        (date(2026, 3, 1), None, False),
        (date(2026, 4, 8), date(2026, 4, 7), False),
        (date(2026, 3, 1), date(2026, 4, 7), True),
    ],
)
def test_ac1_unknown_invalid_or_replaced_prescription_has_no_milestone(
    start, end, renewed
):
    assert calculation().reminder_milestones(start, end, 10, renewed=renewed) == ()


def test_t06_future_start_defers_each_opportunity_without_changing_nominal_day():
    rows = calculation().reminder_milestones(date(2026, 4, 6), date(2026, 4, 7), 10)
    assert [(r["kind"], r["due_date"], r["eligible_date"]) for r in rows] == [
        ("prealert", date(2026, 3, 28), date(2026, 4, 6)),
        ("j1", date(2026, 4, 6), date(2026, 4, 6)),
    ]


@pytest.mark.parametrize(
    ("value", "opened", "opens", "deadline"),
    [
        ("2026-03-29T06:59:59+00:00", False, "07:00", "16:00"),
        ("2026-03-29T07:00:00+00:00", True, "07:00", "16:00"),
        ("2026-03-29T15:59:59+00:00", True, "07:00", "16:00"),
        ("2026-03-29T16:00:00+00:00", False, "07:00", "16:00"),
        ("2026-10-25T08:00:00+00:00", True, "08:00", "17:00"),
        ("2026-05-01T10:00:00+00:00", True, "07:00", "16:00"),
    ],
)
def test_ac1_window_uses_paris_dst_and_keeps_weekends_and_holidays(
    value, opened, opens, deadline
):
    now = instant(value)
    window = calculation().reminder_window(now)
    assert window["service_day"] == now.astimezone(PARIS).date()
    assert window["is_open"] is opened
    assert window["opens_at"].astimezone(ZoneInfo("UTC")).strftime("%H:%M") == opens
    assert (
        window["deadline_at"].astimezone(ZoneInfo("UTC")).strftime("%H:%M") == deadline
    )


@pytest.mark.parametrize(
    ("day", "now", "state"),
    [
        ("2026-03-29", "2026-03-28T12:00:00+00:00", "planned"),
        ("2026-03-28", "2026-03-28T07:59:59+00:00", "planned"),
        ("2026-03-28", "2026-03-28T08:00:00+00:00", "eligible"),
        ("2026-03-28", "2026-03-28T16:59:59+00:00", "eligible"),
        ("2026-03-28", "2026-03-28T17:00:00+00:00", "missed"),
        ("2026-03-27", "2026-03-28T12:00:00+00:00", "missed"),
    ],
)
def test_ac1_catch_up_is_only_for_current_service_day(day, now, state):
    assert (
        calculation().opportunity_state(date.fromisoformat(day), instant(now)) == state
    )
