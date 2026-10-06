"""Per-symbol data cache; PostgreSQL when configured, atomic JSON locally.

Kept separate from profile transactions to avoid copying price history on every
trade write. Cache misses never masquerade as current market observations.
"""
from contextlib import contextmanager
import hashlib
import json
import os

from filelock import FileLock

import db
from profile_store import atomic_json


@contextmanager
def symbol_cache(symbol, directory):
    key = hashlib.sha256(symbol.encode()).hexdigest()
    if db.DATABASE_URL:
        conn = db.get_connection()
        try:
            with conn.cursor() as cur:
                lock = int(key[:15], 16)
                cur.execute('SELECT pg_advisory_xact_lock(%s)', (lock,))
                cur.execute('SELECT data FROM market_symbol_cache WHERE symbol = %s', (symbol,))
                row = cur.fetchone()
            document = dict(row['data']) if row else {}
            original = json.dumps(document, sort_keys=True, allow_nan=False)
            yield document
            updated = json.dumps(document, sort_keys=True, allow_nan=False)
            if updated != original:
                with conn.cursor() as cur:
                    cur.execute('''INSERT INTO market_symbol_cache (symbol,data) VALUES (%s,%s)
                        ON CONFLICT (symbol) DO UPDATE SET data=EXCLUDED.data''', (symbol, updated))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, key + '.json')
        with FileLock(path + '.lock', timeout=120):
            if os.path.exists(path):
                with open(path, encoding='utf-8') as source:
                    document = json.load(source)
            else:
                document = {}
            original = json.dumps(document, sort_keys=True, allow_nan=False)
            yield document
            if json.dumps(document, sort_keys=True, allow_nan=False) != original:
                atomic_json(path, document)