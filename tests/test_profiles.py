from unittest.mock import patch

import db


def test_settings_save_does_not_fail_after_commit(client, profile):
    response = client.put(f"/api/profiles/{profile['id']}", json={"cash_reserve_pct": 0})
    assert response.status_code == 200
    saved = next(p for p in client.get('/api/profiles').get_json() if p['id'] == profile['id'])
    assert saved['cash_reserve_pct'] == 0


def test_holdings_save_does_not_fail_after_commit(client, profile):
    url = f"/api/profiles/{profile['id']}/holdings"
    response = client.post(url, json={"ticker": "AAPL", "entry_price": 100, "shares": 2})
    assert response.status_code == 200
    assert client.get(url).get_json()[0]['shares'] == 2


def test_storage_write_failure_is_not_success(client, profile):
    with patch.object(db, 'upsert_holding', return_value=False):
        response = client.post('/api/holdings', json={
            'profile_id': profile['id'], 'ticker': 'AAPL', 'entry_price': 100, 'shares': 2})
    assert response.status_code == 503


def test_invalid_settings_do_not_mutate(client, profile):
    for value in (-1, 1.1, None, True, 'NaN'):
        response = client.put(f"/api/profiles/{profile['id']}", json={'min_buy_confidence': value})
        assert response.status_code == 400
    saved = next(p for p in client.get('/api/profiles').get_json() if p['id'] == profile['id'])
    assert saved['min_buy_confidence'] == 0.6


def test_invalid_holdings_do_not_mutate(client, profile):
    url = f"/api/profiles/{profile['id']}/holdings"
    for data in (
        {'ticker': '', 'entry_price': 100, 'shares': 1},
        {'ticker': '../BAD', 'entry_price': 100, 'shares': 1},
        {'ticker': 'AAPL', 'entry_price': 0, 'shares': 1},
        {'ticker': 'AAPL', 'entry_price': 100, 'shares': -1},
        {'ticker': 'AAPL', 'entry_price': 'NaN', 'shares': 1},
    ):
        assert client.post(url, json=data).status_code == 400
    assert client.get(url).get_json() == []


def test_unknown_profile_is_not_silently_replaced(client):
    assert client.get('/api/holdings?profile_id=999').status_code == 404
    assert client.get('/api/holdings?profile_id=invalid').status_code == 400


def test_explicit_empty_watchlist_is_preserved(client, profile):
    assert profile['watchlist'] == []
    assert client.get(f"/api/watchlist?profile_id={profile['id']}").get_json()['watchlist'] == []


def test_create_rejects_null_name_and_unknown_settings(client):
    assert client.post('/api/profiles', json={'name': None}).status_code == 400
    assert client.post('/api/profiles', json={'name': 'Typo', 'unknown': 12}).status_code == 400


def test_json_arrays_are_rejected_without_crash(client, profile):
    assert client.post('/api/profiles', json=[]).status_code == 400
    assert client.put(f"/api/profiles/{profile['id']}", json=[]).status_code == 400