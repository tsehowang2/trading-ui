"""
Database module for persistent holdings storage using PostgreSQL.
Falls back to JSON file storage if DATABASE_URL is not set.
"""
import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import List, Dict, Optional

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_connection():
    """Get PostgreSQL connection. Returns None if DATABASE_URL not set."""
    if not DATABASE_URL:
        return None
    # Render/Heroku use postgres:// but psycopg2 needs postgresql://
    url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    return psycopg2.connect(url, cursor_factory=RealDictCursor)

def init_db():
    """Initialize database tables if they don't exist."""
    if not DATABASE_URL:
        return False
    
    try:
        conn = get_connection()
        if not conn:
            return False
        
        with conn.cursor() as cur:
            # Holdings table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS holdings (
                    ticker VARCHAR(20) PRIMARY KEY,
                    entry_price NUMERIC(12, 4) NOT NULL,
                    shares NUMERIC(12, 4) NOT NULL,
                    notes TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Portfolio cache table (JSONB for flexible schema)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_cache (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    data JSONB NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT single_row CHECK (id = 1)
                )
            """)
            
            conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Init error: {e}")
        return False

def read_holdings_db() -> List[Dict]:
    """Read holdings from PostgreSQL. Returns empty list if DB not available."""
    if not DATABASE_URL:
        return []
    
    try:
        conn = get_connection()
        if not conn:
            return []
        
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, entry_price, shares, notes FROM holdings ORDER BY ticker")
            rows = cur.fetchall()
        conn.close()
        
        # Convert Decimal to float for JSON serialization
        result = []
        for row in rows:
            result.append({
                'ticker': str(row['ticker']),
                'entry_price': float(row['entry_price']),
                'shares': float(row['shares']),
                'notes': str(row['notes'] or '')
            })
        
        print(f"[DB] ✓ Read {len(result)} holdings from PostgreSQL")
        return result
    except Exception as e:
        print(f"[DB] Read error: {e}")
        import traceback
        traceback.print_exc()
        return []

def write_holdings_db(holdings: List[Dict]) -> bool:
    """Write holdings to PostgreSQL. Returns success status."""
    if not DATABASE_URL:
        return False
    
    try:
        conn = get_connection()
        if not conn:
            return False
        
        with conn.cursor() as cur:
            # Clear existing holdings
            cur.execute("DELETE FROM holdings")
            
            # Insert new holdings
            for h in holdings:
                ticker = str(h.get('ticker', '')).strip().upper()
                if not ticker:
                    continue
                
                cur.execute("""
                    INSERT INTO holdings (ticker, entry_price, shares, notes)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (ticker) DO UPDATE SET
                        entry_price = EXCLUDED.entry_price,
                        shares = EXCLUDED.shares,
                        notes = EXCLUDED.notes,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    ticker,
                    float(h.get('entry_price', 0)),
                    float(h.get('shares', 0)),
                    str(h.get('notes', ''))
                ))
            
            conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Write error: {e}")
        return False

def delete_holding_db(ticker: str) -> bool:
    """Delete a single holding from PostgreSQL."""
    if not DATABASE_URL:
        return False
    
    try:
        conn = get_connection()
        if not conn:
            return False
        
        with conn.cursor() as cur:
            cur.execute("DELETE FROM holdings WHERE ticker = %s", (ticker.upper(),))
            conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Delete error: {e}")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# Portfolio Cache Operations
# ══════════════════════════════════════════════════════════════════════════════

def read_portfolio_cache_db() -> Optional[Dict]:
    """Read portfolio cache from PostgreSQL. Returns None if not available."""
    if not DATABASE_URL:
        return None
    
    try:
        conn = get_connection()
        if not conn:
            return None
        
        with conn.cursor() as cur:
            cur.execute("SELECT data, updated_at FROM portfolio_cache WHERE id = 1")
            row = cur.fetchone()
        conn.close()
        
        if not row:
            print("[DB] No portfolio cache found")
            return None
        
        cache_data = dict(row['data'])
        cache_data['_cache_time'] = row['updated_at'].strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"[DB] ✓ Read portfolio cache (updated: {cache_data['_cache_time']})")
        return cache_data
    except Exception as e:
        print(f"[DB] Portfolio cache read error: {e}")
        import traceback
        traceback.print_exc()
        return None

