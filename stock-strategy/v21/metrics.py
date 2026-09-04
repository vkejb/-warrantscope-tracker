from __future__ import annotations

import math
import statistics


RETURN_FIELDS={"Gross":"gross_return","Net 0% slippage":"net_return","Net 0.1% one-way slippage":"net_return_slippage_0_1"}


def summarize(rows: list[dict], field: str) -> dict:
    values=[r[field] for r in rows if r.get(field) is not None and math.isfinite(r[field])]
    wins=[v for v in values if v>0]; losses=[v for v in values if v<0]
    win_sum=sum(wins); loss_sum=abs(sum(losses))
    avg_win=statistics.fmean(wins) if wins else 0; avg_loss=statistics.fmean(losses) if losses else 0
    return {
        "trades":len(values),"win_rate":len(wins)/len(values) if values else 0,
        "average_return":statistics.fmean(values) if values else 0,"median_return":statistics.median(values) if values else 0,
        "average_win":avg_win,"average_loss":avg_loss,"payoff_ratio":avg_win/abs(avg_loss) if avg_loss else None,
        "profit_factor":win_sum/loss_sum if loss_sum else None,"expectancy":statistics.fmean(values) if values else 0,
        "max_win":max(values) if values else None,"max_loss":min(values) if values else None,
        "average_holding_days":statistics.fmean(r["holding_days"] for r in rows) if rows else 0,
        "median_holding_days":statistics.median(r["holding_days"] for r in rows) if rows else 0,
        "average_mfe":statistics.fmean(r["mfe"] for r in rows if r.get("mfe") is not None) if rows else 0,
        "average_mae":statistics.fmean(r["mae"] for r in rows if r.get("mae") is not None) if rows else 0,
    }


def grouped_summary(rows: list[dict], group_field: str, return_field: str, extra_exit: bool=False) -> list[dict]:
    groups={}
    for row in rows: groups.setdefault(row[group_field],[]).append(row)
    output=[]
    for key,items in sorted(groups.items()):
        summary=summarize(items,return_field)
        result={group_field:key,"sample_size":len(items),"win_rate":summary["win_rate"],"average_return":summary["average_return"],
                "median_return":summary["median_return"],"profit_factor":summary["profit_factor"],"average_mfe":summary["average_mfe"],"average_mae":summary["average_mae"]}
        if extra_exit:
            post=[r["post_exit_5d_max_return"] for r in items if r.get("post_exit_5d_max_return") is not None]
            result.update(average_holding_days=summary["average_holding_days"],
                          average_post_exit_5d_max_return=statistics.fmean(post) if post else None)
        output.append(result)
    return output
