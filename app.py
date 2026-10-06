from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response
import json
import os
import sys
import math
import secrets
import hmac
from datetime import datetime, timedelta, timezone
from werkzeug.security import check_password_hash
from validation import settings as validate_settings
import backup
import profile_store
import analysis_cache
import ledger
import signal_history
import strategy
import paper
from market_sessions import latest_completed_session

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)          # workspace root (SAC/)
sys.path.insert(0, _HERE)

from main import (run_backtest, _get_live_indicators, save_results, RESULTS_DIR, get_portfolio_data,
                  _bmark_voo_lumpsum, _bmark_dca_voo, _bmark_6040, _bmark_inv_vol_voo, _bmark_random_entry)
from data import cached_download
import pandas as pd
import db  # PostgreSQL storage module

app = Flask(__name__, template_folder=os.path.join(_HERE, 'templates'))
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=5 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')))


@app.errorhandler(ValueError)
def invalid_input(error):
    return jsonify(error=str(error)), 400


@app.errorhandler(KeyError)
def missing_profile(error):
    return jsonify(error='Profile not found'), 404


@app.errorhandler(db.StorageError)
def unavailable_storage(error):
    app.logger.error('Profile storage unavailable')
    return jsonify(error=str(error)), 503


def _json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError('A JSON object is required')
    return body


@app.context_processor
def security_context():
    session.setdefault('csrf_token', secrets.token_hex(32))
    return {'csrf_token': session['csrf_token'], 'login_enabled': bool(os.environ.get('APP_PASSWORD_HASH'))}


@app.before_request
def protect_application():
    if app.testing and app.config.get('SECURITY_DISABLED_FOR_TESTS'):
        return None
    password_hash = os.environ.get('APP_PASSWORD_HASH')
    if (db.DATABASE_URL or os.environ.get('RENDER')) and (
            not password_hash or not os.environ.get('FLASK_SECRET_KEY')):
        return jsonify(error='Configure APP_PASSWORD_HASH and FLASK_SECRET_KEY before exposing this deployment'), 503
    if not password_hash and request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify(error='Authentication must be configured for remote access'), 403
    if request.endpoint == 'static':
        return None
    if password_hash and request.endpoint != 'login' and not session.get('authenticated'):
        if request.path.startswith('/api/'):
            return jsonify(error='Login required'), 401
        return redirect(url_for('login'))
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
        if not token or not hmac.compare_digest(token, session.get('csrf_token', '')):
            return jsonify(error='Invalid CSRF token; reload the page'), 403
    return None


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password_hash = os.environ.get('APP_PASSWORD_HASH', '')
        if not password_hash or not check_password_hash(password_hash, request.form.get('password', '')):
            return render_template('login.html', error='Invalid password'), 401
        session.clear()
        session['authenticated'] = True
        session['csrf_token'] = secrets.token_hex(32)
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))

HOLDINGS_PATH  = os.path.join(_HERE, "holdings.json")
CACHE_PATH     = os.path.join(_HERE, "results", "portfolio_cache.json")

# ── Profile resolution (per-request, not global) ─────────────────────────────
def _resolve_profile_id() -> int:
    """
    Read profile_id from the request (query string or JSON body).
    Falls back to the DB-stored default only if not supplied by the client.
    Each browser session passes its own profile_id so multiple users can
    view different profiles simultaneously.
    """
    raw = request.args.get('profile_id')
    if raw is None:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            raw = body.get('profile_id')
    if raw is None:
        pid = db.get_active_profile_id()
    else:
        if isinstance(raw, bool) or not str(raw).isdigit() or int(raw) <= 0:
            raise ValueError('profile_id must be a positive integer')
        pid = int(raw)
    _require_profile(pid)
    return pid


def _require_profile(pid):
    profile = next((p for p in db.list_profiles() if p['id'] == pid), None)
    if profile is None:
        raise KeyError('Profile not found')
    return profile

