"""Shared validation for HTTP writes and portable profile documents."""
import math
import re

TICKER = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,19}\Z")
LIMITS = {
    'capital': (0, 9_999_999_999), 'max_positions': (1, 1000),
    'risk_per_trade_pct': (0.01, 100), 'cash_reserve_pct': (0, 100),
    'min_profit_for_pyramid': (0, 100), 'min_cushion_for_pyramid': (0, 100),
    'top_signals': (1, 1000), 'min_buy_confidence': (0, 1),
    'min_pyramid_confidence': (0, 1), 'warn_hold_confidence': (0, 1),
}


def number(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f'{field} must be a finite number')
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f'{field} must be a finite number') from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f'{field} must be between {low} and {high}')
    return result


def ticker(value):
    if not isinstance(value, str) or not TICKER.fullmatch(value.strip().upper()):
        raise ValueError('ticker must be a valid stock/ETF symbol')
    return value.strip().upper()


def settings(data):
    if not isinstance(data, dict):
        raise ValueError('settings must be an object')
    unknown = set(data) - set(LIMITS) - {'name', 'watchlist'}
    if unknown:
        raise ValueError('Unknown settings: ' + ', '.join(sorted(unknown)))
    clean = {}
    for key, value in data.items():
        if key == 'name':
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
                raise ValueError('Profile name must contain 1–100 characters')
            clean[key] = value.strip()
        elif key == 'watchlist':
            if not isinstance(value, list) or len(value) > 1000:
                raise ValueError('watchlist must be a list of at most 1000 symbols')
            clean[key] = list(dict.fromkeys(ticker(t) for t in value))
        else:
            low, high = LIMITS[key]
            n = number(value, key, low, high)
            if key in ('max_positions', 'top_signals'):
                if not n.is_integer():
                    raise ValueError(f'{key} must be an integer')
                n = int(n)
            clean[key] = n
    return clean


def holdings(rows):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('holdings must be a list of at most 1000 positions')
    clean, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Each holding must be an object')
        if set(row) - {'ticker', 'entry_price', 'shares', 'notes'}:
            raise ValueError('Unknown holding fields')
        symbol = ticker(row.get('ticker'))
        if symbol in seen:
            raise ValueError(f'Duplicate holding: {symbol}')
        seen.add(symbol)
        notes = row.get('notes', '')
        if not isinstance(notes, str) or len(notes) > 10_000:
            raise ValueError('notes must be text of at most 10000 characters')
        clean.append(dict(ticker=symbol,
                          entry_price=number(row.get('entry_price'), 'entry_price', 0.0001, 99_999_999),
                          shares=number(row.get('shares'), 'shares', 0.0001, 99_999_999),
                          notes=notes))
    return clean