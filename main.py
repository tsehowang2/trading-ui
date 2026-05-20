#!/usr/bin/env python3
"""
v2_9p/main.py - Adaptive Crash-Exit Strategy + Portfolio Manager
=================================================================

All v2_9 features are preserved (--backtest, --signal, --random-universe, etc.)
New: --portfolio mode reads a holdings CSV + watchlist to produce a daily
     position-management dashboard with equal-weight sizing suggestions.

PORTFOLIO MODE DESIGN
---------------------
  - Shared capital pool.  Equal-weight allocation among N active positions.
  - Held stocks: show P&L, trailing stop, action (HOLD / ADD / SELL).
  - Watchlist stocks not held: show BUY signal if entry conditions met.
  - Sizing: target slot = total_equity / max_positions (or --max-positions).
  - No partial exits, no averaging down, no automated pyramiding.
  - Simple and robust — avoids v2.0 over-engineering mistakes.

USAGE
-----
  # Backtest single ticker
  python v2_9p/main.py --backtest NVDA --from-year 2022 --to-year 2022

  # Live signal for a ticker you hold
  python v2_9p/main.py --signal NVDA --entry-price 795

  # Portfolio dashboard (reads holdings CSV + scans watchlist)
  python v2_9p/main.py --portfolio holdings.csv \\
      --watchlist NVDA AAPL MSFT AVGO TSLA META AMZN GOOGL COST JPM \\
                  LLY NFLX AVGO AMD MU AMAT QCOM TXN INTC UNH \\
                  ABBV JNJ TMO ISRG HD NKE SBUX LOW BAC GS \\
      --capital 100000 --max-positions 10

  holdings.csv format (see holdings_template.csv):
    ticker,entry_price,shares
    MU,708.00,100
    SOXL,167.00,200

EXIT RULES
----------
  PRIMARY — Triple-condition panic (ALL three must fire):
    1. Close < SMA200 for 2 consecutive days
    2. VIX > 25
    3. VIX term ratio > 1.05 for 3 consecutive days

  SECONDARY — ATR-based adaptive trailing stop from position peak:
    stop_frac = clip(STOP_ATR_MULT × ATR14 / close, MIN=15%, MAX=40%)
    Bull regime (SPY > SMA200 AND VIX < 15): wider 4× multiplier.

ENTRY RULES
-----------
  Normal gate:  2 consecutive closes > SMA200  AND  Close > SMA50  AND  RS ok
  Fast gate:    2 consecutive closes > SMA200  AND  Close > 20-day high  AND  RS ok
                («20-day high breakout» replaces the old SMA20/rebound check to
                 prevent re-entry on fake recoveries that merely bounce above SMA20.
                 Both gates require the 2-consecutive-close filter to avoid
                 entering on the very first day of a failed SMA200 reclaim.)
  Crash-recovery: within 10 bars after a CRASH exit, re-enter if
                  Close > SMA200 AND VIX < 25.  Skips both the 2-bar filter
                  and the SMA50/breakout requirement — V-shaped bounces need speed.
  RS gate (normal + fast only): stock 20d return − SPY 20d return >= −5%.

EXIT MODEL
----------
  trail-close  Close <= stop level → fill at NEXT-bar open  (end-of-day signal).
  trail-stop   Low  <= stop level but close above → fill at STOP LEVEL same bar
               (simulates a resting stop order; avoids the intraday-trigger /
                next-open fill inconsistency of the old trail-low model).
  crash        Triple-condition panic → fill at NEXT-bar open.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import tomllib
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        

_HERE = os.path.dirname(os.path.abspath(__file__))
from data import cached_download  # type: ignore  # noqa: E402

# ------------------------------------------------------------------------------
# CONSTANTS
# ------------------------------------------------------------------------------
COMMISSION      = 2.00
INITIAL_CAPITAL = 100_000.0

LOGS_DIR    = os.path.join(_HERE, "logs")
RESULTS_DIR = os.path.join(_HERE, "results")

CRASH_BELOW_SMA_DAYS = 2
CRASH_VIX_LEVEL      = 25.0
CRASH_TERM_RATIO     = 1.05
CRASH_TERM_DAYS      = 3

ATR_WINDOW            = 14
STOP_ATR_MULT         = 3.0
STOP_ATR_MULT_BULL    = 4.0
MIN_TRAIL_STOP        = 0.15
MAX_TRAIL_STOP        = 0.40
ATR_VOL_THRESHOLD     = 0.10
MAX_TRAIL_STOP_HIVOL  = 0.30

SPY_SMA_WINDOW = 200
VIX_BULL_LEVEL = 15.0

REENTRY_COOLDOWN_BARS = 10
CRASH_RECOVERY_BARS   = 10
MIN_VOL_TO_ENTER      = 0.01

RS_RET_WINDOW     = 20
RS_GATE_THRESHOLD = -0.05
SMA_WINDOW        = 200
SMA50_WINDOW      = 50
SMA20_WINDOW      = 20
PEAK_DD_WINDOW    = 90
REBOUND_THRESHOLD = 0.10
VIX_TERM_SHIFT    = 5
BREAKOUT_WINDOW   = 20   # fast-gate: close must exceed this many bars' high
CONSEC_SMA200_ENTRY = 2  # consecutive closes above SMA200 required before entry

# ── Confidence thresholds ────────────────────────────────────────────────────
# Score is 0-1 composite: RS(40%) + ADX(40%) + SMA200-dist(20%).
# Same formula used in backtest gate AND live portfolio filter.
MIN_BUY_CONFIDENCE     = 0.60   # skip new entries below this
MIN_PYRAMID_CONFIDENCE = 0.65   # pyramids need a bit more conviction
WARN_HOLD_CONFIDENCE   = 0.40   # warn on held positions below this


# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
class _Tee:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._file   = open(path, "w", encoding="utf-8", errors="replace")
        self._stdout = sys.__stdout__

    def write(self, s: str) -> None:
        self._stdout.write(s)
        self._file.write(s)

    def flush(self) -> None:
        self._stdout.flush()
        self._file.flush()

    def reconfigure(self, **_: object) -> None:
        pass


def _setup_log(tag: str) -> str:
    os.makedirs(LOGS_DIR, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOGS_DIR, f"v29p_{tag}_{ts}.log")
    sys.stdout = _Tee(path)
    return path


# ------------------------------------------------------------------------------
# STATS HELPERS
# ------------------------------------------------------------------------------
def _sharpe(eq: pd.Series, rf: float = 0.0) -> float:
    r = eq.pct_change().dropna()
    if r.std() < 1e-9:
        return 0.0
    return float((r.mean() - rf / 252) / r.std() * np.sqrt(252))


def _mdd(eq: pd.Series) -> float:
    return float((eq / eq.cummax() - 1).min())


def _cagr(eq: pd.Series) -> float:
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    return float((eq.iloc[-1] / eq.iloc[0]) ** (365 / days) - 1)


# ------------------------------------------------------------------------------
# BENCHMARKS
# ------------------------------------------------------------------------------
def _monthly_firsts(idx: pd.DatetimeIndex) -> list:
    seen: set = set()
    out = []
    for d in idx:
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _bar_confidence(rs_vs_spy: float, adx14: float, sma200_dist: float) -> float:
    """Composite confidence score in [0, 1].
    RS vs SPY  (40%) — tanh-normalised, centred at 0.
    ADX-14     (40%) — linear, capped at 60.
    SMA200-dist(20%) — tanh-normalised % above SMA200, meaningful to ~30%.
    Used identically in run_backtest (per-bar) and _get_live_indicators.
    """
    rs_norm   = float((np.tanh(rs_vs_spy * 10) + 1) / 2)
    adx_norm  = float(min(adx14 / 60.0, 1.0))
    dist_norm = float((np.tanh(min(sma200_dist, 0.5) * 5) + 1) / 2)
    return round(rs_norm * 0.40 + adx_norm * 0.40 + dist_norm * 0.20, 4)


def _bmark_voo_lumpsum(test_start: str, test_end: str,
                        capital: float = INITIAL_CAPITAL) -> pd.Series:
    raw = cached_download("VOO", test_start, test_end)
    if raw.empty:
        return pd.Series(dtype=float)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    c = raw["Close"].ffill().bfill()
    ts, te = pd.to_datetime(test_start), pd.to_datetime(test_end)
    c = c.loc[(c.index >= ts) & (c.index <= te)]
    return c / float(c.iloc[0]) * capital


def _bmark_dca_voo(test_start: str, test_end: str,
                    capital: float = INITIAL_CAPITAL) -> pd.Series:
    """DCA benchmark: buy 1 share of VOO at evenly-spaced intervals.

    The number of purchases is determined by how many whole shares the
    *initial* capital can afford (using the first available VOO price as
    the reference).  Intervals are then equally spaced across all trading
    bars so that spending is complete by the end of the period.  Any
    residual cash (rounding) is counted as cash throughout.

    Crucially, profits are never recycled — each purchase costs exactly
    one share at market price, and the budget is the original capital only.
    """
    raw = cached_download("VOO", test_start, test_end)
    if raw.empty:
        return pd.Series(dtype=float)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    voo_c = raw["Close"].ffill().bfill()
    ts, te = pd.to_datetime(test_start), pd.to_datetime(test_end)
    voo_c = voo_c.loc[(voo_c.index >= ts) & (voo_c.index <= te)]
    if voo_c.empty:
        return pd.Series(dtype=float)

    prices = voo_c.values.astype(float)
    n_bars  = len(prices)
    ref_price = prices[0]                           # first bar as cost basis

    # How many 1-share lots fit within the original capital?
    n_buys  = max(int(capital // ref_price), 1)

    # Evenly space buy indices across [0, n_bars-1]
    if n_buys == 1:
        buy_indices = {0}
    else:
        step = (n_bars - 1) / (n_buys - 1)
        buy_indices = {round(i * step) for i in range(n_buys)}

    cash   = float(capital)
    shares = 0.0
    eq_vals: list[float] = []
    for bar_idx, price in enumerate(prices):
        if bar_idx in buy_indices and cash >= price:
            shares += 1.0       # always exactly 1 share
            cash   -= price     # deduct actual market price (not ref_price)
        eq_vals.append(cash + shares * price)

    return pd.Series(eq_vals, index=voo_c.index)


def _bmark_random_entry(test_start: str, test_end: str,
                         capital: float = INITIAL_CAPITAL,
                         symbol: str = "VOO",
                         n_trials: int = 200,
                         max_entry_day: int = 20,
                         seed: int = 42) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    raw = cached_download(symbol, test_start, test_end)
    if raw.empty:
        return 0.0, 0.0
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    c = raw["Close"].ffill().bfill()
    ts, te = pd.to_datetime(test_start), pd.to_datetime(test_end)
    c = c.loc[(c.index >= ts) & (c.index <= te)]
    if len(c) < max_entry_day + 2:
        return 0.0, 0.0
    entry_pool = min(max_entry_day, len(c) - 1)
    rets = []
    for _ in range(n_trials):
        idx = int(rng.integers(0, entry_pool))
        entry_price = float(c.iloc[idx])
        exit_price  = float(c.iloc[-1])
        rets.append(exit_price / entry_price - 1)
    return float(np.mean(rets)), float(np.std(rets))


def _bmark_inv_vol_voo(test_start: str, test_end: str,
                        capital: float = INITIAL_CAPITAL) -> pd.Series:
    voo_raw = cached_download("VOO", test_start, test_end)
    vix_raw = cached_download("^VIX", test_start, test_end)
    if voo_raw.empty or vix_raw.empty:
        return pd.Series(dtype=float)
    for r in (voo_raw, vix_raw):
        if isinstance(r.columns, pd.MultiIndex):
            r.columns = r.columns.get_level_values(0)
    voo_c = voo_raw["Close"].ffill().bfill()
    vix_c = vix_raw["Close"].ffill().bfill()
    ts, te = pd.to_datetime(test_start), pd.to_datetime(test_end)
    idx = voo_c.index.intersection(vix_c.index)
    idx = idx[(idx >= ts) & (idx <= te)]
    voo_c = voo_c.reindex(idx)
    vix_c = vix_c.reindex(idx).ffill().bfill()
    rebal_dates = set(_monthly_firsts(idx))
    cash   = capital
    shares = 0.0
    eq_vals = []
    for date in idx:
        price = float(voo_c[date])
        vix   = float(vix_c[date])
        if date in rebal_dates:
            weight     = min(20.0 / max(vix, 1.0), 1.0)
            target_val = (cash + shares * price) * weight
            new_sh     = target_val / price
            diff_sh    = new_sh - shares
            shares    += diff_sh
            cash      -= diff_sh * price
        eq_vals.append(cash + shares * price)
    return pd.Series(eq_vals, index=idx, dtype=float)


def _bmark_6040(test_start: str, test_end: str,
                 capital: float = INITIAL_CAPITAL) -> pd.Series:
    voo_r = cached_download("VOO", test_start, test_end)
    tlt_r = cached_download("TLT", test_start, test_end)
    if voo_r.empty or tlt_r.empty:
        return pd.Series(dtype=float)
    for r in (voo_r, tlt_r):
        if isinstance(r.columns, pd.MultiIndex):
            r.columns = r.columns.get_level_values(0)
    voo_c = voo_r["Close"].ffill().bfill()
    tlt_c = tlt_r["Close"].ffill().bfill()
    ts, te = pd.to_datetime(test_start), pd.to_datetime(test_end)
    idx = voo_c.index.intersection(tlt_c.index)
    idx = idx[(idx >= ts) & (idx <= te)]
    voo_c, tlt_c = voo_c.reindex(idx), tlt_c.reindex(idx)
    rebal  = set(_monthly_firsts(idx))
    voo_sh = (capital * 0.60) / float(voo_c.iloc[0])
    tlt_sh = (capital * 0.40) / float(tlt_c.iloc[0])
    eq_vals = []
    for date in idx:
        pv = voo_sh * float(voo_c[date]) + tlt_sh * float(tlt_c[date])
        if date in rebal and pv > 0:
            voo_sh = pv * 0.60 / float(voo_c[date])
            tlt_sh = pv * 0.40 / float(tlt_c[date])
        eq_vals.append(pv)
    return pd.Series(eq_vals, index=idx)


# ------------------------------------------------------------------------------
# CORE BACKTEST
# ------------------------------------------------------------------------------
def run_backtest(symbol: str, test_start: str, test_end: str,
                 verbose: bool = True) -> dict:
    ts = pd.to_datetime(test_start)
    te = pd.to_datetime(test_end)
    warmup_start = (ts - pd.DateOffset(days=400)).strftime("%Y-%m-%d")

    raw = cached_download(symbol, warmup_start, test_end)
    if raw.empty:
        print(f"  [{symbol}] No data - skip.")
        return {}
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()

    vix_raw = cached_download("^VIX", warmup_start, test_end)
    if isinstance(vix_raw.columns, pd.MultiIndex):
        vix_raw.columns = vix_raw.columns.get_level_values(0)
    df["vix"] = (vix_raw["Close"].reindex(df.index).ffill().bfill()
                 if not vix_raw.empty else 20.0)
    vix_next       = df["vix"].shift(VIX_TERM_SHIFT).bfill()
    df["vix_term"] = (df["vix"] / vix_next.replace(0, np.nan)).fillna(1.0)

    spy_raw = cached_download("SPY", warmup_start, test_end)
    if not spy_raw.empty:
        if isinstance(spy_raw.columns, pd.MultiIndex):
            spy_raw.columns = spy_raw.columns.get_level_values(0)
        spy_close = spy_raw["Close"].reindex(df.index).ffill().bfill()
        df["spy"]        = spy_close
        df["spy_sma200"] = spy_close.rolling(SPY_SMA_WINDOW).mean()
    else:
        df["spy"] = df["spy_sma200"] = np.nan

    df["sma200"] = df["Close"].rolling(SMA_WINDOW).mean()
    df["sma50"]  = df["Close"].rolling(SMA50_WINDOW).mean()
    df["sma20"]  = df["Close"].rolling(SMA20_WINDOW).mean()
    df["low90"]  = df["Close"].rolling(PEAK_DD_WINDOW).min()
    # Fast-gate breakout: close must exceed the highest close of the prior N bars
    df["high20"] = df["Close"].rolling(BREAKOUT_WINDOW).max().shift(1)
    prev_close   = df["Close"].shift(1)
    tr           = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"]  = tr.rolling(ATR_WINDOW).mean()

    df["stock_ret20"] = df["Close"].pct_change(RS_RET_WINDOW)
    if "spy" in df.columns:
        df["spy_ret20"] = df["spy"].pct_change(RS_RET_WINDOW)
    else:
        df["spy_ret20"] = 0.0

    # ── ADX(14) — needed for per-bar confidence gate ─────────────────────────
    _pc   = df["Close"].shift(1)
    _tr   = pd.concat([df["High"] - df["Low"],
                       (df["High"] - _pc).abs(),
                       (df["Low"]  - _pc).abs()], axis=1).max(axis=1)
    _up   = df["High"] - df["High"].shift(1)
    _dn   = df["Low"].shift(1) - df["Low"]
    _pdm  = np.where((_up > _dn) & (_up > 0), _up, 0.0)
    _mdm  = np.where((_dn > _up) & (_dn > 0), _dn, 0.0)
    _atr  = _tr.ewm(alpha=1/ATR_WINDOW, adjust=False).mean()
    _pdi  = (100 * pd.Series(_pdm, index=df.index)
             .ewm(alpha=1/ATR_WINDOW, adjust=False).mean()
             / _atr.replace(0, np.nan))
    _mdi  = (100 * pd.Series(_mdm, index=df.index)
             .ewm(alpha=1/ATR_WINDOW, adjust=False).mean()
             / _atr.replace(0, np.nan))
    _dx   = (100 * (_pdi - _mdi).abs() / (_pdi + _mdi).replace(0, np.nan)).fillna(0)
    df["adx14"] = _dx.ewm(alpha=1/ATR_WINDOW, adjust=False).mean()

    df.dropna(subset=["sma200", "sma50", "sma20", "low90",
                       "atr14", "adx14", "vix", "vix_term",
                       "stock_ret20", "spy_ret20"], inplace=True)

    tdf = df.loc[(df.index >= ts) & (df.index <= te)].copy()
    if len(tdf) < 10:
        print(f"  [{symbol}] Insufficient bars - skip.")
        return {}

    capital     = INITIAL_CAPITAL
    shares      = 0
    in_position = False
    pos_peak    = 0.0
    entry_ep    = 0.0
    consec_bear = 0
    consec_term = 0
    consec_above200 = 0   # consecutive closes above SMA200 (entry gate)
    cooldown    = 0
    crash_timer = 0
    trade_log: list[dict] = []
    eq_vals:   list[float] = []

    for i, (date, cur) in enumerate(tdf.iterrows()):
        nxt = tdf.iloc[i + 1] if i + 1 < len(tdf) else None

        close = float(cur["Close"])
        atr   = float(cur["atr14"])
        vix   = float(cur["vix"])
        eq_vals.append(capital + shares * close)

        if cooldown    > 0: cooldown    -= 1
        if crash_timer > 0: crash_timer -= 1

        if in_position:
            pos_peak = max(pos_peak, close)

        below_sma   = close < float(cur["sma200"])
        term_high   = float(cur["vix_term"]) > CRASH_TERM_RATIO
        consec_bear = consec_bear + 1 if below_sma else 0
        consec_term = consec_term + 1 if term_high  else 0
        # Track consecutive closes above SMA200 for entry confirmation
        if not in_position:
            consec_above200 = (consec_above200 + 1) if not below_sma else 0

        crash = (
            in_position
            and consec_bear >= CRASH_BELOW_SMA_DAYS
            and vix > CRASH_VIX_LEVEL
            and consec_term >= CRASH_TERM_DAYS
            and nxt is not None
        )

        spy_val  = float(cur.get("spy",        np.nan))
        spy_sma  = float(cur.get("spy_sma200", np.nan))
        bull_regime = (
            not np.isnan(spy_val) and not np.isnan(spy_sma)
            and spy_val > spy_sma
            and vix < VIX_BULL_LEVEL
        )
        mult     = STOP_ATR_MULT_BULL if bull_regime else STOP_ATR_MULT
        hi_vol   = (atr / close) > ATR_VOL_THRESHOLD if close > 0 else False
        max_stop = MAX_TRAIL_STOP_HIVOL if hi_vol else MAX_TRAIL_STOP
        atr_frac = np.clip(mult * atr / close, MIN_TRAIL_STOP, max_stop)
        trail_stop_level = pos_peak * (1.0 - atr_frac)

        trailing_hit_close = (
            in_position and pos_peak > 0 and nxt is not None
            and close <= trail_stop_level
        )
        # trail-stop: low breaches stop but close is above → simulate resting stop
        # fill at the stop level on the same bar (not next-open) for consistency.
        trailing_hit_stop = (
            in_position and pos_peak > 0
            and not trailing_hit_close
            and float(cur["Low"]) <= trail_stop_level
        )
        trailing_hit = trailing_hit_close or trailing_hit_stop

        exit_signal = crash or trailing_hit
        if crash:
            exit_reason = "SELL (crash)"
        elif trailing_hit_close:
            exit_reason = "SELL (trail-close)"
        else:
            exit_reason = "SELL (trail-stop)"

        if exit_signal:
            # trail-stop exits at the stop level (same bar); others at next open
            if trailing_hit_stop:
                ep        = trail_stop_level
                exit_date = date           # same bar — use current bar's date
            elif nxt is not None:
                ep        = float(nxt["Open"])
                exit_date = nxt.name
            else:
                ep        = close          # final bar fallback
                exit_date = date
            capital += shares * ep - COMMISSION
            # Correct the equity snapshot for this bar to reflect the actual fill
            # (trail-stop fills intrabar at stop level, not at close)
            if trailing_hit_stop:
                eq_vals[-1] = capital      # fully out of position at stop fill
            pnl_pct = (ep - entry_ep) / entry_ep if entry_ep > 0 else None
            trade_log.append(dict(date=exit_date, action=exit_reason,
                                  price=ep, shares=shares, equity=capital,
                                  atr_frac=round(atr_frac, 4) if not crash else None,
                                  entry_ep=round(entry_ep, 4),
                                  pnl_pct=round(pnl_pct, 4) if pnl_pct is not None else None,
                                  avoided_loss=None, missed_gain=None))
            shares, in_position = 0, False
            pos_peak    = 0.0
            entry_ep    = 0.0
            consec_bear = consec_term = 0
            consec_above200 = 0
            if crash:
                crash_timer = CRASH_RECOVERY_BARS
                cooldown    = 0
            else:
                cooldown = REENTRY_COOLDOWN_BARS

        if not in_position and nxt is not None:
            above_sma200 = close > float(cur["sma200"])
            above_sma50  = close > float(cur["sma50"])
            high20       = float(cur["high20"]) if not np.isnan(cur["high20"]) else 0.0
            breakout_ok  = high20 > 0 and close > high20
            two_bar_ok   = consec_above200 >= CONSEC_SMA200_ENTRY
            vol_ok       = (atr / close) >= MIN_VOL_TO_ENTER if close > 0 else False
            stock_ret20  = float(cur["stock_ret20"])
            spy_ret20    = float(cur["spy_ret20"])
            rs_vs_spy    = stock_ret20 - spy_ret20
            rs_ok        = rs_vs_spy >= RS_GATE_THRESHOLD

            if crash_timer > 0 and above_sma200 and vix < CRASH_VIX_LEVEL and vol_ok:
                # Crash recovery: fast path, skips 2-bar and breakout requirements
                ep     = float(nxt["Open"])
                new_sh = int((capital - COMMISSION) / ep)
                if new_sh > 0:
                    capital    -= new_sh * ep + COMMISSION
                    shares      = new_sh
                    in_position = True
                    pos_peak    = ep
                    entry_ep    = ep
                    crash_timer = 0
                    consec_above200 = 0
                    action = "BUY (initial)" if not trade_log else "BUY (crash-recovery)"
                    trade_log.append(dict(date=nxt.name, action=action,
                                         entry_reason="crash-recovery",
                                         price=ep, shares=new_sh,
                                         equity=capital + new_sh * ep,
                                         atr_frac=None, entry_ep=None,
                                         pnl_pct=None, avoided_loss=None, missed_gain=None))

            elif cooldown == 0 and crash_timer == 0 and vol_ok and rs_ok and two_bar_ok:
                normal_ok  = above_sma200 and above_sma50
                fast_ok    = above_sma200 and breakout_ok
                if normal_ok or fast_ok:
                    sma200_val  = float(cur["sma200"])
                    dist_bar    = (close / sma200_val - 1.0) if sma200_val > 0 else 0.0
                    adx14_bar   = float(cur["adx14"])
                    confidence  = _bar_confidence(rs_vs_spy, adx14_bar, dist_bar)
                    if confidence >= MIN_BUY_CONFIDENCE:
                        ep     = float(nxt["Open"])
                        new_sh = int((capital - COMMISSION) / ep)
                        if new_sh > 0:
                            capital    -= new_sh * ep + COMMISSION
                            shares      = new_sh
                            in_position = True
                            pos_peak    = ep
                            entry_ep    = ep
                            entry_reason = "normal-gate" if normal_ok else "fast-gate"
                            action = "BUY (initial)" if not trade_log else "BUY (re-entry)"
                            trade_log.append(dict(date=nxt.name, action=action,
                                                 entry_reason=entry_reason,
                                                 confidence=round(confidence, 3),
                                                 price=ep, shares=new_sh,
                                                 equity=capital + new_sh * ep,
                                                 atr_frac=None, entry_ep=None,
                                                 pnl_pct=None, avoided_loss=None, missed_gain=None))

    if shares > 0:
        last_close = float(tdf["Close"].iloc[-1])
        pnl_pct = (last_close - entry_ep) / entry_ep if entry_ep > 0 else None
        capital   += shares * last_close - COMMISSION
        trade_log.append(dict(date=tdf.index[-1], action="SELL (final)",
                              price=last_close, shares=shares, equity=capital,
                              atr_frac=None,
                              entry_ep=round(entry_ep, 4),
                              pnl_pct=round(pnl_pct, 4) if pnl_pct is not None else None,
                              avoided_loss=None, missed_gain=None))

    FORWARD_BARS = 20
    close_arr = tdf["Close"].values
    date_idx  = {d: i for i, d in enumerate(tdf.index)}
    for t in trade_log:
        if not t["action"].startswith("SELL") or t["action"] == "SELL (final)":
            continue
        exit_date = pd.Timestamp(t["date"])
        if exit_date not in date_idx:
            continue
        i0 = date_idx[exit_date]
        future = close_arr[i0 + 1: i0 + 1 + FORWARD_BARS]
        if len(future) == 0:
            continue
        ep = t["price"]
        low_fwd  = float(np.min(future))
        high_fwd = float(np.max(future))
        t["avoided_loss"] = round((ep - low_fwd)  / ep, 4)
        t["missed_gain"]  = round((high_fwd - ep) / ep, 4)

    eq        = pd.Series(eq_vals, index=tdf.index, dtype=float)
    total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1)
    n_sells   = sum(1 for t in trade_log if t["action"].startswith("SELL"))
    months    = max((te - ts).days / 30.44, 1)

    result = dict(
        symbol     = symbol,
        test_start = test_start,
        test_end   = test_end,
        eq         = eq,
        total_ret  = total_ret,
        cagr       = _cagr(eq),
        sharpe     = _sharpe(eq),
        mdd        = _mdd(eq),
        tpm        = n_sells / months,
        n_trades   = len(trade_log),
        tlog       = pd.DataFrame(trade_log),
    )

    if verbose:
        print(f"  [{symbol}]  ret={total_ret:+.1%}  CAGR={result['cagr']:+.1%}"
              f"  Sharpe={result['sharpe']:.2f}  MDD={result['mdd']:.1%}"
              f"  T/mo={result['tpm']:.1f}  trades={len(trade_log)}")

    return result


# ------------------------------------------------------------------------------
# SAVE RESULTS
# ------------------------------------------------------------------------------
def save_results(result: dict, name: str) -> None:
    out_dir = os.path.join(RESULTS_DIR, name)
    os.makedirs(out_dir, exist_ok=True)

    eq         = result["eq"]
    sym        = result["symbol"]
    test_start = result["test_start"]
    test_end   = result["test_end"]
    tlog       = result.get("tlog", pd.DataFrame())

    if len(tlog):
        tlog.to_csv(os.path.join(out_dir, "trades.csv"), index=False)

    start_cap = float(eq.iloc[0])
    voo_eq  = _bmark_voo_lumpsum(test_start, test_end, start_cap)
    dca_eq  = _bmark_dca_voo(test_start, test_end, start_cap)
    b6040   = _bmark_6040(test_start, test_end, start_cap)

    try:
        raw = cached_download(sym, test_start, test_end)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        bnh_price = raw["Close"].reindex(eq.index).ffill().bfill()
        bnh_eq    = bnh_price / float(bnh_price.iloc[0]) * start_cap
    except Exception:
        bnh_eq = pd.Series(dtype=float)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True)
    fig.suptitle(
        f"v2.9p Adaptive Stop - {sym}  ({test_start} to {test_end})\n"
        f"ret={result['total_ret']:+.1%}  CAGR={result['cagr']:+.1%}"
        f"  Sharpe={result['sharpe']:.2f}  MDD={result['mdd']:.1%}"
        f"  T/mo={result['tpm']:.1f}",
        fontsize=11,
    )

    ax1.plot(eq.index, eq.values, label="Strategy", lw=2.0, color="steelblue")
    if not bnh_eq.empty:
        ax1.plot(bnh_eq.index, bnh_eq.values, label=f"{sym} B&H",
                 lw=1.4, ls="--", color="black")
    if not voo_eq.empty:
        ax1.plot(voo_eq.index, voo_eq.values, label="VOO lump-sum",
                 lw=1.2, ls="--", color="grey")
    if not dca_eq.empty:
        ax1.plot(dca_eq.index, dca_eq.values, label="DCA VOO",
                 lw=1.2, ls="-.", color="darkorange")
    if not b6040.empty:
        ax1.plot(b6040.index,  b6040.values,  label="60/40 VOO+TLT",
                 lw=1.2, ls=":",  color="green")

    if len(tlog) and "date" in tlog.columns:
        tl = tlog.copy()
        tl["date"] = pd.to_datetime(tl["date"])
        eq_dict = dict(zip(eq.index, eq.values))
        tl["eq_val"] = tl["date"].map(eq_dict)
        buys    = tl[tl["action"].str.startswith("BUY")]
        crashes = tl[tl["action"] == "SELL (crash)"]
        trails  = tl[tl["action"].str.startswith("SELL (trail")]
        if len(buys):
            ax1.scatter(buys["date"], buys["eq_val"], marker="^",
                        color="limegreen", s=60, zorder=5, label="Buy")
        if len(crashes):
            ax1.scatter(crashes["date"], crashes["eq_val"], marker="v",
                        color="crimson", s=60, zorder=5, label="Sell (crash)")
        if len(trails):
            ax1.scatter(trails["date"], trails["eq_val"], marker="v",
                        color="darkorange", s=60, zorder=5, label="Sell (trail ATR)")

    ax1.set_ylabel("Portfolio ($)")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))

    dd = (eq / eq.cummax() - 1) * 100
    ax2.fill_between(dd.index, dd.values, 0, color="crimson", alpha=0.4)
    ax2.set_ylabel("Drawdown (%)")
    ax2.set_xlabel("Date")
    ax2.grid(alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    plt.xticks(rotation=30)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "chart.png"), dpi=150)
    plt.close(fig)
    print(f"  -> Saved: {out_dir}/  [chart.png + trades.csv]")


# ------------------------------------------------------------------------------
# BENCHMARK TABLE
# ------------------------------------------------------------------------------
def print_benchmarks(result: dict) -> None:
    test_start = result["test_start"]
    test_end   = result["test_end"]
    ts = pd.to_datetime(test_start)
    te = pd.to_datetime(test_end)
    days       = max((te - ts).days, 1)
    eq         = result["eq"]
    strat_ret  = result["total_ret"]
    strat_cagr = result["cagr"]
    initial    = float(eq.iloc[0])

    print(f"\n{'='*65}")
    print(f"  BENCHMARK COMPARISON  [{test_start} -> {test_end}]")
    print(f"{'='*65}")
    print(f"  {'Benchmark':<24} {'TotRet':>8} {'CAGR':>8} {'End $':>12}")
    print(f"  {'-'*54}")
    print(f"  {'Strategy':<24} {strat_ret:>+8.1%} {strat_cagr:>+8.1%}"
          f" ${eq.iloc[-1]:>11,.0f}")

    def _row(label: str, eq_s: pd.Series) -> None:
        if eq_s is None or eq_s.empty:
            return
        r    = float(eq_s.iloc[-1] / eq_s.iloc[0] - 1)
        cagr = float((eq_s.iloc[-1] / eq_s.iloc[0]) ** (365 / days) - 1)
        beat = "  BEATS" if strat_ret > r else ""
        print(f"  {label:<24} {r:>+8.1%} {cagr:>+8.1%} ${eq_s.iloc[-1]:>11,.0f}{beat}")

    _row("VOO lump-sum",    _bmark_voo_lumpsum(test_start, test_end, initial))
    _row("DCA VOO",         _bmark_dca_voo(test_start, test_end, initial))
    _row("60/40 VOO + TLT", _bmark_6040(test_start, test_end, initial))
    _row("Inv-vol VOO",     _bmark_inv_vol_voo(test_start, test_end, initial))

    mu, sigma = _bmark_random_entry(test_start, test_end, initial)
    beat = "  BEATS" if strat_ret > mu else ""
    print(f"  {'Rand-entry VOO (μ)':<24} {mu:>+8.1%}   {'±'+f'{sigma:.1%}':<8}           {beat}")
    print()


# ------------------------------------------------------------------------------
# MULTI-YEAR / MULTI-TICKER GRID
# ------------------------------------------------------------------------------
def run_grid(tickers: list, from_year: int, to_year: int,
             save_charts: bool = True) -> None:
    years = list(range(from_year, to_year + 1))
    total = len(tickers) * len(years)
    print(f"\n  Grid: {len(tickers)} ticker(s) x {len(years)} year(s) = {total} runs")

    all_rows: list[dict] = []

    for sym in tickers:
        print(f"\n{'─'*65}")
        print(f"  Symbol: {sym}")
        sym_rows: list[dict] = []
        run_n = 0

        for year in years:
            run_n += 1
            test_start = f"{year}-01-01"
            cur_year   = datetime.now().year
            test_end   = (datetime.now().strftime("%Y-%m-%d")
                          if year == cur_year else f"{year}-12-31")

            print(f"\n  [{run_n:>3}/{len(years)}]  {sym}  {year}", flush=True)
            r = run_backtest(sym, test_start, test_end, verbose=True)
            if not r:
                continue

            if save_charts:
                save_results(r, f"{sym}_{year}")

            print_benchmarks(r)

            sym_rows.append(dict(
                symbol   = sym,
                year     = year,
                ret      = r["total_ret"],
                cagr     = r["cagr"],
                sharpe   = r["sharpe"],
                mdd      = r["mdd"],
                tpm      = r["tpm"],
                n_trades = r["n_trades"],
            ))

        if sym_rows:
            print(f"\n  {sym} SUMMARY  ({from_year}-{to_year})")
            print(f"  {'Year':>5}  {'Ret':>8}  {'Sharpe':>7}  {'MDD':>7}  {'T/mo':>6}")
            print(f"  {'─'*40}")
            for row in sym_rows:
                print(f"  {row['year']:>5}  {row['ret']:>+8.1%}  "
                      f"{row['sharpe']:>7.2f}  {row['mdd']:>+7.1%}  {row['tpm']:>6.1f}")
            avg_ret    = sum(r["ret"]    for r in sym_rows) / len(sym_rows)
            avg_sharpe = sum(r["sharpe"] for r in sym_rows) / len(sym_rows)
            avg_mdd    = sum(r["mdd"]    for r in sym_rows) / len(sym_rows)
            n_pos      = sum(1 for r in sym_rows if r["ret"] > 0)
            print(f"  {'AVG':>5}  {avg_ret:>+8.1%}  {avg_sharpe:>7.2f}  {avg_mdd:>+7.1%}")
            print(f"  Positive years: {n_pos}/{len(sym_rows)}")
            all_rows.extend(sym_rows)

    if len(tickers) > 1 and all_rows:
        print(f"\n{'='*65}")
        print(f"  GRAND SUMMARY  ({len(tickers)} tickers x {from_year}-{to_year})")
        print(f"{'='*65}")
        print(f"  {'Sym':<8} {'AvgRet':>8} {'AvgSharpe':>10} {'AvgMDD':>8} {'Pos':>6}")
        print(f"  {'─'*44}")
        for sym in tickers:
            rows = [r for r in all_rows if r["symbol"] == sym]
            if not rows:
                continue
            avg_r = sum(r["ret"]    for r in rows) / len(rows)
            avg_s = sum(r["sharpe"] for r in rows) / len(rows)
            avg_m = sum(r["mdd"]    for r in rows) / len(rows)
            n_pos = sum(1 for r in rows if r["ret"] > 0)
            print(f"  {sym:<8} {avg_r:>+8.1%} {avg_s:>10.2f} {avg_m:>+8.1%} "
                  f"{n_pos:>3}/{len(rows)}")


# ------------------------------------------------------------------------------
# RANDOM UNIVERSE TEST
# ------------------------------------------------------------------------------
SP500_POOL = [
    "AAPL","MSFT","NVDA","AVGO","AMD","INTC","QCOM","TXN","MU","AMAT",
    "GOOGL","META","NFLX","DIS","T","VZ","CMCSA",
    "AMZN","TSLA","HD","MCD","NKE","SBUX","LOW","TGT","BKNG",
    "COST","PG","KO","PEP","WMT","MDLZ","CL",
    "LLY","JNJ","UNH","ABBV","MRK","PFE","TMO","AMGN","ISRG",
    "JPM","BAC","WFC","GS","MS","BLK","SCHW","AXP",
    "CAT","UPS","HON","RTX","DE","LMT","GE",
    "XOM","CVX","COP","SLB",
    "AMT","NEE","DUK",
    "IBM","F","GM",
    # WBA removed — delisted / no Yahoo data
]


def run_random_universe(n_stocks: int, n_trials: int,
                        from_year: int, to_year: int) -> None:
    import random
    rng = random.Random(0)
    years = list(range(from_year, to_year + 1))

    trial_traded_rets:    list[float] = []
    trial_traded_sharpes: list[float] = []
    trial_traded_pct:     list[float] = []
    trial_dca_rets:       list[float] = []   # DCA VOO over same traded stock-years

    print(f"\n{'='*65}")
    print(f"  RANDOM UNIVERSE TEST  ({n_stocks} stocks × {n_trials} trials, "
          f"{from_year}-{to_year})")
    print(f"{'='*65}")

    # Cache DCA VOO return per calendar year to avoid re-downloading each trial
    _dca_cache: dict[tuple[str, str], float] = {}

    def _dca_ret(ts: str, te: str) -> float:
        key = (ts, te)
        if key not in _dca_cache:
            eq = _bmark_dca_voo(ts, te)
            _dca_cache[key] = (float(eq.iloc[-1]) / INITIAL_CAPITAL - 1
                               if not eq.empty else 0.0)
        return _dca_cache[key]

    # Session-level blacklist: tickers that returned no data this run
    _no_data_tickers: set[str] = set()

    for trial in range(1, n_trials + 1):
        # Build a clean pool excluding known-bad tickers for this trial
        pool = [t for t in SP500_POOL if t not in _no_data_tickers]
        universe = rng.sample(pool, min(n_stocks, len(pool)))
        # Replacement reserve: pool members not already selected
        reserve  = [t for t in pool if t not in universe]
        traded_rets:    list[float] = []
        traded_sharpes: list[float] = []
        dca_rets:       list[float] = []
        n_total = 0
        n_traded = 0

        for sym in universe:
            for year in years:
                ts  = f"{year}-01-01"
                cur = datetime.now().year
                te  = (datetime.now().strftime("%Y-%m-%d")
                       if year == cur else f"{year}-12-31")
                r = run_backtest(sym, ts, te, verbose=False)
                if not r:
                    # No data returned — blacklist and try one substitute
                    _no_data_tickers.add(sym)
                    if reserve:
                        sub = reserve.pop(0)
                        r = run_backtest(sub, ts, te, verbose=False)
                        if not r:
                            _no_data_tickers.add(sub)
                            continue
                    else:
                        continue
                n_total += 1
                had_trade = r["n_trades"] > 0
                if had_trade:
                    n_traded += 1
                    traded_rets.append(r["total_ret"])
                    traded_sharpes.append(r["sharpe"])
                    dca_rets.append(_dca_ret(ts, te))

        if n_total == 0:
            continue

        trade_pct = n_traded / n_total
        avg_r   = float(np.mean(traded_rets))    if traded_rets    else 0.0
        avg_s   = float(np.mean(traded_sharpes)) if traded_sharpes else 0.0
        avg_dca = float(np.mean(dca_rets))        if dca_rets       else 0.0

        trial_traded_rets.append(avg_r)
        trial_traded_sharpes.append(avg_s)
        trial_traded_pct.append(trade_pct)
        trial_dca_rets.append(avg_dca)

        if trial % 5 == 0 or trial == 1:
            beat = "▲" if avg_r > avg_dca else "▼"
            print(f"  Trial {trial:>3}/{n_trials}  ret(traded)={avg_r:+.1%}  "
                  f"dca-voo={avg_dca:+.1%} {beat}  "
                  f"sharpe(traded)={avg_s:.2f}  traded={trade_pct:.0%}  "
                  f"universe={universe[:4]}…")

    if not trial_traded_rets:
        print("  No results.")
        return

    mean_ret    = float(np.mean(trial_traded_rets))
    std_ret     = float(np.std(trial_traded_rets))
    mean_sharpe = float(np.mean(trial_traded_sharpes))
    std_sharpe  = float(np.std(trial_traded_sharpes))
    mean_trade_pct = float(np.mean(trial_traded_pct))
    mean_dca    = float(np.mean(trial_dca_rets))
    pct_pos_trials   = float(np.mean([r > 0 for r in trial_traded_rets]))
    pct_beats_dca    = float(np.mean([r > d for r, d in
                                      zip(trial_traded_rets, trial_dca_rets)]))

    print(f"\n  {'─'*58}")
    print(f"  RANDOM UNIVERSE SUMMARY ({n_trials} trials, traded stock-years only)")
    print(f"  {'─'*58}")
    print(f"  Stocks that triggered ≥1 trade:  {mean_trade_pct:.0%} of drawn stock-years")
    print(f"  Avg return  (strategy):  {mean_ret:+.1%}  ± {std_ret:.1%}")
    print(f"  Avg return  (DCA VOO  ):  {mean_dca:+.1%}  "
          f"{'← strategy WINS ✓' if mean_ret > mean_dca else '← strategy LOSES ✗'}")
    print(f"  Trials beating DCA VOO:  {pct_beats_dca:.0%}")
    print(f"  Avg Sharpe  (traded):  {mean_sharpe:.2f}  ± {std_sharpe:.2f}")
    print(f"  Trials with positive avg return: {pct_pos_trials:.0%}")
    pass_sharpe = mean_sharpe > 0.5
    print(f"  PASS (Sharpe > 0.5 on traded stocks): {'YES ✓' if pass_sharpe else 'NO ✗'}")


# ------------------------------------------------------------------------------
# LIVE INDICATOR HELPER  (shared by run_signal and run_portfolio)
# ------------------------------------------------------------------------------
def _get_live_indicators(symbol: str) -> dict | None:
    """
    Download latest data for a symbol and compute all v2.9 indicators.
    Returns a dict of values needed for entry/exit decisions, or None on error.
    Called once per ticker in both --signal and --portfolio modes.

    Alignment with run_backtest:
    - Normal gate, fast gate (20d-high breakout), RS, 2-bar SMA200 filter,
      confidence threshold — all fully mirrored here.
    - KNOWN GAP: crash-recovery path.  Backtest maintains a stateful
      crash_timer (10 bars of relaxed re-entry after a crash exit).
      This function is stateless between calls, so it cannot know whether
      a crash exit occurred recently.  If crash_conditions_met is True,
      watch for a V-bounce manually (SMA200 reclaim + VIX < 25).
    """
    today        = datetime.now().strftime("%Y-%m-%d")
    warmup_start = (datetime.now() - pd.DateOffset(days=400)).strftime("%Y-%m-%d")

    raw = cached_download(symbol, warmup_start, today)
    if raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()

    vix_raw = cached_download("^VIX", warmup_start, today)
    if isinstance(vix_raw.columns, pd.MultiIndex):
        vix_raw.columns = vix_raw.columns.get_level_values(0)
    df["vix"] = (vix_raw["Close"].reindex(df.index).ffill().bfill()
                 if not vix_raw.empty else 20.0)
    vix_next      = df["vix"].shift(VIX_TERM_SHIFT).bfill()
    df["vix_term"] = (df["vix"] / vix_next.replace(0, np.nan)).fillna(1.0)

    spy_raw = cached_download("SPY", warmup_start, today)
    if not spy_raw.empty:
        if isinstance(spy_raw.columns, pd.MultiIndex):
            spy_raw.columns = spy_raw.columns.get_level_values(0)
        spy_close = spy_raw["Close"].reindex(df.index).ffill().bfill()
        df["spy"]        = spy_close
        df["spy_sma200"] = spy_close.rolling(SPY_SMA_WINDOW).mean()
    else:
        df["spy"] = df["spy_sma200"] = np.nan

    df["sma200"] = df["Close"].rolling(SMA_WINDOW).mean()
    df["sma50"]  = df["Close"].rolling(SMA50_WINDOW).mean()
    df["sma20"]  = df["Close"].rolling(SMA20_WINDOW).mean()
    df["low90"]  = df["Close"].rolling(PEAK_DD_WINDOW).min()
    df["high20"] = df["Close"].rolling(BREAKOUT_WINDOW).max().shift(1)

    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(ATR_WINDOW).mean()

    df["stock_ret20"] = df["Close"].pct_change(RS_RET_WINDOW)
    df["spy_ret20"]   = (df["spy"].pct_change(RS_RET_WINDOW)
                         if "spy" in df.columns else 0.0)

    # ── ADX(14) ──────────────────────────────────────────────────────────────
    # True Range (reuse tr from above — but tr was a local var, recompute here)
    pc = df["Close"].shift(1)
    tr_s = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - pc).abs(),
        (df["Low"]  - pc).abs(),
    ], axis=1).max(axis=1)
    up_move   = df["High"] - df["High"].shift(1)
    down_move = df["Low"].shift(1) - df["Low"]
    plus_dm   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    _period = ATR_WINDOW  # 14
    atr_s   = tr_s.ewm(alpha=1/_period, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm,  index=df.index).ewm(alpha=1/_period, adjust=False).mean() / atr_s.replace(0, np.nan)
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/_period, adjust=False).mean() / atr_s.replace(0, np.nan)
    dx  = (100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).fillna(0)
    df["adx14"] = dx.ewm(alpha=1/_period, adjust=False).mean()

    df.dropna(subset=["sma200", "sma50", "sma20", "low90",
                       "atr14", "vix", "vix_term",
                       "stock_ret20", "spy_ret20"], inplace=True)
    if df.empty:
        return None

    cur   = df.iloc[-1]
    close = float(cur["Close"])
    atr   = float(cur["atr14"])
    vix   = float(cur["vix"])
    date  = df.index[-1].strftime("%Y-%m-%d")

    sma200 = float(cur["sma200"])
    sma50  = float(cur["sma50"])
    sma20  = float(cur["sma20"])
    low90  = float(cur["low90"])
    cur_low = float(cur["Low"])

    high20       = float(cur["high20"]) if not np.isnan(float(cur.get("high20", np.nan))) else 0.0
    above_sma200 = close > sma200
    above_sma50  = close > sma50
    above_sma20  = close > float(cur["sma20"])
    breakout_ok  = high20 > 0 and close > high20
    fast_ok      = above_sma200 and breakout_ok    # 20-day high breakout
    normal_ok    = above_sma200 and above_sma50

    stock_ret20 = float(cur["stock_ret20"])
    spy_ret20   = float(cur["spy_ret20"])
    rs_vs_spy   = stock_ret20 - spy_ret20
    rs_ok       = rs_vs_spy >= RS_GATE_THRESHOLD
    vol_ok      = (atr / close) >= MIN_VOL_TO_ENTER if close > 0 else False

    spy_val = float(cur.get("spy",        np.nan))
    spy_sma = float(cur.get("spy_sma200", np.nan))
    bull_regime = (
        not np.isnan(spy_val) and not np.isnan(spy_sma)
        and spy_val > spy_sma and vix < VIX_BULL_LEVEL
    )
    mult     = STOP_ATR_MULT_BULL if bull_regime else STOP_ATR_MULT
    hi_vol   = (atr / close) > ATR_VOL_THRESHOLD if close > 0 else False
    max_stop = MAX_TRAIL_STOP_HIVOL if hi_vol else MAX_TRAIL_STOP
    atr_frac = float(np.clip(mult * atr / close, MIN_TRAIL_STOP, max_stop))

    # Crash condition counters (last few bars)
    tail = df.tail(max(CRASH_BELOW_SMA_DAYS + 1, CRASH_TERM_DAYS + 1))
    consec_bear = 0
    for c in tail["Close"].values[::-1]:
        if c < float(tail["sma200"].iloc[-1]):
            consec_bear += 1
        else:
            break
    consec_term = 0
    for v in tail["vix_term"].values[::-1]:
        if v > CRASH_TERM_RATIO:
            consec_term += 1
        else:
            break
    crash_conditions_met = (
        consec_bear >= CRASH_BELOW_SMA_DAYS
        and vix > CRASH_VIX_LEVEL
        and consec_term >= CRASH_TERM_DAYS
    )

    # Determine entry verdict
    # 2-bar SMA200 confirmation: last two closes must be above SMA200
    two_bar_live = (
        len(df) >= 2
        and float(df["Close"].iloc[-1]) > float(df["sma200"].iloc[-1])
        and float(df["Close"].iloc[-2]) > float(df["sma200"].iloc[-2])
    )
    adx14       = float(df["adx14"].iloc[-1]) if "adx14" in df.columns else 0.0
    sma200_dist = (close / sma200 - 1.0) if sma200 > 0 else 0.0
    score       = _bar_confidence(rs_vs_spy, adx14, sma200_dist)
    conf_ok     = score >= MIN_BUY_CONFIDENCE

    if normal_ok and rs_ok and vol_ok and two_bar_live and conf_ok:
        entry_gate   = "normal-gate"
        entry_ok     = True
        entry_reason = "BUY — normal gate (SMA200×2 + SMA50 + RS + confidence)"
    elif above_sma200 and fast_ok and rs_ok and vol_ok and two_bar_live and conf_ok:
        entry_gate   = "fast-gate"
        entry_ok     = True
        entry_reason = "BUY — fast gate (SMA200×2 + 20d-high breakout + RS + confidence)"
    else:
        entry_gate   = None
        entry_ok     = False
        reasons = []
        if not above_sma200:
            reasons.append("below SMA200")
        elif not (normal_ok or fast_ok):
            reasons.append("below SMA50 & no 20d-high breakout")
        if not two_bar_live:
            reasons.append("<2 consecutive closes above SMA200")
        if not rs_ok:
            reasons.append(f"RS lag {rs_vs_spy:+.1%} < -5%")
        if not vol_ok:
            reasons.append("low vol")
        if not conf_ok:
            reasons.append(f"confidence {score:.2f} < {MIN_BUY_CONFIDENCE:.2f}")
        entry_reason = "NO ENTRY — " + ", ".join(reasons)
    rebound = (close - float(cur["low90"])) / float(cur["low90"]) if float(cur["low90"]) > 0 else 0.0

    return dict(
        symbol          = symbol,
        date            = date,
        close           = close,
        cur_low         = cur_low,
        atr             = atr,
        vix             = vix,
        sma200          = sma200,
        sma50           = sma50,
        sma20           = sma20,
        low90           = low90,
        rebound         = rebound,
        above_sma200    = above_sma200,
        above_sma50     = above_sma50,
        above_sma20     = above_sma20,
        fast_ok         = fast_ok,
        normal_ok       = normal_ok,
        rs_vs_spy       = rs_vs_spy,
        rs_ok           = rs_ok,
        vol_ok          = vol_ok,
        bull_regime     = bull_regime,
        mult            = mult,
        hi_vol          = hi_vol,
        atr_frac        = atr_frac,
        adx14           = adx14,
        sma200_dist     = sma200_dist,
        score           = score,
        entry_ok        = entry_ok,
        entry_gate      = entry_gate,
        entry_reason    = entry_reason,
        crash_conditions_met = crash_conditions_met,
        consec_bear     = consec_bear,
        consec_term     = consec_term,
        df              = df,      # full frame, used by position-mode peak detection
    )


# ------------------------------------------------------------------------------
# LIVE SIGNAL (--signal mode)
# ------------------------------------------------------------------------------
def run_signal(symbol: str, entry_price: float | None = None) -> None:
    ind = _get_live_indicators(symbol)
    if ind is None:
        print(f"  [{symbol}] No data or insufficient history.")
        return

    close        = ind["close"]
    atr          = ind["atr"]
    vix          = ind["vix"]
    date         = ind["date"]
    atr_frac     = ind["atr_frac"]
    mult         = ind["mult"]
    bull_regime  = ind["bull_regime"]
    hi_vol       = ind["hi_vol"]
    entry_ok     = ind["entry_ok"]
    entry_reason = ind["entry_reason"]
    crash_conditions_met = ind["crash_conditions_met"]
    consec_bear  = ind["consec_bear"]
    consec_term  = ind["consec_term"]
    rs_vs_spy    = ind["rs_vs_spy"]
    rs_ok        = ind["rs_ok"]
    above_sma200 = ind["above_sma200"]
    above_sma50  = ind["above_sma50"]
    above_sma20  = ind["above_sma20"]
    df           = ind["df"]

    stop_price         = close * (1.0 - atr_frac)
    take_profit_price  = close * (1.0 + 2 * atr / close)

    crash_line = (
        f"CRASH EXIT TRIGGERED  (bear={consec_bear}d, VIX={vix:.0f}, term={consec_term}d)"
        if crash_conditions_met else
        f"No crash signal  (bear={consec_bear}/{CRASH_BELOW_SMA_DAYS}d, "
        f"VIX={vix:.0f}/{CRASH_VIX_LEVEL:.0f}, term={consec_term}/{CRASH_TERM_DAYS}d)"
    )

    W = 58
    print(f"\n{'='*W}")
    print(f"  LIVE SIGNAL  {symbol}  as of {date}")
    print(f"{'='*W}")
    print(f"  Price:   ${close:>10.2f}   VIX: {vix:.1f}")
    print(f"  SMA200:  ${ind['sma200']:>10.2f}   {'ABOVE ✓' if above_sma200 else 'BELOW ✗'}")
    print(f"  SMA50:   ${ind['sma50']:>10.2f}   {'ABOVE ✓' if above_sma50  else 'BELOW ✗'}")
    high20_live = float(ind["df"]["high20"].iloc[-1]) if "high20" in ind["df"].columns else 0.0
    breakout_live = high20_live > 0 and close > high20_live
    print(f"  20d-hi:  ${high20_live:>10.2f}   {'BREAKOUT ✓' if breakout_live else 'below ✗'}")
    print(f"  ATR14:   ${atr:>10.2f}   ({atr/close:.1%} of price)  "
          f"{'HI-VOL' if hi_vol else 'normal'}")
    print(f"  ADX14:   {ind['adx14']:>11.1f}   "
          f"{'strong trend ✓' if ind['adx14'] >= 25 else 'weak trend'}")
    print(f"  RS/SPY (20d): {rs_vs_spy:+.1%}  {'OK ✓' if rs_ok else 'LAGGING ✗'}")
    print(f"  Confidence:  {ind['score']:.2f}  "
          f"({'≥' if ind['score'] >= MIN_BUY_CONFIDENCE else '<'}{MIN_BUY_CONFIDENCE:.2f} threshold  "
          f"{'OK ✓' if ind['score'] >= MIN_BUY_CONFIDENCE else 'BELOW ✗'})")
    print(f"  Bull regime: {'YES (SPY>SMA200 & VIX<15)' if bull_regime else 'no'}")
    print(f"{'─'*W}")

    if entry_price is not None and entry_price > 0:
        # ── Position management mode ─────────────────────────────────────
        closes = df["Close"]
        at_or_below = closes[closes <= entry_price * 1.005]
        if not at_or_below.empty:
            entry_idx = closes.index.get_loc(at_or_below.index[-1])
            pos_peak  = max(entry_price, float(closes.iloc[entry_idx:].max()))
            peak_note = "from price history"
        else:
            pos_peak  = max(entry_price, close)
            peak_note = "estimated"
        if pos_peak < entry_price:
            pos_peak = entry_price

        personal_stop = pos_peak * (1.0 - atr_frac)
        pnl_pct       = (close - entry_price) / entry_price
        dist_to_stop  = (close - personal_stop) / close
        take_profit   = entry_price * (1.0 + 2 * atr / close)

        print(f"  ENTRY:  N/A  (already holding at ${entry_price:.2f})")
        print(f"  EXIT:   {crash_line}")
        print()
        print(f"  ── POSITION SUMMARY ──────────────────────────────────")
        print(f"  Entry price:    ${entry_price:>10.2f}")
        print(f"  Current price:  ${close:>10.2f}   P&L: {pnl_pct:+.1%}")
        print(f"  Position peak:  ${pos_peak:>10.2f}  ({peak_note})")
        print(f"  Trail stop:     ${personal_stop:>10.2f}  "
              f"({atr_frac:.1%} below peak, {mult:.0f}×ATR, "
              f"{'bull' if bull_regime else 'normal'} regime)")
        print(f"  Distance to stop: {dist_to_stop:.1%}  of current price")
        print(f"  Take profit:    ${take_profit:>10.2f}  (entry + 2×ATR, "
              f"{(take_profit/entry_price-1):+.1%} from entry)")
        print()

        if entry_ok:
            trend_verdict = "TREND OK ✓  — entry conditions still met today"
        else:
            trend_verdict = f"TREND WEAK ✗  — {entry_reason.replace('NO ENTRY — ', '')}"

        profitable   = pnl_pct > 0.05
        cushion_ok   = dist_to_stop > 0.08
        if entry_ok and profitable and cushion_ok:
            pyramid_line = ("ADD (pyramid) ✓  — trend intact, profitable, "
                            f"{dist_to_stop:.0%} cushion to stop")
        elif entry_ok and not profitable:
            pyramid_line = "DO NOT ADD  — conditions met but position is underwater"
        elif entry_ok:
            pyramid_line = "DO NOT ADD  — stop is too close (< 8% cushion)"
        else:
            pyramid_line = "DO NOT ADD  — trend conditions no longer met"

        print(f"  Trend health:   {trend_verdict}")
        print(f"  Pyramid advice: {pyramid_line}")
        print()
        print(f"  ── SUGGESTED ORDERS ──────────────────────────────────")
        print(f"  Protect:  Sell-stop order at ${personal_stop:.2f}")
        print(f"  Target:   Limit sell order at ${take_profit:.2f} (take profit)")
        print()

        if crash_conditions_met:
            print(f"  ACTION:  *** SELL NOW — crash exit triggered ***")
        elif close <= personal_stop:
            print(f"  ACTION:  *** SELL NOW — trailing stop hit on close ***")
        elif ind["cur_low"] <= personal_stop:
            print(f"  ACTION:  *** SELL — today's low breached trail-stop (fill ~${personal_stop:.2f}) ***")
        else:
            print(f"  ACTION:  HOLD  "
                  f"(stop at ${personal_stop:.2f}, "
                  f"{dist_to_stop:.1%} cushion remaining)")
    else:
        # ── Entry signal mode ─────────────────────────────────────────────
        print(f"  ENTRY:  {entry_reason}")
        print(f"  EXIT:   {crash_line}")
        print(f"  TRAIL STOP (if entering at today's close):  "
              f"${stop_price:.2f}  ({atr_frac:.1%} below close, {mult:.0f}×ATR, "
              f"{'bull' if bull_regime else 'normal'} regime)")
        print(f"  TAKE PROFIT (2×ATR above close):  ${take_profit_price:.2f}  "
              f"({(take_profit_price/close-1):+.1%} from close)")
        print()
        print(f"  ── SUGGESTED ORDERS (execute at tomorrow's open) ─────")
        if entry_ok:
            print(f"  Enter:   Market buy at open tomorrow")
            print(f"  Protect: Sell-stop at ${stop_price:.2f} (place immediately after fill)")
            print(f"  Target:  Limit sell at ${take_profit_price:.2f} (optional take profit)")
        else:
            # Describe what would actually unlock entry
            high20_val  = float(ind["df"]["high20"].iloc[-1])  if "high20" in ind["df"].columns else 0.0
            sma50_val   = ind["sma50"]
            sma200_val  = ind["sma200"]
            rs_gap      = RS_GATE_THRESHOLD - ind["rs_vs_spy"]
            conf_gap    = MIN_BUY_CONFIDENCE - ind["score"]
            hints: list[str] = []
            if not ind["above_sma200"]:
                hints.append(f"close > SMA200 ${sma200_val:.2f}")
            elif not (ind["normal_ok"] or ind["fast_ok"]):
                hints.append(f"close > SMA50 ${sma50_val:.2f}  OR  "
                             f"close > 20d-high ${high20_val:.2f}")
            if rs_gap > 0:
                hints.append(f"RS vs SPY needs +{rs_gap:.1%} improvement")
            if conf_gap > 0:
                hints.append(f"confidence needs +{conf_gap:.2f} (now {ind['score']:.2f})")
            if not hints:
                hints.append("2 consecutive closes above SMA200")
            print(f"  No entry today. To trigger:")
            for h in hints:
                print(f"    • {h}")
            if ind.get("crash_conditions_met"):
                print(f"  ⚠  Crash exit just triggered — backtest would open a "
                      f"10-bar crash-recovery window (relaxed re-entry: SMA200 + "
                      f"VIX < {CRASH_VIX_LEVEL}). Live mode is stateless and "
                      f"cannot track this; monitor manually.")

    print(f"{'='*W}\n")


# ------------------------------------------------------------------------------
# PORTFOLIO DASHBOARD  (--portfolio mode)
# ------------------------------------------------------------------------------
def _load_holdings(csv_path: str) -> list[dict]:
    """
    Load holdings from CSV or JSON.
    JSON format: list of {ticker, entry_price, shares, notes?}
    CSV format:  ticker,entry_price,shares[,notes]
    """
    holdings = []
    if not os.path.exists(csv_path):
        # Try sibling .json if a .csv path was given (and vice-versa)
        alt = csv_path.rsplit(".", 1)[0] + (".json" if csv_path.endswith(".csv") else ".csv")
        if os.path.exists(alt):
            return _load_holdings(alt)
        print(f"  [portfolio] Holdings file not found: {csv_path}")
        print(f"  Create holdings.json in {os.path.dirname(csv_path)}")
        return holdings

    # ── JSON ──────────────────────────────────────────────────────────────
    if csv_path.endswith(".json"):
        import json as _json
        with open(csv_path, encoding="utf-8") as f:
            rows = _json.load(f)
        for row in rows:
            ticker      = str(row.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            try:
                entry_price = float(row.get("entry_price", 0))
                shares      = float(row.get("shares", 0))
            except (ValueError, TypeError):
                continue
            if entry_price <= 0 or shares <= 0:
                continue
            holdings.append(dict(
                ticker      = ticker,
                entry_price = entry_price,
                shares      = shares,
                notes       = str(row.get("notes", "")).strip(),
            ))
        return holdings

    # ── CSV ───────────────────────────────────────────────────────────────
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        # Strip comment lines (start with #) and blank lines before feeding to DictReader
        lines = [ln for ln in f if ln.strip() and not ln.strip().startswith("#")]
    reader = csv.DictReader(lines)
    for row in reader:
            ticker = row.get("ticker", "").strip().upper()
            if not ticker or ticker.startswith("#"):
                continue
            try:
                entry_price = float(row.get("entry_price", 0))
                shares      = float(row.get("shares", 0))
            except ValueError:
                continue
            if entry_price <= 0 or shares <= 0:
                continue
            holdings.append(dict(
                ticker      = ticker,
                entry_price = entry_price,
                shares      = shares,
                notes       = row.get("notes", "").strip(),
            ))
    return holdings



def run_portfolio(holdings_csv: str,
                  watchlist: list[str],
                  total_capital: float | None,
                  max_positions: int = 10,
                  risk_per_trade_pct: float = 2.0,
                  min_cushion_for_pyramid: float = 0.08,
                  min_profit_for_pyramid: float = 0.05,
                  cash_reserve_pct: float = 0.10,
                  top_signals: int = 5,
                  min_buy_confidence: float = MIN_BUY_CONFIDENCE,
                  min_pyramid_confidence: float = MIN_PYRAMID_CONFIDENCE,
                  warn_hold_confidence: float = WARN_HOLD_CONFIDENCE) -> None:
    """
    Risk-aware portfolio dashboard.

    Phases:
      1. Risk-based position sizing  — each new trade risks exactly
         risk_per_trade_pct % of portfolio.
      2. Composite signal ranking    — RS vs SPY (40%) + ADX (40%)
                                       + SMA200-dist (20%).
      3. Pyramid opportunities       — profitable held positions on valid
                                       entry conditions get priority slots.
      4. Risk monitoring             — per-position risk %, total portfolio
                                       risk, and trim / add suggestions.
    """
    W = 72

    # ── Load holdings ────────────────────────────────────────────────────────
    holdings = _load_holdings(holdings_csv)
    held_tickers = {h["ticker"] for h in holdings}

    # ── All tickers to fetch (held + watchlist, deduped) ────────────────────
    all_tickers: list[str] = []
    seen: set[str] = set()
    for t in list(held_tickers) + [w.upper() for w in watchlist]:
        if t not in seen:
            all_tickers.append(t)
            seen.add(t)

    print(f"\n  Fetching indicators for {len(all_tickers)} ticker(s)…", flush=True)
    indicators: dict[str, dict | None] = {}
    for t in all_tickers:
        print(f"    {t}…", end=" ", flush=True)
        indicators[t] = _get_live_indicators(t)
        status = indicators[t]["date"] if indicators[t] else "ERROR"
        print(status)

    # ── Portfolio value ──────────────────────────────────────────────────────
    holdings_value = 0.0
    for h in holdings:
        ind = indicators.get(h["ticker"])
        if ind:
            holdings_value += h["shares"] * ind["close"]

    if total_capital is not None:
        portfolio_value = total_capital
        cash            = max(total_capital - holdings_value, 0.0)
    else:
        portfolio_value = holdings_value / 0.80 if holdings_value > 0 else INITIAL_CAPITAL
        cash            = portfolio_value - holdings_value

    risk_dollars = portfolio_value * (risk_per_trade_pct / 100.0)
    cash_reserve = portfolio_value * cash_reserve_pct
    date_str     = datetime.now().strftime("%Y-%m-%d")

    print(f"\n{'='*W}")
    print(f"  PORTFOLIO DASHBOARD   {date_str}")
    print(f"  Capital: ${portfolio_value:>12,.2f}   Max positions: {max_positions}   "
          f"Risk/trade: {risk_per_trade_pct:.1f}%  (${risk_dollars:,.0f})")
    print(f"  Holdings: ${holdings_value:>11,.2f}   Cash: ${cash:>11,.2f}  "
          f"({cash/portfolio_value:.0%})   Reserve: ${cash_reserve:,.0f} ({cash_reserve_pct:.0%})")
    print(f"{'='*W}")

    # ── Helper: risk-based share count ──────────────────────────────────────
    def _risk_shares(close: float, atr_frac: float) -> tuple[int, float, float]:
        """Returns (shares, stop_px, tp_px) using risk-per-trade budget."""
        stop_dist = close * atr_frac        # dollar distance to stop
        if stop_dist < 0.001:
            return 0, 0.0, 0.0
        shares   = int(risk_dollars / stop_dist)
        stop_px  = close * (1.0 - atr_frac)
        tp_px    = close + 2.0 * (close * atr_frac)   # 2× risk as target
        return shares, stop_px, tp_px

    # ── A: HELD POSITIONS ────────────────────────────────────────────────────
    held_rows:     list[dict] = []
    pyramid_cands: list[dict] = []   # positions eligible for pyramiding

    print(f"\n  {'─'*W}")
    print(f"  HELD POSITIONS  ({len(holdings)} stocks)")
    print(f"  {'─'*W}")
    print(f"  {'Ticker':<7} {'Entry':>9} {'Now':>9} {'P&L':>7} "
          f"{'Peak':>9} {'Stop':>9} {'Cushion':>8}  Action")
    print(f"  {'─'*W}")

    for h in holdings:
        ticker      = h["ticker"]
        entry_price = h["entry_price"]
        shares_held = h["shares"]
        ind = indicators.get(ticker)

        if ind is None:
            print(f"  {ticker:<7}  ERROR fetching data")
            continue

        close    = ind["close"]
        atr_frac = ind["atr_frac"]

        # Peak detection from price history
        df     = ind["df"]
        closes = df["Close"]
        at_or_below = closes[closes <= entry_price * 1.005]
        if not at_or_below.empty:
            entry_idx = closes.index.get_loc(at_or_below.index[-1])
            pos_peak  = max(entry_price, float(closes.iloc[entry_idx:].max()))
        else:
            pos_peak  = max(entry_price, close)
        if pos_peak < entry_price:
            pos_peak = entry_price

        personal_stop = pos_peak * (1.0 - atr_frac)
        pnl_pct       = (close - entry_price) / entry_price
        dist_to_stop  = (close - personal_stop) / close
        mkt_value     = shares_held * close
        pnl_dollars   = shares_held * (close - entry_price)

        entry_ok   = ind["entry_ok"]
        profitable = pnl_pct >= min_profit_for_pyramid
        cushion_ok = dist_to_stop >= min_cushion_for_pyramid

        # Exit / hold / pyramid classification
        if ind["crash_conditions_met"]:
            action      = "SELL *** CRASH EXIT ***"
            action_flag = "SELL"
        elif close <= personal_stop:
            action      = "SELL *** STOP HIT (close) ***"
            action_flag = "SELL"
        elif ind["cur_low"] <= personal_stop:
            action      = "SELL AT OPEN *** LOW breached stop ***"
            action_flag = "SELL"
        elif entry_ok and profitable and cushion_ok:
            action      = f"HOLD  [PYRAMID OK — {dist_to_stop:.0%} cushion]"
            action_flag = "HOLD+"
        elif not ind["above_sma200"]:
            action      = "HOLD  [trend weak — below SMA200]"
            action_flag = "HOLD~"
        else:
            action      = f"HOLD  [{dist_to_stop:.0%} cushion]"
            action_flag = "HOLD"

        # Append confidence warning for weakening trends (below warn threshold)
        if action_flag in ("HOLD", "HOLD+", "HOLD~"):
            conf_score = ind["score"]
            if conf_score < warn_hold_confidence:
                action += f"  ⚠️ confidence {conf_score:.2f} — trend weakening"

        print(f"  {ticker:<7} ${entry_price:>8.2f} ${close:>8.2f} "
              f"{pnl_pct:>+6.1%} ${pos_peak:>8.2f} ${personal_stop:>8.2f} "
              f"{dist_to_stop:>7.1%}  {action}")

        row = dict(
            ticker        = ticker,
            entry_price   = entry_price,
            shares        = shares_held,
            current_price = round(close, 2),
            pnl_pct       = round(pnl_pct, 4),
            pnl_dollars   = round(pnl_dollars, 2),
            mkt_value     = round(mkt_value, 2),
            pos_peak      = round(pos_peak, 2),
            stop_price    = round(personal_stop, 2),
            cushion       = round(dist_to_stop, 4),
            action_flag   = action_flag,
            action        = action,
            entry_gate    = "(held)",
            score         = round(ind["score"], 3),
            adx14         = round(ind["adx14"], 1),
            rs_vs_spy     = round(ind["rs_vs_spy"], 4),
        )
        held_rows.append(row)

        if action_flag == "HOLD+":
            pyramid_cands.append(dict(ind=ind, row=row, shares_held=shares_held,
                                      personal_stop=personal_stop, pos_peak=pos_peak))

    # ── B + C: WATCHLIST SCREENING ───────────────────────────────────────────
    new_signals: list[dict] = []
    no_signals:  list[dict] = []

    for ticker in watchlist:
        ticker_u = ticker.upper()
        if ticker_u in held_tickers:
            continue
        ind = indicators.get(ticker_u)
        if ind is None:
            no_signals.append(dict(ticker=ticker_u, reason="data error"))
            continue
        if ind["entry_ok"]:
            new_signals.append(ind)
        else:
            no_signals.append(dict(ticker=ticker_u,
                                   reason=ind["entry_reason"].replace("NO ENTRY — ", "")))

    # Sort both lists by composite score descending, then apply confidence threshold
    pyramid_cands.sort(key=lambda x: x["ind"]["score"], reverse=True)
    new_signals.sort(key=lambda x: x["score"], reverse=True)

    # Filter: only recommend signals that meet the minimum confidence threshold
    all_pyramids   = pyramid_cands
    all_new        = new_signals
    pyramid_cands  = [p for p in all_pyramids if p["ind"]["score"] >= min_pyramid_confidence]
    new_signals    = [s for s in all_new       if s["score"]        >= min_buy_confidence]

    n_held_slots    = sum(1 for r in held_rows if r["action_flag"] != "SELL")
    slots_remaining = max(max_positions - n_held_slots, 0)
    cash_deployable = max(cash - cash_reserve, 0.0)
    low_cash        = cash < cash_reserve

    # ── Phase 3: PYRAMID OPPORTUNITIES (priority over new entries) ───────────
    print(f"\n  {'─'*W}")
    if pyramid_cands:
        print(f"  PYRAMID OPPORTUNITIES  ({len(pyramid_cands)} eligible, "
              f"confidence ≥ {min_pyramid_confidence:.0%},  "
              f"{slots_remaining} slot(s) available)")
    if pyramid_cands:
        print(f"  {'─'*W}")
        print(f"  {'#':<3} {'Ticker':<7} {'P&L':>7} {'Score':>6} {'ADX':>5} "
              f"{'RS/SPY':>7}  Suggested add")
        print(f"  {'─'*W}")
        for rank, pc in enumerate(pyramid_cands, 1):
            ind_p    = pc["ind"]
            add_sh, stop_px, tp_px = _risk_shares(ind_p["close"], ind_p["atr_frac"])
            add_cost = add_sh * ind_p["close"]
            pnl_str  = f"{pc['row']['pnl_pct']:+.1%}"
            feasible = add_sh > 0 and add_cost <= cash_deployable and not low_cash
            note = (f"ADD {add_sh} sh  (risk ${add_sh * ind_p['close'] * ind_p['atr_frac']:,.0f}  "
                    f"stop ${stop_px:.2f}  TP ${tp_px:.2f})"
                    if feasible else
                    "ADD — insufficient cash" if add_sh > 0 else "skip (stop too tight)")
            print(f"  {rank:<3} {ind_p['symbol']:<7} {pnl_str:>7} "
                  f"{ind_p['score']:>6.2f} {ind_p['adx14']:>5.0f} "
                  f"{ind_p['rs_vs_spy']:>+6.1%}  {note}")
            # Append to CSV rows
            if feasible:
                held_rows.append(dict(
                    ticker        = ind_p["symbol"],
                    entry_price   = None,
                    shares        = add_sh,
                    current_price = round(ind_p["close"], 2),
                    pnl_pct       = None,
                    pnl_dollars   = None,
                    mkt_value     = round(add_sh * ind_p["close"], 2),
                    pos_peak      = None,
                    stop_price    = round(stop_px, 2),
                    cushion       = None,
                    action_flag   = "PYRAMID",
                    action        = f"ADD {add_sh} sh (pyramid)",
                    entry_gate    = "pyramid",
                    score         = round(ind_p["score"], 3),
                    adx14         = round(ind_p["adx14"], 1),
                    rs_vs_spy     = round(ind_p["rs_vs_spy"], 4),
                ))
    else:
        if all_pyramids:
            best_p = all_pyramids[0]["ind"]
            print(f"  PYRAMID OPPORTUNITIES  (none above confidence {min_pyramid_confidence:.0%} — "
                  f"best: {best_p['symbol']} {best_p['score']:.2f})")
        else:
            print(f"  PYRAMID OPPORTUNITIES  (none — no profitable held positions meet criteria)")

    # ── Phase 2: RANKED NEW BUY SIGNALS ─────────────────────────────────────
    print(f"\n  {'─'*W}")
    if new_signals:
        shown = new_signals[:top_signals]
        print(f"  NEW BUY SIGNALS  ({len(new_signals)} found → showing top {len(shown)}, "
              f"confidence ≥ {min_buy_confidence:.0%},  "
              f"{slots_remaining} slot(s) available)")
        if low_cash:
            print(f"  ⚠  Cash below reserve (${cash:,.0f} < ${cash_reserve:,.0f}) — "
                  f"new entries paused")
        print(f"  {'─'*W}")
        print(f"  {'#':<3} {'Ticker':<7} {'Price':>9} {'Gate':<13} "
              f"{'Score':>6} {'ADX':>5} {'RS/SPY':>7}  Shares  Stop      TP")
        print(f"  {'─'*W}")
        for rank, ind_n in enumerate(shown, 1):
            ticker   = ind_n["symbol"]
            close    = ind_n["close"]
            atr_frac = ind_n["atr_frac"]
            gate     = ind_n["entry_gate"]
            sh, stop_px, tp_px = _risk_shares(close, atr_frac)
            cost    = sh * close
            feasible = sh > 0 and cost <= cash_deployable and not low_cash
            sh_str  = f"{sh}" if feasible else "—"
            print(f"  {rank:<3} {ticker:<7} ${close:>8.2f}  {gate:<13} "
                  f"{ind_n['score']:>6.2f} {ind_n['adx14']:>5.0f} "
                  f"{ind_n['rs_vs_spy']:>+6.1%}  {sh_str:>6}  "
                  f"${stop_px:>8.2f}  ${tp_px:>8.2f}")
            if feasible:
                held_rows.append(dict(
                    ticker        = ticker,
                    entry_price   = None,
                    shares        = sh,
                    current_price = round(close, 2),
                    pnl_pct       = None,
                    pnl_dollars   = None,
                    mkt_value     = round(sh * close, 2),
                    pos_peak      = None,
                    stop_price    = round(stop_px, 2),
                    cushion       = None,
                    action_flag   = "BUY",
                    action        = f"BUY {sh} sh",
                    entry_gate    = gate,
                    score         = round(ind_n["score"], 3),
                    adx14         = round(ind_n["adx14"], 1),
                    rs_vs_spy     = round(ind_n["rs_vs_spy"], 4),
                ))
        if len(new_signals) > top_signals:
            rest = [s["symbol"] for s in new_signals[top_signals:]]
            print(f"  … {len(rest)} more signals below top-{top_signals}: {', '.join(rest)}")
    elif all_new:
        # Stocks passed hard gates but none cleared the confidence threshold
        best_new = all_new[0]
        others   = [s["symbol"] for s in all_new[1:4]]
        print(f"  NEW BUY SIGNALS  (none above confidence {min_buy_confidence:.0%} — "
              f"best: {best_new['symbol']} {best_new['score']:.2f}"
              f"{', ' + ', '.join(others) if others else ''})")
        print(f"  → Wait for higher-confidence setups.")
    else:
        print(f"  NEW BUY SIGNALS  (none from watchlist)")

    # ── NO SIGNAL list ───────────────────────────────────────────────────────
    if no_signals:
        print()
        print(f"  ── NO SIGNAL ({len(no_signals)} stocks) ─────────────────────────────")
        for ns in no_signals:
            print(f"    {ns['ticker']:<7}  {ns.get('reason', '')}")

    # ── Phase 4: RISK MONITORING TABLE ──────────────────────────────────────
    print(f"\n  {'─'*W}")
    print(f"  POSITION RISK MONITOR  "
          f"(target: {risk_per_trade_pct:.1f}% per position = ${risk_dollars:,.0f})")
    print(f"  {'─'*W}")
    print(f"  {'Ticker':<7} {'Shares':>7} {'Price':>9} {'Stop':>9} "
          f"{'Risk$':>9} {'Risk%':>7} {'vs Target':>10}  Suggestion")
    print(f"  {'─'*W}")

    total_risk_pct  = 0.0
    risk_report_rows: list[dict] = []

    for h in holdings:
        ticker      = h["ticker"]
        shares_held = h["shares"]
        ind = indicators.get(ticker)
        if ind is None:
            continue
        # Find the stop we computed for this position
        row = next((r for r in held_rows if r["ticker"] == ticker
                    and r["action_flag"] not in ("BUY", "PYRAMID")), None)
        if row is None:
            continue
        close     = ind["close"]
        stop_px   = row["stop_price"]
        risk_per_sh = max(close - stop_px, 0.0)
        pos_risk_d  = shares_held * risk_per_sh
        pos_risk_pct = pos_risk_d / portfolio_value * 100 if portfolio_value > 0 else 0.0
        total_risk_pct += pos_risk_pct
        target_pct = risk_per_trade_pct

        if pos_risk_pct > target_pct * 1.5:
            # Position is too large — suggest trim
            excess_risk_d   = pos_risk_d - risk_dollars
            trim_shares     = int(excess_risk_d / risk_per_sh) if risk_per_sh > 0 else 0
            suggestion      = f"REDUCE by ~{trim_shares} sh"
        elif pos_risk_pct < target_pct * 0.5 and ind["entry_ok"] and row["action_flag"] != "SELL":
            # Position is too small and conditions still valid
            deficit_d   = risk_dollars - pos_risk_d
            add_shares  = int(deficit_d / risk_per_sh) if risk_per_sh > 0 else 0
            suggestion  = f"could ADD ~{add_shares} sh" if add_shares > 0 else "OK"
        else:
            suggestion = "OK"

        delta_str = f"{pos_risk_pct - target_pct:>+.1f}%"
        print(f"  {ticker:<7} {int(shares_held):>7} ${close:>8.2f} ${stop_px:>8.2f} "
              f"  ${pos_risk_d:>7,.0f} {pos_risk_pct:>6.1f}%  {delta_str:>10}  {suggestion}")

        risk_report_rows.append(dict(
            ticker       = ticker,
            shares       = int(shares_held),
            price        = round(close, 2),
            stop         = round(stop_px, 2),
            risk_dollars = round(pos_risk_d, 2),
            risk_pct     = round(pos_risk_pct, 3),
            target_pct   = target_pct,
            suggestion   = suggestion,
        ))

    print(f"  {'─'*W}")
    risk_color = "⚠ HIGH" if total_risk_pct > risk_per_trade_pct * max_positions * 0.8 else "OK"
    print(f"  Total portfolio risk: {total_risk_pct:.1f}%  "
          f"(target ≤ {risk_per_trade_pct * max_positions:.0f}%)  {risk_color}")

    # ── Allocation summary ───────────────────────────────────────────────────
    invested = sum(r["mkt_value"] for r in held_rows
                   if r["action_flag"] not in ("SELL", "BUY", "PYRAMID")
                   and r.get("mkt_value"))
    print(f"\n  {'─'*W}")
    print(f"  ALLOCATION SUMMARY")
    print(f"  {'─'*W}")
    print(f"  Holdings (active):   {n_held_slots} positions   ${invested:>12,.2f}  "
          f"({invested/portfolio_value:.1%})")
    print(f"  Pyramid signals:     {len(pyramid_cands)}")
    print(f"  New buy signals:     {len(new_signals)}  (showing top {min(top_signals, len(new_signals))})")
    print(f"  Cash available:      ${cash:>12,.2f}  ({cash/portfolio_value:.1%})   "
          f"Reserve: ${cash_reserve:,.0f}")
    print(f"{'='*W}\n")

    # ── Save reports ─────────────────────────────────────────────────────────
    os.makedirs(RESULTS_DIR, exist_ok=True)
    portfolio_path = os.path.join(RESULTS_DIR, f"portfolio_report_{date_str}.csv")
    risk_path      = os.path.join(RESULTS_DIR, f"risk_report_{date_str}.csv")
    signals_path   = os.path.join(RESULTS_DIR, f"signals_report_{date_str}.csv")
    if held_rows:
        pd.DataFrame(held_rows).to_csv(portfolio_path, index=False)
        print(f"  -> Portfolio report: {portfolio_path}")
    if risk_report_rows:
        pd.DataFrame(risk_report_rows).to_csv(risk_path, index=False)
        print(f"  -> Risk report:      {risk_path}")
    # Write ALL new buy signal candidates (passed + below-confidence) to CSV
    signal_rows = []
    for s in all_new:
        sh, stop_px, tp_px = _risk_shares(s["close"], s["atr_frac"])
        cost     = sh * s["close"]
        feasible = sh > 0 and cost <= cash_deployable and not low_cash
        signal_rows.append(dict(
            rank          = None,
            ticker        = s["symbol"],
            price         = round(s["close"], 2),
            gate          = s["entry_gate"],
            score         = round(s["score"], 3),
            above_min_conf= s["score"] >= min_buy_confidence,
            adx14         = round(s["adx14"], 1),
            rs_vs_spy     = round(s["rs_vs_spy"], 4),
            suggested_sh  = sh if feasible else 0,
            stop          = round(stop_px, 2),
            tp            = round(tp_px, 2),
            est_cost      = round(cost, 2),
            cash_ok       = feasible,
        ))
    for i, row in enumerate(signal_rows, 1):
        row["rank"] = i
    if signal_rows:
        pd.DataFrame(signal_rows).to_csv(signals_path, index=False)
        print(f"  -> Signals report:   {signals_path}  ({len(signal_rows)} candidates)")

    # ── Write portfolio cache for GUI ─────────────────────────────────────────
    import json as _json
    _held_gui = []
    for r in held_rows:
        if r.get("action_flag") in ("BUY", "PYRAMID"):
            continue   # those are appended for CSV only; GUI gets separate sections
        _held_gui.append(dict(
            ticker        = r["ticker"],
            entry_price   = r.get("entry_price"),
            shares        = r.get("shares"),
            current_price = r.get("current_price"),
            pnl_pct       = r.get("pnl_pct"),
            pnl_dollars   = r.get("pnl_dollars"),
            mkt_value     = r.get("mkt_value"),
            pos_peak      = r.get("pos_peak"),
            stop_price    = r.get("stop_price"),
            cushion       = r.get("cushion"),
            action_flag   = r.get("action_flag"),
            action        = r.get("action"),
            score         = r.get("score"),
            adx14         = r.get("adx14"),
            rs_vs_spy     = r.get("rs_vs_spy"),
            warn_conf     = (r.get("score", 1) < warn_hold_confidence
                             and r.get("action_flag") in ("HOLD", "HOLD+", "HOLD~")),
            error         = False,
        ))

    _pyramid_gui = []
    for pc in pyramid_cands:
        ind_p = pc["ind"]
        add_sh, stop_px, tp_px = _risk_shares(ind_p["close"], ind_p["atr_frac"])
        add_cost = add_sh * ind_p["close"]
        feasible = add_sh > 0 and add_cost <= cash_deployable and not low_cash
        _pyramid_gui.append(dict(
            ticker    = ind_p["symbol"],
            pnl_pct   = pc["row"]["pnl_pct"],
            score     = round(ind_p["score"], 3),
            adx14     = round(ind_p["adx14"], 1),
            rs_vs_spy = round(ind_p["rs_vs_spy"], 4),
            add_sh    = add_sh,
            stop_px   = round(stop_px, 2),
            tp_px     = round(tp_px, 2),
            add_cost  = round(add_cost, 2),
            feasible  = feasible,
        ))

    _signal_gui = []
    for ind_n in new_signals[:top_signals]:
        sh, stop_px, tp_px = _risk_shares(ind_n["close"], ind_n["atr_frac"])
        cost     = sh * ind_n["close"]
        feasible = sh > 0 and cost <= cash_deployable and not low_cash
        _signal_gui.append(dict(
            ticker    = ind_n["symbol"],
            price     = round(ind_n["close"], 2),
            gate      = ind_n["entry_gate"],
            score     = round(ind_n["score"], 3),
            adx14     = round(ind_n["adx14"], 1),
            rs_vs_spy = round(ind_n["rs_vs_spy"], 4),
            shares    = sh if feasible else None,
            stop      = round(stop_px, 2),
            tp        = round(tp_px, 2),
            feasible  = feasible,
        ))

    _risk_gui = []
    for r in risk_report_rows:
        _risk_gui.append(dict(**r, delta=round(r["risk_pct"] - r["target_pct"], 3)))

    _invested = sum(r.get("mkt_value") or 0 for r in _held_gui
                    if r.get("action_flag") not in ("SELL", "BUY", "PYRAMID"))

    _cache = dict(
        date                   = date_str,
        _cache_time            = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        portfolio_value        = round(portfolio_value, 2),
        holdings_value         = round(holdings_value, 2),
        cash                   = round(cash, 2),
        cash_pct               = round(cash / portfolio_value, 4) if portfolio_value else 0,
        cash_reserve           = round(cash_reserve, 2),
        reserve_pct            = cash_reserve_pct,
        max_positions          = max_positions,
        risk_pct               = risk_per_trade_pct,
        risk_dollars           = round(risk_dollars, 2),
        low_cash               = low_cash,
        n_held_slots           = n_held_slots,
        slots_remaining        = slots_remaining,
        held                   = _held_gui,
        pyramids               = _pyramid_gui,
        n_all_pyramids         = len(pyramid_cands),
        signals                = _signal_gui,
        n_all_signals          = len(new_signals),
        rest_tickers           = [s["symbol"] for s in new_signals[top_signals:]],
        no_signals             = no_signals,
        risk_monitor           = _risk_gui,
        total_risk_pct         = round(total_risk_pct, 2),
        risk_ok                = total_risk_pct <= risk_per_trade_pct * max_positions,
        invested               = round(_invested, 2),
        invested_pct           = round(_invested / portfolio_value, 4) if portfolio_value else 0,
        min_buy_confidence     = min_buy_confidence,
        min_pyramid_confidence = min_pyramid_confidence,
        top_signals            = top_signals,
    )
    cache_path = os.path.join(RESULTS_DIR, "portfolio_cache.json")
    with open(cache_path, "w", encoding="utf-8") as _f:
        _json.dump(_cache, _f, default=str)
    print(f"  -> Portfolio cache:  {cache_path}")


# ------------------------------------------------------------------------------
# PORTFOLIO DATA API  (returns structured dict — no printing)
# ------------------------------------------------------------------------------
def get_portfolio_data(holdings_csv: str,
                       watchlist: list[str],
                       total_capital: float | None,
                       max_positions: int = 10,
                       risk_per_trade_pct: float = 2.0,
                       min_cushion_for_pyramid: float = 0.08,
                       min_profit_for_pyramid: float = 0.05,
                       cash_reserve_pct: float = 0.10,
                       top_signals: int = 5,
                       min_buy_confidence: float = MIN_BUY_CONFIDENCE,
                       min_pyramid_confidence: float = MIN_PYRAMID_CONFIDENCE,
                       warn_hold_confidence: float = WARN_HOLD_CONFIDENCE) -> dict:
    """
    Same logic as run_portfolio() but returns a structured dict for the
    Flask dashboard API instead of printing to stdout.
    """
    holdings     = _load_holdings(holdings_csv)
    held_tickers = {h["ticker"] for h in holdings}

    all_tickers: list[str] = []
    seen: set[str] = set()
    for t in list(held_tickers) + [w.upper() for w in watchlist]:
        if t not in seen:
            all_tickers.append(t)
            seen.add(t)

    indicators: dict[str, dict | None] = {}
    for t in all_tickers:
        indicators[t] = _get_live_indicators(t)

    holdings_value = 0.0
    for h in holdings:
        ind = indicators.get(h["ticker"])
        if ind:
            holdings_value += h["shares"] * ind["close"]

    if total_capital is not None:
        portfolio_value = total_capital
        cash            = max(total_capital - holdings_value, 0.0)
    else:
        portfolio_value = holdings_value / 0.80 if holdings_value > 0 else INITIAL_CAPITAL
        cash            = portfolio_value - holdings_value

    risk_dollars  = portfolio_value * (risk_per_trade_pct / 100.0)
    cash_reserve  = portfolio_value * cash_reserve_pct
    date_str      = datetime.now().strftime("%Y-%m-%d")

    def _risk_shares(close: float, atr_frac: float):
        stop_dist = close * atr_frac
        if stop_dist < 0.001:
            return 0, 0.0, 0.0
        shares  = int(risk_dollars / stop_dist)
        stop_px = close * (1.0 - atr_frac)
        tp_px   = close + 2.0 * (close * atr_frac)
        return shares, stop_px, tp_px

    # ── Held positions ────────────────────────────────────────────────────────
    held_rows:     list[dict] = []
    pyramid_cands: list[dict] = []

    for h in holdings:
        ticker      = h["ticker"]
        entry_price = h["entry_price"]
        shares_held = h["shares"]
        ind = indicators.get(ticker)
        if ind is None:
            held_rows.append(dict(ticker=ticker, error=True))
            continue

        close    = ind["close"]
        atr_frac = ind["atr_frac"]
        df_h     = ind["df"]
        closes   = df_h["Close"]
        at_or_below = closes[closes <= entry_price * 1.005]
        if not at_or_below.empty:
            entry_idx = closes.index.get_loc(at_or_below.index[-1])
            pos_peak  = max(entry_price, float(closes.iloc[entry_idx:].max()))
        else:
            pos_peak  = max(entry_price, close)
        if pos_peak < entry_price:
            pos_peak = entry_price

        personal_stop = pos_peak * (1.0 - atr_frac)
        pnl_pct       = (close - entry_price) / entry_price
        dist_to_stop  = (close - personal_stop) / close
        mkt_value     = shares_held * close
        pnl_dollars   = shares_held * (close - entry_price)

        entry_ok   = ind["entry_ok"]
        profitable = pnl_pct >= min_profit_for_pyramid
        cushion_ok = dist_to_stop >= min_cushion_for_pyramid

        if ind["crash_conditions_met"]:
            action_flag = "SELL"; action = "SELL *** CRASH EXIT ***"
        elif close <= personal_stop:
            action_flag = "SELL"; action = "SELL *** STOP HIT ***"
        elif ind["cur_low"] <= personal_stop:
            action_flag = "SELL"; action = "SELL — LOW breached stop"
        elif entry_ok and profitable and cushion_ok:
            action_flag = "HOLD+"; action = f"HOLD — PYRAMID OK ({dist_to_stop:.0%} cushion)"
        elif not ind["above_sma200"]:
            action_flag = "HOLD~"; action = "HOLD — below SMA200"
        else:
            action_flag = "HOLD"; action = f"HOLD ({dist_to_stop:.0%} cushion)"

        conf_score = ind["score"]
        warn = (conf_score < warn_hold_confidence and
                action_flag in ("HOLD", "HOLD+", "HOLD~"))

        row = dict(
            ticker        = ticker,
            entry_price   = entry_price,
            shares        = shares_held,
            notes         = h.get("notes", ""),
            current_price = round(close, 2),
            pnl_pct       = round(pnl_pct, 4),
            pnl_dollars   = round(pnl_dollars, 2),
            mkt_value     = round(mkt_value, 2),
            pos_peak      = round(pos_peak, 2),
            stop_price    = round(personal_stop, 2),
            cushion       = round(dist_to_stop, 4),
            action_flag   = action_flag,
            action        = action,
            score         = round(conf_score, 3),
            adx14         = round(ind["adx14"], 1),
            rs_vs_spy     = round(ind["rs_vs_spy"], 4),
            warn_conf     = warn,
            error         = False,
            # Indicator detail for tooltip
            vix           = round(ind["vix"], 1),
            sma200        = round(ind["sma200"], 2),
            sma50         = round(ind["sma50"], 2),
            above_sma200  = ind["above_sma200"],
            above_sma50   = ind["above_sma50"],
            entry_gate    = ind["entry_gate"],
            entry_reason  = ind["entry_reason"],
            sma200_dist   = round(ind["sma200_dist"], 4),
            atr           = round(ind["atr"], 2),
            atr_frac      = round(ind["atr_frac"], 4),
            bull_regime   = ind["bull_regime"],
        )
        held_rows.append(row)

        if action_flag == "HOLD+":
            pyramid_cands.append(dict(ind=ind, row=row))

    # ── Watchlist screening ───────────────────────────────────────────────────
    new_signals:      list[dict] = []
    trending_signals: list[dict] = []   # above SMA200 but no fresh entry signal today
    no_signals:       list[dict] = []

    for ticker in watchlist:
        ticker_u = ticker.upper()
        # Note: held tickers are NOT skipped — shows entry_ok signal even if already owned
        ind = indicators.get(ticker_u)
        if ind is None:
            no_signals.append(dict(ticker=ticker_u, reason="data error"))
            continue
        if ind["entry_ok"]:
            new_signals.append(ind)
        elif ind["above_sma200"]:
            trending_signals.append(ind)   # strong trend, signal already passed
        else:
            no_signals.append(dict(ticker=ticker_u,
                                   reason=ind["entry_reason"].replace("NO ENTRY — ", "")))

    pyramid_cands.sort(key=lambda x: x["ind"]["score"], reverse=True)
    new_signals.sort(key=lambda x: x["score"], reverse=True)
    trending_signals.sort(key=lambda x: x["score"], reverse=True)
    all_new       = new_signals
    pyramid_cands = [p for p in pyramid_cands if p["ind"]["score"] >= min_pyramid_confidence]
    new_signals   = [s for s in all_new       if s["score"]        >= min_buy_confidence]

    n_held_slots    = sum(1 for r in held_rows if not r.get("error") and r["action_flag"] != "SELL")
    slots_remaining = max(max_positions - n_held_slots, 0)
    cash_deployable = max(cash - cash_reserve, 0.0)
    low_cash        = cash < cash_reserve

    # ── Build pyramid list ────────────────────────────────────────────────────
    pyramid_rows: list[dict] = []
    for pc in pyramid_cands:
        ind_p = pc["ind"]
        add_sh, stop_px, tp_px = _risk_shares(ind_p["close"], ind_p["atr_frac"])
        add_cost = add_sh * ind_p["close"]
        feasible = add_sh > 0 and add_cost <= cash_deployable and not low_cash
        pyramid_rows.append(dict(
            ticker      = ind_p["symbol"],
            pnl_pct     = pc["row"]["pnl_pct"],
            score       = round(ind_p["score"], 3),
            adx14       = round(ind_p["adx14"], 1),
            rs_vs_spy   = round(ind_p["rs_vs_spy"], 4),
            add_sh      = add_sh,
            stop_px     = round(stop_px, 2),
            tp_px       = round(tp_px, 2),
            add_cost    = round(add_cost, 2),
            feasible    = feasible,
            price       = round(ind_p["close"], 2),
            vix         = round(ind_p["vix"], 1),
            sma200      = round(ind_p["sma200"], 2),
            sma50       = round(ind_p["sma50"], 2),
            above_sma200= ind_p["above_sma200"],
            above_sma50 = ind_p["above_sma50"],
            entry_gate  = ind_p["entry_gate"],
            sma200_dist = round(ind_p["sma200_dist"], 4),
            atr         = round(ind_p["atr"], 2),
            atr_frac    = round(ind_p["atr_frac"], 4),
            bull_regime = ind_p["bull_regime"],
        ))

    # ── Build new signals list ────────────────────────────────────────────────
    signal_rows: list[dict] = []
    for ind_n in new_signals[:top_signals]:
        sh, stop_px, tp_px = _risk_shares(ind_n["close"], ind_n["atr_frac"])
        cost     = sh * ind_n["close"]
        feasible = sh > 0 and cost <= cash_deployable and not low_cash
        is_held  = ind_n["symbol"] in held_tickers
        signal_rows.append(dict(
            ticker      = ind_n["symbol"],
            price       = round(ind_n["close"], 2),
            gate        = ind_n["entry_gate"],
            score       = round(ind_n["score"], 3),
            adx14       = round(ind_n["adx14"], 1),
            rs_vs_spy   = round(ind_n["rs_vs_spy"], 4),
            shares      = sh if (feasible and not is_held) else None,
            stop        = round(stop_px, 2),
            tp          = round(tp_px, 2),
            feasible    = feasible,
            is_held     = is_held,
            vix         = round(ind_n["vix"], 1),
            sma200      = round(ind_n["sma200"], 2),
            sma50       = round(ind_n["sma50"], 2),
            above_sma200= ind_n["above_sma200"],
            above_sma50 = ind_n["above_sma50"],
            sma200_dist = round(ind_n["sma200_dist"], 4),
            atr         = round(ind_n["atr"], 2),
            atr_frac    = round(ind_n["atr_frac"], 4),
            bull_regime = ind_n["bull_regime"],
            entry_reason= ind_n["entry_reason"],
        ))
    rest_tickers = [s["symbol"] for s in new_signals[top_signals:]]

    # ── Build trending list (above SMA200, strong, but no fresh entry today) ─
    trending_rows: list[dict] = []
    for ind_t in trending_signals:
        _, stop_px, tp_px = _risk_shares(ind_t["close"], ind_t["atr_frac"])
        is_held = ind_t["symbol"] in held_tickers
        trending_rows.append(dict(
            ticker      = ind_t["symbol"],
            price       = round(ind_t["close"], 2),
            score       = round(ind_t["score"], 3),
            adx14       = round(ind_t["adx14"], 1),
            rs_vs_spy   = round(ind_t["rs_vs_spy"], 4),
            stop        = round(stop_px, 2),
            is_held     = is_held,
            reason      = ind_t["entry_reason"].replace("NO ENTRY — ", ""),
            vix         = round(ind_t["vix"], 1),
            sma200      = round(ind_t["sma200"], 2),
            sma50       = round(ind_t["sma50"], 2),
            above_sma50 = ind_t["above_sma50"],
            sma200_dist = round(ind_t["sma200_dist"], 4),
            atr         = round(ind_t["atr"], 2),
            atr_frac    = round(ind_t["atr_frac"], 4),
            bull_regime = ind_t["bull_regime"],
            entry_reason= ind_t["entry_reason"],
        ))

    # ── Risk monitor ──────────────────────────────────────────────────────────
    risk_rows: list[dict] = []
    total_risk_pct = 0.0
    for h in holdings:
        ticker      = h["ticker"]
        shares_held = h["shares"]
        ind = indicators.get(ticker)
        if ind is None:
            continue
        row = next((r for r in held_rows
                    if not r.get("error") and r["ticker"] == ticker
                    and r["action_flag"] not in ("BUY", "PYRAMID")), None)
        if row is None:
            continue
        close       = ind["close"]
        stop_px     = row["stop_price"]
        risk_per_sh = max(close - stop_px, 0.0)
        pos_risk_d  = shares_held * risk_per_sh
        pos_risk_pct = pos_risk_d / portfolio_value * 100 if portfolio_value > 0 else 0.0
        total_risk_pct += pos_risk_pct
        target_pct  = risk_per_trade_pct
        delta       = pos_risk_pct - target_pct

        if pos_risk_pct > target_pct * 1.5:
            excess  = pos_risk_d - risk_dollars
            trim_sh = int(excess / risk_per_sh) if risk_per_sh > 0 else 0
            suggestion = f"REDUCE by ~{trim_sh} sh"
        elif pos_risk_pct < target_pct * 0.5 and ind["entry_ok"] and row["action_flag"] != "SELL":
            deficit = risk_dollars - pos_risk_d
            add_sh  = int(deficit / risk_per_sh) if risk_per_sh > 0 else 0
            suggestion = f"ADD ~{add_sh} sh" if add_sh > 0 else "OK"
        else:
            suggestion = "OK"

        risk_rows.append(dict(
            ticker       = ticker,
            shares       = int(shares_held),
            price        = round(close, 2),
            stop         = round(stop_px, 2),
            risk_dollars = round(pos_risk_d, 2),
            risk_pct     = round(pos_risk_pct, 3),
            target_pct   = target_pct,
            delta        = round(delta, 3),
            suggestion   = suggestion,
        ))

    invested = sum(r["mkt_value"] for r in held_rows
                   if not r.get("error") and
                   r["action_flag"] not in ("SELL", "BUY", "PYRAMID")
                   and r.get("mkt_value"))

    result = dict(
        date            = date_str,
        portfolio_value = round(portfolio_value, 2),
        holdings_value  = round(holdings_value, 2),
        cash            = round(cash, 2),
        cash_pct        = round(cash / portfolio_value, 4) if portfolio_value else 0,
        cash_reserve    = round(cash_reserve, 2),
        reserve_pct     = cash_reserve_pct,
        max_positions   = max_positions,
        risk_pct        = risk_per_trade_pct,
        risk_dollars    = round(risk_dollars, 2),
        low_cash        = low_cash,
        n_held_slots    = n_held_slots,
        slots_remaining = slots_remaining,
        held            = held_rows,
        pyramids        = pyramid_rows,
        n_all_pyramids  = len(pyramid_cands),
        signals         = signal_rows,
        n_all_signals   = len(new_signals),
        rest_tickers    = rest_tickers,
        trending        = trending_rows,
        no_signals      = no_signals,
        risk_monitor    = risk_rows,
        total_risk_pct  = round(total_risk_pct, 2),
        risk_ok         = total_risk_pct <= risk_per_trade_pct * max_positions,
        invested        = round(invested, 2),
        invested_pct    = round(invested / portfolio_value, 4) if portfolio_value else 0,
        min_buy_confidence     = min_buy_confidence,
        min_pyramid_confidence = min_pyramid_confidence,
        top_signals     = top_signals,
    )

    # ── Write portfolio cache for GUI ────────────────────────────────────────
    import json as _json
    os.makedirs(RESULTS_DIR, exist_ok=True)
    cache_path = os.path.join(RESULTS_DIR, "portfolio_cache.json")
    with open(cache_path, "w", encoding="utf-8") as _f:
        _json.dump(result, _f, default=str)

    return result

# ------------------------------------------------------------------------------
# CONFIG FILE LOADER
# ------------------------------------------------------------------------------
def _load_config(path: str) -> dict:
    """
    Load a TOML config file.  Returns the parsed dict, or an empty dict if the
    file does not exist (all values fall back to CLI defaults).
    """
    if not os.path.exists(path):
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


# ------------------------------------------------------------------------------
# DEFAULT UNIVERSE
# ------------------------------------------------------------------------------
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "AVGO", "LLY", "JPM", "COST", "NFLX",
]


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="v2.9p - Adaptive Crash-Exit Strategy + Portfolio Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Run with config file defaults (edit v2_9p/config.toml):\n"
            "  python v2_9p/main.py\n\n"
            "  # Override mode / settings via CLI (always wins over config):\n"
            "  python v2_9p/main.py --backtest AAPL --from-year 2020 --to-year 2025\n"
            "  python v2_9p/main.py --signal NVDA --entry-price 795\n"
            "  python v2_9p/main.py --portfolio holdings.csv --capital 100000\n\n"
            "  # Use a different config file:\n"
            "  python v2_9p/main.py --config my_other_config.toml\n"
        ),
    )
    # ── Config file ───────────────────────────────────────────────────────────
    ap.add_argument("--config",          default=None, metavar="TOML",
                    help="Path to config file (default: v2_9p/config.toml)")
    # ── Explicit CLI overrides  (all optional — config supplies defaults) ─────
    ap.add_argument("--backtest",        nargs="+", default=None, metavar="TICKER",
                    help="One or more tickers to backtest (e.g. --backtest NVDA AAPL)")
    ap.add_argument("--from-year",       type=int,   default=None)
    ap.add_argument("--to-year",         type=int,   default=None)
    ap.add_argument("--no-log",          action="store_true", default=None)
    ap.add_argument("--no-charts",       action="store_true", default=None)
    ap.add_argument("--random-universe", type=int,   default=None, metavar="N")
    ap.add_argument("--trials",          type=int,   default=None)
    ap.add_argument("--signal",          default=None, metavar="TICKER",
                    help="Entry/exit signal (comma-separated tickers ok)")
    ap.add_argument("--entry-price",     type=float, default=None,
                    help="Average entry price → activates HOLD/SELL mode")
    ap.add_argument("--portfolio",       default=None, metavar="CSV",
                    help="Path to holdings CSV (ticker,entry_price,shares)")
    ap.add_argument("--watchlist",       nargs="+",  default=None, metavar="TICKER")
    ap.add_argument("--capital",         type=float, default=None)
    ap.add_argument("--max-positions",   type=int,   default=None)
    args = ap.parse_args()

    # ── Load config file ──────────────────────────────────────────────────────
    # Default config path: v2_9p/config.toml (same directory as this script)
    cfg_path = args.config or os.path.join(_HERE, "config.toml")
    cfg = _load_config(cfg_path)
    if cfg:
        print(f"[v2.9p] Config loaded: {cfg_path}")
    else:
        print(f"[v2.9p] No config file found at {cfg_path} — using CLI / built-in defaults")

    # Shorthand helpers to read from config with a fallback default value.
    # CLI argument always wins (not None) → config value → hardcoded fallback.
    def _c(cli_val, section: str, key: str, fallback):
        """Return cli_val if explicitly set, else config[section][key], else fallback."""
        if cli_val is not None:           # CLI was explicitly provided
            return cli_val
        return cfg.get(section, {}).get(key, fallback)

    # ── Resolve all effective settings ───────────────────────────────────────
    cur_year = datetime.now().year

    # Mode detection: CLI flags have priority; fall back to config [mode].default
    if args.portfolio is not None:
        active_mode = "portfolio"
    elif args.signal is not None:
        active_mode = "signal"
    elif args.random_universe is not None:
        active_mode = "random"
    elif args.backtest is not None:
        active_mode = "backtest"
    else:
        active_mode = cfg.get("mode", {}).get("default", "backtest")

    # ── PORTFOLIO settings ────────────────────────────────────────────────────
    portfolio_csv  = _c(args.portfolio,     "portfolio", "holdings",      "holdings.csv")
    watchlist_raw  = args.watchlist or cfg.get("portfolio", {}).get("watchlist", [])
    capital        = _c(args.capital,       "portfolio", "capital",       None)
    if capital == 0:
        capital = None  # 0 in config means "auto-compute"
    max_positions  = _c(args.max_positions, "portfolio", "max_positions", 10)

    # ── SIGNAL settings ───────────────────────────────────────────────────────
    signal_ticker  = args.signal or cfg.get("signal", {}).get("ticker", "")
    entry_price    = args.entry_price
    if entry_price is None:
        ep_cfg = cfg.get("signal", {}).get("entry_price", 0)
        entry_price = float(ep_cfg) if ep_cfg else None

    # ── BACKTEST settings ─────────────────────────────────────────────────────
    bt = cfg.get("backtest", {})
    symbol_cfg   = bt.get("symbol", "") or None
    tickers_cfg  = bt.get("tickers", []) or []

    if args.backtest:
        bt_tickers = [t.upper() for t in args.backtest]
        bt_symbol  = bt_tickers[0] if len(bt_tickers) == 1 else None
    elif symbol_cfg:
        bt_symbol  = symbol_cfg.upper()
        bt_tickers = [bt_symbol]
    elif tickers_cfg:
        bt_symbol  = None
        bt_tickers = [t.upper() for t in tickers_cfg]
    else:
        bt_symbol  = None
        bt_tickers = DEFAULT_TICKERS

    from_year_bt = _c(args.from_year, "backtest", "from_year", 2020)
    to_year_bt   = _c(args.to_year,   "backtest", "to_year",   0)
    if not to_year_bt:
        to_year_bt = cur_year
    no_log    = args.no_log    or bt.get("no_log",    False)
    no_charts = args.no_charts or bt.get("no_charts", False)

    # ── RANDOM settings ───────────────────────────────────────────────────────
    rnd = cfg.get("random", {})
    rnd_n_stocks  = _c(args.random_universe, "random", "n_stocks",  20)
    rnd_trials    = _c(args.trials,          "random", "trials",    20)
    rnd_from_year = _c(args.from_year,       "random", "from_year", 2020)
    rnd_to_year   = _c(args.to_year,         "random", "to_year",   0)
    if not rnd_to_year:
        rnd_to_year = cur_year

    # ── Logging setup (backtest modes only) ───────────────────────────────────
    tag = bt_tickers[0] if len(bt_tickers) == 1 else "grid"
    if not no_log and active_mode not in ("portfolio", "signal"):
        lp = _setup_log(tag)
        print(f"[v2.9p] Log -> {lp}")

    print(f"[v2.9p] Started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  "
          f"mode={active_mode}")
    t0 = time.time()

    # ── Dispatch ──────────────────────────────────────────────────────────────
    if active_mode == "portfolio":
        rsk  = cfg.get("risk",       {})
        conf = cfg.get("confidence", {})
        run_portfolio(
            holdings_csv            = portfolio_csv,
            watchlist               = [w.upper() for w in watchlist_raw],
            total_capital           = capital,
            max_positions           = int(max_positions),
            risk_per_trade_pct      = float(rsk.get("risk_per_trade_pct",      2.0)),
            min_cushion_for_pyramid = float(rsk.get("min_cushion_for_pyramid", 8.0)) / 100,
            min_profit_for_pyramid  = float(rsk.get("min_profit_for_pyramid",  5.0)) / 100,
            cash_reserve_pct        = float(rsk.get("cash_reserve_pct",       10.0)) / 100,
            top_signals             = int(rsk.get("top_signals",                5)),
            min_buy_confidence      = float(conf.get("min_buy_confidence",     MIN_BUY_CONFIDENCE)),
            min_pyramid_confidence  = float(conf.get("min_pyramid_confidence", MIN_PYRAMID_CONFIDENCE)),
            warn_hold_confidence    = float(conf.get("warn_hold_confidence",   WARN_HOLD_CONFIDENCE)),
        )

    elif active_mode == "signal":
        for ticker in signal_ticker.upper().split(","):
            run_signal(ticker.strip(), entry_price=entry_price)

    elif active_mode == "random":
        print(f"[v2.9p] Years: {rnd_from_year}-{rnd_to_year}")
        run_random_universe(
            n_stocks  = int(rnd_n_stocks),
            n_trials  = int(rnd_trials),
            from_year = int(rnd_from_year),
            to_year   = int(rnd_to_year),
        )

    else:  # backtest
        print(f"[v2.9p] Tickers: {bt_tickers}  |  Years: {from_year_bt}-{to_year_bt}")
        run_grid(
            tickers    = bt_tickers,
            from_year  = int(from_year_bt),
            to_year    = int(to_year_bt),
            save_charts= not no_charts,
        )

    print(f"\n[v2.9p] Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
