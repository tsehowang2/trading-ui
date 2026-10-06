from copy import deepcopy
from unittest.mock import patch

import backup
import db
import profile_store


def exported(client, profile):
    client.post(f"/api/profiles/{profile['id']}/holdings", json={
        'ticker': 'AAPL', 'entry_price': 123.45, 'shares': 2.5, 'notes': 'cost basis only'})
    return client.get('/api/backup/export').get_json()


def preview(client, document):
    response = client.post('/api/backup/preview', json={'document': document})
    assert response.status_code == 200
    return response.get_json()['preview_token']


def test_export_has_all_settings_holdings_and_no_secrets(client, profile):
    document = exported(client, profile)
    assert document['scope'] == 'all_profiles'
    assert document['schema_version'] == 4
    item = next(p for p in document['profiles'] if p['name'] == 'Test')
    assert set(item['settings']) == set(db.DEFAULT_PROFILE_SETTINGS)
    assert item['holdings'][0]['entry_price'] == '123.45'
    assert 'DATABASE_URL' not in str(document)
    assert 'secret' not in str(document).lower()
    assert backup.validate_document(document)


def test_import_as_new_preserves_existing_profiles_and_holdings(client, profile):
    document = exported(client, profile)
    before = profile_store.snapshot()
    token = preview(client, document)
    response = client.post('/api/backup/import', json={'document': document, 'preview_token': token})
    assert response.status_code == 200
    after = profile_store.snapshot()
    assert after['profiles'][:len(before['profiles'])] == before['profiles']
    assert after['holdings'][str(profile['id'])] == before['holdings'][str(profile['id'])]
    mapping = response.get_json()['id_mapping']
    assert after['holdings'][str(mapping[str(profile['id'])])] == before['holdings'][str(profile['id'])]
    assert len({p['name'].casefold() for p in after['profiles']}) == len(after['profiles'])
    assert client.post('/api/backup/import', json={'document': document, 'preview_token': token}).status_code == 400


def test_invalid_document_never_mutates(client, profile):
    document = exported(client, profile)
    before = profile_store.snapshot()
    document['profiles'][0]['settings']['capital'] = '-1'
    assert client.post('/api/backup/preview', json={'document': document}).status_code == 400
    payload = {k: v for k, v in document.items() if k != 'sha256'}
    document['sha256'] = backup.digest(payload)
    assert client.post('/api/backup/preview', json={'document': document}).status_code == 400
    assert profile_store.snapshot() == before


def test_future_version_rejected(client, profile):
    document = exported(client, profile)
    document['schema_version'] = 5
    assert client.post('/api/backup/preview', json={'document': document}).status_code == 400


def test_import_rollback_on_local_write_failure(client, profile):
    document = exported(client, profile)
    before = profile_store.snapshot()
    with patch.object(profile_store, 'atomic_json', side_effect=OSError('disk unavailable')):
        try:
            backup.import_document(document)
            assert False, 'Expected storage failure'
        except db.StorageError:
            pass
    assert profile_store.snapshot() == before


def test_restore_requires_full_backup_and_confirmation(client, profile):
    document = exported(client, profile)
    token = preview(client, document)
    response = client.post('/api/backup/import', json={
        'document': document, 'preview_token': token, 'mode': 'restore'})
    assert response.status_code == 400


def test_changed_state_blocks_import(client, profile):
    document = exported(client, profile)
    token = preview(client, document)
    client.put(f"/api/profiles/{profile['id']}", json={'capital': 9999})
    response = client.post('/api/backup/import', json={'document': document, 'preview_token': token})
    assert response.status_code == 400


def test_large_upload_rejected(client):
    response = client.post('/api/backup/preview', data=b'x' * (5 * 1024 * 1024 + 1), content_type='application/json')
    assert response.status_code == 413


def test_full_restore_roundtrip(client, profile):
    document = exported(client, profile)
    token = preview(client, document)
    response = client.post('/api/backup/import', json={
        'document': document, 'preview_token': token, 'mode': 'restore',
        'confirmation': 'REPLACE ALL PROFILES'})
    assert response.status_code == 200
    restored = client.get('/api/backup/export').get_json()
    for original, actual in zip(document['profiles'], restored['profiles']):
        assert actual['name'] == original['name']
        assert actual['settings'] == original['settings']
        assert actual['holdings'] == original['holdings']


