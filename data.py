"""Explicit Yahoo price policy and completed-session incremental downloads.

Yahoo auto_adjust=False OHLC is split-adjusted by the provider, but not dividend
adjusted. These are NOT guaranteed original historical execution prices. Strategy
replay uses this split-adjusted price scale consistently; manual ledger never
replaces actual execution prices with this series.
"""
from datetime import datetime, timezone
import os

import numpy as np
import pandas as pd
import yfinance as yf

from market_cache import symbol_cache
from market_sessions import latest_completed_session, session_dates, session_on_or_before

DATA_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data_cache')
POLICY = 'yahoo-split-adjusted-no-dividend-adjustment-v1'
PRICE_COLUMNS = ['Open', 'High', 'Low', 'Close', 'Volume']


def normalize(frame, cutoff):
    if frame.empty:
        return pd.DataFrame(columns=PRICE_COLUMNS + ['Dividends', 'Stock Splits'])
    frame = frame.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    if any(c not in frame for c in PRICE_COLUMNS):
        raise ValueError('Provider omitted OHLCV fields')
    frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
    frame = frame.loc[frame.index <= pd.Timestamp(cutoff)]
    frame = frame[~frame.index.duplicated(keep='last')].sort_index()
    for c in ('Dividends', 'Stock Splits'):
        if c not in frame:
            frame[c] = 0.0
    frame = frame[PRICE_COLUMNS + ['Dividends', 'Stock Splits']].apply(pd.to_numeric, errors='coerce')
    valid = np.isfinite(frame).all(axis=1) & (frame[['Open', 'High', 'Low', 'Close']] > 0).all(axis=1)
    valid &= (frame['Volume'] >= 0) & (frame['High'] >= frame[['Open', 'Close', 'Low']].max(axis=1))
    valid &= frame['Low'] <= frame[['Open', 'Close', 'High']].min(axis=1)
    return frame.loc[valid]


def _records(frame):
    return [dict(date=date.strftime('%Y-%m-%d'), **{c: float(row[c]) for c in frame.columns})
            for date, row in frame.iterrows()]


def cached_download(symbol, start, end, force=False):
    if not isinstance(symbol, str) or not symbol or len(symbol) > 30 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789^.-=' for c in symbol):
        raise ValueError('Invalid market symbol')
    symbol = symbol.upper()
    completed = latest_completed_session()
    cutoff = min(session_on_or_before(end), completed)
    requested = session_dates(start, cutoff)
    if requested.empty:
        return pd.DataFrame(columns=PRICE_COLUMNS)
    with symbol_cache(symbol, DATA_CACHE_DIR) as document:
        if document.get('policy') != POLICY:
            document.clear()
        bars = document.get('bars', [])
        existing = pd.DataFrame(bars)
        if not existing.empty:
            # Historical backtests must not truncate a newer cache snapshot.
            existing = normalize(existing.set_index('date'), completed)
        else:
            existing = normalize(pd.DataFrame(), cutoff)
        missing = requested.difference(existing.index)
        # Suppress repeated same-session attempts for delisted/missing bars, but
        # do not mark them as available. New session or force retries them.
        attempt = document.get('attempt', {})
        # Pre-listing history isn't a fresh gap every day. Once the provider
        # was queried from that date, only retry gaps inside/after known bars.
        if (not existing.empty and attempt.get('start', '9999') <= str(start)[:10]):
            missing = missing[missing >= existing.index.min()]
        covered_attempt = (attempt.get('session') == completed and attempt.get('start', '9999') <= str(start)[:10]
                           and attempt.get('end', '') >= cutoff)
        if force or (not missing.empty and not covered_attempt):
            begin = requested[0] if force else missing[0]
            try:
                raw = yf.download(symbol, start=begin.strftime('%Y-%m-%d'),
                                  end=(pd.Timestamp(cutoff) + pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
                                  auto_adjust=False, actions=True, progress=False, threads=False)
                fetched = normalize(raw, cutoff)
            except Exception as error:
                document['last_error'] = type(error).__name__
                fetched = normalize(pd.DataFrame(), cutoff)
            if not fetched.empty:
                # Splits can revise the provider's whole historical price scale.
                # Full re-fetch is mandatory when a newly observed split occurs.
                new_splits = fetched.loc[(fetched['Stock Splits'] != 0) & ~fetched.index.isin(existing.index)]
                if not existing.empty and not new_splits.empty and begin > existing.index.min():
                    raw = yf.download(symbol, start=existing.index.min().strftime('%Y-%m-%d'),
                                      end=(pd.Timestamp(cutoff) + pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
                                      auto_adjust=False, actions=True, progress=False, threads=False)
                    refreshed = normalize(raw, cutoff)
                    if not existing.index.isin(refreshed.index).all():
                        raise ValueError('Incomplete historical refresh after split; cannot mix price scales')
                    # A historical refresh retains bars beyond its cutoff.
                    existing = pd.concat([refreshed, existing.loc[existing.index > pd.Timestamp(cutoff)]])
                else:
                    existing = pd.concat([existing, fetched])
                    existing = existing[~existing.index.duplicated(keep='last')].sort_index()
            document['attempt'] = dict(session=completed, start=min(attempt.get('start',str(start)[:10]),str(start)[:10]), end=cutoff)
        document.update(policy=POLICY, symbol=symbol, bars=_records(existing),
                        checked_at=datetime.now(timezone.utc).isoformat())
        result = existing.loc[(existing.index >= pd.Timestamp(start)) & (existing.index <= pd.Timestamp(cutoff))].copy()
        result.attrs['price_policy'] = POLICY
        result.attrs['completed_session'] = cutoff
        result.attrs['missing_sessions'] = [d.strftime('%Y-%m-%d') for d in requested.difference(result.index)]
        return result