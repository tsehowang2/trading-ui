from unittest.mock import patch

import pandas as pd

import data
from market_sessions import latest_completed_session


def bars(dates, price=100):
    return pd.DataFrame({'Open': price, 'High': price+2, 'Low': price-2,
                         'Close': price, 'Volume': 1000}, index=pd.to_datetime(dates))


def test_completed_session_handles_open_close_holidays_and_early_close():
    assert latest_completed_session('2025-07-07T15:00:00Z') == '2025-07-03'
    assert latest_completed_session('2025-07-07T20:10:00Z') == '2025-07-03'
    assert latest_completed_session('2025-07-07T20:31:00Z') == '2025-07-07'
    assert latest_completed_session('2025-07-03T17:31:00Z') == '2025-07-03'
    assert latest_completed_session('2025-03-10T20:31:00Z') == '2025-03-10'


def test_weekend_cache_and_new_symbol_only(client, tmp_path, monkeypatch):
    monkeypatch.setattr(data, 'DATA_CACHE_DIR', str(tmp_path / 'market'))
    monkeypatch.setattr(data, 'latest_completed_session', lambda: '2025-07-07')
    dates = ['2025-07-02', '2025-07-03']
    with patch.object(data.yf, 'download', return_value=bars(dates)) as download:
        data.cached_download('AAPL', '2025-07-02', '2025-07-06')
        data.cached_download('AAPL', '2025-07-02', '2025-07-06')
        data.cached_download('MSFT', '2025-07-02', '2025-07-06')
    assert download.call_count == 2
    assert all(c.kwargs['auto_adjust'] is False for c in download.call_args_list)


def test_forming_bar_filtered_and_missing_bar_not_claimed(client, tmp_path, monkeypatch):
    monkeypatch.setattr(data, 'DATA_CACHE_DIR', str(tmp_path / 'market'))
    monkeypatch.setattr(data, 'latest_completed_session', lambda: '2025-07-03')
    with patch.object(data.yf, 'download', return_value=bars(['2025-07-02','2025-07-07'])) as download:
        result = data.cached_download('AAPL', '2025-07-02', '2025-07-07')
        data.cached_download('AAPL', '2025-07-02', '2025-07-07')
    assert result.index.max() == pd.Timestamp('2025-07-02')
    assert result.attrs['missing_sessions'] == ['2025-07-03']
    assert download.call_count == 1


def test_new_session_download_is_incremental(client, tmp_path, monkeypatch):
    monkeypatch.setattr(data, 'DATA_CACHE_DIR', str(tmp_path / 'market'))
    clock = ['2025-07-03']
    monkeypatch.setattr(data, 'latest_completed_session', lambda: clock[0])
    with patch.object(data.yf, 'download', side_effect=[bars(['2025-07-02','2025-07-03']), bars(['2025-07-07'])]) as download:
        data.cached_download('AAPL', '2025-07-02', '2025-07-03')
        clock[0] = '2025-07-07'
        result = data.cached_download('AAPL', '2025-07-02', '2025-07-07')
    assert download.call_args_list[1].kwargs['start'] == '2025-07-07'
    assert len(result) == 3


def test_prelisting_history_does_not_force_full_download_each_session(client,tmp_path,monkeypatch):
    monkeypatch.setattr(data,'DATA_CACHE_DIR',str(tmp_path/'market'))
    clock=['2025-07-03'];monkeypatch.setattr(data,'latest_completed_session',lambda:clock[0])
    with patch.object(data.yf,'download',side_effect=[bars(['2025-07-02','2025-07-03']),bars(['2025-07-07'])]) as download:
        data.cached_download('AAPL','2025-01-01','2025-07-03')
        clock[0]='2025-07-07'
        data.cached_download('AAPL','2025-01-01','2025-07-07')
    assert download.call_args_list[1].kwargs['start']=='2025-07-07'


def test_historical_request_does_not_erase_newer_cached_bars(client,tmp_path,monkeypatch):
    monkeypatch.setattr(data,'DATA_CACHE_DIR',str(tmp_path/'market'))
    monkeypatch.setattr(data,'latest_completed_session',lambda:'2025-07-07')
    with patch.object(data.yf,'download',return_value=bars(['2025-07-02','2025-07-03','2025-07-07'])) as download:
        data.cached_download('AAPL','2025-07-02','2025-07-07')
        historical=data.cached_download('AAPL','2025-07-02','2025-07-03')
        current=data.cached_download('AAPL','2025-07-02','2025-07-07')
    assert download.call_count==1
    assert len(historical)==2 and len(current)==3


def test_new_split_refreshes_old_price_scale(client,tmp_path,monkeypatch):
    monkeypatch.setattr(data,'DATA_CACHE_DIR',str(tmp_path/'market'))
    clock=['2025-07-03'];monkeypatch.setattr(data,'latest_completed_session',lambda:clock[0])
    initial=bars(['2025-07-02','2025-07-03'],price=100)
    split=bars(['2025-07-07'],price=50);split['Stock Splits']=2
    refreshed=bars(['2025-07-02','2025-07-03','2025-07-07'],price=50)
    refreshed['Stock Splits']=[0,0,2]
    with patch.object(data.yf,'download',side_effect=[initial,split,refreshed]) as download:
        data.cached_download('AAPL','2025-07-02','2025-07-03')
        clock[0]='2025-07-07'
        result=data.cached_download('AAPL','2025-07-02','2025-07-07')
    assert download.call_count==3
    assert (result['Close']==50).all()