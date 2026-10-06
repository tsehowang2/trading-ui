"""Independent, cash-constrained daily strategy simulation; no broker calls.

Run configuration/universe is frozen at launch. Historical starts explicitly
apply that selected universe retrospectively (not survivorship-free research).
Sessions commit independently; retries resume at the saved checkpoint.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone, date
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
import math
from uuid import uuid4

import ledger
import profile_store
import strategy
from market_sessions import latest_completed_session, session_dates, schedule

VERSION = 'paper-cash-v1'


def D(value):
    return Decimal(str(value))


def quote(value):
    return str(D(value).quantize(D('0.00000001')))


def config_from(profile):
    return {k: deepcopy(v) for k,v in profile.items() if k not in ('id','name','is_active','capital','top_signals')}


def run_in(state, pid):
    profile_store._profile(state,pid)
    return next((r for r in state.get('paper_runs',[]) if r['profile_id']==pid),None)


def start(pid, start_date, cash, reset=False):
    try:
        requested=date.fromisoformat(start_date).isoformat()
    except (TypeError,ValueError):
        raise ValueError('Choose a valid start date') from None
    if requested < strategy.HISTORY_START or requested > latest_completed_session():
        raise ValueError('Start date must be between 1990 and the latest completed session')
    first=session_dates(requested,latest_completed_session())[0].strftime('%Y-%m-%d')
    amount=ledger.decimal(cash,'initial_cash',True)
    with profile_store.transaction(True) as state:
        profile=profile_store._profile(state,pid)
        old=run_in(state,pid)
        if old and not reset:
            raise ValueError('A strategy run already exists; resume it or explicitly reset')
        if not profile['watchlist']:
            raise ValueError('Select a nonempty watchlist before starting')
        account=next((a for a in state['accounts'] if a['profile_id']==pid and a['kind']=='paper'),None)
        if account and account['events'] and not reset:
            raise ValueError('Paper account already has funding/history; explicit reset is required')
        if account:
            account['events']=[]
        else:
            account=dict(id=str(uuid4()),profile_id=pid,kind='paper',currency='USD',
                         created_at=datetime.now(timezone.utc).isoformat(),events=[])
            state['accounts'].append(account)
        opening=schedule(first,first).iloc[0]['market_open']
        funding=ledger.normalize_event(dict(id=str(uuid4()),kind='DEPOSIT',amount=str(amount),
                                           occurred_at=opening.isoformat(),notes='Strategy simulation seed cash'))
        funding.update(sequence=1,recorded_at=datetime.now(timezone.utc).isoformat())
        account['events'].append(funding)
        run=dict(id=str(uuid4()),profile_id=pid,account_id=account['id'],start_date=first,requested_start=requested,
                 initial_cash=str(amount),created_at=datetime.now(timezone.utc).isoformat(),
                 version=VERSION,strategy_version=strategy.VERSION,config=config_from(profile),
                 checkpoint=None,paused=False,positions={},pending=[],history=[],orders=[],input_prefixes={},
                 price_model='Yahoo split-adjusted synthetic shares; not broker/raw-share execution')
        state['paper_runs']=[r for r in state.get('paper_runs',[]) if r['profile_id']!=pid]+[run]
    return deepcopy(run)


def _event(account, kind, when, **values):
    for key in ('price','amount','shares'):
        if key in values:
            values[key]=str(D(values[key]).quantize(D('0.00000001')))
    e=ledger.normalize_event(dict(id=str(uuid4()),kind=kind,occurred_at=when,**values))
    e.update(sequence=len(account['events'])+1,recorded_at=datetime.now(timezone.utc).isoformat())
    if len(account['events'])>=20_000:raise ValueError('Account event limit reached')
    account['events'].append(e)
    return e


def _size(cash,equity,held,symbol,price,stop,config,total_risk):
    """Consumes remaining cash/exposure/stop-risk; fees included, no leverage."""
    reserve=equity*D(config['cash_reserve_pct'])/100
    per_risk=equity*D(config['risk_per_trade_pct'])/100
    aggregate=equity*min(D('0.20'),D(config['risk_per_trade_pct'])*D(config['max_positions'])/100)
    exposure=equity/D(config['max_positions'])
    existing=held.get(symbol,{})
    quantity=D(existing.get('shares',0))
    distance=max(price-stop,D('0'))
    if distance<=0:return 0,'No positive stop-risk distance'
    fee=D('2')
    budgets=[(cash-reserve-fee)/price,(exposure-quantity*price)/price,
             (per_risk-quantity*distance)/distance,(aggregate-total_risk)/distance]
    sh=max(int(min(budgets).to_integral_value(rounding=ROUND_FLOOR)),0)
    return sh,'Cash/reserve, exposure or stop-risk limit' if sh==0 else 'Ranked within shared account limits'


def _prefix(frame,session):
    # Hash provider bars, not drifting window initialization.
    columns=[c for c in ('Open','High','Low','Close','Volume','Dividends','Stock Splits','spy','vix','vix3m') if c in frame]
    return strategy.frame_revision(frame.loc[:session,columns].astype(float))


def advance(run,account,session,frames):
    """Mutates private session copies only; caller commits atomically."""
    cfg=run['config'];sc=strategy.StrategyConfig(min_buy_confidence=float(cfg['min_buy_confidence']))
    symbols=sorted(set(cfg['watchlist'])|set(run['positions'])|{o['ticker'] for o in run['pending']})
    day=pd.Timestamp(session)
    indicators={}
    for symbol in symbols:
        frame=frames.get(symbol)
        if frame is None or frame.empty or day not in frame.index:
            raise ValueError(f'{session}: missing data for {symbol}')
        row=frame.loc[day]
        if not all(math.isfinite(float(row[c])) and float(row[c])>0 for c in ('Open','Close')):
            raise ValueError(f'{session}: invalid stock bar for {symbol}')
        if run['checkpoint'] and run['input_prefixes'].get(symbol) and _prefix(frame,run['checkpoint'])!=run['input_prefixes'][symbol]:
            raise ValueError(f'{symbol}: processed price history was revised; reset/replay rather than mixing scales')
        ind=strategy.snapshot(frame.loc[:day],sc)
        if ind is None:
            raise ValueError(f'{session}: insufficient warmup or missing SPY/VIX/VIX3M for {symbol}')
        indicators[symbol]=ind
    market=schedule(session,session).iloc[0]
    when=market['market_open'].isoformat()
    held=run['positions']
    # Dividend entitlement for overnight holders before open fills, explicitly
    # on the same synthetic split-adjusted share scale as the provider series.
    for symbol,p in held.items():
        dividend=float(frames[symbol].loc[day].get('Dividends',0))
        if dividend>0 and p['shares']>0:
            _event(account,'DIVIDEND',when,ticker=symbol,amount=str(D(p['shares'])*D(dividend)),notes='Modeled provider cash dividend')
    for order in sorted(run['pending'],key=lambda o:(o['action']!='SELL',o['rank'])):
        symbol=order['ticker'];px=D(quote(frames[symbol].loc[day]['Open']));p=held.get(symbol)
        filled=0;reason=''
        if order['action']=='SELL' and p and p['shares']>0:
            filled=p['shares']
            if D(filled)*px<D('2'):raise ValueError('Sell proceeds cannot cover modeled fee')
            e=_event(account,'SELL',when,ticker=symbol,shares=str(filled),price=str(px),fee='2',tag='strategy',notes=f"Signal {order['signal_session']} {order['reason']} — modeled next-open fill")
            held[symbol]=dict(asdict(strategy.PositionState(cooldown=0 if order['reason']=='crash' else 10,recovery=10 if order['reason']=='crash' else 0)),adds=0,last_add=0,last_add_session=None)
        elif order['action'] in ('BUY','ADD'):
            if order['action']=='ADD' and (not p or p['shares']<=0 or px<=D(p['entry_price']) or px<D(p['last_add'])):
                reason='Open gap would average down or no position remains'
            report=ledger.replay(account['events']);cash=D(report['cash'])
            active={s:p for s,p in held.items() if p['shares']>0}
            equity=cash+sum((D(p['shares'])*D(frames[s].loc[day]['Open']) for s,p in active.items()),D(0))
            risk=sum((D(p['shares'])*max(D(frames[s].loc[day]['Open'])-D(p['stop']),D(0)) for s,p in active.items()),D(0))
            if not p or p['shares']==0:
                if len(active)>=cfg['max_positions']:reason='Maximum position slots reached'
            stop=D(p['stop']) if p and p['shares']>0 else px*(1-D(order['atr_frac']))
            if not reason:
                sized,reason=_size(cash,equity,active,symbol,px,stop,cfg,risk)
                filled=min(sized,order['shares'])
            if filled:
                e=_event(account,'BUY',when,ticker=symbol,shares=str(filled),price=str(px),fee='2',tag='strategy',notes=f"Signal {order['signal_session']} {order['reason']} — modeled next-open fill")
                if p and p['shares']>0:
                    old=p['shares'];p['entry_price']=(old*p['entry_price']+filled*float(px))/(old+filled)
                    p['shares']+=filled;p['adds']+=1;p['last_add']=float(px);p['last_add_session']=session
                else:
                    held[symbol]=dict(asdict(strategy.PositionState(shares=filled,entry_price=float(px),peak=float(px),stop=float(stop))),adds=0,last_add=float(px),last_add_session=session)
        order.update(fill_session=session,status='FILLED' if filled else 'REJECTED',fill_price=str(px) if filled else None,
                     filled_shares=filled,fill_reason=reason,event_id=e['id'] if filled else None)
        run['orders'].append(order)
    run['pending']=[]
    marks={s:quote(ind['close']) for s,ind in indicators.items()}
    report=ledger.replay(account['events'],marks)
    cash,equity=D(report['cash']),D(report['equity'])
    active={s:p for s,p in held.items() if p['shares']>0}
    candidates=[];decisions=[];planned=[]
    for symbol in symbols:
        p=held.get(symbol,dict(asdict(strategy.PositionState()),adds=0,last_add=0,last_add_session=None))
        updated,decision=strategy.evaluate_session(strategy.PositionState(**{k:p[k] for k in asdict(strategy.PositionState())}),indicators[symbol],sc)
        p.update(asdict(updated));held[symbol]=p
        ind=indicators[symbol];decisions.append(dict(ticker=symbol,**decision,score=ind['score'],close=ind['close']))
        if decision['action']=='SELL':
            planned.append(dict(ticker=symbol,action='SELL',shares=p['shares'],reason=decision['reason'],signal_session=session,rank=-1,atr_frac=ind['atr_frac']))
        elif decision['action']=='BUY' and symbol in cfg['watchlist']:
            candidates.append((0,symbol,'BUY',decision['reason']))
        elif p['shares']>0 and decision['action']=='HOLD' and symbol in cfg['watchlist']:
            profit=ind['close']/p['entry_price']-1;cushion=(ind['close']-p['stop'])/ind['close']
            if (ind['entry_ok'] and ind['score']>=cfg['min_pyramid_confidence'] and profit>=cfg['min_profit_for_pyramid']/100
                and cushion>=cfg['min_cushion_for_pyramid']/100 and p['adds']<3
                and ind['close']>=p['last_add']*(1+max(cfg['min_profit_for_pyramid']/100,.01)) and p['last_add_session']!=session):
                candidates.append((1,symbol,'ADD','pyramid'))
    risk=sum((D(p['shares'])*max(D(indicators[s]['close'])-D(p['stop']),D(0)) for s,p in active.items()),D(0))
    planned_held=deepcopy(active);remaining=cash;rejected=[]
    candidates.sort(key=lambda c:(c[0],-indicators[c[1]]['score'],-indicators[c[1]]['rs_vs_spy'],c[1]))
    for rank,(priority,symbol,action,reason) in enumerate(candidates):
        ind=indicators[symbol];px=D(ind['close']);p=planned_held.get(symbol)
        if action=='BUY' and len(planned_held)>=cfg['max_positions']:
            rejected.append(dict(ticker=symbol,reason='Maximum position slots reached'));continue
        stop=D(p['stop']) if p else px*(1-D(ind['atr_frac']))
        sh,why=_size(remaining,equity,planned_held,symbol,px,stop,cfg,risk)
        if not sh:rejected.append(dict(ticker=symbol,reason=why));continue
        planned.append(dict(ticker=symbol,action=action,shares=sh,reason=reason,signal_session=session,rank=rank,atr_frac=ind['atr_frac']))
        remaining-=sh*px+D(2);risk+=sh*(px-stop)
        planned_held[symbol]=dict(shares=(p['shares'] if p else 0)+sh,stop=float(stop))
    run['pending']=planned;run['checkpoint']=session
    run['input_prefixes']={s:_prefix(frames[s],session) for s in symbols}
    previous_peak=max([float(run['initial_cash'])]+[float(h['equity']) for h in run['history']])
    run['history'].append(dict(session=session,equity=report['equity'],cash=report['cash'],net_profit=report['net_profit'],
                               realized_pnl=report['realized_pnl'],drawdown=float(equity)/max(previous_peak,float(equity))-1,
                               decisions=decisions,rejected=rejected,mode='retrospective-modeled-session',
                               processed_at=datetime.now(timezone.utc).isoformat()))
    ledger.replay(account['events'],marks)
    return run


# pandas is used only for indexed daily data supplied by the cache/provider.
import pandas as pd


def status(pid):
    state=profile_store.snapshot();run=run_in(state,pid)
    if run is None:return dict(run=None,latest_completed_session=latest_completed_session())
    account=ledger.account_in(state,pid,run['account_id'])
    marks={d['ticker']:quote(d['close']) for d in run['history'][-1]['decisions']} if run['history'] else {}
    return dict(run=deepcopy(run),report=ledger.replay(account['events'],marks),events=deepcopy(account['events']),latest_completed_session=latest_completed_session())


def catch_up(pid, end=None, limit=10, downloader=None):
    if type(limit) is not int or not 1<=limit<=30:raise ValueError('Session chunk limit must be 1–30')
    if end is not None:
        if not isinstance(end,str):raise ValueError('end_date must be a date string')
        try:date.fromisoformat(end)
        except ValueError:raise ValueError('Invalid end date') from None
    target=min(end or latest_completed_session(),latest_completed_session())
    initial=profile_store.snapshot();run=run_in(initial,pid)
    if not run:raise ValueError('Start a strategy account first')
    if target<run['start_date']:raise ValueError('Catch-up end date precedes the strategy start')
    if run['paused']:return dict(processed=0,remaining=False,paused=True)
    if run['strategy_version']!=strategy.VERSION:raise ValueError('Strategy version changed; reset/replay explicitly')
    begin=(pd.Timestamp(run['checkpoint'])+pd.Timedelta(days=1)).strftime('%Y-%m-%d') if run['checkpoint'] else run['start_date']
    sessions=session_dates(begin,target)
    if sessions.empty:return dict(processed=0,remaining=False,checkpoint=run['checkpoint'])
    chunk=sessions[:limit]
    if downloader is None:
        from data import cached_download
        downloader=cached_download
    symbols=sorted(set(run['config']['watchlist'])|set(run['positions']))
    warmup=(pd.Timestamp(run['start_date'])-pd.Timedelta(days=500)).strftime('%Y-%m-%d')
    benchmarks=[downloader(s,warmup,target) for s in ('SPY','^VIX','^VIX3M')]
    frames={s:strategy.build_indicators(downloader(s,warmup,target),*benchmarks,as_of=target) for s in symbols}
    processed=0
    for day in chunk:
        session=day.strftime('%Y-%m-%d')
        try:
            with profile_store.transaction(True) as state:
                current=run_in(state,pid)
                if not current or current['id']!=run['id']:raise ValueError('Run changed during download')
                if current['paused']:break
                if current['checkpoint'] and current['checkpoint']>=session:continue
                next_expected=session_dates((pd.Timestamp(current['checkpoint'])+pd.Timedelta(days=1)).strftime('%Y-%m-%d') if current['checkpoint'] else current['start_date'],session)
                if next_expected.empty or next_expected[0].strftime('%Y-%m-%d')!=session:
                    raise ValueError('Session order changed; retry from the saved checkpoint')
                account=ledger.account_in(state,pid,current['account_id'])
                advance(current,account,session,frames)
            processed+=1
        except ValueError as error:
            return dict(processed=processed,remaining=True,blocked_session=session,error=str(error))
    latest=run_in(profile_store.snapshot(),pid)
    return dict(processed=processed,remaining=bool(latest['checkpoint'] is None or latest['checkpoint']<sessions[-1].strftime('%Y-%m-%d')),
                checkpoint=latest['checkpoint'])


def set_paused(pid,paused):
    if type(paused) is not bool:raise ValueError('paused must be boolean')
    with profile_store.transaction(True) as state:
        run=run_in(state,pid)
        if not run:raise ValueError('No strategy run exists')
        run['paused']=paused


def validate_runs(runs,accounts):
    if not isinstance(runs,list) or len(runs)>100:raise ValueError('Invalid paper runs')
    seen=set()
    for r in runs:
        if not isinstance(r,dict) or set(r)!= {'id','profile_id','account_id','start_date','requested_start','initial_cash','created_at','version','strategy_version','config','checkpoint','paused','positions','pending','history','orders','input_prefixes','price_model'}:
            raise ValueError('Invalid paper run structure')
        ledger.identifier(r['id']);ledger.stamp(r['created_at']);ledger.decimal(r['initial_cash'],'initial_cash',True)
        if r['profile_id'] in seen or r['version']!=VERSION or r['strategy_version']!=strategy.VERSION:
            raise ValueError('Unsupported/duplicate paper run')
        a=next((a for a in accounts if a['id']==r['account_id'] and a['profile_id']==r['profile_id'] and a['kind']=='paper'),None)
        if not a:raise ValueError('Paper run account reference missing')
        for k in ('start_date','requested_start'):
            date.fromisoformat(r[k])
        if r['checkpoint'] is not None:date.fromisoformat(r['checkpoint'])
        if type(r['paused']) is not bool or not isinstance(r['history'],list) or not isinstance(r['positions'],dict):raise ValueError('Invalid run state')
        from validation import settings
        settings(r['config'])
        required={'watchlist','max_positions','cash_reserve_pct','risk_per_trade_pct','min_buy_confidence','min_pyramid_confidence','min_profit_for_pyramid','min_cushion_for_pyramid'}
        if not required.issubset(r['config']):raise ValueError('Missing paper configuration')
        if not isinstance(r['pending'],list) or not isinstance(r['orders'],list) or not isinstance(r['input_prefixes'],dict):raise ValueError('Invalid paper orders')
        for symbol,p in r['positions'].items():
            from validation import ticker
            ticker(symbol)
            keys=set(asdict(strategy.PositionState()))|{'adds','last_add','last_add_session'}
            if not isinstance(p,dict) or set(p)!=keys:raise ValueError('Invalid paper position fields')
            for k in keys-{'last_add_session'}:
                if isinstance(p[k],bool) or not isinstance(p[k],(int,float)) or not math.isfinite(p[k]) or p[k]<0:
                    raise ValueError('Invalid numeric paper position')
        event_ids={e['id'] for e in a['events']}
        for order in r['pending']+r['orders']:
            if not isinstance(order,dict) or not {'ticker','action','shares','signal_session','rank','atr_frac','reason'}.issubset(order):raise ValueError('Invalid paper order fields')
            ticker(order['ticker'])
            if order['action'] not in ('BUY','ADD','SELL') or type(order['shares']) is not int or order['shares']<=0:raise ValueError('Invalid paper order quantity')
            date.fromisoformat(order['signal_session'])
            if order.get('event_id') and order['event_id'] not in event_ids:raise ValueError('Paper fill event reference missing')
        for h in r['history']:
            if not isinstance(h,dict) or not {'session','equity','cash','decisions','rejected','net_profit','drawdown'}.issubset(h):raise ValueError('Invalid paper equity history')
            date.fromisoformat(h['session']);ledger.decimal(str(D(h['equity']).normalize()),'equity');ledger.decimal(str(D(h['cash']).normalize()),'cash')
        json.dumps(r,allow_nan=False)
        report=ledger.replay(a['events'])
        actual={p['ticker']:D(p['shares']) for p in report['positions']}
        recorded={s:D(p['shares']) for s,p in r['positions'].items() if p['shares']>0}
        if actual!=recorded:raise ValueError('Paper checkpoint positions do not match ledger')
        if r['history'] and r['history'][-1]['session']!=r['checkpoint']:raise ValueError('Paper history/checkpoint mismatch')
        if bool(r['history'])!=(r['checkpoint'] is not None):raise ValueError('Paper checkpoint lacks history')
        if any(r['history'][i]['session']>=r['history'][i+1]['session'] for i in range(len(r['history'])-1)):raise ValueError('Paper history must be chronological')
        seen.add(r['profile_id'])
    return deepcopy(runs)