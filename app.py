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

app = Flask(__name__, template_folder=os.path.join(_HERE, 'templates'))
app.secret_key = "your-secret-key"

HOLDINGS_PATH  = os.path.join(_HERE, "holdings.json")
CACHE_PATH     = os.path.join(_HERE, "results", "portfolio_cache.json")

# ── JSON holdings helpers ──────────────────────────────────────────────────────
def _read_holdings_json() -> list[dict]:
    """Return raw holdings list from JSON (no live enrichment)."""
    if not os.path.exists(HOLDINGS_PATH):
        return []
    with open(HOLDINGS_PATH, encoding="utf-8") as f:
        return json.load(f)

def _write_holdings_json(rows: list[dict]) -> None:
    """Persist holdings list to JSON (only canonical fields)."""
    clean = []
    for h in rows:
        ticker = str(h.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        clean.append(dict(
            ticker      = ticker,
            entry_price = float(h.get("entry_price", 0)),
            shares      = float(h.get("shares", 0)),
            notes       = str(h.get("notes", "")),
        ))
    with open(HOLDINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)

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
                close     = ind.get('close', 0.0)
                atr_frac  = ind.get('atr_frac', 0.15)
                h['current_price'] = safe_round(close)
                entry_price = float(h.get('entry_price', 0))
                if entry_price > 0 and close > 0:
                    h['pnl_pct'] = safe_round((close - entry_price) / entry_price, 0.0)
                h['mkt_value']  = safe_round(float(h.get('shares', 0)) * close, 0.0)
                h['stop_price'] = safe_round(close * (1 - atr_frac), 0.0)
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
        return jsonify(_read_holdings_json())
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
        ticker   = request.args.get('ticker', '').upper()
        holdings = [h for h in _read_holdings_json() if h['ticker'] != ticker]
        _write_holdings_json(holdings)
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
    try:
        import tomllib
        config_path = os.path.join(_HERE, "config.toml")
        if os.path.exists(config_path):
            with open(config_path, "rb") as f:
                cfg = tomllib.load(f)
            watchlist = cfg.get("portfolio", {}).get("watchlist", [])
            return jsonify({"watchlist": watchlist})
        return jsonify({"watchlist": []})
    except Exception as e:
        return jsonify({"watchlist": [], "error": str(e)})


@app.route('/backtest')
def backtest_page():
    return render_template('backtest.html')


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


@app.route('/api/dashboard/cached')
def api_dashboard_cached():
    """Return the last cached portfolio analysis result (instant, no recompute)."""
    if not os.path.exists(CACHE_PATH):
        return jsonify({"success": False, "cached": False, "error": "No cache yet — click Refresh to run analysis."})
    with open(CACHE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    mtime = os.path.getmtime(CACHE_PATH)
    data["_cache_time"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    return jsonify({"success": True, "cached": True, "data": data})


@app.route('/api/dashboard/refresh')
def api_dashboard():
    """Run a live portfolio analysis (slow), write cache, return result."""
    try:
        import tomllib
        config_path = os.path.join(_HERE, "config.toml")
        cfg = {}
        if os.path.exists(config_path):
            with open(config_path, "rb") as f:
                cfg = tomllib.load(f)

        p = cfg.get("portfolio", {})
        r = cfg.get("risk", {})
        s = cfg.get("confidence", {})

        # Resolve holdings path relative to workspace root (same as CLI)
        holdings_rel = p.get("holdings", "v2_9p/holdings.json")
        if os.path.isabs(holdings_rel):
            holdings_path = holdings_rel
        else:
            holdings_path = os.path.join(_ROOT, holdings_rel)

        watchlist   = p.get("watchlist", [])
        capital     = p.get("capital", None) or None
        max_pos     = int(p.get("max_positions", 10))
        risk_pct    = float(r.get("risk_per_trade_pct", 2.0))
        reserve_pct = float(r.get("cash_reserve_pct", 10.0)) / 100.0
        top_n       = int(r.get("top_signals", 5))
        min_buy     = float(s.get("min_buy_confidence", 0.60))
        min_pyr     = float(s.get("min_pyramid_confidence", 0.65))
        warn_hold   = float(s.get("warn_hold_confidence", 0.40))

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
        data["_cache_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({"success": True, "cached": False, "data": data})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    print("[FLASK] Starting v2.9p Dashboard with REAL backtest engine")
    app.run(debug=False, port=5000, host='0.0.0.0')