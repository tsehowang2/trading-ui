from flask import Flask, render_template, request, jsonify
import json
import os
import sys
import math
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)          # workspace root (SAC/)
sys.path.insert(0, _HERE)

from main import (run_backtest, _get_live_indicators, save_results, RESULTS_DIR, get_portfolio_data,
                  _bmark_voo_lumpsum, _bmark_dca_voo, _bmark_6040, _bmark_inv_vol_voo, _bmark_random_entry)
from data import cached_download
import pandas as pd
import db  # PostgreSQL storage module

app = Flask(__name__, template_folder=os.path.join(_HERE, 'templates'))
app.secret_key = "your-secret-key"

HOLDINGS_PATH  = os.path.join(_HERE, "holdings.json")
CACHE_PATH     = os.path.join(_HERE, "results", "portfolio_cache.json")

# ── Profile-aware holdings helpers ───────────────────────────────────────────
def _get_active_profile_id() -> int:
    return db.get_active_profile_id()

def _read_holdings_json(profile_id: int | None = None) -> list[dict]:
    """Return raw holdings for the given (or active) profile."""
    pid = profile_id if profile_id is not None else _get_active_profile_id()
    return db.read_holdings_for_profile(pid)

def _write_holdings_json(rows: list[dict], profile_id: int | None = None) -> None:
    """Persist holdings for the given (or active) profile."""
    pid = profile_id if profile_id is not None else _get_active_profile_id()
    db.write_holdings_for_profile(pid, rows)
    _invalidate_portfolio_cache()

def _invalidate_portfolio_cache() -> None:
    """Clear cached portfolio analysis so next load forces a re-run."""
    # Remove JSON cache file
    try:
        if os.path.exists(CACHE_PATH):
            os.remove(CACHE_PATH)
    except Exception:
        pass
    # Remove PostgreSQL cache row
    if db.DATABASE_URL:
        try:
            conn = db.get_connection()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM portfolio_cache WHERE id = 1")
                conn.commit()
                conn.close()
        except Exception:
            pass

def _read_config_watchlist() -> list:
    """Read watchlist from config.toml, returning [] on failure."""
    try:
        import tomllib
        config_path = os.path.join(_HERE, "config.toml")
        if os.path.exists(config_path):
            with open(config_path, "rb") as f:
                cfg = tomllib.load(f)
            return cfg.get("portfolio", {}).get("watchlist", [])
    except Exception:
        pass
    return []

def safe_round(value, default=0.0):
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return round(value, 2)

def load_holdings():
    """Load holdings + enrich with live prices for the index page."""
    raw = _read_holdings_json()
    enriched = []
    for h in raw:
        ticker = h.get('ticker', '').upper().strip()
        h.setdefault('current_price', 0.0)
        h.setdefault('pnl_pct', 0.0)
        h.setdefault('stop_price', 0.0)
        h.setdefault('mkt_value', 0.0)
        try:
            ind = _get_live_indicators(ticker)
            if ind:
                close       = ind.get('close', 0.0)
                atr_frac    = ind.get('atr_frac', 0.15)
                entry_price = float(h.get('entry_price', 0))
                h['current_price'] = safe_round(close)
                if entry_price > 0 and close > 0:
                    h['pnl_pct'] = safe_round((close - entry_price) / entry_price, 0.0)
                h['mkt_value']  = safe_round(float(h.get('shares', 0)) * close, 0.0)
                # compute peak-based trailing stop (mirrors get_portfolio_data logic)
                if entry_price > 0 and ind.get('df') is not None:
                    df_h   = ind['df']
                    cls    = df_h['Close']
                    below  = cls[cls <= entry_price * 1.005]
                    if not below.empty:
                        ei       = cls.index.get_loc(below.index[-1])
                        pos_peak = max(entry_price, float(cls.iloc[ei:].max()))
                    else:
                        pos_peak = max(entry_price, close)
                    pos_peak = max(pos_peak, entry_price)
                else:
                    pos_peak = max(entry_price, close) if entry_price > 0 else close
                h['stop_price'] = safe_round(pos_peak * (1 - atr_frac), 0.0)
        except Exception as e:
            print(f"Indicator error for {ticker}: {e}")
        enriched.append(h)
    return enriched

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/holdings', methods=['GET', 'POST', 'DELETE'])
def api_holdings():
    if request.method == 'GET':
        holdings = _read_holdings_json()
        print(f"[API] GET /api/holdings → returning {len(holdings)} holdings")
        return jsonify(holdings)
    elif request.method == 'POST':
        data     = request.json
        ticker   = str(data.get('ticker', '')).strip().upper()
        holdings = _read_holdings_json()
        existing = next((h for h in holdings if h['ticker'] == ticker), None)
        if existing:
            existing['entry_price'] = float(data.get('entry_price', existing['entry_price']))
            existing['shares']      = float(data.get('shares', existing['shares']))
            existing['notes']       = str(data.get('notes', existing.get('notes', '')))
        else:
            holdings.append(dict(
                ticker      = ticker,
                entry_price = float(data.get('entry_price', 0)),
                shares      = float(data.get('shares', 0)),
                notes       = str(data.get('notes', '')),
            ))
        _write_holdings_json(holdings)
        return jsonify({"status": "success"})
    elif request.method == 'DELETE':
        ticker = request.args.get('ticker', '').upper()
        pid = _get_active_profile_id()
        db.delete_holding_for_profile(pid, ticker)
        _invalidate_portfolio_cache()
        return jsonify({"status": "success"})

