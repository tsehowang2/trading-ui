"""Causal daily trend engine, shared by snapshots and chronological replay.

Version 3 intentionally changes old results: actual VIX/VIX3M ratio, finite
rolling ADX (no moving EWM seed), ratcheted close-only stops, and next-open fills.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import math

import numpy as np
import pandas as pd

VERSION = 'daily-close-term-structure-v3'
HISTORY_START = '1990-01-01'


@dataclass(frozen=True)
class StrategyConfig:
    min_buy_confidence: float = .60
    commission: float = 2.0
    cooldown_bars: int = 10
    recovery_bars: int = 10

    def __post_init__(self):
        if not math.isfinite(self.min_buy_confidence) or not 0 <= self.min_buy_confidence <= 1:
            raise ValueError('Invalid confidence threshold')
        if not math.isfinite(self.commission) or self.commission < 0:
            raise ValueError('Invalid commission')
        if self.cooldown_bars < 0 or self.recovery_bars < 0:
            raise ValueError('Invalid state timers')


@dataclass
class PositionState:
    shares: float = 0
    entry_price: float = 0
    peak: float = 0
    stop: float = 0
    cooldown: int = 0
    recovery: int = 0


def confidence(rs, adx, distance):
    return round(float((np.tanh(rs * 10) + 1) / 2 * .4
                       + min(adx / 60, 1) * .4
                       + (np.tanh(min(distance, .5) * 5) + 1) / 2 * .2), 4)


def _streak(values):
    out, count = [], 0
    for value in values:
        count = count + 1 if bool(value) else 0
        out.append(count)
    return out


def build_indicators(stock, spy, vix, vix3m, as_of=None):
    """All windows are past-only; exact-date benchmarks, no ffill/bfill."""
    df = stock.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    for column in ('Open', 'High', 'Low', 'Close', 'Volume', 'Dividends', 'Stock Splits'):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors='coerce')
    df = df.sort_index()
    if as_of is not None:
        df = df.loc[df.index <= pd.Timestamp(as_of)]
    # Insert absent exchange sessions as unknown, so rolling windows/streaks
    # cannot silently bridge a missing observation as a consecutive day.
    if not df.empty:
        from market_sessions import session_dates
        expected = session_dates(df.index.min().strftime('%Y-%m-%d'), df.index.max().strftime('%Y-%m-%d'))
        df = df.reindex(df.index.union(expected)).sort_index()
    for name, frame in [('spy', spy), ('vix', vix), ('vix3m', vix3m)]:
        if frame.empty:
            df[name] = np.nan
        else:
            close = frame['Close'].copy()
            close = pd.to_numeric(close, errors='coerce')
            df[name] = close.reindex(df.index)
    df['vix_term'] = df['vix'] / df['vix3m'].where(df['vix3m'] > 0)
    df['spy_sma200'] = df['spy'].rolling(200).mean()
    for n in (20, 50, 200):
        df[f'sma{n}'] = df['Close'].rolling(n).mean()
    df['low90'] = df['Close'].rolling(90).min()
    df['high20'] = df['Close'].rolling(20).max().shift(1)
    previous = df['Close'].shift(1)
    tr = pd.concat([df['High']-df['Low'], (df['High']-previous).abs(), (df['Low']-previous).abs()], axis=1).max(axis=1)
    df['atr14'] = tr.rolling(14).mean()
    up, down = df['High'].diff(), -df['Low'].diff()
    plus = pd.Series(np.where((up > down) & (up > 0), up, 0), index=df.index).rolling(14).mean()
    minus = pd.Series(np.where((down > up) & (down > 0), down, 0), index=df.index).rolling(14).mean()
    dx = 100 * (plus-minus).abs() / (plus+minus).replace(0, np.nan)
    df['adx14'] = dx.fillna(0).rolling(14).mean()
    df['stock_ret20'] = df['Close'].pct_change(20, fill_method=None)
    df['spy_ret20'] = df['spy'].pct_change(20, fill_method=None)
    df['rs_vs_spy'] = df['stock_ret20'] - df['spy_ret20']
    df['consec_above200'] = _streak(df['Close'] > df['sma200'])
    df['consec_bear'] = _streak(df['Close'] < df['sma200'])
    df['consec_term'] = _streak(df['vix_term'] > 1.05)
    required = ['sma200','sma50','sma20','atr14','adx14','low90','high20','spy','spy_sma200',
                'vix','vix3m','vix_term','stock_ret20','spy_ret20']
    numeric = df[required].to_numpy(dtype=float, na_value=np.nan)
    df['ready'] = np.isfinite(numeric).all(axis=1) & (df['vix'] > 0) & (df['vix3m'] > 0)
    return df


def snapshot(frame, config=StrategyConfig()):
    if frame.empty or not bool(frame.iloc[-1]['ready']):
        return None
    cur = frame.iloc[-1]
    close, atr, vix = float(cur['Close']), float(cur['atr14']), float(cur['vix'])
    distance = close / float(cur['sma200']) - 1
    rs, adx = float(cur['rs_vs_spy']), float(cur['adx14'])
    score = confidence(rs, adx, distance)
    normal = close > cur['sma200'] and close > cur['sma50']
    fast = close > cur['sma200'] and close > cur['high20']
    vol = atr / close >= .01
    rs_ok = rs >= -.05
    entry = bool((normal or fast) and vol and rs_ok and cur['consec_above200'] >= 2 and score >= config.min_buy_confidence)
    gate = ('normal-gate' if normal else 'fast-gate') if entry else None
    bull = bool(cur['spy'] > cur['spy_sma200'] and vix < 15)
    mult = 4 if bull else 3
    hi_vol = atr/close > .10
    frac = float(np.clip(mult * atr/close, .15, .30 if hi_vol else .40))
    crash = bool(cur['consec_bear'] >= 2 and vix > 25 and cur['consec_term'] >= 3)
    reasons = []
    if close <= cur['sma200']: reasons.append('below SMA200')
    if not normal and not fast: reasons.append('below SMA50 & no 20d-high breakout')
    if cur['consec_above200'] < 2: reasons.append('<2 consecutive closes above SMA200')
    if not rs_ok: reasons.append('RS lag below -5%')
    if not vol: reasons.append('low vol')
    if score < config.min_buy_confidence: reasons.append(f'confidence {score:.2f} < {config.min_buy_confidence:.2f}')
    return dict(date=frame.index[-1].strftime('%Y-%m-%d'), close=close, atr=atr, cur_low=float(cur['Low']),
                vix=vix, vix3m=float(cur['vix3m']), vix_term=float(cur['vix_term']),
                sma200=float(cur['sma200']), sma50=float(cur['sma50']), sma20=float(cur['sma20']),
                low90=float(cur['low90']), rebound=close/float(cur['low90'])-1,
                above_sma200=bool(close > cur['sma200']), above_sma50=bool(close > cur['sma50']),
                above_sma20=bool(close > cur['sma20']), normal_ok=bool(normal), fast_ok=bool(fast),
                rs_vs_spy=rs, rs_ok=rs_ok, vol_ok=vol, bull_regime=bull, mult=mult, hi_vol=hi_vol,
                atr_frac=frac, adx14=adx, sma200_dist=distance, score=score, entry_ok=entry,
                entry_gate=gate, entry_reason='BUY — '+str(gate) if entry else 'NO ENTRY — '+', '.join(reasons),
                consec_above_sma200=int(cur['consec_above200']), consec_bear=int(cur['consec_bear']),
                consec_term=int(cur['consec_term']), crash_conditions_met=crash, strategy_version=VERSION)


def evaluate_session(state, ind, config=StrategyConfig()):
    """One completed close decision. Caller invokes once per session."""
    state = replace(state)
    if ind is None:
        return state, dict(action='UNAVAILABLE', reason='Missing required same-session data/warmup')
    if state.shares > 0:
        state.peak = max(state.peak, state.entry_price, ind['close'])
        state.stop = max(state.stop, state.peak * (1-ind['atr_frac']))
        if ind['crash_conditions_met']:
            return state, dict(action='SELL', reason='crash', signal_date=ind['date'], stop=state.stop)
        if ind['close'] <= state.stop:
            return state, dict(action='SELL', reason='trail-close', signal_date=ind['date'], stop=state.stop)
        return state, dict(action='HOLD', reason='position open', signal_date=ind['date'], stop=state.stop)
    # Timers decrement only after a complete, usable session decision.
    cooldown, recovery = state.cooldown, state.recovery
    state.cooldown = max(cooldown-1, 0)
    state.recovery = max(recovery-1, 0)
    recovery_ok = recovery > 0 and ind['above_sma200'] and ind['vix'] < 25 and ind['vol_ok']
    if recovery_ok:
        return state, dict(action='BUY', reason='crash-recovery', signal_date=ind['date'])
    if cooldown == 0 and ind['entry_ok']:
        return state, dict(action='BUY', reason=ind['entry_gate'], signal_date=ind['date'])
    return state, dict(action='WAIT', reason='cooldown' if cooldown else ind['entry_reason'], signal_date=ind['date'])


def replay(frame, start, end, capital=100_000, config=StrategyConfig(), liquidate=False):
    state, pending, trades, decisions, equity = PositionState(), None, [], [], []
    cash = float(capital)
    window = frame.loc[(frame.index >= pd.Timestamp(start)) & (frame.index <= pd.Timestamp(end))]
    for date, cur in window.iterrows():
        # A missing stock bar is never silently crossed with a later-open fill.
        # Provider/cache reports missing sessions; wrappers validate the window.
        opened, close = float(cur['Open']), float(cur['Close'])
        if not math.isfinite(opened) or not math.isfinite(close) or opened <= 0 or close <= 0:
            raise ValueError('Missing stock session in replay; next-open fill unavailable')
        dividend = float(cur.get('Dividends', 0))
        cash += state.shares * dividend
        if pending:
            if pending['action'] == 'SELL' and state.shares:
                sh, entry = state.shares, state.entry_price
                cash += sh*opened-config.commission
                trades.append(dict(date=date, signal_date=pd.Timestamp(pending['signal_date']), action='SELL ('+pending['reason']+')',
                                   price=opened, shares=sh, equity=cash, entry_ep=entry,
                                   pnl_pct=(opened-entry)/entry, avoided_loss=None, missed_gain=None))
                state = PositionState(recovery=config.recovery_bars if pending['reason']=='crash' else 0,
                                      cooldown=0 if pending['reason']=='crash' else config.cooldown_bars)
            elif pending['action'] == 'BUY' and state.shares == 0:
                sh = max(int((cash-config.commission)/opened), 0)
                if sh:
                    cash -= sh*opened+config.commission
                    state = PositionState(shares=sh, entry_price=opened, peak=opened)
                    trades.append(dict(date=date, signal_date=pd.Timestamp(pending['signal_date']), action='BUY ('+pending['reason']+')',
                                       entry_reason=pending['reason'], price=opened, shares=sh, equity=cash+sh*opened,
                                       entry_ep=None, pnl_pct=None, avoided_loss=None, missed_gain=None))
            pending = None
        ind = snapshot(frame.loc[:date], config)
        state, decision = evaluate_session(state, ind, config)
        decisions.append(dict(date=date.strftime('%Y-%m-%d'), **decision))
        if decision['action'] in ('BUY', 'SELL'):
            pending = decision
        equity.append(cash+state.shares*close)
    if liquidate and state.shares and len(window):
        close, date = float(window.iloc[-1]['Close']), window.index[-1]
        cash += state.shares*close-config.commission
        trades.append(dict(date=date, signal_date=None, action='SELL (terminal valuation)', price=close,
                           shares=state.shares, equity=cash, entry_ep=state.entry_price,
                           pnl_pct=(close-state.entry_price)/state.entry_price, avoided_loss=None, missed_gain=None))
        equity[-1] = cash
        state = PositionState()
        pending = None
    return dict(eq=pd.Series(equity, index=window.index, dtype=float), tlog=pd.DataFrame(trades),
                decisions=decisions, pending_order=pending, state=state, cash=cash, strategy_version=VERSION)


def frame_revision(frame):
    return hashlib.sha256(pd.util.hash_pandas_object(frame, index=True).values.tobytes()).hexdigest()


def load_frame(symbol, end, downloader, live=False):
    frames = [downloader(s, HISTORY_START, end) for s in (symbol, 'SPY', '^VIX', '^VIX3M')]
    if frames[0].empty:
        return pd.DataFrame()
    if live:
        # Finite indicators need <=200 bars; retain 400 for bounded streaks and
        # legacy diagnostic charts rather than recalculating decades each call.
        frames = [f.tail(400) for f in frames]
    else:
        # Replay history is calculated once per requested run, not persisted as
        # a second decades-long duplicate of OHLCV for every requested cutoff.
        return build_indicators(*frames, as_of=end)
    from market_cache import symbol_cache
    from data import DATA_CACHE_DIR
    revision = hashlib.sha256(''.join(frame_revision(f) for f in frames).encode()).hexdigest()
    with symbol_cache('indicators:'+symbol+(':live' if live else ':replay'), DATA_CACHE_DIR) as cached:
        if cached.get('version') == VERSION and cached.get('revision') == revision:
            df = pd.DataFrame(cached['rows']).set_index('date')
            df.index = pd.to_datetime(df.index)
            # JSON null represents unknown, not zero or a filled future value.
            for c in df.columns:
                if c != 'ready': df[c] = pd.to_numeric(df[c],errors='coerce')
            return df
        df = build_indicators(*frames, as_of=end)
        records = df.reset_index(names='date')
        records['date'] = records['date'].dt.strftime('%Y-%m-%d')
        rows = json.loads(records.to_json(orient='records',double_precision=15))
        cached.clear();cached.update(version=VERSION,revision=revision,rows=rows)
        df = pd.DataFrame(rows).set_index('date')
        df.index = pd.to_datetime(df.index)
        for c in df.columns:
            if c != 'ready': df[c] = pd.to_numeric(df[c],errors='coerce')
        return df