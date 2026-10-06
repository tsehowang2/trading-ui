from decimal import Decimal
from uuid import uuid4

import pytest

import ledger
import profile_store


def event(kind, when='2025-01-01T10:00:00+00:00', **values):
    return dict(id=str(uuid4()), kind=kind, occurred_at=when, **values)


def saved(events):
    return [dict(e, sequence=i + 1, recorded_at='2026-01-01T00:00:00+00:00') for i, e in enumerate(events)]


def test_fifo_partial_sale_includes_fees_and_preserves_cash_identity():
    events = saved([
        event('DEPOSIT', amount='5000'),
        event('BUY', '2025-01-02T10:00:00+00:00', ticker='AAPL', shares='10', price='100', fee='2', tag='strategy'),
        event('BUY', '2025-01-03T10:00:00+00:00', ticker='AAPL', shares='5', price='120', fee='1'),
        event('SELL', '2025-01-04T10:00:00+00:00', ticker='AAPL', shares='12', price='150', fee='2'),
    ])
    report = ledger.replay(events, {'AAPL': '140'})
    assert Decimal(report['cash']) == Decimal('5195')
    assert Decimal(report['realized_pnl']) == Decimal('555.6')
    assert Decimal(report['positions'][0]['cost_basis']) == Decimal('360.6')
    assert Decimal(report['unrealized_pnl']) == Decimal('59.4')
    assert Decimal(report['net_profit']) == Decimal('615')
    assert Decimal(report['net_profit']) == Decimal(report['realized_pnl']) + Decimal(report['unrealized_pnl'])
    assert len(report['sales'][0]['allocations']) == 2


def test_funding_is_not_profit_and_missing_marks_are_explicit():
    events = saved([event('DEPOSIT', amount='1000'),
                    event('BUY', ticker='AAPL', shares='2', price='100'),
                    event('WITHDRAWAL', amount='100')])
    unmarked = ledger.replay(events)
    assert unmarked['cash'] == '700'
    assert unmarked['equity'] is None
    assert unmarked['missing_marks'] == ['AAPL']
    marked = ledger.replay(events, {'AAPL': '110'})
    assert marked['net_funding'] == '900'
    assert marked['net_profit'] == '20'


def test_split_and_dividend_accounting():
    events = saved([event('DEPOSIT', amount='1000'),
                    event('BUY', ticker='AAPL', shares='3', price='100'),
                    event('SPLIT', ticker='AAPL', ratio='1.5'),
                    event('DIVIDEND', ticker='AAPL', amount='9'),
                    event('SELL', ticker='AAPL', shares='0.5', price='80')])
    report = ledger.replay(events, {'AAPL': '80'})
    assert report['positions'][0]['shares'] == '4.0'
    assert Decimal(report['equity']) == Decimal('1069')
    assert Decimal(report['net_profit']) == Decimal('69')
    assert Decimal(report['realized_pnl']) + Decimal(report['unrealized_pnl']) + Decimal(report['dividends']) == Decimal('69')


def test_backdated_events_sort_by_occurrence_not_entry_order():
    events = saved([event('BUY', '2025-01-02T00:00:00Z', ticker='AAPL', shares='1', price='100'),
                    event('DEPOSIT', '2025-01-01T00:00:00Z', amount='1000')])
    assert ledger.replay(events)['cash'] == '900'


@pytest.mark.parametrize('data', [
    event('BUY', ticker='AAPL', shares='1.5', price='100'),
    event('BUY', ticker='AAPL', shares='1', price='NaN'),
    event('DEPOSIT', amount='-1'),
    event('DEPOSIT', amount=True),
    event('BUY', ticker='../BAD', shares='1', price='100'),
    event('DEPOSIT', when='2025-01-01T00:00:00', amount='100'),
])
def test_invalid_event_values_rejected(data):
    with pytest.raises(ValueError):
        ledger.normalize_event(data)


def test_oversell_and_negative_cash_rejected():
    with pytest.raises(ValueError, match='Insufficient cash'):
        ledger.replay(saved([event('BUY', ticker='AAPL', shares='1', price='100')]))
    with pytest.raises(ValueError, match='exceeds held'):
        ledger.replay(saved([event('DEPOSIT', amount='1000'), event('SELL', ticker='AAPL', shares='1', price='100')]))


def test_void_replays_without_changing_original():
    deposit = event('DEPOSIT', amount='1000')
    buy = event('BUY', ticker='AAPL', shares='1', price='100')
    void = event('VOID', target_id=buy['id'], notes='Wrong symbol')
    events = saved([deposit, buy, void])
    result = ledger.replay(events)
    assert result['cash'] == '1000'
    assert result['positions'] == []
    assert events[1]['kind'] == 'BUY'
    assert result['voided_ids'] == [buy['id']]


def test_atomic_account_event_idempotency_and_isolation(client, profile):
    manual = ledger.create_account(profile['id'], 'manual')
    paper = ledger.create_account(profile['id'], 'paper')
    funding = event('DEPOSIT', amount='1000')
    assert ledger.add_event(profile['id'], manual['id'], funding)[1]
    assert not ledger.add_event(profile['id'], manual['id'], funding)[1]
    assert ledger.report(profile['id'], manual['id'])['cash'] == '1000'
    assert ledger.report(profile['id'], paper['id'])['cash'] == '0'
    with pytest.raises(ValueError, match='Idempotency'):
        ledger.add_event(profile['id'], manual['id'], dict(funding, amount='999'))
    with pytest.raises(KeyError):
        ledger.report(1, manual['id'])


def test_invalid_backdated_event_rolls_back_and_legacy_unchanged(client, profile):
    import db
    pid = profile['id']
    db.upsert_holding(pid, dict(ticker='AAPL', entry_price=100, shares=1))
    account = ledger.create_account(pid, 'manual')
    ledger.add_event(pid, account['id'], event('DEPOSIT', '2025-01-02T00:00:00Z', amount='1000'))
    before = profile_store.snapshot()
    with pytest.raises(ValueError, match='Insufficient cash'):
        ledger.add_event(pid, account['id'], event('BUY', '2025-01-01T00:00:00Z', ticker='AAPL', shares='1', price='100'))
    assert profile_store.snapshot() == before
    assert ledger.report(pid, account['id'])['legacy_reconciliation'][0]['shares_match'] is False


def test_future_event_rejected(client, profile):
    account = ledger.create_account(profile['id'], 'manual')
    with pytest.raises(ValueError, match='Future'):
        ledger.add_event(profile['id'], account['id'], event('DEPOSIT', '2099-01-01T00:00:00Z', amount='1000'))


def test_fractional_seconds_sort_correctly():
    events = saved([event('BUY', '2025-01-01T00:00:00.100Z', ticker='AAPL', shares='1', price='100'),
                    event('DEPOSIT', '2025-01-01T00:00:00Z', amount='1000')])
    assert ledger.replay(events)['cash'] == '900'


def test_malformed_trade_tag_rejected():
    with pytest.raises(ValueError):
        ledger.normalize_event(event('BUY', ticker='AAPL', shares='1', price='100', tag=[]))