def test_restore_without_current_download_rejected(client, profile):
    document = backup.export_document()
    token = preview(client, document)
    response = client.post('/api/backup/import', json={
        'document': document, 'preview_token': token, 'mode': 'restore',
        'confirmation': 'REPLACE ALL PROFILES'})
    assert response.status_code == 400


def test_v1_backup_remains_importable(client, profile):
    document = exported(client, profile)
    document['schema_version'] = 1
    for p in document['profiles']:
        p.pop('accounts')
        p.pop('signal_history')
        p.pop('paper_runs')
    document['sha256'] = backup.digest({k: v for k, v in document.items() if k != 'sha256'})
    summary = backup.preview(document)
    assert 'Version 1 contains no account history' in summary['warnings']
    assert backup.import_document(document)['imported']


def test_ledger_history_roundtrip_and_new_import_ids(client, profile):
    import ledger
    from uuid import uuid4
    pid = profile['id']
    account = ledger.create_account(pid, 'manual')
    def add(kind, **values):
        e = dict(id=str(uuid4()), kind=kind, occurred_at='2025-01-01T00:00:00Z', **values)
        ledger.add_event(pid, account['id'], e)
        return e
    add('DEPOSIT', amount='1000.01')
    buy = add('BUY', ticker='AAPL', shares='1', price='100.02', fee='0.03')
    add('VOID', target_id=buy['id'], notes='Correction fixture')
    document = backup.export_document()
    imported = backup.import_document(document)
    new_pid = imported['id_mapping'][str(pid)]
    new_account = next(a for a in profile_store.snapshot()['accounts'] if a['profile_id'] == new_pid)
    assert new_account['id'] != account['id']
    assert new_account['events'][1]['id'] != buy['id']
    assert new_account['events'][2]['target_id'] == new_account['events'][1]['id']
    assert ledger.report(new_pid, new_account['id'])['cash'] == '1000.01'
    backup.import_document(document, mode='restore')
    assert next(a for a in profile_store.snapshot()['accounts'] if a['id'] == account['id'])['events'] == ledger.validate_accounts([
        next(p for p in document['profiles'] if p['source_id'] == pid)['accounts'][0]])[0]['events']


def test_invalid_ledger_backup_rejected_atomically(client, profile):
    import ledger
    from uuid import uuid4
    account = ledger.create_account(profile['id'], 'manual')
    document = backup.export_document()
    item = next(p for p in document['profiles'] if p['source_id'] == profile['id'])
    item['accounts'][0]['events'] = [dict(id=str(uuid4()), kind='SELL', occurred_at='2025-01-01T00:00:00+00:00',
        recorded_at='2025-01-01T00:00:00+00:00', sequence=1, ticker='AAPL', shares='1', price='100', fee='0', tag='strategy', notes='')]
    document['sha256'] = backup.digest({k: v for k, v in document.items() if k != 'sha256'})
    before = profile_store.snapshot()
    assert client.post('/api/backup/preview', json={'document': document}).status_code == 400
    assert profile_store.snapshot() == before


def test_restore_preserves_profile_ids_with_gaps(client, profile):
    disposable = db.create_profile('Disposable')
    final = db.create_profile('After gap')
    db.delete_profile(disposable['id'])
    document = backup.export_document()
    backup.import_document(document, mode='restore')
    assert [p['id'] for p in profile_store.snapshot()['profiles']] == [p['source_id'] for p in document['profiles']]
    assert final['id'] in {p['id'] for p in db.list_profiles()}


def test_non_ascii_checksum_is_client_error(client, profile):
    document = exported(client, profile)
    document['sha256'] = '非ASCII'
    assert client.post('/api/backup/preview', json={'document': document}).status_code == 400


def test_profile_export_does_not_validate_unrelated_account(client, profile):
    state = profile_store.snapshot()
    state['accounts'] = [{'profile_id': 1, 'invalid': 'unrelated corruption'}]
    document = backup.export_document(profile['id'], state)
    assert document['profiles'][0]['source_id'] == profile['id']