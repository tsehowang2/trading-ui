from dataclasses import replace

import numpy as np
import pandas as pd

import strategy


def prices(n=300):
    from market_sessions import session_dates
    index = session_dates('2023-01-03', '2028-01-01')[:n]
    close = np.linspace(100, 180, n)
    return pd.DataFrame(dict(Open=close-1, High=close+2, Low=close-2, Close=close, Volume=1000,
                             Dividends=0, **{'Stock Splits':0}), index=index)


def frame(n=300):
    stock = prices(n)
    spy = stock.copy(); spy['Close'] = np.linspace(100,120,n)
    vix = stock.copy(); vix['Close'] = 20
    term = stock.copy(); term['Close'] = 22
    return strategy.build_indicators(stock, spy, vix, term)


def test_prefix_invariance_and_future_perturbation():
    stock = prices(300); spy=prices(300); vix=prices(300); term=prices(300)
    vix['Close']=20;term['Close']=22
    full = strategy.build_indicators(stock,spy,vix,term)
    for cutoff in (210,250,280):
        prefix=strategy.build_indicators(stock.iloc[:cutoff],spy.iloc[:cutoff],vix.iloc[:cutoff],term.iloc[:cutoff])
        pd.testing.assert_frame_equal(prefix,full.iloc[:cutoff])
    changed=stock.copy(); changed.loc[changed.index[250]:,'Close']*=5
    revised=strategy.build_indicators(changed,spy,vix,term)
    pd.testing.assert_frame_equal(full.iloc[:250],revised.iloc[:250])


def test_finite_indicator_warmup_is_stable():
    stock=prices(500); spy=prices(500);vix=prices(500);term=prices(500)
    vix['Close']=20;term['Close']=22
    a=strategy.snapshot(strategy.build_indicators(stock,spy,vix,term))
    b=strategy.snapshot(strategy.build_indicators(stock.iloc[-250:],spy.iloc[-250:],vix.iloc[-250:],term.iloc[-250:]))
    # Trend-age counters can differ before the finite history window; indicators don't.
    for key in ('score','adx14','atr','sma200','entry_ok','crash_conditions_met'):
        assert a[key] == b[key]


def test_true_term_ratio_and_missing_current_benchmark_block():
    stock=prices(); spy=prices();vix=prices();term=prices()
    vix['Close']=30;term['Close']=25
    df=strategy.build_indicators(stock,spy,vix,term)
    assert df.iloc[-1]['vix_term']==1.2
    bad=strategy.build_indicators(stock,spy,vix,term.iloc[:-1])
    assert strategy.snapshot(bad) is None


def test_close_only_stop_and_ratchet():
    ind=strategy.snapshot(frame(),strategy.StrategyConfig(min_buy_confidence=0))
    ind['cur_low']=1
    state=strategy.PositionState(shares=1,entry_price=160,peak=180,stop=150)
    state, decision=strategy.evaluate_session(state,ind)
    assert decision['action']=='HOLD'
    stop=state.stop
    ind=dict(ind,close=160,atr_frac=.4)
    state, _=strategy.evaluate_session(state,ind)
    assert state.stop>=stop
    _, decision=strategy.evaluate_session(state,dict(ind,close=stop-1))
    assert decision['action']=='SELL'
    assert decision['reason']=='trail-close'


def test_replay_next_open_and_no_terminal_fake_sell():
    df=frame();config=strategy.StrategyConfig(min_buy_confidence=0)
    start=df.index[220];end=df.index[-1]
    result=strategy.replay(df,start,end,config=config)
    first=result['tlog'].iloc[0]
    assert first['signal_date']==start
    assert first['date']==df.index[221]
    assert first['price']==df.iloc[221]['Open']
    assert result['eq'].iloc[0]==100000
    assert not result['tlog']['action'].str.contains('final|terminal').any()
    marked=result['eq'].iloc[-1]
    liquidated=strategy.replay(df,start,end,config=config,liquidate=True)
    assert liquidated['eq'].iloc[-1]==marked-config.commission


def test_live_decision_matches_replay_same_state():
    df=frame();config=strategy.StrategyConfig(min_buy_confidence=0)
    start=df.index[-1]
    _, live=strategy.evaluate_session(strategy.PositionState(),strategy.snapshot(df,config),config)
    historical=strategy.replay(df,start,start,config=config)['decisions'][0]
    assert historical['action']==live['action']
    assert historical['reason']==live['reason']
    assert historical['signal_date']==live['signal_date']


def test_cooldown_and_crash_recovery_shared_state():
    ind=strategy.snapshot(frame(),strategy.StrategyConfig(min_buy_confidence=0))
    config=strategy.StrategyConfig(min_buy_confidence=0)
    state=strategy.PositionState(cooldown=2)
    state,d=strategy.evaluate_session(state,ind,config); assert d['action']=='WAIT'
    state,d=strategy.evaluate_session(state,ind,config); assert d['action']=='WAIT'
    state,d=strategy.evaluate_session(state,ind,config); assert d['action']=='BUY'
    state,d=strategy.evaluate_session(strategy.PositionState(recovery=2),ind,config)
    assert d['reason']=='crash-recovery'