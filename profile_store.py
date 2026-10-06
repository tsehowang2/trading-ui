"""Transactional profile repository, shared by CRUD and backup/restore.

PostgreSQL is authoritative when configured. Local mode uses one atomic document
and a cross-process lock; legacy JSON files are read only during first migration.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import tempfile

from filelock import FileLock

from validation import settings, holdings


class StorageError(RuntimeError):
    pass


def _db():
    import db
    return db


def atomic_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.profile-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, indent=2, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


def _legacy_state():
    db = _db()
    if os.path.exists(db.PROFILES_JSON_PATH):
        with open(db.PROFILES_JSON_PATH, encoding='utf-8') as source:
            profiles = json.load(source)
    else:
        profiles = [{'id': 1, 'name': 'Default', **deepcopy(db.DEFAULT_PROFILE_SETTINGS)}]
    active = profiles[0]['id']
    if os.path.exists(db.ACTIVE_PROFILE_JSON_PATH):
        with open(db.ACTIVE_PROFILE_JSON_PATH, encoding='utf-8') as source:
            active = json.load(source)['active_profile_id']
    positions = {}
    for profile in profiles:
        profile.pop('is_active', None)
        profile.update({k: deepcopy(v) for k, v in db.DEFAULT_PROFILE_SETTINGS.items() if k not in profile})
        path = db._holdings_path_for(profile['id'])
        if os.path.exists(path):
            with open(path, encoding='utf-8') as source:
                positions[str(profile['id'])] = json.load(source)
        else:
            positions[str(profile['id'])] = []
    return dict(version=3, profiles=profiles, holdings=positions, active_profile_id=active, accounts=[], signal_history=[])


def _read_pg(conn):
    db = _db()
    with conn.cursor() as cur:
        columns = ', '.join(['id', 'name', *db.DEFAULT_PROFILE_SETTINGS])
        cur.execute(f'SELECT {columns} FROM profiles ORDER BY id')
        profiles = [db._profile_row_to_dict(row, -1) for row in cur.fetchall()]
        for profile in profiles:
            profile.pop('is_active')
        cur.execute('SELECT profile_id, ticker, entry_price, shares, notes FROM holdings ORDER BY profile_id, ticker')
        positions = {str(p['id']): [] for p in profiles}
        for row in cur.fetchall():
            positions[str(row['profile_id'])].append(dict(
                ticker=row['ticker'], entry_price=float(row['entry_price']),
                shares=float(row['shares']), notes=row['notes'] or ''))
        active = db._get_active_profile_id_db_conn(conn)
        cur.execute('SELECT data FROM profile_account_state ORDER BY profile_id')
        accounts = [account for row in cur.fetchall() for account in row['data']]
        cur.execute('SELECT data FROM profile_signal_history ORDER BY profile_id')
        signals = [record for row in cur.fetchall() for record in row['data']]
        cur.execute('SELECT data FROM profile_paper_state ORDER BY profile_id')
        runs = [row['data'] for row in cur.fetchall()]
    return dict(version=4, profiles=profiles, holdings=positions, active_profile_id=active, accounts=accounts, signal_history=signals, paper_runs=runs)


def _write_pg(conn, state, replace=False):
    db = _db()
    keys = ['name', *db.DEFAULT_PROFILE_SETTINGS]
    with conn.cursor() as cur:
        ids = [p['id'] for p in state['profiles']]
        if replace:
            # Delete before reassigning IDs/names: old unique names must not
            # collide with profiles arriving under newly mapped IDs.
            cur.execute('DELETE FROM profiles')
        else:
            cur.execute('DELETE FROM profiles WHERE NOT (id = ANY(%s))', (ids,))
        for p in state['profiles']:
            values = [json.dumps(p[k]) if k == 'watchlist' else p[k] for k in keys]
            cur.execute(
                f"INSERT INTO profiles (id, {', '.join(keys)}) VALUES ({', '.join(['%s'] * (len(keys) + 1))}) "
                f"ON CONFLICT (id) DO UPDATE SET {', '.join(k + '=EXCLUDED.' + k for k in keys)}",
                (p['id'], *values))
            cur.execute('DELETE FROM holdings WHERE profile_id = %s', (p['id'],))
            for h in state['holdings'].get(str(p['id']), []):
                cur.execute('INSERT INTO holdings (profile_id,ticker,entry_price,shares,notes) VALUES (%s,%s,%s,%s,%s)',
                            (p['id'], h['ticker'], h['entry_price'], h['shares'], h.get('notes', '')))
            accounts = [a for a in state.get('accounts', []) if a['profile_id'] == p['id']]
            cur.execute('''INSERT INTO profile_account_state (profile_id,data) VALUES (%s,%s)
                ON CONFLICT (profile_id) DO UPDATE SET data=EXCLUDED.data''',
                        (p['id'], json.dumps(accounts, allow_nan=False)))
            signals = [s for s in state.get('signal_history', []) if s['profile_id'] == p['id']]
            cur.execute('''INSERT INTO profile_signal_history (profile_id,data) VALUES (%s,%s)
                ON CONFLICT (profile_id) DO UPDATE SET data=EXCLUDED.data''', (p['id'],json.dumps(signals,allow_nan=False)))
        cur.execute("INSERT INTO app_settings (key,value) VALUES ('active_profile_id',%s) "
                    'ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value', (str(state['active_profile_id']),))
        cur.execute('DELETE FROM profile_paper_state')
        for run in state.get('paper_runs',[]):
            cur.execute('INSERT INTO profile_paper_state (profile_id,data) VALUES (%s,%s)',(run['profile_id'],json.dumps(run,allow_nan=False)))
        cur.execute("SELECT setval(pg_get_serial_sequence('profiles','id'), (SELECT MAX(id) FROM profiles))")


@contextmanager
def transaction(write=False, replace=False):
    db = _db()
    conn = None
    try:
        if db.DATABASE_URL:
            conn = db.get_connection()
            with conn.cursor() as cur:
                # Serialize all repository writers and exports across Gunicorn workers.
                cur.execute('SELECT pg_advisory_xact_lock(74629101)')
            state = _read_pg(conn)
            yield state
            if write:
                _write_pg(conn, state, replace=replace)
            conn.commit()
        else:
            path = os.path.join(db._HERE_DB, 'profiles_state.json')
            with FileLock(path + '.lock', timeout=15):
                if os.path.exists(path):
                    with open(path, encoding='utf-8') as source:
                        state = json.load(source)
                else:
                    state = _legacy_state()
                    atomic_json(path, state)
                state.setdefault('accounts', [])
                state.setdefault('signal_history', [])
                state.setdefault('paper_runs', [])
                state['version'] = 4
                yield state
                if write:
                    atomic_json(path, state)
    except json.JSONDecodeError as error:
        if conn is not None:
            conn.rollback()
        raise StorageError('Profile data is corrupt; no default data was substituted') from error
    except (ValueError, KeyError):
        if conn is not None:
            conn.rollback()
        raise
    except Exception as error:
        if conn is not None:
            conn.rollback()
        raise StorageError('Profile storage unavailable; no fallback data was used') from error
    finally:
        if conn is not None:
            conn.close()


def snapshot():
    with transaction() as state:
        return deepcopy(state)


def _profile(state, profile_id):
    p = next((p for p in state['profiles'] if p['id'] == profile_id), None)
    if p is None:
        raise KeyError('Profile not found')
    return p


def list_profiles():
    state = snapshot()
    return [dict(p, is_active=p['id'] == state['active_profile_id']) for p in state['profiles']]


def get_active_profile_id():
    return snapshot()['active_profile_id']


def get_active_profile():
    state = snapshot()
    return deepcopy(_profile(state, state['active_profile_id']))


def set_active_profile(profile_id):
    with transaction(True) as state:
        _profile(state, profile_id)
        state['active_profile_id'] = profile_id
    return True


def create_profile(name, values=None):
    db = _db()
    clean = settings({'name': name, **(values or {})})
    with transaction(True) as state:
        if any(p['name'].casefold() == clean['name'].casefold() for p in state['profiles']):
            raise ValueError('A profile with that name already exists')
        if len(state['profiles']) >= 100:
            raise ValueError('Maximum 100 profiles supported')
        p = dict(deepcopy(db.DEFAULT_PROFILE_SETTINGS), **clean)
        p['id'] = max(p['id'] for p in state['profiles']) + 1
        state['profiles'].append(p)
        state['holdings'][str(p['id'])] = []
    return deepcopy(p)


def update_profile(profile_id, updates):
    clean = settings(updates)
    with transaction(True) as state:
        p = _profile(state, profile_id)
        if 'name' in clean and any(other['id'] != profile_id and other['name'].casefold() == clean['name'].casefold()
                                   for other in state['profiles']):
            raise ValueError('A profile with that name already exists')
        p.update(clean)
    return True


def delete_profile(profile_id):
    with transaction(True) as state:
        _profile(state, profile_id)
        if len(state['profiles']) <= 1:
            raise ValueError('Cannot delete the only profile')
        state['profiles'] = [p for p in state['profiles'] if p['id'] != profile_id]
        state['holdings'].pop(str(profile_id), None)
        state['accounts'] = [a for a in state.get('accounts', []) if a['profile_id'] != profile_id]
        state['signal_history'] = [s for s in state.get('signal_history', []) if s['profile_id'] != profile_id]
        state['paper_runs'] = [r for r in state.get('paper_runs',[]) if r['profile_id'] != profile_id]
        if state['active_profile_id'] == profile_id:
            state['active_profile_id'] = state['profiles'][0]['id']
    return True


def read_holdings_for_profile(profile_id):
    state = snapshot()
    _profile(state, profile_id)
    return deepcopy(state['holdings'].get(str(profile_id), []))


def write_holdings_for_profile(profile_id, rows):
    clean = holdings(rows)
    with transaction(True) as state:
        _profile(state, profile_id)
        state['holdings'][str(profile_id)] = clean
    return True


def upsert_holding(profile_id, row):
    with transaction(True) as state:
        _profile(state, profile_id)
        rows = state['holdings'].get(str(profile_id), [])
        from validation import ticker
        symbol = ticker(row.get('ticker'))
        old = next((h for h in rows if h['ticker'] == symbol), {})
        clean = holdings([{**old, **row, 'ticker': symbol}])[0]
        state['holdings'][str(profile_id)] = [h for h in rows if h['ticker'] != symbol] + [clean]
    return True


def delete_holding_for_profile(profile_id, ticker):
    from validation import ticker as validate_ticker
    symbol = validate_ticker(ticker)
    with transaction(True) as state:
        _profile(state, profile_id)
        state['holdings'][str(profile_id)] = [h for h in state['holdings'].get(str(profile_id), []) if h['ticker'] != symbol]
    return True