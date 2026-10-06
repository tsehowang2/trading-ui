"""Disposable profile-scoped cache. Never a fallback for authoritative data."""
from contextlib import closing
import json
import os

import db
from profile_store import atomic_json


def read(profile_id, path):
    if db.DATABASE_URL:
        with closing(db.get_connection()) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT data FROM profile_analysis_cache WHERE profile_id = %s', (profile_id,))
                row = cur.fetchone()
            return dict(row['data']) if row else None
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as source:
        return json.load(source)


def write(profile_id, data, path):
    if db.DATABASE_URL:
        with closing(db.get_connection()) as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute('''INSERT INTO profile_analysis_cache (profile_id, data)
                        VALUES (%s, %s) ON CONFLICT (profile_id) DO UPDATE SET
                        data=EXCLUDED.data, updated_at=CURRENT_TIMESTAMP''',
                                (profile_id, json.dumps(data, allow_nan=False)))
    else:
        atomic_json(path, data)