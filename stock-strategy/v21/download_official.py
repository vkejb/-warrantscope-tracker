#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


UA = "Mozilla/5.0 V21BaselineResearch/1.0"


def get_json(url: str, params: dict | None = None, attempts: int = 4):
    command=["/usr/bin/curl","-sS","-L","--max-time","20","--get",url]
    for key,value in (params or {}).items():
        command.extend(["--data-urlencode",f"{key}={value}"])
    for attempt in range(attempts):
        try:
            return json.loads(subprocess.check_output(command,timeout=25).decode("utf-8-sig"))
        except Exception:
            if attempt + 1 == attempts:
                raise
            time.sleep(1.5 * (attempt + 1))


def roc_date(date: str, slash: bool = False) -> str:
    year = int(date[:4]) - 1911
    return f"{year:03d}/{date[4:6]}/{date[6:]}" if slash else f"{year:03d}{date[4:]}"


def download_taiex(output: Path, start_year: int = 2021, end_year: int = 2026, end_month: int = 8):
    months=[]
    for year in range(start_year,end_year+1):
        for month in range(1,13):
            if year==end_year and month>end_month: break
            months.append(f"{year}{month:02d}01")

    def fetch(month):
        payload=get_json("https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST",{"date":month,"response":"json"})
        rows=[]
        for row in payload.get("data",[]):
            parts=row[0].split("/"); date=f"{int(parts[0])+1911:04d}{int(parts[1]):02d}{int(parts[2]):02d}"
            rows.append([date]+[x.replace(",","") for x in row[1:5]])
        return rows

    rows=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(fetch,months): rows.extend(result)
    unique={row[0]:row for row in rows}
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open("w",newline="",encoding="utf-8") as handle:
        writer=csv.writer(handle); writer.writerow(["date","open","high","low","close"])
        writer.writerows(unique[d] for d in sorted(unique))
    print(json.dumps({"taiex_rows":len(unique),"first":min(unique),"last":max(unique),"output":str(output)},ensure_ascii=False))


def download_twse_prices(output: Path, dates: list[str]):
    """Download official TWSE OHLCV rows for explicitly requested dates."""
    rows = []
    required = {
        "證券代號",
        "證券名稱",
        "成交股數",
        "開盤價",
        "最高價",
        "最低價",
        "收盤價",
    }
    for date in sorted(set(dates)):
        payload = get_json(
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
            {"date": date, "type": "ALLBUT0999", "response": "json"},
        )
        if payload.get("stat") != "OK":
            raise RuntimeError(f"TWSE price request failed for {date}: {payload}")
        table = next(
            (
                table
                for table in payload.get("tables", [])
                if required.issubset(set(table.get("fields", [])))
            ),
            None,
        )
        if table is None:
            raise RuntimeError(f"TWSE price table not found for {date}")
        fields = table["fields"]
        indices = {field: fields.index(field) for field in required}
        count = 0
        for source in table.get("data", []):
            values = [
                source[indices[field]].replace(",", "").strip()
                for field in ("成交股數", "開盤價", "最高價", "最低價", "收盤價")
            ]
            if any(value in {"", "--"} for value in values):
                continue
            rows.append(
                [
                    date,
                    source[indices["證券代號"]].strip(),
                    source[indices["證券名稱"]].strip(),
                    *values,
                ]
            )
            count += 1
        print(json.dumps({"date": date, "official_price_rows": count}), flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "code", "name", "volume", "open", "high", "low", "close"])
        writer.writerows(rows)
    print(json.dumps({"dates": sorted(set(dates)), "rows": len(rows), "output": str(output)}))


def download_stock_info(output: Path):
    """Download FinMind's dated Taiwan security master and industry labels."""
    payload = get_json(
        "https://api.finmindtrade.com/api/v4/data",
        {"dataset": "TaiwanStockInfo"},
    )
    if payload.get("status") != 200:
        raise RuntimeError(payload.get("msg", "FinMind stock-info error"))
    rows = payload.get("data", [])
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["date", "stock_id", "stock_name", "type", "industry_category"]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    print(json.dumps({"stock_info_rows": len(rows), "output": str(output)}))


