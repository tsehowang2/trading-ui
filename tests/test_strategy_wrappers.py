from unittest.mock import patch

import pandas as pd

import main
import strategy
import data
from test_strategy import prices


def downloader_frames():
    stock=prices(350)
    spy=stock.copy();spy['Close']=stock['Close']*.8
    vix=stock.copy();vix['Close']=20
    term=stock.copy();term['Close']=22
    return {'AAPL':stock,'SPY':spy,'^VIX':vix,'^VIX3M':term}


def test_live_and_backtest_share_same_last_decision(client,tmp_path,monkeypatch):
    frames=downloader_frames();end=frames['AAPL'].index[-1].strftime('%Y-%m-%d')
    monkeypatch.setattr(data,'DATA_CACHE_DIR',str(tmp_path/'market'))
    with patch.object(main,'cached_download',side_effect=lambda symbol,start,end:frames[symbol]):
        live=main._get_live_indicators('AAPL',min_buy_confidence=0,as_of=end)
        replay=main.run_backtest('AAPL',end,end,verbose=False,min_buy_confidence=0)
    assert live['entry_ok']
    assert replay['pending_order']['action']=='BUY'
    assert replay['decisions'][0]['signal_date']==live['date']
    assert replay['tlog'].empty
    assert replay['strategy_version']==live['strategy_version']


def test_indicator_cache_avoids_recalculation_and_revision_drift(client,tmp_path,monkeypatch):
    frames=downloader_frames();end=frames['AAPL'].index[-1].strftime('%Y-%m-%d')
    monkeypatch.setattr(data,'DATA_CACHE_DIR',str(tmp_path/'market'))
    download=lambda symbol,start,end:frames[symbol]
    first=strategy.load_frame('AAPL',end,download,live=True)
    with patch.object(strategy,'build_indicators',side_effect=AssertionError('Must use indicator cache')):
        second=strategy.load_frame('AAPL',end,download,live=True)
    pd.testing.assert_frame_equal(first,second)
    assert strategy.frame_revision(first)==strategy.frame_revision(second)


def test_missing_current_vix3m_never_uses_prior_ready_bar(client,tmp_path,monkeypatch):
    frames=downloader_frames();end=frames['AAPL'].index[-1].strftime('%Y-%m-%d')
    frames['^VIX3M']=frames['^VIX3M'].iloc[:-1]
    monkeypatch.setattr(data,'DATA_CACHE_DIR',str(tmp_path/'market'))
    with patch.object(main,'cached_download',side_effect=lambda symbol,start,end:frames[symbol]):
        assert main._get_live_indicators('AAPL',as_of=end) is None


def test_live_requests_recent_history_but_backtest_keeps_full_history(client, tmp_path, monkeypatch):
    frames = downloader_frames()
    end = frames['AAPL'].index[-1].strftime('%Y-%m-%d')
    monkeypatch.setattr(data, 'DATA_CACHE_DIR', str(tmp_path / 'market'))
    with patch.object(main, 'cached_download', side_effect=lambda symbol, start, end: frames[symbol]) as download:
        main._get_live_indicators('AAPL', as_of=end)
        assert len(download.call_args_list) == 4
        assert all(pd.Timestamp(end) - pd.Timestamp(call.args[1]) < pd.Timedelta(days=800)
                   for call in download.call_args_list)
        download.reset_mock()
        main.run_backtest('AAPL', end, end, verbose=False)
        assert all(call.args[1] == strategy.HISTORY_START for call in download.call_args_list)