@app.route('/api/backtest/<ticker>')
def api_backtest(ticker):
    try:
        # Get year parameters from query string (default: last 365 days)
        from_year = request.args.get('from_year', type=int)
        to_year = request.args.get('to_year', type=int)
        
        if from_year and to_year:
            test_start = f"{from_year}-01-01"
            test_end = f"{to_year}-12-31"
            # If to_year is current year, use today's date
            if to_year == datetime.now().year:
                test_end = datetime.now().strftime('%Y-%m-%d')
        else:
            # Default: last 365 days
            end_date = datetime.now()
            start_date = end_date - timedelta(days=365)
            test_start = start_date.strftime('%Y-%m-%d')
            test_end = end_date.strftime('%Y-%m-%d')
        
        print(f"[BACKTEST] Running for {ticker} from {test_start} to {test_end}")
        result = run_backtest(ticker.upper(), test_start, test_end, verbose=True)

        if not result or 'eq' not in result:
            print(f"[BACKTEST] No result for {ticker}")
            return jsonify({"error": "Backtest failed – no data"}), 400

        # ---------- SAVE RESULTS TO DISK (like CLI) ----------
        year = test_start[:4]
        name = f"{ticker.upper()}_{year}"
        try:
            save_results(result, name)
            print(f"[BACKTEST] Saved results to {RESULTS_DIR}/{name}/")
        except Exception as save_err:
            print(f"Warning: could not save results: {save_err}")

        # ---------- Prepare OHLCV for chart ----------
        df = cached_download(ticker, test_start, test_end)
        if df.empty:
            return jsonify({"error": "No price data"}), 400
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
        df.reset_index(inplace=True)
        date_col = df.columns[0]
        df.rename(columns={date_col: 'Date'}, inplace=True)
        df['Date'] = pd.to_datetime(df['Date']).dt.strftime('%Y-%m-%d')
        ohlcv = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']].to_dict('records')

        # ---------- Extract trades ----------
        trades = []
        tlog_df = result.get('tlog', pd.DataFrame())
        if not tlog_df.empty:
            for _, row in tlog_df.iterrows():
                trade_date = row['date']
                if isinstance(trade_date, pd.Timestamp):
                    trade_date = trade_date.strftime('%Y-%m-%d')
                trades.append({
                    'date': trade_date,
                    'action': row['action'],
                    'price': float(row['price']),
                    'shares': int(row['shares']),
                    'equity': float(row['equity']) if pd.notna(row.get('equity')) else None,
                    'entry_ep': float(row['entry_ep']) if pd.notna(row.get('entry_ep')) else None,
                    'pnl_pct': float(row['pnl_pct']) if pd.notna(row.get('pnl_pct')) else None,
                    'avoided_loss': float(row['avoided_loss']) if pd.notna(row.get('avoided_loss')) else None,
                    'missed_gain': float(row['missed_gain']) if pd.notna(row.get('missed_gain')) else None,
                })

        # ---------- Win/loss stats ----------
        sell_df = tlog_df[tlog_df['action'].str.startswith('SELL') & tlog_df['pnl_pct'].notna()] if not tlog_df.empty else pd.DataFrame()
        win_rate = avg_win = avg_loss = best_trade = worst_trade = None
        if not sell_df.empty:
            pnls = sell_df['pnl_pct'].astype(float)
            wins = pnls[pnls > 0]; losses = pnls[pnls <= 0]
            win_rate   = round(float(len(wins) / len(pnls)), 4)
            avg_win    = round(float(wins.mean()), 4) if len(wins) else None
            avg_loss   = round(float(losses.mean()), 4) if len(losses) else None
            best_trade = round(float(pnls.max()), 4)
            worst_trade= round(float(pnls.min()), 4)

        # ---------- Benchmarks ----------
        eq_curve = result['eq']
        initial  = float(eq_curve.iloc[0])
        end_val  = float(eq_curve.iloc[-1])
        ts_dt    = pd.to_datetime(test_start)
        te_dt    = pd.to_datetime(test_end)
        days     = max((te_dt - ts_dt).days, 1)

        def _bstats(label, eq_s):
            try:
                if eq_s is None or eq_s.empty: return None
                r = float(eq_s.iloc[-1] / eq_s.iloc[0] - 1)
                c = float((eq_s.iloc[-1] / eq_s.iloc[0]) ** (365.0 / days) - 1)
                return {'label': label, 'ret': round(r,4), 'cagr': round(c,4), 'end_val': round(float(eq_s.iloc[-1]),2)}
            except Exception:
                return None

        benchmarks = []
        for lbl, eq_s in [
            ('VOO lump-sum',   _bmark_voo_lumpsum(test_start, test_end, initial)),
            ('DCA VOO',        _bmark_dca_voo(test_start, test_end, initial)),
            ('60/40 VOO+TLT',  _bmark_6040(test_start, test_end, initial)),
            ('Inv-vol VOO',    _bmark_inv_vol_voo(test_start, test_end, initial)),
        ]:
            s = _bstats(lbl, eq_s)
            if s: benchmarks.append(s)
        try:
            mu, sigma = _bmark_random_entry(test_start, test_end, initial)
            benchmarks.append({'label': 'Rand-entry VOO (μ)', 'ret': round(float(mu),4), 'cagr': None,
                                'end_val': None, 'sigma': round(float(sigma),4)})
        except Exception:
            pass

        return jsonify({
            "success": True,
            "ticker": ticker,
            "test_start": test_start,
            "test_end": test_end,
            "ohlcv": ohlcv,
            "trades": trades,
            "metrics": {
                "total_return": round(float(result['total_ret']), 4),
                "cagr":         round(float(result['cagr']), 4),
                "sharpe":       round(float(result['sharpe']), 4),
                "mdd":          round(float(result['mdd']), 4),
                "tpm":          round(float(result['tpm']), 2),
                "n_trades":     int(result['n_trades']),
                "initial_cap":  round(initial, 2),
                "end_val":      round(end_val, 2),
                "win_rate":     win_rate,
                "avg_win":      avg_win,
                "avg_loss":     avg_loss,
                "best_trade":   best_trade,
                "worst_trade":  worst_trade,
            },
            "benchmarks": benchmarks,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/watchlist')
def api_watchlist():
    """Return active profile's watchlist (falls back to config.toml)."""
    try:
        profile = db.get_active_profile()
        wl = profile.get("watchlist") or []
        if wl:
            return jsonify({"watchlist": wl})
        # Fallback to config.toml if profile has no watchlist yet
        return jsonify({"watchlist": _read_config_watchlist()})
    except Exception as e:
        return jsonify({"watchlist": [], "error": str(e)})

@app.route('/api/config/watchlist')
def api_config_watchlist():
    """Return the raw watchlist from config.toml (used for import in Settings)."""
    return jsonify({"watchlist": _read_config_watchlist()})


# ── Debug endpoint ──
@app.route('/api/debug/storage')
def debug_storage():
    """Debug endpoint to check storage status."""
    return jsonify({
        "database_url_set": bool(db.DATABASE_URL),
        "holdings_from_db": db.read_holdings_db() if db.DATABASE_URL else None,
        "holdings_from_json": _read_holdings_json(),
        "json_file_exists": os.path.exists(HOLDINGS_PATH),
        "json_file_path": HOLDINGS_PATH,
        "portfolio_cache_from_db": db.read_portfolio_cache_db() if db.DATABASE_URL else None,
        "portfolio_cache_json_exists": os.path.exists(CACHE_PATH),
        "portfolio_cache_path": CACHE_PATH
    })


@app.route('/backtest')
def backtest_page():
    return render_template('backtest.html')


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


@app.route('/api/dashboard/cached')
def api_dashboard_cached():
    """Return the last cached portfolio analysis result (instant, no recompute)."""
    # Try PostgreSQL first
    if db.DATABASE_URL:
        cache_data = db.read_portfolio_cache_db()
        if cache_data:
            return jsonify({"success": True, "cached": True, "data": cache_data})
    
    # Fallback to JSON file
    if not os.path.exists(CACHE_PATH):
        return jsonify({"success": False, "cached": False, "error": "No cache yet — click Refresh to run analysis."})
    
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        mtime = os.path.getmtime(CACHE_PATH)
        data["_cache_time"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({"success": True, "cached": True, "data": data})
    except Exception as e:
        print(f"[CACHE] Read error: {e}")
        return jsonify({"success": False, "cached": False, "error": "Cache read failed"})


@app.route('/api/dashboard/refresh')
def api_dashboard():
    """Run a live portfolio analysis (slow), write cache, return result."""
    try:
        # Load settings from active profile (DB or JSON), not config.toml
        profile = db.get_active_profile()
        pid     = profile["id"]

        # Sync active-profile holdings to a temp JSON file (main.py reads from file)
        holdings_from_profile = db.read_holdings_for_profile(pid)
        holdings_path = os.path.join(_HERE, f"_holdings_profile_{pid}.json")
        try:
            with open(holdings_path, "w", encoding="utf-8") as f:
                json.dump(holdings_from_profile, f, indent=2)
        except Exception as sync_err:
            print(f"[DASHBOARD] Warning: Could not write temp holdings file: {sync_err}")
            holdings_path = HOLDINGS_PATH  # fallback

        watchlist   = profile.get("watchlist", [])
        capital     = profile.get("capital") or None
        max_pos     = int(profile.get("max_positions", 10))
        risk_pct    = float(profile.get("risk_per_trade_pct", 2.0))
        reserve_pct = float(profile.get("cash_reserve_pct", 10.0)) / 100.0
        top_n       = int(profile.get("top_signals", 5))
        min_buy     = float(profile.get("min_buy_confidence", 0.60))
        min_pyr     = float(profile.get("min_pyramid_confidence", 0.65))
        warn_hold   = float(profile.get("warn_hold_confidence", 0.40))

        data = get_portfolio_data(
            holdings_csv           = holdings_path,
            watchlist              = watchlist,
            total_capital          = float(capital) if capital else None,
            max_positions          = max_pos,
            risk_per_trade_pct     = risk_pct,
            cash_reserve_pct       = reserve_pct,
            top_signals            = top_n,
            min_buy_confidence     = min_buy,
            min_pyramid_confidence = min_pyr,
            warn_hold_confidence   = warn_hold,
        )
        data["_cache_time"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data["_profile_id"]   = pid
        data["_profile_name"] = profile.get("name", "Default")

        # Save to PostgreSQL cache (primary)
        if db.DATABASE_URL:
            if db.write_portfolio_cache_db(data):
                print("[CACHE] ✓ Saved to PostgreSQL")
            else:
                print("[CACHE] ⚠ PostgreSQL save failed, trying JSON fallback")

        # Also save to JSON file as backup
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            print("[CACHE] ✓ Saved to JSON file")
        except Exception as e:
            print(f"[CACHE] ⚠ JSON save failed: {e}")

        return jsonify({"success": True, "cached": False, "data": data})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ══ Profile management API ════════════════════════════════════════════════════

@app.route('/settings')
def settings_page():
    return render_template('settings.html')


@app.route('/api/profiles', methods=['GET', 'POST'])
def api_profiles():
    if request.method == 'GET':
        return jsonify(db.list_profiles())
    # POST → create new profile
    data = request.json or {}
    name = str(data.get('name', '')).strip()
    if not name:
        return jsonify({"error": "Profile name required"}), 400
    existing = [p for p in db.list_profiles() if p['name'].lower() == name.lower()]
    if existing:
        return jsonify({"error": "A profile with that name already exists"}), 409
    settings = {k: data[k] for k in db.DEFAULT_PROFILE_SETTINGS if k in data}
    # Auto-seed watchlist from config.toml if none provided
    if 'watchlist' not in settings or not settings['watchlist']:
        settings['watchlist'] = _read_config_watchlist()
    profile = db.create_profile(name, settings)
    if profile is None:
        return jsonify({"error": "Failed to create profile"}), 500
    return jsonify(profile), 201


@app.route('/api/profiles/active', methods=['GET', 'POST'])
def api_profiles_active():
    if request.method == 'GET':
        return jsonify(db.get_active_profile())
    # POST → set active profile by id
    data = request.json or {}
    pid = data.get('profile_id')
    if pid is None:
        return jsonify({"error": "profile_id required"}), 400
    if not db.set_active_profile(int(pid)):
        return jsonify({"error": "Failed to set active profile"}), 500
    # Invalidate cache — new profile has different holdings / settings
    _invalidate_portfolio_cache()
    return jsonify({"status": "ok", "active_profile_id": int(pid)})


@app.route('/api/profiles/<int:profile_id>', methods=['PUT', 'DELETE'])
def api_profile_detail(profile_id):
    if request.method == 'PUT':
        data = request.json or {}
        if not db.update_profile(profile_id, data):
            return jsonify({"error": "Update failed"}), 500
        # Invalidate cache if the active profile's settings changed
        if profile_id == _get_active_profile_id():
            _invalidate_portfolio_cache()
        return jsonify({"status": "ok"})
    # DELETE
    if len(db.list_profiles()) <= 1:
        return jsonify({"error": "Cannot delete the only profile"}), 400
    if not db.delete_profile(profile_id):
        return jsonify({"error": "Delete failed"}), 500
    return jsonify({"status": "ok"})


@app.route('/api/profiles/<int:profile_id>/holdings', methods=['GET', 'POST', 'DELETE'])
def api_profile_holdings(profile_id):
    if request.method == 'GET':
        return jsonify(db.read_holdings_for_profile(profile_id))
    if request.method == 'POST':
        data   = request.json or {}
        # Bulk replace all holdings at once (used by settings page)
        if data.get('_bulk'):
            rows = data.get('holdings', [])
            db.write_holdings_for_profile(profile_id, rows)
            # Invalidate cache only if this is the active profile's holdings
            if profile_id == _get_active_profile_id():
                _invalidate_portfolio_cache()
            return jsonify({"status": "ok"})
        ticker = str(data.get('ticker', '')).strip().upper()
        if not ticker:
            return jsonify({"error": "ticker required"}), 400
        holdings = db.read_holdings_for_profile(profile_id)
        existing = next((h for h in holdings if h['ticker'] == ticker), None)
        if existing:
            existing['entry_price'] = float(data.get('entry_price', existing['entry_price']))
            existing['shares']      = float(data.get('shares', existing['shares']))
            existing['notes']       = str(data.get('notes', existing.get('notes', '')))
        else:
            holdings.append(dict(
                ticker=ticker,
                entry_price=float(data.get('entry_price', 0)),
                shares=float(data.get('shares', 0)),
                notes=str(data.get('notes', '')),
            ))
        db.write_holdings_for_profile(profile_id, holdings)
        if profile_id == _get_active_profile_id():
            _invalidate_portfolio_cache()
        return jsonify({"status": "ok"})
    # DELETE
    ticker = request.args.get('ticker', '').upper()
    db.delete_holding_for_profile(profile_id, ticker)
    if profile_id == _get_active_profile_id():
        _invalidate_portfolio_cache()
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    print("[FLASK] Starting v2.9p Dashboard with REAL backtest engine")
    app.run(debug=False, port=5000, host='0.0.0.0')