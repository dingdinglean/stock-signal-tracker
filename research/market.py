"""NYSE session clock. Holidays and early closes come from exchange_calendars, not a weekday filter."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import pandas as pd


@lru_cache(maxsize=8)
def xnys(start_year: int, end_year: int):
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS", start=f"{start_year}-01-01", end=f"{end_year}-12-31")


def parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        stamp = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def parse_day(value: object) -> date | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _stamp(day: date) -> pd.Timestamp:
    return pd.Timestamp(day)


def is_session(calendar, day: date) -> bool:
    return bool(calendar.is_session(_stamp(day)))


def session_bounds(calendar, day: date) -> tuple[datetime, datetime] | None:
    """UTC open and close. Early-close days use the calendar's actual close."""
    stamp = _stamp(day)
    if not calendar.is_session(stamp):
        return None
    row = calendar.schedule.loc[stamp]
    return row.open.to_pydatetime(), row.close.to_pydatetime()


def is_half_day(calendar, day: date) -> bool:
    bounds = session_bounds(calendar, day)
    if bounds is None:
        return False
    close = bounds[1].astimezone(timezone.utc)
    # A regular cash close is 20:00 or 21:00 UTC. Earlier than 19:00 UTC is an early close.
    return close.hour < 19


def sessions_in(calendar, start: date, end: date) -> list[date]:
    first = pd.Timestamp(calendar.first_session).date()
    last = pd.Timestamp(calendar.last_session).date()
    start = max(start, first)
    end = min(end, last)
    if end < start:
        return []
    index = calendar.sessions_in_range(_stamp(start), _stamp(end))
    return [pd.Timestamp(value).date() for value in index]


def session_is_complete(calendar, day: date, asof: datetime, buffer_minutes: int = 20) -> bool:
    """A daily bar is knowable only after that session's real close plus the settle buffer."""
    bounds = session_bounds(calendar, day)
    if bounds is None:
        return False
    ready = bounds[1] + timedelta(minutes=buffer_minutes)
    return asof >= ready


def prior_sessions(calendar, day: date, count: int) -> list[date]:
    if count <= 0:
        return []
    window = sessions_in(calendar, day - timedelta(days=count * 3 + 40), day - timedelta(days=1))
    return window[-count:]


def following_sessions(calendar, day: date, count: int) -> list[date]:
    if count <= 0:
        return []
    window = sessions_in(calendar, day + timedelta(days=1), day + timedelta(days=count * 3 + 40))
    return window[:count]


def session_offset(calendar, day: date, count: int) -> date | None:
    found = following_sessions(calendar, day, count)
    return found[-1] if len(found) == count else None


def next_entry_session(calendar, asof: datetime) -> date | None:
    """First NYSE open at or after the information time.

    An open that already happened is not a fill. The next open after a holiday
    or an early close is the next session the calendar actually lists.
    """
    start = asof.date() - timedelta(days=1)
    for day in sessions_in(calendar, start, asof.date() + timedelta(days=18)):
        bounds = session_bounds(calendar, day)
        if bounds and bounds[0] >= asof:
            return day
    return None


def week_end_session(calendar, day: date) -> date | None:
    """Last scheduled NYSE session of the Monday-Sunday week that contains day."""
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    week = sessions_in(calendar, monday, sunday)
    return week[-1] if week else None
