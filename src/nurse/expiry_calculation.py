"""Civil-date expiry rules, independent of transport and persistence."""

from datetime import timedelta

CALCULATION_VERSION = "c12-20261101"
CLASSIFICATION_VERSION = "m05-q3-1"


def classify_dates(start_date, end_date, business_date, renewed=False):
    if start_date is not None and end_date is not None and start_date > end_date:
        state = "invalid"
    elif start_date is None or end_date is None:
        state = "unknown"
    elif start_date > business_date:
        state = "future"
    elif end_date < business_date:
        state = "expired"
    else:
        state = "valid"
    to_renew = (
        state == "valid"
        and not renewed
        and end_date <= business_date + timedelta(days=10)
    )
    return {
        "date_state": state,
        "to_renew": to_renew,
        "proximity": "near"
        if to_renew and end_date <= business_date + timedelta(days=1)
        else "none",
    }
