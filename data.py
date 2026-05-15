"""
v2_6/data.py – Self-contained price download with filesystem cache.
Adapted from v4/data.py; no cross-folder imports.
"""
from __future__ import annotations
import os
import warnings
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning)

# Cache lives inside v2_6/ so the whole folder is portable.
_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_CACHE_DIR = os.path.join(_DIR, "data_cache")


def cached_download(symbol: str, start: str, end: str) -> pd.DataFrame:
    """
    Download OHLCV with filesystem cache and incremental backfill.
    Returns a DataFrame indexed by date with columns: Open High Low Close Volume.
    Returns empty DataFrame on error.
    """
    os.makedirs(DATA_CACHE_DIR, exist_ok=True)
    safe = symbol.replace("^", "_").replace("/", "_")
    cache_file = os.path.join(DATA_CACHE_DIR, f"{safe}.csv")
    start_dt = pd.to_datetime(start)
    end_dt   = pd.to_datetime(end)
    existing  = None
    fetch_from = start_dt

    if os.path.exists(cache_file):
        try:
            existing = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            existing.index = pd.to_datetime(existing.index).tz_localize(None)
            if len(existing) > 0:
                cached_start = existing.index.min()
                cached_end   = existing.index.max()
                # Prepend older data if needed
                if start_dt < cached_start - pd.Timedelta(days=3):
                    older = yf.download(
                        symbol,
                        start=start_dt.strftime("%Y-%m-%d"),
                        end=cached_start.strftime("%Y-%m-%d"),
                        progress=False,
                    )
                    if isinstance(older.columns, pd.MultiIndex):
                        older.columns = older.columns.get_level_values(0)
                    if not older.empty:
                        older.index = pd.to_datetime(older.index).tz_localize(None)
                        existing = pd.concat([older, existing])
                        existing = existing[~existing.index.duplicated(keep="last")]
                        existing.sort_index(inplace=True)
                        cached_end = existing.index.max()
                # Cache covers requested window – return immediately
                if cached_end >= end_dt - pd.Timedelta(days=1):
                    existing.to_csv(cache_file)
                    return existing.loc[
                        (existing.index >= start_dt) & (existing.index <= end_dt)
                    ]
                fetch_from = cached_end + pd.Timedelta(days=1)
        except Exception:
            existing   = None
            fetch_from = start_dt

    new_df = pd.DataFrame()
    if fetch_from <= end_dt:
        try:
            new_df = yf.download(
                symbol,
                start=fetch_from.strftime("%Y-%m-%d"),
                end=(end_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                progress=False,
            )
            if isinstance(new_df.columns, pd.MultiIndex):
                new_df.columns = new_df.columns.get_level_values(0)
            if not new_df.empty:
                new_df.index = pd.to_datetime(new_df.index).tz_localize(None)
        except Exception:
            new_df = pd.DataFrame()

    if existing is not None and not existing.empty and not new_df.empty:
        combined = pd.concat([existing, new_df])
        combined = combined[~combined.index.duplicated(keep="last")]
        combined.sort_index(inplace=True)
    elif not new_df.empty:
        combined = new_df
    elif existing is not None and not existing.empty:
        combined = existing
    else:
        return pd.DataFrame()

    try:
        combined.to_csv(cache_file)
    except Exception:
        pass

    return combined.loc[
        (combined.index >= start_dt) & (combined.index <= end_dt)
    ]
