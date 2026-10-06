from datetime import datetime, timezone
from unittest.mock import patch

import app as web
import main


def run(client, pid):
    with patch.object(web, 'get_portfolio_data', return_value={'held': []}) as calculate:
        response = client.post(f'/api/dashboard/refresh?profile_id={pid}')
    assert response.status_code == 200
    return calculate.call_args.kwargs


def test_refresh_passes_settings_and_rows_without_temp_file(client, profile, tmp_path):
    pid = profile['id']
    client.put(f'/api/profiles/{pid}', json={'min_profit_for_pyramid': 12, 'min_cushion_for_pyramid': 15, 'min_buy_confidence': .2})
    args = run(client, pid)
    assert args['min_profit_for_pyramid'] == .12
    assert args['min_cushion_for_pyramid'] == .15
    assert args['min_buy_confidence'] == .2
    assert args['holdings_rows'] == []
    assert args['holdings_csv'] == ''


def test_refresh_get_is_not_mutating(client, profile):
    assert client.get(f"/api/dashboard/refresh?profile_id={profile['id']}").status_code == 405


def test_profiles_have_independent_cache_and_invalidation(client, profile):
    first, second = 1, profile['id']
    run(client, first)
    run(client, second)
    for pid in (first, second):
        response = client.get(f'/api/dashboard/cached?profile_id={pid}').get_json()
        assert response['success']
        assert response['data']['_profile_id'] == pid
    client.put(f'/api/profiles/{second}', json={'capital': 7777})
    assert client.get(f'/api/dashboard/cached?profile_id={first}').get_json()['success']
    assert not client.get(f'/api/dashboard/cached?profile_id={second}').get_json()['success']


def test_old_date_cache_not_silently_served(client, profile):
    import json
    pid = profile['id']
    run(client, pid)
    path = web._profile_cache_path(pid)
    with open(path, encoding='utf-8') as source:
        data = json.load(source)
    data['_completed_session'] = '2000-01-01'
    with open(path, 'w', encoding='utf-8') as out:
        json.dump(data, out)
    assert not client.get(f'/api/dashboard/cached?profile_id={pid}').get_json()['success']


def test_changed_profile_during_calculation_rejected(client, profile):
    import db
    pid = profile['id']
    def calculate(**kwargs):
        db.update_profile(pid, {'capital': 7777})
        return {'held': []}
    with patch.object(web, 'get_portfolio_data', side_effect=calculate):
        response = client.post(f'/api/dashboard/refresh?profile_id={pid}')
    assert response.status_code == 409


def test_portfolio_calculation_does_not_write_shared_cache(client, tmp_path):
    with patch.object(main, 'RESULTS_DIR', str(tmp_path / 'results')):
        result = main.get_portfolio_data('', [], 1000, holdings_rows=[])
    assert result['held'] == []
    assert not (tmp_path / 'results' / 'portfolio_cache.json').exists()


def test_real_refresh_reuses_benchmarks_and_saved_market_data(client, profile, tmp_path, monkeypatch):
    import pandas as pd
    import data
    import market_cache
    import market_sessions
    from test_market_data import bars

    end = '2025-07-07'
    monkeypatch.setattr(data, 'DATA_CACHE_DIR', str(tmp_path / 'market'))
    monkeypatch.setattr(data, 'latest_completed_session', lambda: end)
    monkeypatch.setattr(web, 'latest_completed_session', lambda: end)
    monkeypatch.setattr(market_sessions, 'latest_completed_session', lambda: end)
    pid = profile['id']
    client.put(f'/api/profiles/{pid}', json={'watchlist': ['AAPL', 'MSFT']})

    def provider(symbol, **kwargs):
        dates = market_sessions.session_dates(kwargs['start'], end)
        return bars(dates, price=20 if symbol.startswith('^VIX') else 100)

    with patch.object(data.yf, 'download', side_effect=provider) as download, \
            patch.object(data, 'symbol_cache', wraps=data.symbol_cache) as cache:
        first = client.post(f'/api/dashboard/refresh?profile_id={pid}')
    assert first.status_code == 200 and first.json['success'], first.json
    assert download.call_count == 5  # two stocks + three shared benchmarks
    for symbol in ('SPY', '^VIX', '^VIX3M'):
        assert sum(call.args[0] == symbol for call in cache.call_args_list) == 1
    assert all(pd.Timestamp(end) - pd.Timestamp(call.kwargs['start']) < pd.Timedelta(days=800)
               for call in download.call_args_list)

    with patch.object(data.yf, 'download', side_effect=AssertionError('No network on cache hit')) as download, \
            patch.object(market_cache, 'atomic_json', wraps=market_cache.atomic_json) as write:
        second = client.post(f'/api/dashboard/refresh?profile_id={pid}')
    assert second.status_code == 200 and second.json['success'], second.json
    assert download.call_count == 0
    assert write.call_count == 0


def test_real_refresh_rate_limited_stock_is_not_a_fresh_signal(client, profile, tmp_path, monkeypatch):
    import data
    import market_sessions
    from test_market_data import bars

    end = '2025-07-07'
    monkeypatch.setattr(data, 'DATA_CACHE_DIR', str(tmp_path / 'market'))
    monkeypatch.setattr(data, 'latest_completed_session', lambda: end)
    monkeypatch.setattr(web, 'latest_completed_session', lambda: end)
    monkeypatch.setattr(market_sessions, 'latest_completed_session', lambda: end)
    pid = profile['id']
    client.put(f'/api/profiles/{pid}', json={'watchlist': ['SOXL', 'AAPL']})

    def provider(symbol, **kwargs):
        if symbol == 'SOXL':
            raise RuntimeError('Too Many Requests')
        return bars(market_sessions.session_dates(kwargs['start'], end),
                    price=20 if symbol.startswith('^VIX') else 100)

    with patch.object(data.yf, 'download', side_effect=provider) as download:
        first = client.post(f'/api/dashboard/refresh?profile_id={pid}')
        second = client.post(f'/api/dashboard/refresh?profile_id={pid}')
    assert first.json['success'] and second.json['success']
    assert {'ticker': 'SOXL', 'reason': 'data error'} in first.json['data']['no_signals']
    assert sum(call.args[0] == 'SOXL' for call in download.call_args_list) == 1