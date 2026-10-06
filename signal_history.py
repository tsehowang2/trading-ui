"""Immutable observed eligibility snapshots; not fabricated historical alerts.

Same session/config/input revision is idempotent. A later revision is appended,
so earlier observations remain visible. These records are observations, not
proof of a historical execution or account-state BUY/SELL decision.
"""
from copy import deepcopy
from datetime import datetime, timezone, date
import hashlib
import json
from uuid import uuid4

import profile_store
import strategy
from validation import ticker
from ledger import identifier, stamp

FIELDS = {'id','profile_id','ticker','signal_session','observed_at','strategy_version',
          'config_hash','input_revision','entry_ok','fresh_entry','entry_reason','inputs','context'}


def config_hash(profile):
    return hashlib.sha256(json.dumps({k:v for k,v in profile.items() if k not in ('id','name','is_active')},
                                     sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate_history(records):
    if not isinstance(records, list) or len(records) > 100_000:
        raise ValueError('Signal history must contain at most 100000 observations')
    clean, seen = [], set()
    for record in records:
        if not isinstance(record, dict) or set(record) != FIELDS:
            raise ValueError('Invalid observed signal record')
        r = deepcopy(record)
        r['id'] = identifier(r['id'])
        if r['id'] in seen or type(r['profile_id']) is not int or r['profile_id'] < 1:
            raise ValueError('Invalid/duplicate observed signal ID')
        r['ticker'] = ticker(r['ticker'])
        try:
            date.fromisoformat(r['signal_session'])
        except (TypeError,ValueError):
            raise ValueError('Invalid signal session') from None
        r['observed_at'] = stamp(r['observed_at'])
        if type(r['entry_ok']) is not bool or type(r['fresh_entry']) is not bool:
            raise ValueError('Signal flags must be boolean')
        if not isinstance(r['inputs'], dict) or not isinstance(r['entry_reason'], str):
            raise ValueError('Invalid observation evidence')
        if r['context'] != 'eligibility-not-account-execution' or not isinstance(r['strategy_version'], str):
            raise ValueError('Unsupported signal observation context')
        for key in ('config_hash','input_revision'):
            import re
            if not isinstance(r[key], str) or not re.fullmatch('[a-f0-9]{64}', r[key]):
                raise ValueError('Invalid observation hash')
        try:
            json.dumps(r['inputs'],allow_nan=False)
        except (ValueError,TypeError):
            raise ValueError('Observation inputs must be finite JSON') from None
        seen.add(r['id']);clean.append(r)
    return clean


def record_indicators(profile_id, indicators, expected_config_hash=None):
    with profile_store.transaction(True) as state:
        profile = profile_store._profile(state, profile_id)
        cfg = config_hash(profile)
        if expected_config_hash is not None and cfg != expected_config_hash:
            raise ValueError('Profile settings changed while observing signals')
        records = state.setdefault('signal_history', [])
        keys = {(r['profile_id'],r['ticker'],r['signal_session'],r['config_hash'],r['input_revision']) for r in records}
        appended = []
        for ind in indicators:
            if ind is None or 'input_revision' not in ind:
                continue
            key = (profile_id,ind['symbol'],ind['date'],cfg,ind['input_revision'])
            if key in keys:
                continue
            if len(records) >= 100_000:
                raise ValueError('Signal history limit reached; export before continuing')
            inputs = {k:v for k,v in ind.items() if k not in ('df','symbol','input_revision')}
            r = dict(id=str(uuid4()),profile_id=profile_id,ticker=ind['symbol'],signal_session=ind['date'],
                     observed_at=datetime.now(timezone.utc).isoformat(timespec='microseconds'),strategy_version=strategy.VERSION,
                     config_hash=cfg,input_revision=ind['input_revision'],entry_ok=bool(ind['entry_ok']),
                     fresh_entry=bool(ind.get('fresh_entry',False)),entry_reason=ind['entry_reason'],
                     inputs=inputs,context='eligibility-not-account-execution')
            records.append(r);keys.add(key);appended.append(deepcopy(r))
    return appended