def write_portfolio_cache_db(data: Dict) -> bool:
    """Write portfolio cache to PostgreSQL. Uses UPSERT."""
    if not DATABASE_URL:
        return False
    
    try:
        conn = get_connection()
        if not conn:
            return False
        
        import json
        
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO portfolio_cache (id, data, updated_at)
                VALUES (1, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    data = EXCLUDED.data,
                    updated_at = CURRENT_TIMESTAMP
            """, (json.dumps(data),))
            conn.commit()
        conn.close()
        
        print(f"[DB] ✓ Saved portfolio cache to PostgreSQL")
        return True
    except Exception as e:
        print(f"[DB] Portfolio cache write error: {e}")
        import traceback
        traceback.print_exc()
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Profile Management
# ══════════════════════════════════════════════════════════════════════════════

_HERE_DB = os.path.dirname(os.path.abspath(__file__))
PROFILES_JSON_PATH      = os.path.join(_HERE_DB, "profiles.json")
ACTIVE_PROFILE_JSON_PATH = os.path.join(_HERE_DB, "active_profile.json")

DEFAULT_PROFILE_SETTINGS: dict = {
    "capital": 12000.0,
    "max_positions": 10,
    "watchlist": [],
    "risk_per_trade_pct": 2.0,
    "min_profit_for_pyramid": 5.0,
    "min_cushion_for_pyramid": 8.0,
    "cash_reserve_pct": 10.0,
    "top_signals": 5,
    "min_buy_confidence": 0.60,
    "min_pyramid_confidence": 0.65,
    "warn_hold_confidence": 0.40,
}

def _init_profile_tables(conn) -> None:
    """Create profile-related tables and migrate holdings schema."""
    with conn.cursor() as cur:
        # Profiles table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id                    SERIAL PRIMARY KEY,
                name                  VARCHAR(100) UNIQUE NOT NULL,
                capital               NUMERIC(14,2)  DEFAULT 12000,
                max_positions         INT            DEFAULT 10,
                watchlist             JSONB          DEFAULT '[]'::jsonb,
                risk_per_trade_pct    NUMERIC(6,2)   DEFAULT 2.0,
                min_profit_for_pyramid   NUMERIC(6,2) DEFAULT 5.0,
                min_cushion_for_pyramid  NUMERIC(6,2) DEFAULT 8.0,
                cash_reserve_pct      NUMERIC(6,2)   DEFAULT 10.0,
                top_signals           INT            DEFAULT 5,
                min_buy_confidence    NUMERIC(5,3)   DEFAULT 0.60,
                min_pyramid_confidence NUMERIC(5,3)  DEFAULT 0.65,
                warn_hold_confidence  NUMERIC(5,3)   DEFAULT 0.40,
                created_at            TIMESTAMP      DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # App settings table (key-value)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   VARCHAR(100) PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Ensure a Default profile exists
        cur.execute("""
            INSERT INTO profiles (name) VALUES ('Default')
            ON CONFLICT (name) DO NOTHING
        """)
        # Migration: add profile_id to holdings if not present
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'holdings' AND column_name = 'profile_id'
        """)
        if not cur.fetchone():
            cur.execute("ALTER TABLE holdings ADD COLUMN profile_id INT")
            cur.execute("""
                UPDATE holdings
                SET profile_id = (SELECT id FROM profiles WHERE name = 'Default' LIMIT 1)
                WHERE profile_id IS NULL
            """)
            cur.execute("ALTER TABLE holdings ALTER COLUMN profile_id SET NOT NULL")
            cur.execute("""
                ALTER TABLE holdings DROP CONSTRAINT IF EXISTS holdings_pkey
            """)
            cur.execute("""
                ALTER TABLE holdings ADD PRIMARY KEY (profile_id, ticker)
            """)
            cur.execute("""
                ALTER TABLE holdings ADD CONSTRAINT holdings_profile_fk
                FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
            """)
    conn.commit()


