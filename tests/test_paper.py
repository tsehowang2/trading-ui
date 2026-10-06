from copy import deepcopy
from unittest.mock import patch

import pandas as pd
import pytest

import backup
import db
import ledger
import paper
import profile_store
import strategy
from test_strategy import prices


def setup_run(client,profile,cash='10000',symbols=None):
    db.update_profile(profile['id'],dict(watchlist=symbols or ['AAA','BBB','CCC'],max_positions=2,
        min_buy_confidence=0,min_pyramid_confidence=0,cash_reserve_pct=10,risk_per_trade_pct=2))
    frames={s:prices(350) for s in ['AAA','BBB','CCC','SPY','^VIX','^VIX3M']}
    frames['^VIX']['Close']=20;frames['^VIX3M']['Close']=22
    start=frames['AAA'].index[250].strftime('%Y-%m-%d')
    end=frames['AAA'].index[260].strftime('%Y-%m-%d')
    run=paper.start(profile['id'],start,cash)
    downloader=lambda s,start,end:frames[s].loc[:end]
    return run,frames,start,end,downloader


def test_independent_account_ranked_cash_slots_and_next_open(client,profile):
    manual=ledger.create_account(profile['id'],'manual')
    run,frames,start,end,download=setup_run(client,profile)
    pid=profile['id'];second=frames['AAA'].index[251].strftime('%Y-%m-%d')
    paper.catch_up(pid,end=start,downloader=download)
    result=paper.status(pid)
    assert len(result['run']['pending'])==2
    assert {o['ticker'] for o in result['run']['pending']}=={'AAA','BBB'}
    assert len(result['events'])==1
    execution=paper.catch_up(pid,end=second,downloader=download)
    assert 'error' not in execution,execution
    result=paper.status(pid)
    assert len(result['report']['positions'])==2
    assert float(result['report']['cash'])>=0
    assert all(o['fill_session']==second and o['signal_session']==start for o in result['run']['orders'])
    assert ledger.report(pid,manual['id'])['cash']=='0'
    assert profile_store.snapshot()['holdings'][str(pid)]==[]


def test_catchup_chunks_equal_daily_processing_and_no_duplicate_retry(client,profile):
    pid=profile['id'];run,frames,start,end,download=setup_run(client,profile)
    paper.catch_up(pid,end=end,limit=4,downloader=download)
    assert paper.catch_up(pid,end=end,limit=30,downloader=download)['remaining'] is False
    first=paper.status(pid)
    assert paper.catch_up(pid,end=end,downloader=download)['processed']==0
    assert paper.status(pid)==first
    paper.start(pid,start,'10000',reset=True)
    for day in frames['AAA'].loc[start:end].index:
        paper.catch_up(pid,end=day.strftime('%Y-%m-%d'),downloader=download)
    second=paper.status(pid)
    assert first['report']['cash']==second['report']['cash']
    assert first['report']['positions']==second['report']['positions']
    assert [h['equity'] for h in first['run']['history']]==[h['equity'] for h in second['run']['history']]


def test_missing_data_blocks_checkpoint_and_can_resume(client,profile):
    pid=profile['id'];run,frames,start,end,download=setup_run(client,profile)
    bad=deepcopy(frames);missing=frames['AAA'].index[253]
    bad['^VIX3M']=bad['^VIX3M'].drop(missing)
    result=paper.catch_up(pid,end=end,downloader=lambda s,start,end:bad[s].loc[:end])
    assert result['blocked_session']==missing.strftime('%Y-%m-%d')
    assert result['processed']==3
    assert paper.status(pid)['run']['checkpoint']==frames['AAA'].index[252].strftime('%Y-%m-%d')
    resumed=paper.catch_up(pid,end=end,limit=30,downloader=download)
    assert resumed['remaining'] is False


def test_frozen_config_never_retroactively_adds_new_symbol(client,profile):
    pid=profile['id'];run,frames,start,end,download=setup_run(client,profile)
    db.update_profile(pid,{'watchlist':['NEW'],'max_positions':1})
    requested=[]
    def tracked(s,start,end):requested.append(s);return download(s,start,end)
    paper.catch_up(pid,end=end,downloader=tracked)
    assert 'NEW' not in requested
    assert paper.status(pid)['run']['config']['max_positions']==2


def test_price_revision_blocks_mixed_history(client,profile):
    pid=profile['id'];run,frames,start,end,download=setup_run(client,profile)
    paper.catch_up(pid,end=start,downloader=download)
    frames['AAA'].iloc[100,frames['AAA'].columns.get_loc('Close')]+=1
    result=paper.catch_up(pid,end=end,downloader=download)
    assert 'revised' in result['error']
    assert paper.status(pid)['run']['checkpoint']==start


def test_paper_backup_restore_resumes_without_duplicate_fills(client,profile):
    pid=profile['id'];run,frames,start,end,download=setup_run(client,profile)
    paper.catch_up(pid,end=end,limit=30,downloader=download)
    before=paper.status(pid)
    document=backup.export_document()
    assert document['schema_version']==4
    backup.import_document(document,mode='restore')
    assert paper.status(pid)==before
    assert paper.catch_up(pid,end=end,downloader=download)['processed']==0
    imported=backup.import_document(document)
    new_pid=imported['id_mapping'][str(pid)]
    copied=paper.status(new_pid)
    assert copied['run']['account_id']!=before['run']['account_id']
    assert copied['report']['equity']==before['report']['equity']


