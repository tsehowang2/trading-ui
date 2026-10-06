"""Profiles, ledgers and observed signal evidence; reads v1/v2, exports v3."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re

import db
import profile_store
import ledger
import signal_history
import paper
from uuid import uuid4
from validation import settings, holdings

FORMAT = 'tradingui-profiles'
VERSION = 4


def digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError):
        raise ValueError('Backup must contain finite JSON values') from None
    return hashlib.sha256(encoded).hexdigest()


def export_document(profile_id=None, state=None):
    state = profile_store.snapshot() if state is None else state
    profiles = state['profiles']
    if profile_id is not None:
        profiles = [p for p in profiles if p['id'] == profile_id]
        if not profiles:
            raise KeyError('Profile not found')
    documents = []
    source_accounts = state.get('accounts', [])
    if profile_id is not None:
        source_accounts = [a for a in source_accounts if a['profile_id'] == profile_id]
    accounts = ledger.validate_accounts(source_accounts)
    observed = signal_history.validate_history([r for r in state.get('signal_history', [])
                                                if profile_id is None or r['profile_id'] == profile_id])
    runs=paper.validate_runs([r for r in state.get('paper_runs',[]) if profile_id is None or r['profile_id']==profile_id],accounts)
    for profile in profiles:
        # Validate exports too; never label a corrupt/partial snapshot as valid.
        clean = settings({'name': profile['name'], **{k: profile[k] for k in db.DEFAULT_PROFILE_SETTINGS}})
        rows = holdings(state['holdings'].get(str(profile['id']), []))
        documents.append(dict(source_id=profile['id'], name=clean.pop('name'),
                              settings={k: str(v) if isinstance(v, (float, int)) else v for k, v in clean.items()},
                              holdings=[dict(h, entry_price=str(h['entry_price']), shares=str(h['shares'])) for h in rows],
                              accounts=[a for a in accounts if a['profile_id'] == profile['id']],
                              signal_history=[r for r in observed if r['profile_id'] == profile['id']],
                              paper_runs=[r for r in runs if r['profile_id']==profile['id']]))
    document = dict(format=FORMAT, schema_version=VERSION,
                    exported_at=datetime.now(timezone.utc).isoformat(),
                    scope='all_profiles' if profile_id is None else 'single_profile',
                    active_profile_id=state['active_profile_id'] if profile_id is None else profile_id,
                    profiles=documents)
    document['sha256'] = digest(document)
    return document


def validate_document(document):
    if not isinstance(document, dict):
        raise ValueError('Backup must be a JSON object')
    required = {'format', 'schema_version', 'exported_at', 'scope', 'active_profile_id', 'profiles', 'sha256'}
    if set(document) != required:
        raise ValueError('Backup has missing or unsupported fields; no data was imported')
    version = document['schema_version']
    if document['format'] != FORMAT or type(version) is not int or version not in (1,2,3,VERSION):
        raise ValueError('Unsupported backup format/version')
    checksum = document['sha256']
    payload = {k: v for k, v in document.items() if k != 'sha256'}
    if not isinstance(checksum, str) or not re.fullmatch(r'[a-f0-9]{64}', checksum) or not hmac.compare_digest(checksum, digest(payload)):
        raise ValueError('Backup checksum mismatch')
    try:
        timestamp = datetime.fromisoformat(document['exported_at'])
        if timestamp.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('exported_at must be a timezone-aware timestamp') from None
    profiles = document['profiles']
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 100:
        raise ValueError('Backup must contain between 1 and 100 profiles')
    if document['scope'] not in ('all_profiles', 'single_profile'):
        raise ValueError('Invalid backup scope')
    if document['scope'] == 'single_profile' and len(profiles) != 1:
        raise ValueError('Single-profile backup must contain one profile')
    clean, ids, names = [], set(), set()
    all_accounts = []
    for profile in profiles:
        fields = {'source_id', 'name', 'settings', 'holdings'} | ({'accounts'} if version >= 2 else set()) | ({'signal_history'} if version >= 3 else set())
        if version>=4:fields.add('paper_runs')
        if not isinstance(profile, dict) or set(profile) != fields:
            raise ValueError('Invalid profile structure')
        pid = profile['source_id']
        if type(pid) is not int or not 1 <= pid <= 2_147_483_647 or pid in ids:
            raise ValueError('Profile source IDs must be unique positive integers')
        ids.add(pid)
        values = profile['settings']
        if not isinstance(values, dict) or set(values) != set(db.DEFAULT_PROFILE_SETTINGS):
            raise ValueError('Backup must include all supported profile settings')
        values = settings({**values, 'name': profile['name']})
        if values['name'].casefold() in names:
            raise ValueError('Duplicate profile names in backup')
        names.add(values['name'].casefold())
        accounts = ledger.validate_accounts(profile.get('accounts', []))
        if any(a['profile_id'] != pid for a in accounts):
            raise ValueError('Account belongs to another profile')
        all_accounts.extend(accounts)
        observed = signal_history.validate_history(profile.get('signal_history', []))
        if any(r['profile_id'] != pid for r in observed):
            raise ValueError('Observed signal belongs to another profile')
        runs=paper.validate_runs(profile.get('paper_runs',[]),accounts)
        if any(r['profile_id']!=pid for r in runs):raise ValueError('Paper run belongs to another profile')
        clean.append(dict(id=pid, **values, holdings=holdings(profile['holdings']), accounts=accounts, signal_history=observed,paper_runs=runs))
    ledger.validate_accounts(all_accounts)
    if type(document['active_profile_id']) is not int or document['active_profile_id'] not in ids:
        raise ValueError('Active profile reference is missing')
    return clean


def preview(document):
    profiles = validate_document(document)
    return dict(schema_version=document['schema_version'], scope=document['scope'], exported_at=document['exported_at'],
                profiles=[dict(name=p['name'], holdings=len(p['holdings']), watchlist=len(p['watchlist']),
                               accounts=len(p['accounts']), events=sum(len(a['events']) for a in p['accounts']),
                               observations=len(p['signal_history'])) for p in profiles],
                total_holdings=sum(len(p['holdings']) for p in profiles),
                total_events=sum(len(a['events']) for p in profiles for a in p['accounts']),
                warnings=(['Version 1 contains no account history'] if document['schema_version'] == 1 else [])
                         + (['Backup contains no observed signal history'] if document['schema_version'] < 3 else [])
                         + (['Backup contains no automatic strategy progress'] if document['schema_version'] < 4 else []))


def import_document(document, mode='new', expected_state=None):
    profiles = validate_document(document)
    if mode not in ('new', 'restore'):
        raise ValueError('Import mode must be new or restore')
    if mode == 'restore' and document['scope'] != 'all_profiles':
        raise ValueError('Replacing all profiles requires an all-profiles backup')
    with profile_store.transaction(True, replace=mode == 'restore') as state:
        if expected_state is not None and digest(state) != expected_state:
            raise ValueError('Profiles changed after preview; download a fresh backup and preview again')
        if mode == 'restore':
            state['profiles'] = []
            state['holdings'] = {}
            state['accounts'] = []
            state['signal_history'] = []
            state['paper_runs'] = []
        if len(state['profiles']) + len(profiles) > 100:
            raise ValueError('Import would exceed the 100-profile backup limit')
        if len(state.get('accounts', [])) + sum(len(p['accounts']) for p in profiles) > 200:
            raise ValueError('Import would exceed the 200-account limit')
        names = {p['name'].casefold() for p in state['profiles']}
        next_id = max((p['id'] for p in state['profiles']), default=0) + 1
        mapping, imported = {}, []
        for incoming in profiles:
            p = deepcopy(incoming)
            rows = p.pop('holdings')
            accounts = p.pop('accounts')
            observed = p.pop('signal_history')
            runs=p.pop('paper_runs')
            source_id = p['id']
            base_name, name, suffix = p['name'], p['name'], 1
            while name.casefold() in names:
                label = f' (import {suffix})'
                name = base_name[:100 - len(label)] + label
                suffix += 1
            p['name'] = name
            p['id'] = source_id if mode == 'restore' else next_id
            next_id += 1
            names.add(name.casefold())
            mapping[str(source_id)] = p['id']
            imported.append(dict(id=p['id'], name=name))
            state['profiles'].append(p)
            state['holdings'][str(p['id'])] = rows
            account_remap={};event_remap={}
            for account in accounts:
                original_account=account['id']
                account['profile_id'] = p['id']
                if mode == 'new':
                    account['id'] = str(uuid4())
                    remap = {e['id']: str(uuid4()) for e in account['events']}
                    event_remap.update(remap)
                    for e in account['events']:
                        e['id'] = remap[e['id']]
                        if e['kind'] == 'VOID':
                            e['target_id'] = remap[e['target_id']]
                state.setdefault('accounts', []).append(account)
                account_remap[original_account]=account['id']
            for run in runs:
                run['profile_id']=p['id'];run['account_id']=account_remap[run['account_id']]
                if mode=='new':
                    run['id']=str(uuid4())
                    for order in run['orders']:
                        if order.get('event_id'):order['event_id']=event_remap[order['event_id']]
                state.setdefault('paper_runs',[]).append(run)
            for record in observed:
                record['profile_id'] = p['id']
                if mode == 'new':
                    record['id'] = str(uuid4())
                state.setdefault('signal_history', []).append(record)
        if mode == 'restore':
            state['active_profile_id'] = mapping[str(document['active_profile_id'])]
    return dict(imported=imported, id_mapping=mapping, mode=mode)