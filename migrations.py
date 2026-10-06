"""Additive, serialized PostgreSQL migrations for existing Render databases."""


def migrate(conn):
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(74629101)')
            cur.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)')
            cur.execute('SELECT version FROM schema_migrations WHERE version = 1')
            if not cur.fetchone():
                cur.execute('''CREATE TABLE IF NOT EXISTS profile_analysis_cache (
                    profile_id INTEGER PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
                    data JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )''')
                cur.execute('INSERT INTO schema_migrations (version) VALUES (1)')
            cur.execute('SELECT version FROM schema_migrations WHERE version = 2')
            if not cur.fetchone():
                cur.execute('''CREATE TABLE IF NOT EXISTS profile_account_state (
                    profile_id INTEGER PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
                    data JSONB NOT NULL DEFAULT '[]'::jsonb
                )''')
                cur.execute('INSERT INTO schema_migrations (version) VALUES (2)')
            cur.execute('SELECT version FROM schema_migrations WHERE version = 3')
            if not cur.fetchone():
                cur.execute('CREATE TABLE IF NOT EXISTS market_symbol_cache (symbol TEXT PRIMARY KEY, data JSONB NOT NULL)')
                cur.execute('''CREATE TABLE IF NOT EXISTS profile_signal_history (
                    profile_id INTEGER PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
                    data JSONB NOT NULL DEFAULT '[]'::jsonb
                )''')
                cur.execute('INSERT INTO schema_migrations (version) VALUES (3)')
            cur.execute('SELECT version FROM schema_migrations WHERE version = 4')
            if not cur.fetchone():
                cur.execute('''CREATE TABLE IF NOT EXISTS profile_paper_state (
                    profile_id INTEGER PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
                    data JSONB NOT NULL
                )''')
                cur.execute('INSERT INTO schema_migrations (version) VALUES (4)')
        conn.commit()
    except Exception:
        conn.rollback()
        raise