# ── JSON-fallback helpers for profiles ───────────────────────────────────────

def _read_profiles_json() -> list[dict]:
    if not os.path.exists(PROFILES_JSON_PATH):
        default = {"id": 1, "name": "Default", **DEFAULT_PROFILE_SETTINGS}
        _write_profiles_json([default])
        return [default]
    try:
        with open(PROFILES_JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        default = {"id": 1, "name": "Default", **DEFAULT_PROFILE_SETTINGS}
        return [default]

def _write_profiles_json(profiles: list[dict]) -> None:
    try:
        with open(PROFILES_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=2)
    except Exception as e:
        print(f"[PROFILES] JSON write error: {e}")

def _get_active_profile_id_json() -> int:
    try:
        if os.path.exists(ACTIVE_PROFILE_JSON_PATH):
            with open(ACTIVE_PROFILE_JSON_PATH, encoding="utf-8") as f:
                return int(json.load(f).get("active_profile_id", 1))
    except Exception:
        pass
    return 1

def _set_active_profile_id_json(profile_id: int) -> None:
    try:
        with open(ACTIVE_PROFILE_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump({"active_profile_id": profile_id}, f)
    except Exception as e:
        print(f"[PROFILES] active_profile write error: {e}")

def _holdings_path_for(profile_id: int) -> str:
    """Return the JSON holdings file path for a given profile id."""
    if profile_id == 1:
        return os.path.join(_HERE_DB, "holdings.json")
    return os.path.join(_HERE_DB, f"holdings_{profile_id}.json")


# ── Unified profile API ───────────────────────────────────────────────────────

def list_profiles() -> list[dict]:
    """Return all profiles as a list of dicts."""
    if DATABASE_URL:
        try:
            conn = get_connection()
            active_id = _get_active_profile_id_db_conn(conn)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, name, capital, max_positions, watchlist,
                           risk_per_trade_pct, min_profit_for_pyramid,
                           min_cushion_for_pyramid, cash_reserve_pct, top_signals,
                           min_buy_confidence, min_pyramid_confidence, warn_hold_confidence
                    FROM profiles ORDER BY id
                """)
                rows = cur.fetchall()
            conn.close()
            return [_profile_row_to_dict(r, active_id) for r in rows]
        except Exception as e:
            print(f"[PROFILES] list error: {e}")
            return []
    else:
        active_id = _get_active_profile_id_json()
        profiles = _read_profiles_json()
        for p in profiles:
            p["is_active"] = (p["id"] == active_id)
        return profiles

def get_active_profile_id() -> int:
    if DATABASE_URL:
        try:
            conn = get_connection()
            pid = _get_active_profile_id_db_conn(conn)
            conn.close()
            return pid
        except Exception:
            return 1
    return _get_active_profile_id_json()

def get_active_profile() -> dict:
    """Return the full settings dict of the active profile."""
    pid = get_active_profile_id()
    profiles = list_profiles()
    for p in profiles:
        if p["id"] == pid:
            return p
    return {"id": 1, "name": "Default", **DEFAULT_PROFILE_SETTINGS}

def set_active_profile(profile_id: int) -> bool:
    if DATABASE_URL:
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO app_settings (key, value) VALUES ('active_profile_id', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, (str(profile_id),))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"[PROFILES] set_active error: {e}")
            return False
    _set_active_profile_id_json(profile_id)
    return True

def create_profile(name: str, settings: dict | None = None) -> dict | None:
    s = {**DEFAULT_PROFILE_SETTINGS, **(settings or {})}
    if DATABASE_URL:
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO profiles (name, capital, max_positions, watchlist,
                        risk_per_trade_pct, min_profit_for_pyramid, min_cushion_for_pyramid,
                        cash_reserve_pct, top_signals, min_buy_confidence,
                        min_pyramid_confidence, warn_hold_confidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (
                    name.strip(),
                    float(s["capital"]),
                    int(s["max_positions"]),
                    json.dumps(s["watchlist"]),
                    float(s["risk_per_trade_pct"]),
                    float(s["min_profit_for_pyramid"]),
                    float(s["min_cushion_for_pyramid"]),
                    float(s["cash_reserve_pct"]),
                    int(s["top_signals"]),
                    float(s["min_buy_confidence"]),
                    float(s["min_pyramid_confidence"]),
                    float(s["warn_hold_confidence"]),
                ))
                new_id = cur.fetchone()["id"]
            conn.commit()
            conn.close()
            return {"id": new_id, "name": name.strip(), **s}
        except Exception as e:
            print(f"[PROFILES] create error: {e}")
            return None
    else:
        profiles = _read_profiles_json()
        new_id = max((p["id"] for p in profiles), default=0) + 1
        new_profile = {"id": new_id, "name": name.strip(), **s}
        profiles.append(new_profile)
        _write_profiles_json(profiles)
        return new_profile

def update_profile(profile_id: int, updates: dict) -> bool:
    allowed = {
        "name", "capital", "max_positions", "watchlist",
        "risk_per_trade_pct", "min_profit_for_pyramid", "min_cushion_for_pyramid",
        "cash_reserve_pct", "top_signals", "min_buy_confidence",
        "min_pyramid_confidence", "warn_hold_confidence",
    }
    updates = {k: v for k, v in updates.items() if k in allowed}
    if not updates:
        return True
    if DATABASE_URL:
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                for col, val in updates.items():
                    if col == "watchlist":
                        val = json.dumps(val)
                    cur.execute(
                        f"UPDATE profiles SET {col} = %s WHERE id = %s",
                        (val, profile_id)
                    )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"[PROFILES] update error: {e}")
            return False
    else:
        profiles = _read_profiles_json()
        for p in profiles:
            if p["id"] == profile_id:
                p.update(updates)
        _write_profiles_json(profiles)
        return True

def delete_profile(profile_id: int) -> bool:
    """Delete a profile. Refuses if it's the only profile."""
    profiles = list_profiles()
    if len(profiles) <= 1:
        return False  # Cannot delete the last profile
    if DATABASE_URL:
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))
            conn.commit()
            conn.close()
            # If deleted profile was active, switch to first remaining
            if get_active_profile_id() == profile_id:
                remaining = [p for p in profiles if p["id"] != profile_id]
                if remaining:
                    set_active_profile(remaining[0]["id"])
            return True
        except Exception as e:
            print(f"[PROFILES] delete error: {e}")
            return False
    else:
        profiles = [p for p in profiles if p["id"] != profile_id]
        _write_profiles_json(profiles)
        # Clean up holdings file
        h_path = _holdings_path_for(profile_id)
        if os.path.exists(h_path) and profile_id != 1:
            try:
                os.remove(h_path)
            except Exception:
                pass
        if _get_active_profile_id_json() == profile_id:
            if profiles:
                _set_active_profile_id_json(profiles[0]["id"])
        return True


