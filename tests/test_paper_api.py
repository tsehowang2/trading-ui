from unittest.mock import patch

import db
import paper
from test_paper import setup_run


def test_runtime_start_page_and_resume_api(client,profile):
    db.update_profile(profile['id'],{'watchlist':['AAA']})
    pid=profile['id'];url=f'/api/profiles/{pid}/paper'
    response=client.post(url,json={'start_date':'2024-01-02','initial_cash':'1000'})
    assert response.status_code==201
    assert response.get_json()['run']['start_date']=='2024-01-02'
    assert client.get('/paper').status_code==200
    assert client.get(url).get_json()['report']['cash']=='1000'
    assert client.post(url,json={'start_date':'2024-01-02','initial_cash':'1000'}).status_code==400
    assert client.post(url+'/pause',json={'paused':True}).status_code==200
    assert client.post(url+'/run',json={}).get_json()['paused']


def test_api_run_uses_saved_strategy_account(client,profile):
    run,frames,start,end,download=setup_run(client,profile)
    with patch('data.cached_download',side_effect=download):
        response=client.post(f"/api/profiles/{profile['id']}/paper/run",json={'end_date':end,'limit':30})
    assert response.status_code==200
    assert response.get_json()['remaining'] is False
    assert client.get(f"/api/profiles/{profile['id']}/paper").get_json()['run']['checkpoint']==end


def test_invalid_start_values_rejected(client,profile):
    pid=profile['id'];db.update_profile(pid,{'watchlist':['AAA']})
    for start in ('invalid','2099-01-01',None):
        assert client.post(f'/api/profiles/{pid}/paper',json={'start_date':start,'initial_cash':'1000'}).status_code==400


def test_catchup_target_before_start_rejected(client,profile):
    db.update_profile(profile['id'],{'watchlist':['AAA']})
    url=f"/api/profiles/{profile['id']}/paper"
    client.post(url,json={'start_date':'2024-01-02','initial_cash':'1000'})
    assert client.post(url+'/run',json={'end_date':'2023-01-01'}).status_code==400
    assert client.post(url+'/run',json={'end_date':123}).status_code==400