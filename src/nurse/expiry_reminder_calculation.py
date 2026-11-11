"""Civil Paris milestones; no holiday adjustment or historical catch-up."""

from datetime import datetime, time, timedelta

from nurse.expiry_state import PARIS


def reminder_milestones(start_date, end_date, lead_days, *, renewed=False):
    if type(lead_days) is not int or lead_days not in (7, 8, 9, 10):
        raise ValueError("invalid lead days")
    if renewed or start_date is None or end_date is None or start_date > end_date:
        return ()
    offsets = {"prealert": lead_days, "j1": 1}
    return tuple(
        {
            "kind": kind,
            "due_date": end_date - timedelta(days=offset),
            "eligible_date": max(start_date, end_date - timedelta(days=offset)),
        }
        for kind, offset in offsets.items()
    )


def reminder_window(now):
    local = now.astimezone(PARIS)
    day = local.date()
    opens = datetime.combine(day, time(9), PARIS)
    deadline = datetime.combine(day, time(18), PARIS)
    return {
        "service_day": day,
        "opens_at": opens,
        "deadline_at": deadline,
        "is_open": opens <= local < deadline,
    }


def opportunity_state(eligible_date, now):
    window = reminder_window(now)
    if eligible_date < window["service_day"] or (
        eligible_date == window["service_day"] and now >= window["deadline_at"]
    ):
        return "missed"
    if eligible_date == window["service_day"] and window["is_open"]:
        return "eligible"
    return "planned"
