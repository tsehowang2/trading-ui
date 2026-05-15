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
    """Initialize database table if it doesn't exist."""
    if not DATABASE_URL:
        return False
    
    try:
        conn = get_connection()
        if not conn:
            return False
        
        with conn.cursor() as cur:
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

# Initialize DB on module load if DATABASE_URL is set
if DATABASE_URL:
    print("[DB] PostgreSQL detected — initializing table...")
    if init_db():
        print("[DB] ✓ Ready")
    else:
        print("[DB] ✗ Initialization failed, will fall back to JSON")
else:
    print("[DB] No DATABASE_URL — using JSON file storage")
