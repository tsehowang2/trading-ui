from copy import deepcopy

import backup
import profile_store
import signal_history
import strategy


def indicator():
    return dict(symbol='AAPL',date='2025-01-02',entry_ok=True,fresh_entry=True,
                entry_reason='BUY — normal-gate',close=100,score=.7,input_revision='a'*64)


def test_observation_idempotency_and_revisions_preserve_old_record(client, profile):
    pid=profile['id'];ind=indicator()
    first=signal_history.record_indicators(pid,[ind])
    assert len(first)==1
    assert signal_history.record_indicators(pid,[ind])==[]
    changed=dict(ind,input_revision='b'*64,entry_ok=False,entry_reason='NO ENTRY',fresh_entry=False)
    signal_history.record_indicators(pid,[changed])
    records=client.get(f'/api/profiles/{pid}/signals?ticker=AAPL').get_json()
    assert len(records)==2
    assert records[0]==first[0]
    assert records[0]['entry_ok'] is True and records[1]['entry_ok'] is False


def test_observed_signal_backup_roundtrip(client, profile):
    pid=profile['id']
    signal_history.record_indicators(pid,[indicator()])
    doc=backup.export_document()
    assert doc['schema_version']==4
    result=backup.import_document(doc)
    imported_pid=result['id_mapping'][str(pid)]
    records=[r for r in profile_store.snapshot()['signal_history'] if r['profile_id']==imported_pid]
    assert len(records)==1
    assert records[0]['input_revision']=='a'*64
    assert records[0]['id']!=next(r for r in profile_store.snapshot()['signal_history'] if r['profile_id']==pid)['id']
    backup.import_document(doc,mode='restore')
    assert len(profile_store.snapshot()['signal_history'])==1


def test_v2_backups_import_without_observed_history(client, profile):
    doc=backup.export_document()
    doc['schema_version']=2
    for p in doc['profiles']:
        p.pop('signal_history');p.pop('paper_runs')
    doc['sha256']=backup.digest({k:v for k,v in doc.items() if k!='sha256'})
    assert backup.validate_document(doc)
    assert 'Backup contains no observed signal history' in backup.preview(doc)['warnings']