def test_strategy_funding_edits_are_blocked(client,profile):
    run,*_=setup_run(client,profile)
    from uuid import uuid4
    with pytest.raises(ValueError,match='owns'):
        ledger.add_event(profile['id'],run['account_id'],dict(id=str(uuid4()),kind='DEPOSIT',amount='10',occurred_at='2025-01-01T00:00:00Z'))


def test_pause_resume_and_runtime_reset(client,profile):
    pid=profile['id'];run,frames,start,end,download=setup_run(client,profile)
    paper.set_paused(pid,True)
    assert paper.catch_up(pid,end=end,downloader=download)['processed']==0
    paper.set_paused(pid,False)
    assert paper.catch_up(pid,end=end,downloader=download)['processed']>0
    with pytest.raises(ValueError):paper.start(pid,start,'1000')
    restarted=paper.start(pid,start,'2000',reset=True)
    assert restarted['initial_cash']=='2000' and restarted['checkpoint'] is None


def test_close_exit_fills_next_open_and_releases_cash(client,profile):
    pid=profile['id'];run,frames,start,end,download=setup_run(client,profile)
    day=frames['AAA'].index[253];next_day=frames['AAA'].index[254]
    for symbol in ('AAA','BBB','CCC'):
        frames[symbol].loc[day,['Open','High','Low','Close']]=[140,142,100,110]
        frames[symbol].loc[next_day,['Open','High','Low','Close']]=[100,105,95,100]
    result=paper.catch_up(pid,end=next_day.strftime('%Y-%m-%d'),limit=30,downloader=download)
    assert 'error' not in result
    status=paper.status(pid)
    sells=[o for o in status['run']['orders'] if o['action']=='SELL']
    assert len(sells)==2
    assert all(o['signal_session']==day.strftime('%Y-%m-%d') and o['fill_session']==next_day.strftime('%Y-%m-%d') for o in sells)
    assert status['report']['positions']==[]
    assert all(p['cooldown']>0 for s,p in status['run']['positions'].items() if s in ('AAA','BBB'))


def test_open_gap_resizes_orders_to_actual_cash(client,profile):
    pid=profile['id'];run,frames,start,end,download=setup_run(client,profile)
    paper.catch_up(pid,end=start,downloader=download)
    requested=sum(o['shares'] for o in paper.status(pid)['run']['pending'])
    next_day=frames['AAA'].index[251]
    for symbol in ('AAA','BBB','CCC'):
        frames[symbol].loc[next_day,['Open','High','Low','Close']]=[300,305,295,300]
    result=paper.catch_up(pid,end=next_day.strftime('%Y-%m-%d'),downloader=download)
    assert 'error' not in result
    status=paper.status(pid)
    assert sum(o['filled_shares'] for o in status['run']['orders'])<requested
    assert float(status['report']['cash'])>=float(status['report']['equity'])*.1


def test_concurrent_catchups_do_not_duplicate_sessions(client,profile):
    from concurrent.futures import ThreadPoolExecutor
    pid=profile['id'];run,frames,start,end,download=setup_run(client,profile)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:paper.catch_up(pid,end=end,limit=30,downloader=download),range(2)))
    status=paper.status(pid)
    assert len(status['run']['history'])==11
    assert len({h['session'] for h in status['run']['history']})==11
    assert len({e['id'] for e in status['events']})==len(status['events'])


def test_malformed_pending_backup_rejected(client,profile):
    run,frames,start,end,download=setup_run(client,profile)
    paper.catch_up(profile['id'],end=start,downloader=download)
    document=backup.export_document()
    target=next(p for p in document['profiles'] if p['source_id']==profile['id'])
    target['paper_runs'][0]['pending'][0]['shares']=-1
    document['sha256']=backup.digest({k:v for k,v in document.items() if k!='sha256'})
    with pytest.raises(ValueError):backup.validate_document(document)


def test_profitable_pyramid_is_bounded_and_not_averaging_down(client,profile):
    pid=profile['id'];run,frames,start,end,download=setup_run(client,profile,symbols=['AAA'])
    first=frames['AAA'].index[252];pullback=frames['AAA'].index[253];fill=frames['AAA'].index[254]
    frames['AAA'].loc[first,['Open','High','Low','Close']]=[230,232,228,230]
    frames['AAA'].loc[pullback,['Open','High','Low','Close']]=[215,217,213,215]
    frames['AAA'].loc[fill,['Open','High','Low','Close']]=[215,217,213,215]
    result=paper.catch_up(pid,end=fill.strftime('%Y-%m-%d'),limit=30,downloader=download)
    assert 'error' not in result
    status=paper.status(pid)
    adds=[o for o in status['run']['orders'] if o['action']=='ADD' and o['status']=='FILLED']
    assert adds
    position=status['run']['positions']['AAA']
    assert position['adds']<=3
    assert position['stop']>=230*.85
    assert float(status['report']['cash'])>=float(status['report']['equity'])*.1