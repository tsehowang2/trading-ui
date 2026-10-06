from uuid import uuid4

import profile_store


def account(client, pid, kind='manual'):
    response = client.post(f'/api/profiles/{pid}/accounts', json={'kind': kind})
    assert response.status_code in (200, 201)
    return response.get_json()['id']


def row(kind, **values):
    return dict(id=str(uuid4()), kind=kind, occurred_at='2025-01-01T00:00:00Z', **values)


def test_complete_manual_flow(client, profile):
    pid = profile['id']; aid = account(client, pid)
    url = f'/api/profiles/{pid}/accounts/{aid}'
    funding = row('DEPOSIT', amount='1000')
    assert client.post(url + '/events', json=funding).status_code == 201
    assert client.post(url + '/events', json=funding).status_code == 200
    buy = row('BUY', ticker='AAPL', shares='2', price='100', fee='1', tag='strategy')
    assert client.post(url + '/events', json=buy).status_code == 201
    sale = row('SELL', ticker='AAPL', shares='1', price='120', fee='1')
    assert client.post(url + '/events', json=sale).status_code == 201
    report = client.post(url + '/report', json={'marks': {'AAPL': '130'}}).get_json()
    assert report['cash'] == '918'
    assert report['realized_pnl'] == '18.5'
    assert report['net_profit'] == '48'
    assert report['realized_by_entry_tag']['strategy'] == '18.5'
    assert report['account']['events'][0]['occurred_at'].endswith('+00:00')
    assert client.get('/trades').status_code == 200


def test_batch_repair_is_atomic(client, profile):
    pid = profile['id']; aid = account(client, pid)
    url = f'/api/profiles/{pid}/accounts/{aid}/events'
    deposit = row('DEPOSIT', amount='1000')
    buy = row('BUY', ticker='AAPL', shares='1', price='100')
    sell = row('SELL', ticker='AAPL', shares='1', price='150')
    assert client.post(url, json={'events': [deposit, buy, sell]}).status_code == 201
    correction = row('VOID', target_id=buy['id'], notes='Correct purchase cost')
    before = profile_store.snapshot()
    assert client.post(url, json=correction).status_code == 400
    assert profile_store.snapshot() == before
    replacement = row('BUY', ticker='AAPL', shares='1', price='110')
    # Explicit timestamp before the sell makes FIFO order unambiguous.
    replacement['occurred_at'] = '2024-12-31T23:59:59Z'
    deposit_replacement = row('DEPOSIT', amount='1000')
    deposit_replacement['occurred_at'] = '2024-12-30T00:00:00Z'
    void_deposit = row('VOID', target_id=deposit['id'], notes='Move original funding earlier')
    assert client.post(url, json={'events': [correction, void_deposit, deposit_replacement, replacement]}).status_code == 201
    report = client.get(f'/api/profiles/{pid}/accounts/{aid}/report').get_json()
    assert report['realized_pnl'] == '40'
    assert len(report['account']['events']) == 7


def test_accounts_are_profile_scoped_and_paper_cannot_manual_trade(client, profile):
    pid = profile['id']; aid = account(client, pid, 'paper')
    assert client.get(f'/api/profiles/1/accounts/{aid}/report').status_code == 404
    assert client.post(f'/api/profiles/{pid}/accounts/{aid}/events', json=row('DEPOSIT', amount='1000')).status_code == 201
    assert client.post(f'/api/profiles/{pid}/accounts/{aid}/events', json=row('BUY', ticker='AAPL', shares='1', price='100')).status_code == 400
    assert len(client.get(f'/api/profiles/{pid}/accounts').get_json()) == 1
    assert account(client, pid, 'paper') == aid
    response = client.post(f'/api/profiles/{pid}/accounts', json={'kind': 'paper'})
    assert response.status_code == 200
    assert response.get_json()['created'] is False


def test_bad_report_marks_and_ledger_payloads(client, profile):
    pid = profile['id']; aid = account(client, pid)
    url = f'/api/profiles/{pid}/accounts/{aid}'
    assert client.post(url + '/report', json={'marks': {'AAPL': 'NaN'}}).status_code == 400
    assert client.post(url + '/report', json={'marks': []}).status_code == 400
    assert client.post(url + '/events', json={'events': []}).status_code == 400
    assert client.post(url + '/events', json=row('SELL', ticker='AAPL', shares='1', price='100')).status_code == 400
    assert client.get(f'/api/profiles/{pid}/accounts/not-a-uuid/report').status_code == 400


def test_profile_delete_removes_only_its_accounts(client, profile):
    aid = account(client, profile['id'])
    keep = account(client, 1)
    assert client.delete(f"/api/profiles/{profile['id']}").status_code == 200
    ids = {a['id'] for a in profile_store.snapshot()['accounts']}
    assert aid not in ids and keep in ids


def test_paper_funding_correction_preserves_history(client, profile):
    pid = profile['id']; aid = account(client, pid, 'paper')
    url = f'/api/profiles/{pid}/accounts/{aid}'
    deposit = row('DEPOSIT', amount='1000')
    client.post(url + '/events', json=deposit)
    correction = row('VOID', target_id=deposit['id'], notes='Wrong initial funding')
    assert client.post(url + '/events', json=correction).status_code == 201
    report = client.get(url + '/report').get_json()
    assert report['cash'] == '0'
    assert len(report['account']['events']) == 2