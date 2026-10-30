"""Working-day helpers for windows, dispute deadlines and start dates (HANDOFF-04 B2).

Reuses the Vietnamese business-day calendar (`app/repayment_schedule/calendar.py`) and ICT. Time is always
injected by the caller; nothing here reads the wall clock.
"""
from __future__ import annotations

import datetime as dt

from app.core.timezone import ICT
from app.repayment_schedule.calendar import BusinessDayCalendar

_DEFAULT_CAL: BusinessDayCalendar | None = None


def default_calendar() -> BusinessDayCalendar:
    global _DEFAULT_CAL
    if _DEFAULT_CAL is None:
        _DEFAULT_CAL = BusinessDayCalendar()
    return _DEFAULT_CAL


def ict_date(moment: dt.datetime) -> dt.date:
    """The ICT calendar date of a timezone-aware datetime."""
    if moment.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return moment.astimezone(ICT).date()


def start_of_day_ict(d: dt.date) -> dt.datetime:
    return dt.datetime.combine(d, dt.time.min, tzinfo=ICT)


def add_working_days(d: dt.date, n: int, calendar: BusinessDayCalendar | None = None) -> dt.date:
    """The n-th business day strictly after `d` (n >= 1)."""
    if n < 1:
        raise ValueError("n must be >= 1")
    cal = calendar or default_calendar()
    return cal.generate_business_days(d + dt.timedelta(days=1), n)[-1]


def first_working_day_after(d: dt.date, calendar: BusinessDayCalendar | None = None) -> dt.date:
    """The first business day strictly after `d` (repayment start date rule, PROVISIONAL(Q-D4))."""
    return add_working_days(d, 1, calendar)


def working_deadline(moment: dt.datetime, n: int, calendar: BusinessDayCalendar | None = None) -> dt.datetime:
    """End of the n-th working day after `moment`'s ICT date (exclusive upper bound = 00:00 ICT of the next day).

    "2 working days from notification" (Loc.md timers): a tranche attested on Friday 15:00 has until the end of
    Tuesday (the 2nd working day after Friday) to be disputed.
    """
    last = add_working_days(ict_date(moment), n, calendar)
    return start_of_day_ict(last + dt.timedelta(days=1))


def count_business_days(start: dt.date, end_exclusive: dt.date, calendar: BusinessDayCalendar | None = None) -> int:
    """Number of business days d with start <= d < end_exclusive."""
    cal = calendar or default_calendar()
    count = 0
    d = start
    while d < end_exclusive:
        if cal.is_business_day(d):
            count += 1
        d += dt.timedelta(days=1)
    return count
