from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import db
import profile_store


def test_concurrent_holding_saves_preserve_both(client, profile):
    pid = profile['id']
    def save(symbol):
        return db.upsert_holding(pid, dict(ticker=symbol, entry_price=100, shares=2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(save, ['AAPL', 'MSFT']))
    assert {h['ticker'] for h in db.read_holdings_for_profile(pid)} == {'AAPL', 'MSFT'}


def test_bulk_duplicates_rejected_before_write(client, profile):
    before = profile_store.snapshot()
    row = dict(ticker='AAPL', entry_price=100, shares=2)
    response = client.post(f"/api/profiles/{profile['id']}/holdings", json={'_bulk': True, 'holdings': [row, row]})
    assert response.status_code == 400
    assert profile_store.snapshot() == before


def test_legacy_local_files_preserved(client, tmp_path):
    import json
    legacy = tmp_path / 'holdings.json'
    legacy.write_text(json.dumps([dict(ticker='AAPL', entry_price=100, shares=2)]))
    assert db.read_holdings_for_profile(1)[0]['ticker'] == 'AAPL'
    db.upsert_holding(1, dict(ticker='MSFT', entry_price=200, shares=1))
    assert len(json.loads(legacy.read_text())) == 1
    assert len(db.read_holdings_for_profile(1)) == 2


def test_corrupt_local_files_are_not_silent_defaults(client, tmp_path):
    (tmp_path / 'profiles.json').write_text('invalid-json')
    assert client.get('/api/profiles').status_code == 503


def test_postgres_zero_settings_preserved():
    row = dict(id=1, name='Zero', **db.DEFAULT_PROFILE_SETTINGS)
    for key in ('capital', 'cash_reserve_pct', 'min_profit_for_pyramid',
                'min_cushion_for_pyramid', 'min_buy_confidence',
                'min_pyramid_confidence', 'warn_hold_confidence'):
        row[key] = 0
    parsed = db._profile_row_to_dict(row, 1)
    assert all(parsed[k] == 0 for k, v in row.items() if v == 0)