def parse_int(value):
    text=str(value).replace(",","").strip()
    return int(text) if text not in {"","--"} else 0


def fetch_institutional_date(date: str, wanted: set[str]):
    records=[]; errors=[]
    try:
        payload=get_json("https://www.twse.com.tw/rwd/zh/fund/T86",{"response":"json","date":date,"selectType":"ALLBUT0999"})
        fields=payload.get("fields",[])
        code_i=fields.index("證券代號")
        foreign_i=next(i for i,f in enumerate(fields) if "外陸資買賣超股數(不含外資自營商)" in f)
        trust_i=fields.index("投信買賣超股數")
        for row in payload.get("data",[]):
            code=row[code_i].strip()
            if code in wanted: records.append([code,date,parse_int(row[foreign_i]),parse_int(row[trust_i]),"TWSE"])
    except Exception as exc:
        errors.append(f"TWSE:{type(exc).__name__}:{exc}")
    try:
        payload=get_json("https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php",{
            "l":"zh-tw","o":"json","se":"EW","t":"D","d":roc_date(date,True),"s":"0,asc"})
        tables=payload.get("tables",[]); data=tables[0].get("data",[]) if tables else []
        for row in data:
            code=row[0].strip()
            if code in wanted: records.append([code,date,parse_int(row[10]),parse_int(row[13]),"TPEX"])
    except Exception as exc:
        errors.append(f"TPEX:{type(exc).__name__}:{exc}")
    return date,records,errors


def fetch_tpex_date(date: str, wanted: set[str]):
    """Fetch the official TPEx daily institutional table for one trading date.

    The TPEx JSON table is laid out as code/name followed by seven three-column
    buy/sell/net groups.  Index 10 is foreign investors' net amount excluding
    foreign dealers, and index 13 is investment trusts' net amount.
    """
    payload = get_json(
        "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php",
        {
            "l": "zh-tw",
            "o": "json",
            "se": "EW",
            "t": "D",
            "d": roc_date(date, True),
            "s": "0,asc",
        },
    )
    tables = payload.get("tables", [])
    data = tables[0].get("data", []) if tables else []
    records = []
    for row in data:
        code = row[0].strip()
        if code in wanted:
            records.append(
                [code, date, parse_int(row[10]), parse_int(row[13]), "TPEX"]
            )
    return date, records


