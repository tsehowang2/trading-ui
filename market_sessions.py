"""US daily sessions. Never use a forming daily candle for decisions."""
from functools import lru_cache
from datetime import datetime, timezone

import pandas as pd
import pandas_market_calendars as calendars

PUBLICATION_DELAY = pd.Timedelta(minutes=30)


@lru_cache(maxsize=128)
def schedule(start, end):
    return calendars.get_calendar('NYSE').schedule(start_date=start, end_date=end)


def latest_completed_session(now=None):
    now = pd.Timestamp(now if now is not None else datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise ValueError('Market clock must include a timezone')
    now = now.tz_convert('UTC')
    sessions = schedule((now - pd.Timedelta(days=20)).date().isoformat(), now.date().isoformat())
    finished = sessions.loc[sessions['market_close'] + PUBLICATION_DELAY <= now]
    if finished.empty:
        raise ValueError('No completed US market session available')
    return finished.index[-1].strftime('%Y-%m-%d')


def session_on_or_before(date):
    end = pd.Timestamp(date).date()
    sessions = schedule((pd.Timestamp(end) - pd.Timedelta(days=20)).date().isoformat(), end.isoformat())
    return sessions.index[-1].strftime('%Y-%m-%d')


def session_dates(start, end):
    if pd.Timestamp(start) > pd.Timestamp(end):
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(schedule(str(start)[:10], str(end)[:10]).index).tz_localize(None)