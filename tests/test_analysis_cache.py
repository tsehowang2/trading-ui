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