def download_tpex(
    needs_path: Path, output: Path, checkpoint: Path, workers: int = 2
):
    """Supplement an institutional CSV with official TPEx historical rows."""
    needs = {
        date: set(codes)
        for date, codes in json.loads(needs_path.read_text(encoding="utf-8")).items()
    }
    checkpoint_payload = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    coverage = (
        {date: set(codes) for date, codes in checkpoint_payload.items()}
        if isinstance(checkpoint_payload, dict)
        else {}
    )
    existing = {}
    if output.exists():
        with output.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                existing[(row["stock_id"], row["date"])] = row

    unresolved = {
        date: {
            code for code in codes if (code, date) not in existing
        }
        for date, codes in needs.items()
    }
    pending = [
        date
        for date in sorted(needs)
        if unresolved[date]
        and not unresolved[date].issubset(coverage.get(date, set()))
    ]
    mismatch_count = 0
    mismatch_samples = []
    failures = {}

    def date_checked(date: str) -> bool:
        return all(
            (code, date) in existing or code in coverage.get(date, set())
            for code in needs[date]
        )

    def persist():
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="", encoding="utf-8") as handle:
            fields = [
                "stock_id",
                "date",
                "foreign_netbuy",
                "investment_trust_netbuy",
                "market",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(
                existing[key] for key in sorted(existing, key=lambda x: (x[1], x[0]))
            )
        checkpoint.write_text(
            json.dumps(
                {date: sorted(codes) for date, codes in sorted(coverage.items())}
            ),
            encoding="utf-8",
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_tpex_date, date, unresolved[date]): date
            for date in pending
        }
        for number, future in enumerate(as_completed(futures), 1):
            date = futures[future]
            try:
                _, records = future.result()
                for code, point_date, foreign, trust, market in records:
                    key = (code, point_date)
                    prior = existing.get(key)
                    if prior and (
                        parse_int(prior["foreign_netbuy"]) != foreign
                        or parse_int(prior["investment_trust_netbuy"]) != trust
                    ):
                        mismatch_count += 1
                        if len(mismatch_samples) < 10:
                            mismatch_samples.append(
                                {
                                    "stock_id": code,
                                    "date": point_date,
                                    "existing_foreign": parse_int(
                                        prior["foreign_netbuy"]
                                    ),
                                    "official_foreign": foreign,
                                    "existing_trust": parse_int(
                                        prior["investment_trust_netbuy"]
                                    ),
                                    "official_trust": trust,
                                }
                            )
                    existing[key] = {
                        "stock_id": code,
                        "date": point_date,
                        "foreign_netbuy": foreign,
                        "investment_trust_netbuy": trust,
                        "market": market,
                    }
                coverage.setdefault(date, set()).update(unresolved[date])
            except Exception as exc:
                failures[date] = f"{type(exc).__name__}:{exc}"
            if number % 25 == 0:
                persist()
                print(
                    json.dumps(
                        {
                            "processed": number,
                            "completed": sum(date_checked(date) for date in needs),
                            "total": len(needs),
                            "rows": len(existing),
                            "failures": len(failures),
                            "overlap_mismatches": mismatch_count,
                        }
                    ),
                    flush=True,
                )
    persist()
    print(
        json.dumps(
            {
                "dates_requested": len(needs),
                "dates_completed": sum(date_checked(date) for date in needs),
                "rows": len(existing),
                "failures": failures,
                "overlap_mismatches": mismatch_count,
                "mismatch_samples": mismatch_samples,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def download_institutional(needs_path: Path, output: Path, checkpoint: Path, workers: int = 4):
    needs={date:set(codes) for date,codes in json.loads(needs_path.read_text(encoding="utf-8")).items()}
    completed=set(json.loads(checkpoint.read_text()) if checkpoint.exists() else [])
    existing={}
    if output.exists():
        with output.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle): existing[(row["stock_id"],row["date"])]=row
    pending=[d for d in needs if d not in completed]
    failures={}

    def persist():
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open("w",newline="",encoding="utf-8") as handle:
            fields=["stock_id","date","foreign_netbuy","investment_trust_netbuy","market"]
            writer=csv.DictWriter(handle,fieldnames=fields); writer.writeheader()
            writer.writerows(existing[key] for key in sorted(existing,key=lambda x:(x[1],x[0])))
        checkpoint.write_text(json.dumps(sorted(completed)),encoding="utf-8")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(fetch_institutional_date,date,needs[date]):date for date in pending}
        for number,future in enumerate(as_completed(futures),1):
            date,records,errors=future.result()
            for code,d,foreign,trust,market in records:
                existing[(code,d)]={"stock_id":code,"date":d,"foreign_netbuy":foreign,"investment_trust_netbuy":trust,"market":market}
            if errors: failures[date]=errors
            else: completed.add(date)
            if number%25==0:
                persist()
                print(json.dumps({"processed":number,"completed":len(completed),"total":len(needs),"rows":len(existing),"failures":len(failures)}),flush=True)
    persist()
    print(json.dumps({"dates_requested":len(needs),"dates_completed":len(completed),"rows":len(existing),"failures":failures},ensure_ascii=False),flush=True)


def download_finmind(needs_path: Path, output: Path, checkpoint: Path, workers: int = 2):
    needs={date:set(codes) for date,codes in json.loads(needs_path.read_text(encoding="utf-8")).items()}
    needed_by_code={}
    for date,codes in needs.items():
        for code in codes: needed_by_code.setdefault(code,set()).add(date)
    checkpoint_payload=json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    coverage={code:set(dates) for code,dates in checkpoint_payload.items()} if isinstance(checkpoint_payload,dict) else {}
    existing={}
    if output.exists():
        with output.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle): existing[(row["stock_id"],row["date"])]=row
    completed={
        code for code,dates in needed_by_code.items()
        if dates.issubset(coverage.get(code,set())) or all((code,date) in existing for date in dates)
    }

    def fetch(code):
        payload=get_json("https://api.finmindtrade.com/api/v4/data",{
            "dataset":"TaiwanStockInstitutionalInvestorsBuySell","data_id":code,
            "start_date":"2021-12-01","end_date":"2026-08-28"})
        if payload.get("status")!=200: raise RuntimeError(payload.get("msg","FinMind error"))
        daily={}
        for row in payload.get("data",[]):
            if row["date"].replace("-","") not in needed_by_code[code]: continue
            if row["name"] not in {"Foreign_Investor","Investment_Trust"}: continue
            date=row["date"].replace("-",""); point=daily.setdefault(date,{"foreign":0,"trust":0})
            value=int(row["buy"])-int(row["sell"])
            if row["name"]=="Foreign_Investor": point["foreign"]+=value
            else: point["trust"]+=value
        return code,daily

    def persist():
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open("w",newline="",encoding="utf-8") as handle:
            fields=["stock_id","date","foreign_netbuy","investment_trust_netbuy","market"]
            writer=csv.DictWriter(handle,fieldnames=fields); writer.writeheader()
            writer.writerows(existing[key] for key in sorted(existing,key=lambda x:(x[1],x[0])))
        checkpoint.write_text(json.dumps({code:sorted(dates) for code,dates in sorted(coverage.items())}),encoding="utf-8")

    failures={}; pending=[c for c in sorted(needed_by_code) if c not in completed]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(fetch,code):code for code in pending}
        for number,future in enumerate(as_completed(futures),1):
            code=futures[future]
            try:
                _,daily=future.result()
                for date,point in daily.items():
                    existing[(code,date)]={"stock_id":code,"date":date,"foreign_netbuy":point["foreign"],"investment_trust_netbuy":point["trust"],"market":"FinMind(TWSE/TPEx)"}
                coverage.setdefault(code,set()).update(needed_by_code[code])
                completed.add(code)
            except Exception as exc:
                failures[code]=f"{type(exc).__name__}:{exc}"
            if number%10==0:
                persist(); print(json.dumps({"processed":number,"completed":len(completed),"total":len(needed_by_code),"rows":len(existing),"failures":len(failures)}),flush=True)
    persist(); print(json.dumps({"codes_requested":len(needed_by_code),"codes_completed":len(completed),"rows":len(existing),"failures":failures},ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command",required=True)
    taiex=sub.add_parser("taiex"); taiex.add_argument("--output",type=Path,required=True)
    prices=sub.add_parser("twse-prices"); prices.add_argument("--output",type=Path,required=True); prices.add_argument("--dates",nargs="+",required=True)
    stock_info=sub.add_parser("stock-info"); stock_info.add_argument("--output",type=Path,required=True)
    inst=sub.add_parser("institutional"); inst.add_argument("--needs",type=Path,required=True); inst.add_argument("--output",type=Path,required=True); inst.add_argument("--checkpoint",type=Path,required=True); inst.add_argument("--workers",type=int,default=4)
    fm=sub.add_parser("finmind"); fm.add_argument("--needs",type=Path,required=True); fm.add_argument("--output",type=Path,required=True); fm.add_argument("--checkpoint",type=Path,required=True); fm.add_argument("--workers",type=int,default=2)
    tpex=sub.add_parser("tpex"); tpex.add_argument("--needs",type=Path,required=True); tpex.add_argument("--output",type=Path,required=True); tpex.add_argument("--checkpoint",type=Path,required=True); tpex.add_argument("--workers",type=int,default=2)
    args=parser.parse_args()
    if args.command=="taiex": download_taiex(args.output)
    elif args.command=="twse-prices": download_twse_prices(args.output,args.dates)
    elif args.command=="stock-info": download_stock_info(args.output)
    elif args.command=="institutional": download_institutional(args.needs,args.output,args.checkpoint,args.workers)
    elif args.command=="tpex": download_tpex(args.needs,args.output,args.checkpoint,args.workers)
    else: download_finmind(args.needs,args.output,args.checkpoint,args.workers)


if __name__=="__main__": main()
