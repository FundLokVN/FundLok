"""T8 (HANDOFF-03 Issue 9 / OP-05) -- Vietnamese business-day calendar.

Pure logic, no DB -- unlike most of this suite, these run without
conftest.py's real-Postgres fixture.
"""
import datetime

import pytest

from app.repayment_schedule.calendar import (
    BusinessDayCalendar,
    MissingHolidayCalendarYearError,
    calendar_year_status,
    known_calendar_years,
)


@pytest.fixture
def cal() -> BusinessDayCalendar:
    return BusinessDayCalendar()


def test_ordinary_weekday_is_a_business_day(cal):
    assert cal.is_business_day(datetime.date(2026, 9, 17)) is True  # Thursday


def test_weekend_is_not_a_business_day(cal):
    assert cal.is_business_day(datetime.date(2026, 9, 19)) is False  # Saturday
    assert cal.is_business_day(datetime.date(2026, 9, 20)) is False  # Sunday


@pytest.fixture
def cal_holidays() -> BusinessDayCalendar:
    """Flag-on calendar: the pre-D-046 behaviour, where VN public holidays are skipped."""
    return BusinessDayCalendar(observe_public_holidays=True)


def test_public_holiday_is_a_normal_repayment_day(cal):
    """D-046 / TD-01: holidays are ignored by default. 24 Nov 2026 (Tue) and
    1 Jan 2027 (Fri) used to be skipped (Appendix M 7.5); now they carry instalments."""
    assert cal.is_business_day(datetime.date(2026, 11, 24)) is True
    assert cal.is_business_day(datetime.date(2027, 1, 1)) is True
    days = cal.generate_business_days(datetime.date(2026, 11, 23), 3)
    assert days == [datetime.date(2026, 11, 23), datetime.date(2026, 11, 24), datetime.date(2026, 11, 25)]
    days = cal.generate_business_days(datetime.date(2026, 12, 31), 2)
    assert days == [datetime.date(2026, 12, 31), datetime.date(2027, 1, 1)]


def test_tet_2026_is_a_normal_working_week_by_default(cal):
    for day in range(16, 21):
        assert cal.is_business_day(datetime.date(2026, 2, day)) is True


def test_weekends_still_skipped(cal):
    # Flag off: weekends are still skipped, 21 days a month / 252 a year.
    assert cal.is_business_day(datetime.date(2026, 11, 28)) is False  # Saturday
    assert cal.is_business_day(datetime.date(2026, 11, 29)) is False  # Sunday
    days = cal.generate_business_days(datetime.date(2026, 11, 2), 252)
    assert all(d.weekday() < 5 for d in days)
    assert len(days) == 252
    # 252 weekdays from Mon 2 Nov 2026 end on Tue 19 Oct 2027 (no holiday is removed on the way)
    assert days[-1] == datetime.date(2027, 10, 19)
    # month of 21 weekdays with no holiday removal: Nov 2026 has 21 weekdays
    nov = [d for d in cal.generate_business_days(datetime.date(2026, 11, 1), 21) if d.month == 11]
    assert len(nov) == 21


def test_flag_restores_holiday_skipping(cal_holidays):
    assert cal_holidays.is_business_day(datetime.date(2026, 11, 24)) is False
    assert cal_holidays.is_business_day(datetime.date(2027, 1, 1)) is False
    days = cal_holidays.generate_business_days(datetime.date(2026, 11, 23), 3)
    assert datetime.date(2026, 11, 24) not in days
    # flag on is also the mode in which a year with no table entry fails loudly
    with pytest.raises(MissingHolidayCalendarYearError):
        cal_holidays.is_business_day(datetime.date(2030, 1, 3))
    # weekends still skipped with the flag on
    assert cal_holidays.is_business_day(datetime.date(2026, 11, 28)) is False


def test_holiday_table_is_kept_even_though_ignored():
    assert {2026, 2027} <= known_calendar_years()


def test_tet_2026_is_excluded_even_though_all_weekdays(cal_holidays):
    cal = cal_holidays
    """EC-01: Tết cannot be derived from weekday rules -- Feb 16-20, 2026
    are Mon-Fri, all statutory holidays."""
    for day in range(16, 21):
        d = datetime.date(2026, 2, day)
        assert d.weekday() < 5  # sanity: these really are weekdays
        assert cal.is_business_day(d) is False


def test_days_immediately_around_tet_are_ordinary_business_days(cal_holidays):
    cal = cal_holidays
    assert cal.is_business_day(datetime.date(2026, 2, 13)) is True  # Friday before
    assert cal.is_business_day(datetime.date(2026, 2, 23)) is True  # Monday after


def test_schedule_spanning_tet_extends_with_no_gap_and_no_extension_event(cal_holidays):
    cal = cal_holidays
    """A schedule spanning Tết extends in calendar terms with no extension
    event and no fee -- generate_business_days simply skips non-business
    days and keeps counting until it has `count` real business days."""
    days = cal.generate_business_days(datetime.date(2026, 2, 10), 10)
    assert len(days) == 10
    for holiday_day in range(16, 21):
        assert datetime.date(2026, 2, holiday_day) not in days
    # every returned date actually is a business day
    for d in days:
        assert cal.is_business_day(d) is True


def test_generate_business_days_rejects_non_positive_count(cal):
    with pytest.raises(ValueError):
        cal.generate_business_days(datetime.date(2026, 9, 17), 0)


def test_nth_business_day_matches_last_of_generated_sequence(cal):
    start = datetime.date(2026, 9, 21)
    n = 30
    sequence = cal.generate_business_days(start, n)
    assert cal.nth_business_day(start, n) == sequence[-1]


def test_backstop_date_is_recomputable_and_index_is_not_encoded_in_it(cal):
    """EC-15: backstop_date is derived and recomputable; N_bs (the index)
    is a separate, immutable fact (enforced at the DB layer in T6) that
    this calendar service never mutates -- adding a holiday shifts the
    resulting *date* without this module touching the index at all."""
    start = datetime.date(2026, 9, 21)
    without_extra = cal.recompute_backstop_date(start, 118)

    cal_with_extra_holiday = BusinessDayCalendar(extra_holidays=frozenset({without_extra}))
    with_extra = cal_with_extra_holiday.recompute_backstop_date(start, 118)

    assert with_extra != without_extra
    assert with_extra > without_extra


def test_unknown_year_is_fine_while_holidays_are_ignored(cal):
    assert cal.is_business_day(datetime.date(2030, 1, 1)) is True  # Tuesday; no table needed (no 2028 added)


def test_unknown_year_raises_loudly_when_observing_holidays(cal_holidays):
    cal = cal_holidays
    with pytest.raises(MissingHolidayCalendarYearError):
        cal.is_business_day(datetime.date(2030, 1, 1))


def test_2026_is_confirmed_and_2027_is_provisional():
    assert calendar_year_status(2026) == "confirmed"
    assert calendar_year_status(2027) == "provisional"
    assert calendar_year_status(2030) is None


def test_known_calendar_years_covers_2026_and_2027():
    assert {2026, 2027} <= known_calendar_years()
