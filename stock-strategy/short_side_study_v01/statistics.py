"""Descriptive statistics with no parameter fitting or missing-exposure zeros."""
from math import sqrt


def metrics(rows):
    complete=[r for r in rows if r['status']=='SCORED_TRADE']
    values=[r['net_pnl'] for r in sorted(complete,key=lambda r:r['date'])]
    gross_profit=sum(v for v in values if v>0)
    gross_loss=-sum(v for v in values if v<0)
    peak=equity=drawdown=0.
    for value in values:
        equity+=value; peak=max(peak,equity); drawdown=max(drawdown,peak-equity)
    mtm_peak=mtm_equity=mtm_dd=0.
    for row in sorted(rows,key=lambda r:r['date']):
        for mark in row.get('mark_pnl_path',[]):
            current=mtm_equity+mark
            mtm_peak=max(mtm_peak,current)
            mtm_dd=max(mtm_dd,mtm_peak-current)
        if row.get('net_pnl') is not None:
            mtm_equity+=row['net_pnl']
            mtm_peak=max(mtm_peak,mtm_equity)
            mtm_dd=max(mtm_dd,mtm_peak-mtm_equity)
    incomplete=[r for r in rows if r.get('net_pnl') is None]
    winners=[v for v in values if v>0];losers=[v for v in values if v<0]
    entered=[r for r in rows if r.get('entry_quantity',0)>0]
    def average(name):
        observed=[r[name] for r in entered if r.get(name) is not None]
        return sum(observed)/len(observed) if observed else None
    return {'trades':len(values),'entered_trades':len(entered),'net_pnl':sum(values) if not incomplete else None,
            'completed_subset_net_pnl':sum(values),'profit_factor':gross_profit/gross_loss if gross_loss else None,
            'win_rate':sum(v>0 for v in values)/len(values) if values else None,
            'max_daily_realized_drawdown':drawdown,'mean_trade_pnl':sum(values)/len(values) if values else None,
            'max_quote_mark_account_drawdown':mtm_dd if not incomplete else None,
            'avg_winner':sum(winners)/len(winners) if winners else None,'avg_loser':sum(losers)/len(losers) if losers else None,
            'mean_mfe_net_pnl':average('mfe_net_pnl'),'mean_mae_net_pnl':average('mae_net_pnl'),
            'mean_giveback_net_pnl':average('giveback_net_pnl'),
            'unscorable_or_open_exposure_days':len(incomplete),'account_complete':not incomplete}


def robustness(rows):
    days=sorted({r['date'] for r in rows})
    scored=[r for r in rows if r['status']=='SCORED_TRADE']
    cases=[{'removed_day':d,**metrics([r for r in rows if r['date']!=d])} for d in days]
    output={'leave_one_day_out':cases}
    for name,select in [('remove_best_trade',max),('remove_worst_trade',min)]:
        if scored:
            removed=select(scored,key=lambda r:r['net_pnl'])
            output[name]={'removed_date':removed['date'],'removed_net_pnl':removed['net_pnl'],
                          **metrics([r for r in rows if r is not removed])}
        else:
            output[name]=None
    return output


def correlation(long,short):
    short_by_day={r['date']:r for r in short}
    pairs=[(a['net_pnl'],short_by_day[a['date']]['net_pnl']) for a in long if a['date'] in short_by_day and a['net_pnl'] is not None and short_by_day[a['date']]['net_pnl'] is not None]
    if len(pairs)<2:
        return None
    x=[a for a,b in pairs]; y=[b for a,b in pairs]
    mx=sum(x)/len(x); my=sum(y)/len(y)
    denominator=sqrt(sum((v-mx)**2 for v in x)*sum((v-my)**2 for v in y))
    return sum((a-mx)*(b-my) for a,b in pairs)/denominator if denominator else None
