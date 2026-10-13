# //// Neoffice — added file (no upstream equivalent): the calendar a worker filters with is computed in code (13.10).
"""« This week » is the Swiss week, Monday to Sunday, and the code says so to the worker.

Asked which quotes expire this week on a Tuesday, a worker took the week for the seven days from today (13 to 19 October)
where the week runs from the 12th to the 18th; another had taken the week before for the current one. A model computes a
calendar badly: the periods a person names are written into the task by the router, in the site's time, for the router's
own tasks and for the ones the orchestrator creates from a chat (both read worker_reply_directive).
"""

import datetime as dt
import sys
import types

import pytest

from gateway import nora_chat_router as R

TUESDAY = dt.datetime(2026, 10, 13, 15, 30, tzinfo=dt.timezone.utc)


def test_the_week_runs_from_monday_to_sunday():
    hint = R.calendar_hint(TUESDAY)
    assert "today is Tuesday 2026-10-13" in hint
    assert "This week = 2026-10-12 (Monday) to 2026-10-18 (Sunday)" in hint
    assert "last week = 2026-10-05 to 2026-10-11" in hint


@pytest.mark.parametrize(
    "day",
    [dt.date(2026, 10, 12) + dt.timedelta(days=n) for n in range(7)],
)
def test_every_day_of_a_week_names_the_same_week(day):
    hint = R.calendar_hint(dt.datetime.combine(day, dt.time(9, 0)))
    assert "This week = 2026-10-12 (Monday) to 2026-10-18 (Sunday)" in hint
    assert "last week = 2026-10-05 to 2026-10-11" in hint
    assert f"today is {R._WEEKDAY_NAMES[day.weekday()]} {day.isoformat()}" in hint


@pytest.mark.parametrize(
    ("day", "this_week", "last_week"),
    [
        # a week that straddles two years
        (dt.date(2026, 1, 1), "2025-12-29 (Monday) to 2026-01-04 (Sunday)", "2025-12-22 to 2025-12-28"),
        (dt.date(2024, 3, 1), "2024-02-26 (Monday) to 2024-03-03 (Sunday)", "2024-02-19 to 2024-02-25"),
    ],
)
def test_a_week_may_straddle_a_month_or_a_year(day, this_week, last_week):
    hint = R.calendar_hint(dt.datetime.combine(day, dt.time(9, 0)))
    assert f"This week = {this_week}" in hint and f"last week = {last_week}" in hint, hint


@pytest.mark.parametrize(
    ("day", "this_month", "last_month"),
    [
        (dt.date(2026, 10, 13), "2026-10-01 to 2026-10-31", "2026-09-01 to 2026-09-30"),
        (dt.date(2026, 1, 15), "2026-01-01 to 2026-01-31", "2025-12-01 to 2025-12-31"),
        (dt.date(2028, 3, 10), "2028-03-01 to 2028-03-31", "2028-02-01 to 2028-02-29"),
        (dt.date(2026, 12, 31), "2026-12-01 to 2026-12-31", "2026-11-01 to 2026-11-30"),
        (dt.date(2027, 2, 28), "2027-02-01 to 2027-02-28", "2027-01-01 to 2027-01-31"),
    ],
)
def test_the_month_and_the_month_before(day, this_month, last_month):
    hint = R.calendar_hint(dt.datetime.combine(day, dt.time(9, 0)))
    assert f"this month = {this_month}" in hint and f"last month = {last_month}" in hint, hint
    assert f"this year = {day.year}-01-01 to {day.year}-12-31" in hint


@pytest.mark.parametrize("language", ["fr", "de", "it", "en"])
def test_the_calendar_rides_with_the_reply_directive_of_every_language(language):
    directive = R.worker_reply_directive(language)
    assert directive.startswith(R.REPLY_DIRECTIVE_MARK) and directive.endswith("STOP.]")
    assert "Calendar (site time): today is" in directive
    # the partner guard still comes after it, as before
    assert directive.index("Calendar (site time)") < directive.index("business partner")


def test_the_task_the_orchestrator_creates_carries_it_too():
    R.remember_chat_language("chat-calendar", "fr")
    assert "Calendar (site time): today is" in R.chat_reply_directive("chat-calendar")


def test_the_clock_is_the_sites(monkeypatch):
    site_time = dt.datetime(2026, 12, 31, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    monkeypatch.setitem(sys.modules, "hermes_time", types.SimpleNamespace(now=lambda: site_time))
    assert "today is Thursday 2026-12-31" in R.calendar_hint()


def test_a_clock_that_cannot_be_read_never_stops_a_task(monkeypatch):
    def broken():
        raise RuntimeError("no timezone")

    monkeypatch.setitem(sys.modules, "hermes_time", types.SimpleNamespace(now=broken))
    assert "Calendar (site time): today is" in R.calendar_hint()
    assert R.worker_reply_directive("fr").endswith("STOP.]")
