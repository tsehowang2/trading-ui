"""Isolated local browser smoke server; no real DB/accounts/market downloads."""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop('DATABASE_URL', None)
os.environ.pop('RENDER', None)
os.environ.pop('APP_PASSWORD_HASH', None)

import db
import app as web
import data
from test_strategy import prices


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='tradingui-browser-') as directory:
        db._HERE_DB = directory
        db.PROFILES_JSON_PATH = str(Path(directory) / 'profiles.json')
        db.ACTIVE_PROFILE_JSON_PATH = str(Path(directory) / 'active_profile.json')
        web.CACHE_PATH = str(Path(directory) / 'results' / 'portfolio_cache.json')
        profile = db.create_profile('Browser fixture', {'watchlist': ['AAPL', 'MSFT']})
        db.upsert_holding(profile['id'], dict(ticker='AAPL', entry_price=123.45, shares=2))
        stock=prices(350)
        def fixture_download(symbol,start,end,**kwargs):
            frame=stock.copy()
            if symbol=='^VIX':frame['Close']=20
            if symbol=='^VIX3M':frame['Close']=22
            return frame.loc[(frame.index>=start)&(frame.index<=end)]
        data.cached_download=fixture_download
        web.app.run(host='127.0.0.1', port=5055, debug=False, use_reloader=False)