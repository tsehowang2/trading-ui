"""USD cash accounts with immutable events and decimal FIFO cost accounting.

No prices are downloaded here. Missing marks are reported, never substituted
with zero or cost basis. Corrections void an event and replay the entire account.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from uuid import UUID, uuid4

import profile_store
from validation import ticker

ZERO = Decimal('0')
KINDS = {'DEPOSIT', 'WITHDRAWAL', 'BUY', 'SELL', 'DIVIDEND', 'SPLIT', 'VOID'}
TAGS = {'strategy', 'discretionary'}


def decimal(value, field, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 60:
        raise ValueError(f'{field} must be a decimal number')
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f'{field} must be a decimal number') from None
    if not n.is_finite() or n < 0 or n > Decimal('1000000000000') or (positive and n == 0):
        raise ValueError(f'{field} must be {"positive" if positive else "nonnegative"} and finite')
    if n.as_tuple().exponent < -12:
        raise ValueError(f'{field} supports at most 12 decimal places')
    return n


def stamp(value):
    if not isinstance(value, str):
        raise ValueError('occurred_at must be an ISO timestamp with timezone')
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Invalid timestamp') from None
    if d.tzinfo is None:
        raise ValueError('Timestamp timezone is required')
    return d.astimezone(timezone.utc).isoformat(timespec='microseconds')


def identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('A valid UUID idempotency key is required') from None


def normalize_event(data, generated=False):
    if not isinstance(data, dict):
        raise ValueError('Event must be an object')
    common = {'id', 'kind', 'occurred_at', 'notes'}
    extra = {'DEPOSIT': {'amount'}, 'WITHDRAWAL': {'amount'},
             'BUY': {'ticker', 'shares', 'price', 'fee', 'tag'},
             'SELL': {'ticker', 'shares', 'price', 'fee', 'tag'},
             'DIVIDEND': {'ticker', 'amount'}, 'SPLIT': {'ticker', 'ratio'},
             'VOID': {'target_id'}}
    kind = data.get('kind')
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError('Unsupported ledger event kind')
    allowed = common | extra[kind] | ({'recorded_at', 'sequence'} if generated else set())
    if set(data) - allowed:
        raise ValueError('Unsupported event fields')
    notes = data.get('notes', '')
    if not isinstance(notes, str) or len(notes) > 10_000:
        raise ValueError('notes must be text of at most 10000 characters')
    event = dict(id=identifier(data.get('id')), kind=kind,
                 occurred_at=stamp(data.get('occurred_at')), notes=notes)
    if kind in ('BUY', 'SELL'):
        sh = decimal(data.get('shares'), 'shares', True)
        if kind == 'BUY' and sh != sh.to_integral_value():
            raise ValueError('Buy quantities must be whole shares')
        tag = data.get('tag', 'discretionary')
        if not isinstance(tag, str) or tag not in TAGS:
            raise ValueError('Trade tag must be strategy or discretionary')
        event.update(ticker=ticker(data.get('ticker')), shares=str(sh),
                     price=str(decimal(data.get('price'), 'price', True)),
                     fee=str(decimal(data.get('fee', '0'), 'fee')), tag=tag)
    if kind in ('DEPOSIT', 'WITHDRAWAL', 'DIVIDEND'):
        event['amount'] = str(decimal(data.get('amount'), 'amount', True))
    if kind in ('DIVIDEND', 'SPLIT'):
        event['ticker'] = ticker(data.get('ticker'))
    if kind == 'SPLIT':
        event['ratio'] = str(decimal(data.get('ratio'), 'ratio', True))
    if kind == 'VOID':
        event['target_id'] = identifier(data.get('target_id'))
        if not notes.strip():
            raise ValueError('Voiding an event requires a correction reason')
    if generated:
        event['recorded_at'] = stamp(data.get('recorded_at'))
        seq = data.get('sequence')
        if type(seq) is not int or seq < 1:
            raise ValueError('Event sequence must be a positive integer')
        event['sequence'] = seq
    return event


def replay(events, marks=None):
    with localcontext() as context:
        context.prec = 40
        return _replay(events, marks or {})


def _replay(events, marks):
    normalized = [normalize_event(e, generated=True) for e in events]
    ids, sequences, voided = set(), set(), set()
    known = {}
    for event in sorted(normalized, key=lambda e: e['sequence']):
        if event['id'] in ids or event['sequence'] in sequences:
            raise ValueError('Duplicate event ID or sequence')
        ids.add(event['id']); sequences.add(event['sequence'])
        if event['kind'] == 'VOID':
            target = known.get(event['target_id'])
            if not target or target['kind'] == 'VOID' or target['id'] in voided:
                raise ValueError('Correction must target an earlier, non-voided event')
            voided.add(target['id'])
        known[event['id']] = event
    cash = funding = realized = dividends = fees = ZERO
    lots, stats, sales = {}, {}, []
    tag_realized = {tag: ZERO for tag in TAGS}
    def stock(symbol):
        return stats.setdefault(symbol, dict(realized=ZERO, dividends=ZERO, fees=ZERO,
                                            buys=0, sells=0, strategy_trades=0, discretionary_trades=0))
    active = [e for e in normalized if e['id'] not in voided and e['kind'] != 'VOID']
    for event in sorted(active, key=lambda e: (e['occurred_at'], e['sequence'])):
        kind = event['kind']
        if kind in ('DEPOSIT', 'WITHDRAWAL'):
            amount = Decimal(event['amount']) * (1 if kind == 'DEPOSIT' else -1)
            cash += amount; funding += amount
        elif kind == 'DIVIDEND':
            amount = Decimal(event['amount'])
            cash += amount; dividends += amount
            stock(event['ticker'])['dividends'] += amount
        elif kind == 'SPLIT':
            current = lots.get(event['ticker'], [])
            if not current:
                raise ValueError('Cannot apply a split without an open position')
            ratio = Decimal(event['ratio'])
            for lot in current:
                lot['shares'] *= ratio  # cost is unchanged
        elif kind in ('BUY', 'SELL'):
            symbol, sh, px, fee = event['ticker'], Decimal(event['shares']), Decimal(event['price']), Decimal(event['fee'])
            s = stock(symbol)
            fees += fee; s['fees'] += fee
            s[event['tag'] + '_trades'] += 1
            current = lots.setdefault(symbol, [])
            if kind == 'BUY':
                cost = sh * px + fee
                cash -= cost; s['buys'] += 1
                current.append(dict(shares=sh, cost=cost, acquired_at=event['occurred_at'],
                                    event_id=event['id'], tag=event['tag']))
            else:
                if sh > sum((l['shares'] for l in current), ZERO):
                    raise ValueError(f'Sell exceeds held shares for {symbol} at {event["occurred_at"]}')
                proceeds = sh * px - fee
                if proceeds < 0:
                    raise ValueError('Sell fee exceeds proceeds')
                cash += proceeds; s['sells'] += 1
                remaining, cost, allocations = sh, ZERO, []
                while remaining > 0:
                    lot = current[0]
                    used = min(remaining, lot['shares'])
                    basis = lot['cost'] if used == lot['shares'] else lot['cost'] * used / lot['shares']
                    cost += basis
                    allocations.append(dict(buy_id=lot['event_id'], shares=str(used), cost_basis=str(basis), entry_tag=lot['tag']))
                    lot['shares'] -= used; lot['cost'] -= basis; remaining -= used
                    if lot['shares'] == 0:
                        current.pop(0)
                profit = proceeds - cost
                allocated_proceeds = ZERO
                for i, allocation in enumerate(allocations):
                    part = proceeds - allocated_proceeds if i == len(allocations) - 1 else proceeds * Decimal(allocation['shares']) / sh
                    allocated_proceeds += part
                    gain = part - Decimal(allocation['cost_basis'])
                    allocation['realized_pnl'] = str(gain)
                    tag_realized[allocation['entry_tag']] += gain
                realized += profit; s['realized'] += profit
                sales.append(dict(event_id=event['id'], ticker=symbol, occurred_at=event['occurred_at'],
                                  proceeds=str(proceeds), cost_basis=str(cost), realized_pnl=str(profit),
                                  exit_tag=event['tag'], allocations=allocations))
        if cash < 0:
            raise ValueError(f'Insufficient cash at {event["occurred_at"]}; enter earlier funding first')
    positions, missing, market_value, unrealized = [], [], ZERO, ZERO
    for symbol in sorted(lots):
        current = lots[symbol]
        if not current:
            continue
        sh = sum((l['shares'] for l in current), ZERO)
        cost = sum((l['cost'] for l in current), ZERO)
        mark = decimal(marks[symbol], 'mark', True) if symbol in marks else None
        mv = sh * mark if mark is not None else None
        pnl = mv - cost if mv is not None else None
        if mv is None:
            missing.append(symbol)
        else:
            market_value += mv; unrealized += pnl
        positions.append(dict(ticker=symbol, shares=str(sh), cost_basis=str(cost),
                              average_cost=str(cost / sh), entry_at=current[0]['acquired_at'],
                              mark=str(mark) if mark is not None else None,
                              market_value=str(mv) if mv is not None else None,
                              unrealized_pnl=str(pnl) if pnl is not None else None))
    equity = cash + market_value if not missing else None
    return dict(cash=str(cash), net_funding=str(funding), realized_pnl=str(realized),
                dividends=str(dividends), fees=str(fees), positions=positions, sales=sales,
                missing_marks=missing, market_value=str(market_value) if not missing else None,
                unrealized_pnl=str(unrealized) if not missing else None,
                equity=str(equity) if equity is not None else None,
                net_profit=str(equity - funding) if equity is not None else None,
                realized_by_entry_tag={tag: str(value) for tag, value in sorted(tag_realized.items())},
                per_stock=[dict(ticker=symbol, **{k: str(v) if isinstance(v, Decimal) else v for k, v in s.items()})
                           for symbol, s in sorted(stats.items())], voided_ids=sorted(voided))


def validate_accounts(accounts):
    if not isinstance(accounts, list) or len(accounts) > 200:
        raise ValueError('accounts must be a list of at most 200 accounts')
    seen, combinations, result = set(), set(), []
    for account in accounts:
        if not isinstance(account, dict) or set(account) != {'id', 'profile_id', 'kind', 'currency', 'created_at', 'events'}:
            raise ValueError('Invalid account fields')
        account = deepcopy(account)
        account['id'] = identifier(account['id'])
        if account['id'] in seen or account['kind'] not in ('manual', 'paper') or account['currency'] != 'USD':
            raise ValueError('Invalid or duplicate account')
        pid = account['profile_id']
        if type(pid) is not int or pid < 1 or (pid, account['kind']) in combinations:
            raise ValueError('Only one manual and one paper account per profile are supported')
        account['created_at'] = stamp(account['created_at'])
        if not isinstance(account['events'], list) or len(account['events']) > 20_000:
            raise ValueError('Account events must be a list of at most 20000 records')
        account['events'] = [normalize_event(e, generated=True) for e in account['events']]
        replay(account['events'])
        seen.add(account['id']); combinations.add((pid, account['kind'])); result.append(account)
    return result


def create_account(profile_id, kind, with_status=False):
    if kind not in ('manual', 'paper'):
        raise ValueError('Account kind must be manual or paper')
    with profile_store.transaction(True) as state:
        profile_store._profile(state, profile_id)
        accounts = state.setdefault('accounts', [])
        old = next((a for a in accounts if a['profile_id'] == profile_id and a['kind'] == kind), None)
        if old:
            return (deepcopy(old), False) if with_status else deepcopy(old)
        if len(accounts) >= 200:
            raise ValueError('Maximum 200 accounts supported')
        account = dict(id=str(uuid4()), profile_id=profile_id, kind=kind, currency='USD',
                       created_at=datetime.now(timezone.utc).isoformat(), events=[])
        accounts.append(account)
    return (deepcopy(account), True) if with_status else deepcopy(account)


def account_in(state, profile_id, account_id):
    profile_store._profile(state, profile_id)
    account_id = identifier(account_id)
    account = next((a for a in state.get('accounts', []) if a['id'] == account_id and a['profile_id'] == profile_id), None)
    if account is None:
        raise KeyError('Account not found')
    return account


def add_event(profile_id, account_id, data):
    events, created = add_events(profile_id, account_id, [data])
    return events[0], created > 0


def add_events(profile_id, account_id, rows):
    """Batch history entry/correction validates once and commits all or none."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError('Submit between 1 and 1000 events per batch')
    normalized = [normalize_event(data) for data in rows]
    if any(datetime.fromisoformat(e['occurred_at']) > datetime.now(timezone.utc) for e in normalized):
        raise ValueError('Future executions/funding cannot be recorded')
    results, created = [], 0
    with profile_store.transaction(True) as state:
        account = account_in(state, profile_id, account_id)
        if any(r['account_id']==account_id for r in state.get('paper_runs',[])):
            raise ValueError('An active strategy run owns this paper account; reset through Strategy Paper, not manual funding edits')
        for clean in normalized:
            if account['kind'] == 'paper' and clean['kind'] not in ('DEPOSIT', 'WITHDRAWAL'):
                target = next((e for e in account['events'] if e['id'] == clean.get('target_id')), None)
                if clean['kind'] != 'VOID' or not target or target['kind'] not in ('DEPOSIT', 'WITHDRAWAL'):
                    raise ValueError('Paper executions are reserved for the future simulator')
            existing = next((e for e in account['events'] if e['id'] == clean['id']), None)
            if existing:
                payload = {k: v for k, v in existing.items() if k not in ('sequence', 'recorded_at')}
                if payload != clean:
                    raise ValueError('Idempotency key already used for a different event')
                results.append(deepcopy(existing))
                continue
            if len(account['events']) >= 20_000:
                raise ValueError('Account reached the 20000-event limit')
            clean['sequence'] = max((e['sequence'] for e in account['events']), default=0) + 1
            clean['recorded_at'] = datetime.now(timezone.utc).isoformat()
            account['events'].append(clean)
            results.append(deepcopy(clean)); created += 1
        replay(account['events'])  # reject invalid backdated edits atomically
    return results, created


def report(profile_id, account_id, marks=None):
    state = profile_store.snapshot()
    account = account_in(state, profile_id, account_id)
    result = replay(account['events'], marks)
    result.update(account=account)
    actual = {p['ticker']: p for p in result['positions']}
    legacy = {h['ticker']: h for h in state['holdings'].get(str(profile_id), [])}
    reconciliation = []
    for symbol in sorted(set(actual) | set(legacy)):
        ledger_sh = Decimal(actual[symbol]['shares']) if symbol in actual else ZERO
        legacy_sh = Decimal(str(legacy[symbol]['shares'])) if symbol in legacy else ZERO
        reconciliation.append(dict(ticker=symbol, ledger_shares=str(ledger_sh), legacy_shares=str(legacy_sh),
                                   shares_match=ledger_sh == legacy_sh,
                                   ledger_average_cost=actual.get(symbol, {}).get('average_cost'),
                                   legacy_entry_price=str(legacy[symbol]['entry_price']) if symbol in legacy else None))
    result['legacy_reconciliation'] = reconciliation
    result['legacy_holdings_unchanged'] = True
    return result