def _read_holdings_json(profile_id: int | None = None) -> list[dict]:
    """Return raw holdings for the given profile (or resolve from request)."""
    pid = profile_id if profile_id is not None else _resolve_profile_id()
    return db.read_holdings_for_profile(pid)

def _write_holdings_json(rows: list[dict], profile_id: int | None = None) -> None:
    """Persist holdings for the given profile (or resolve from request)."""
    pid = profile_id if profile_id is not None else _resolve_profile_id()
    if not db.write_holdings_for_profile(pid, rows):
        raise db.StorageError('Holdings save failed')
    _invalidate_portfolio_cache(pid)

def _invalidate_portfolio_cache(profile_id: int | None = None) -> None:
    """Clear cached portfolio analysis so next load forces a re-run."""
    # Remove JSON cache file
    try:
        path = _profile_cache_path(profile_id) if profile_id is not None else CACHE_PATH
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    # Remove PostgreSQL cache row
    if db.DATABASE_URL:
        try:
            conn = db.get_connection()
            if conn:
                with conn.cursor() as cur:
                    if profile_id is None:
                        cur.execute('DELETE FROM profile_analysis_cache')
                    else:
                        cur.execute('DELETE FROM profile_analysis_cache WHERE profile_id = %s', (profile_id,))
                conn.commit()
                conn.close()
        except Exception:
            pass

def _profile_cache_path(pid):
    return os.path.join(os.path.dirname(CACHE_PATH), f'portfolio_cache_{pid}.json')


def _profile_fingerprint(pid, state=None):
    import hashlib
    state = profile_store.snapshot() if state is None else state
    profile = next((dict(p) for p in state['profiles'] if p['id'] == pid), None)
    if profile is None:
        raise KeyError('Profile not found')
    profile.pop('is_active', None)
    payload = {'profile': profile, 'holdings': state['holdings'].get(str(pid), [])}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


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
    pid = _resolve_profile_id()
    return _holdings_response(pid)


def _holdings_response(pid):
    _require_profile(pid)
    if request.method == 'GET':
        return jsonify(db.read_holdings_for_profile(pid))
    if request.method == 'DELETE':
        ok = db.delete_holding_for_profile(pid, request.args.get('ticker', ''))
    else:
        data = _json_body()
        if data.get('_bulk'):
            ok = db.write_holdings_for_profile(pid, data.get('holdings'))
        else:
            row = {k: v for k, v in data.items() if k != 'profile_id'}
            ok = db.upsert_holding(pid, row)
    if not ok:
        raise db.StorageError('Holdings save failed')
    _invalidate_portfolio_cache(pid)
    return jsonify(status='success')

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
        profile = _require_profile(_resolve_profile_id())
        result = run_backtest(ticker.upper(), test_start, test_end, verbose=True,
                      min_buy_confidence=float(profile['min_buy_confidence']))

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
                    'signal_date': pd.Timestamp(row['signal_date']).strftime('%Y-%m-%d') if pd.notna(row.get('signal_date')) else None,
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
            "decisions": result.get('decisions', []),
            "pending_order": result.get('pending_order'),
            "strategy_version": result.get('strategy_version'),
            "execution_model": "Completed close decision; next available session open. Open positions marked, not forced sold.",
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
    """Return the requesting profile's watchlist (falls back to config.toml)."""
    profile = _require_profile(_resolve_profile_id())
    return jsonify(watchlist=profile['watchlist'])

@app.route('/api/config/watchlist')
def api_config_watchlist():
    """Return the raw watchlist from config.toml (used for import in Settings)."""
    return jsonify({"watchlist": _read_config_watchlist()})


# ── Debug endpoint ──
@app.route('/api/debug/storage')
def debug_storage():
    """Debug endpoint to check storage status."""
    return jsonify(database_url_set=bool(db.DATABASE_URL), profiles=len(db.list_profiles()))


@app.route('/backtest')
def backtest_page():
    return render_template('backtest.html')


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