# ── Profile-aware holdings ────────────────────────────────────────────────────

def read_holdings_for_profile(profile_id: int) -> list[dict]:
    if DATABASE_URL:
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ticker, entry_price, shares, notes
                    FROM holdings WHERE profile_id = %s ORDER BY ticker
                """, (profile_id,))
                rows = cur.fetchall()
            conn.close()
            return [{"ticker": str(r["ticker"]), "entry_price": float(r["entry_price"]),
                     "shares": float(r["shares"]), "notes": str(r["notes"] or "")}
                    for r in rows]
        except Exception as e:
            print(f"[HOLDINGS] read_for_profile error: {e}")
            return []
    else:
        path = _holdings_path_for(profile_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

def write_holdings_for_profile(profile_id: int, holdings: list[dict]) -> bool:
    clean = []
    for h in holdings:
        ticker = str(h.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        clean.append(dict(
            ticker=ticker,
            entry_price=float(h.get("entry_price", 0)),
            shares=float(h.get("shares", 0)),
            notes=str(h.get("notes", "")),
        ))
    if DATABASE_URL:
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM holdings WHERE profile_id = %s", (profile_id,))
                for h in clean:
                    cur.execute("""
                        INSERT INTO holdings (profile_id, ticker, entry_price, shares, notes)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (profile_id, ticker) DO UPDATE SET
                            entry_price = EXCLUDED.entry_price,
                            shares = EXCLUDED.shares,
                            notes = EXCLUDED.notes,
                            updated_at = CURRENT_TIMESTAMP
                    """, (profile_id, h["ticker"], h["entry_price"], h["shares"], h["notes"]))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"[HOLDINGS] write_for_profile error: {e}")
            return False
    else:
        path = _holdings_path_for(profile_id)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(clean, f, indent=2)
            return True
        except Exception as e:
            print(f"[HOLDINGS] file write error: {e}")
            return False

def delete_holding_for_profile(profile_id: int, ticker: str) -> bool:
    ticker = ticker.strip().upper()
    if DATABASE_URL:
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM holdings WHERE profile_id = %s AND ticker = %s",
                    (profile_id, ticker)
                )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"[HOLDINGS] delete_for_profile error: {e}")
            return False
    else:
        holdings = read_holdings_for_profile(profile_id)
        holdings = [h for h in holdings if h["ticker"] != ticker]
        return write_holdings_for_profile(profile_id, holdings)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_active_profile_id_db_conn(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = 'active_profile_id'")
        row = cur.fetchone()
        if row:
            return int(row["value"])
        # Fallback: return id of 'Default' profile
        cur.execute("SELECT id FROM profiles WHERE name = 'Default' LIMIT 1")
        row = cur.fetchone()
        return int(row["id"]) if row else 1

def _profile_row_to_dict(row, active_id: int) -> dict:
    return {
        "id":                      int(row["id"]),
        "name":                    str(row["name"]),
        "capital":                 float(row["capital"] or 12000),
        "max_positions":           int(row["max_positions"] or 10),
        "watchlist":               list(row["watchlist"] or []),
        "risk_per_trade_pct":      float(row["risk_per_trade_pct"] or 2.0),
        "min_profit_for_pyramid":  float(row["min_profit_for_pyramid"] or 5.0),
        "min_cushion_for_pyramid": float(row["min_cushion_for_pyramid"] or 8.0),
        "cash_reserve_pct":        float(row["cash_reserve_pct"] or 10.0),
        "top_signals":             int(row["top_signals"] or 5),
        "min_buy_confidence":      float(row["min_buy_confidence"] or 0.60),
        "min_pyramid_confidence":  float(row["min_pyramid_confidence"] or 0.65),
        "warn_hold_confidence":    float(row["warn_hold_confidence"] or 0.40),
        "is_active":               (int(row["id"]) == active_id),
    }


# Initialize DB on module load if DATABASE_URL is set
if DATABASE_URL:
    print("[DB] PostgreSQL detected — initializing table...")
    if init_db():
        try:
            _conn = get_connection()
            _init_profile_tables(_conn)
            _conn.close()
            print("[DB] ✓ Profile tables ready")
        except Exception as _e:
            print(f"[DB] ✗ Profile table init failed: {_e}")
        print("[DB] ✓ Ready")
    else:
        print("[DB] ✗ Initialization failed, will fall back to JSON")
else:
    print("[DB] No DATABASE_URL — using JSON file storage")
