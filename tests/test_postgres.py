"""Opt-in only: TEST_DATABASE_URL must name a disposable PostgreSQL database.

Each test uses a uniquely named temporary schema and drops only that schema.
Never uses DATABASE_URL or accesses production tables.
"""
import os
import uuid

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
import pytest

import backup
import db
from migrations import migrate
import profile_store


@pytest.fixture
def postgres(monkeypatch):
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('No explicitly authorized disposable TEST_DATABASE_URL')
    schema = 'tradingui_test_' + uuid.uuid4().hex
    admin = psycopg2.connect(url)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    def connection():
        return psycopg2.connect(url, cursor_factory=RealDictCursor, options=f'-c search_path={schema}')
    monkeypatch.setattr(db, 'DATABASE_URL', url)
    monkeypatch.setattr(db, 'get_connection', connection)
    try:
        assert db.init_db()
        conn = connection()
        try:
            db._init_profile_tables(conn)
            migrate(conn)
            migrate(conn)
        finally:
            conn.close()
        yield
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


def test_postgres_crud_backup_restore_and_migration(postgres):
    profile = db.create_profile('Portable', {'watchlist': ['AAPL'], 'min_buy_confidence': 0})
    db.upsert_holding(profile['id'], dict(ticker='AAPL', entry_price=123.45, shares=2.5))
    document = backup.export_document()
    expected = profile_store.snapshot()
    # IDs with holes and renamed profiles used to collide on restore.
    unused = db.create_profile('Temporary')
    db.delete_profile(unused['id'])
    db.update_profile(1, {'name': 'Changed default'})
    imported = backup.import_document(document, mode='restore')
    restored = profile_store.snapshot()
    assert [p['name'] for p in restored['profiles']] == [p['name'] for p in expected['profiles']]
    restored_pid = imported['id_mapping'][str(profile['id'])]
    assert db.read_holdings_for_profile(restored_pid) == expected['holdings'][str(profile['id'])]
    assert next(p for p in db.list_profiles() if p['id'] == restored_pid)['min_buy_confidence'] == 0


def test_postgres_transaction_rolls_back(postgres):
    before = profile_store.snapshot()
    with pytest.raises(RuntimeError):
        with profile_store.transaction(True) as state:
            state['profiles'][0]['name'] = 'Never committed'
            raise RuntimeError('abort')
    assert profile_store.snapshot() == before