@app.route('/api/dashboard/cached')
def api_dashboard_cached():
    """Return the last cached portfolio analysis result (instant, no recompute).
    Only returns the cache if it was computed for the same profile_id the client requests.
    """
    pid = _resolve_profile_id()

    fingerprint = _profile_fingerprint(pid)
    try:
        data = analysis_cache.read(pid, _profile_cache_path(pid))
    except Exception:
        app.logger.warning('Analysis cache unavailable')
        data = None
    if not data or data.get('_profile_id') != pid or data.get('_profile_fingerprint') != fingerprint:
        return jsonify(success=False, cached=False, error='No current analysis for this profile — click Refresh.')
    # Session-calendar freshness replaces this conservative UTC date check in
    # the market-data milestone. For now never silently serve yesterday's run.
    if data.get('_completed_session') != latest_completed_session() or data.get('strategy_version') != strategy.VERSION:
        return jsonify(success=False, cached=False, error='Analysis is from an earlier date — click Refresh.')
    return jsonify(success=True, cached=True, data=data)


@app.route('/api/dashboard/refresh', methods=['POST'])
def api_dashboard():
    """Run a live portfolio analysis (slow), write cache, return result."""
    pid = _resolve_profile_id()
    state = profile_store.snapshot()
    profile = next((p for p in state['profiles'] if p['id'] == pid), None)
    if profile is None:
        raise KeyError('Profile not found')
    holdings_from_profile = state['holdings'].get(str(pid), [])
    fingerprint = _profile_fingerprint(pid, state)
    observed = []
    try:

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
            holdings_csv           = '',
            holdings_rows          = holdings_from_profile,
            indicator_observer     = observed.append,
            watchlist              = watchlist,
            total_capital          = float(capital) if capital else None,
            max_positions          = max_pos,
            risk_per_trade_pct     = risk_pct,
            min_profit_for_pyramid = float(profile['min_profit_for_pyramid']) / 100.0,
            min_cushion_for_pyramid = float(profile['min_cushion_for_pyramid']) / 100.0,
            cash_reserve_pct       = reserve_pct,
            top_signals            = top_n,
            min_buy_confidence     = min_buy,
            min_pyramid_confidence = min_pyr,
            warn_hold_confidence   = warn_hold,
        )
        data["_cache_time"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data["_profile_id"]   = pid
        data["_profile_name"] = profile.get("name", "Default")
        data['_profile_fingerprint'] = fingerprint
        data['_analysis_date_utc'] = datetime.now(timezone.utc).date().isoformat()
        data['_completed_session'] = latest_completed_session()
        data['strategy_version'] = strategy.VERSION
        # Reject results if settings/holdings changed during the slow download.
        if _profile_fingerprint(pid) != fingerprint:
            return jsonify(success=False, error='Profile changed during analysis; refresh again'), 409
        signal_history.record_indicators(pid, observed, expected_config_hash=signal_history.config_hash(profile))
        try:
            analysis_cache.write(pid, data, _profile_cache_path(pid))
        except Exception:
            app.logger.warning('Analysis completed but disposable cache could not be saved')
            data['_cache_warning'] = 'Analysis cache could not be saved'

        return jsonify({"success": True, "cached": False, "data": data})
    except (ValueError, KeyError, db.StorageError):
        raise
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
    data = _json_body()
    data = validate_settings(data)
    name = data.get('name', '')
    if not name:
        return jsonify({"error": "Profile name required"}), 400
    existing = [p for p in db.list_profiles() if p['name'].lower() == name.lower()]
    if existing:
        return jsonify({"error": "A profile with that name already exists"}), 409
    settings = validate_settings({k: data[k] for k in db.DEFAULT_PROFILE_SETTINGS if k in data})
    # Auto-seed watchlist from config.toml if none provided
    if 'watchlist' not in settings:
        settings['watchlist'] = _read_config_watchlist()
    profile = db.create_profile(name, settings)
    if profile is None:
        return jsonify({"error": "Failed to create profile"}), 500
    return jsonify(profile), 201


@app.after_request
def private_responses(response):
    if request.path.startswith('/api/') or request.endpoint == 'login':
        response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@app.route('/api/profiles/active', methods=['GET', 'POST'])
def api_profiles_active():
    if request.method == 'GET':
        return jsonify(db.get_active_profile())
    # POST → set active profile by id
    data = _json_body()
    pid = data.get('profile_id')
    if pid is None:
        return jsonify({"error": "profile_id required"}), 400
    if isinstance(pid, bool) or not str(pid).isdigit():
        raise ValueError('profile_id must be a positive integer')
    _require_profile(int(pid))
    if not db.set_active_profile(int(pid)):
        return jsonify({"error": "Failed to set active profile"}), 500
    return jsonify({"status": "ok", "active_profile_id": int(pid)})


@app.route('/api/profiles/<int:profile_id>', methods=['PUT', 'DELETE'])
def api_profile_detail(profile_id):
    _require_profile(profile_id)
    if request.method == 'PUT':
        data = validate_settings(_json_body())
        if not db.update_profile(profile_id, data):
            raise db.StorageError('Profile update failed')
        _invalidate_portfolio_cache(profile_id)
        return jsonify({"status": "ok"})
    # DELETE
    if len(db.list_profiles()) <= 1:
        return jsonify({"error": "Cannot delete the only profile"}), 400
    if not db.delete_profile(profile_id):
        raise db.StorageError('Profile deletion failed')
    _invalidate_portfolio_cache(profile_id)
    return jsonify({"status": "ok"})


@app.route('/api/profiles/<int:profile_id>/holdings', methods=['GET', 'POST', 'DELETE'])
def api_profile_holdings(profile_id):
    return _holdings_response(profile_id)


@app.route('/api/backup/export')
def api_backup_export():
    pid = _resolve_profile_id() if 'profile_id' in request.args else None
    state = profile_store.snapshot()
    document = backup.export_document(pid, state)
    session['last_backup_state'] = backup.digest(state)
    session['last_backup_scope'] = document['scope']
    session['last_backup_at'] = datetime.now().timestamp()
    filename = f"tradingui-{'all-profiles' if pid is None else 'profile-' + str(pid)}-{datetime.now():%Y%m%d-%H%M%S}.json"
    return Response(json.dumps(document, indent=2, allow_nan=False), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"', 'Cache-Control': 'no-store'})


@app.route('/trades')
def trades_page():
    return render_template('trades.html')


@app.route('/paper')
def paper_page():
    return render_template('paper.html')


@app.route('/api/profiles/<int:profile_id>/paper',methods=['GET','POST'])
def api_paper(profile_id):
    _require_profile(profile_id)
    if request.method=='GET':return jsonify(paper.status(profile_id))
    body=_json_body()
    if set(body)-{'start_date','initial_cash','confirmation'}:raise ValueError('Unknown start fields')
    reset=body.get('confirmation')=='RESET STRATEGY ACCOUNT'
    run=paper.start(profile_id,body.get('start_date'),body.get('initial_cash'),reset=reset)
    return jsonify(run=run),201


@app.route('/api/profiles/<int:profile_id>/paper/run',methods=['POST'])
def api_paper_run(profile_id):
    body=_json_body()
    if set(body)-{'end_date','limit'}:raise ValueError('Unknown run fields')
    return jsonify(paper.catch_up(profile_id,end=body.get('end_date'),limit=body.get('limit',10)))


@app.route('/api/profiles/<int:profile_id>/paper/pause',methods=['POST'])
def api_paper_pause(profile_id):
    body=_json_body()
    if set(body)!={'paused'}:raise ValueError('paused required')
    paper.set_paused(profile_id,body['paused'])
    return jsonify(success=True)


@app.route('/api/profiles/<int:profile_id>/accounts', methods=['GET', 'POST'])
def api_accounts(profile_id):
    _require_profile(profile_id)
    if request.method == 'POST':
        body = _json_body()
        if set(body) != {'kind'}:
            raise ValueError('Account creation requires only kind')
        account, created = ledger.create_account(profile_id, body['kind'], with_status=True)
        return jsonify(dict({k: v for k, v in account.items() if k != 'events'}, created=created)), 201 if created else 200
    state = profile_store.snapshot()
    return jsonify([dict({k: v for k, v in a.items() if k != 'events'}, event_count=len(a['events']))
                    for a in state.get('accounts', []) if a['profile_id'] == profile_id])


@app.route('/api/profiles/<int:profile_id>/accounts/<account_id>/events', methods=['POST'])
def api_account_events(profile_id, account_id):
    body = _json_body()
    if 'events' in body:
        if set(body) != {'events'}:
            raise ValueError('Batch submission requires only events')
        events, count = ledger.add_events(profile_id, account_id, body['events'])
    else:
        event, created = ledger.add_event(profile_id, account_id, body)
        events, count = [event], int(created)
    return jsonify(events=events, created=count), 201 if count else 200


@app.route('/api/profiles/<int:profile_id>/accounts/<account_id>/report', methods=['GET', 'POST'])
def api_account_report(profile_id, account_id):
    marks = {}
    if request.method == 'POST':
        body = _json_body()
        if set(body) != {'marks'} or not isinstance(body['marks'], dict) or len(body['marks']) > 1000:
            raise ValueError('Report body must contain a marks object')
        from validation import ticker
        marks = {ticker(k): str(ledger.decimal(v, 'mark', True)) for k, v in body['marks'].items()}
    result = ledger.report(profile_id, account_id, marks)
    result['valuation_source'] = 'User supplied quotes; not persisted or independently verified' if marks else 'No quotes supplied'
    return jsonify(result)


@app.route('/api/profiles/<int:profile_id>/signals')
def api_observed_signals(profile_id):
    _require_profile(profile_id)
    symbol = request.args.get('ticker')
    if symbol is not None:
        from validation import ticker
        symbol = ticker(symbol)
    records = [r for r in profile_store.snapshot().get('signal_history', [])
               if r['profile_id'] == profile_id and (symbol is None or r['ticker'] == symbol)]
    return jsonify(records[-500:])


@app.route('/api/backup/preview', methods=['POST'])
def api_backup_preview():
    body = _json_body()
    document = body.get('document')
    summary = backup.preview(document)
    state_hash = backup.digest(profile_store.snapshot())
    token = secrets.token_hex(32)
    session['import_preview'] = dict(token=token, document_hash=backup.digest(document),
                                     state_hash=state_hash, at=datetime.now().timestamp())
    return jsonify(summary=summary, preview_token=token)


@app.route('/api/backup/import', methods=['POST'])
def api_backup_import():
    body = _json_body()
    document, mode = body.get('document'), body.get('mode', 'new')
    preview = session.get('import_preview', {})
    if (not preview or not isinstance(body.get('preview_token'), str)
            or not hmac.compare_digest(body['preview_token'], preview['token'])
            or datetime.now().timestamp() - preview['at'] > 1800
            or backup.digest(document) != preview['document_hash']):
        raise ValueError('Preview this exact backup before importing; previews expire after 30 minutes')
    if mode == 'restore':
        if body.get('confirmation') != 'REPLACE ALL PROFILES':
            raise ValueError('Explicit replacement confirmation is required')
        if (session.get('last_backup_state') != preview['state_hash']
                or datetime.now().timestamp() - session.get('last_backup_at', 0) > 1800):
            raise ValueError('Download a current all-profiles backup before replacing profiles')
        if session.get('last_backup_scope') != 'all_profiles':
            raise ValueError('Download an all-profiles backup before replacement')
    result = backup.import_document(document, mode, expected_state=preview['state_hash'])
    session.pop('import_preview', None)
    return jsonify(success=True, **result)


if __name__ == '__main__':
    print("[FLASK] Starting v2.9p Dashboard with REAL backtest engine")
    app.run(debug=False, port=5000, host='0.0.0.0')