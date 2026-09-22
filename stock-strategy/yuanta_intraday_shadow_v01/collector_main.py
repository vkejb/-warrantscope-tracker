#!/usr/bin/env python3
"""Interactive, read-only Stage A Top30 Yuanta quote collector."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import threading
import time

from .collector import AppendOnlyRun, DEFAULT_RUNTIME_DIR, load_stage_a_watchlist, utc_now
from .main import DEFAULT_VENDOR_DIR, _dragged_path, _load_api, _normalise_account, _safe_text


def _quote_time(value) -> str:
    try:
        return f"{int(value.bytHour):02d}:{int(value.bytMin):02d}:{int(value.bytSec):02d}.{int(value.ushtMSec):03d}"
    except Exception:
        return ""


def _book_payload(value) -> dict:
    flag = _safe_text(value.IndexFlag)
    payload = {"index_flag": flag}
    for suffix in ("20", "21", "42", "43"):
        nested = getattr(value, f"IndexFlag_{suffix}", None)
        if nested is not None and suffix in flag:
            payload["prices"] = [_safe_text(getattr(nested, f"Price{i}")) for i in range(1, 6)]
            payload["volumes"] = [_safe_text(getattr(nested, f"Vol{i}")) for i in range(1, 6)]
            return payload
    for suffix in ("50", "51"):
        nested = getattr(value, f"IndexFlag_{suffix}", None)
        if nested is not None and suffix in flag:
            payload["buy_prices"] = [_safe_text(getattr(nested, f"BuyPrice{i}")) for i in range(1, 6)]
            payload["buy_volumes"] = [_safe_text(getattr(nested, f"BuyVol{i}")) for i in range(1, 6)]
            payload["sell_prices"] = [_safe_text(getattr(nested, f"SellPrice{i}")) for i in range(1, 6)]
            payload["sell_volumes"] = [_safe_text(getattr(nested, f"SellVol{i}")) for i in range(1, 6)]
            return payload
    payload["value"] = _safe_text(getattr(value, "Value", ""))
    return payload


def run(
    api_types,
    *,
    seconds: int,
    runtime_dir: Path,
    credentials: dict[str, str] | None = None,
    stop_event: threading.Event | None = None,
    progress_callback=None,
    compress: bool = False,
) -> int:
    seal, items, provenance = load_stage_a_watchlist()
    print(f"Stage A seal：{seal['signal_date']}｜{seal['seal_hash'][:12]}｜30檔")
    print(f"市場別：TWSE {sum(x.market == 'TWSE' for x in items)}｜TPEx {sum(x.market == 'TPEX' for x in items)}")
    print("模式：PROD只讀行情；不含任何委託類別或下單函式。")

    if credentials is None:
        pfx_text = input("請把.pfx憑證拖到此視窗後按Enter：")
        pfx_password = getpass.getpass("憑證密碼（不會顯示）：")
        account_text = input("元大證券帳號（不是CMA帳號）：")
        trading_password = getpass.getpass("證券電子交易密碼（不會顯示）：")
    else:
        pfx_text = credentials.get("pfx", "")
        pfx_password = credentials.get("pfx_password", "")
        account_text = credentials.get("account", "")
        trading_password = credentials.get("trading_password", "")
        credentials.update({"pfx_password": "", "trading_password": ""})
    pfx = _dragged_path(pfx_text)
    if not pfx.is_file() or pfx.suffix.lower() != ".pfx":
        print("找不到有效的.pfx憑證。")
        return 2
    try:
        account = _normalise_account(account_text)
    except ValueError as exc:
        print(exc)
        return 2
    if not pfx_password or not trading_password:
        print("密碼不可空白。")
        return 2

    artifact = AppendOnlyRun(runtime_dir, seal, items, provenance, compress=compress)
    meta = {x.stock_id: x for x in items}
    login_event = threading.Event()
    login_ok = False
    api = None
    opened = logged_in = subscribed_tick = subscribed_book = False
    stock_list = book_list = None
    started_at = utc_now()
    final_status = "FAILED"
    error_type = ""

    def on_response(int_mark, _index, response_name, _handle, value):
        nonlocal login_ok
        name = _safe_text(response_name)
        try:
            if int(int_mark) == 1 and name == "Login":
                code = _safe_text(value.LoginStatus.MsgCode)
                login_ok = code in {"0001", "00001"}
                print(f"登入結果：{code}｜{_safe_text(value.LoginStatus.MsgContent)}｜帳戶筆數 {_safe_text(value.LoginStatus.Count)}")
                if progress_callback:
                    progress_callback({"type": "LOGIN", "ok": login_ok, "code": code})
                login_event.set()
                return
            if int(int_mark) != 2:
                return
            stock_id = _safe_text(getattr(value, "StkCode", ""))
            item = meta.get(stock_id)
            if item is None:
                artifact.callback_error()
                return
            base = {
                "received_at": utc_now(), "signal_date": seal["signal_date"],
                "stock_id": stock_id, "stock_name": item.stock_name, "market": item.market,
                "stage_a_rank": item.rank, "stage_a_score": item.score,
            }
            if name in {"SubscribeStockTick", "SubscribeStocktick"}:
                artifact.append("ticks", {
                    **base, "event_type": "STOCK_TICK", "quote_time": _quote_time(value.Time),
                    "serial_no": int(value.SerialNo), "buy_price": _safe_text(value.BuyPrice),
                    "sell_price": _safe_text(value.SellPrice), "deal_price": _safe_text(value.DealPrice),
                    "deal_volume": _safe_text(value.DealVol), "in_out_flag": _safe_text(value.InOutFlag),
                    "tick_type": _safe_text(value.Type),
                })
            elif name == "SubscribeFiveTickA":
                artifact.append("books", {**base, "event_type": "FIVE_LEVEL", **_book_payload(value)})
        except Exception:
            artifact.callback_error()

    try:
        api = api_types["Trader"]()
        api.SetLogType(api_types["LogType"].NONE)
        api.OnResponse += on_response
        print("正在開啟連線……")
        api.Open(api_types["Environment"].PROD)
        opened = True
        time.sleep(2)
        accepted = bool(api.Login(str(pfx), pfx_password, account, trading_password))
        pfx_password = trading_password = ""
        if not accepted:
            raise RuntimeError("API未接受登入請求")
        if not login_event.wait(20) or not login_ok:
            raise RuntimeError("登入未成功或逾時")
        logged_in = True

        stock_list = api_types["List"][api_types["StockTick"]]()
        book_list = api_types["List"][api_types["FiveTickA"]]()
        enums = {"TWSE": api_types["Market"].TWSE, "TPEX": api_types["Market"].TWOTC}
        for item in items:
            stock = api_types["StockTick"](); stock.MarketType = enums[item.market]; stock.StockCode = item.stock_id; stock_list.Add(stock)
            book = api_types["FiveTickA"](); book.MarketType = enums[item.market]; book.StockCode = item.stock_id; book_list.Add(book)
        api.SubscribeStockTick(account, stock_list, api_types["Language"].UTF8)
        subscribed_tick = True
        api.SubscribeFiveTickA(account, book_list, api_types["Language"].UTF8)
        subscribed_book = True
        print(f"30檔逐筆與五檔訂閱已送出，收集 {seconds} 秒。按 Control-C 可安全停止。")
        if progress_callback:
            progress_callback({"type": "SUBSCRIBED", "watchlist_count": len(items), "seconds": seconds})
        deadline = time.monotonic() + seconds
        next_report = time.monotonic() + 30
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                final_status = "STOPPED_BY_USER"
                print("收到停止要求，正在安全解除訂閱並封存本次資料。")
                return 130
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            if time.monotonic() >= next_report:
                print(f"目前事件：逐筆 {artifact.counts['ticks']}｜五檔 {artifact.counts['books']}")
                if progress_callback:
                    progress_callback({"type": "PROGRESS", **artifact.counts, "remaining_seconds": max(0, int(deadline - time.monotonic()))})
                next_report += 30
        final_status = "COMPLETE"
        return 0
    except KeyboardInterrupt:
        final_status = "INTERRUPTED"
        print("使用者停止，正在安全解除訂閱並封存本次資料。")
        return 130
    except Exception as exc:
        error_type = type(exc).__name__
        print(f"收集失敗：{error_type}: {exc}")
        return 1
    finally:
        pfx_password = trading_password = ""
        if api is not None:
            if subscribed_tick and stock_list is not None:
                try: api.UnSubscribeStockTick(account, stock_list, api_types["Language"].UTF8)
                except Exception: pass
            if subscribed_book and book_list is not None:
                try: api.UnSubscribeFiveTickA(account, book_list, api_types["Language"].UTF8)
                except Exception: pass
            if logged_in:
                try: api.LogOut(); time.sleep(1)
                except Exception: pass
            if opened:
                try: api.Close()
                except Exception: pass
            try: api.Dispose()
            except Exception: pass
        manifest = artifact.finalize(status=final_status, started_at=started_at, ended_at=utc_now(), error_type=error_type)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        if progress_callback:
            progress_callback({"type": "FINAL", "manifest": manifest, "run_dir": str(artifact.run_dir)})


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Stage A Top30 元大即時行情 append-only 收集器")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--vendor-dir", type=Path, default=Path(os.environ.get("YUANTA_SPARK_API_DIR", DEFAULT_VENDOR_DIR)))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not 10 <= args.seconds <= 18000:
        print("seconds必須介於10至18000。")
        return 2
    try:
        return run(_load_api(args.vendor_dir.resolve()), seconds=args.seconds, runtime_dir=args.runtime_dir.resolve())
    except Exception as exc:
        print(f"啟動失敗：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
