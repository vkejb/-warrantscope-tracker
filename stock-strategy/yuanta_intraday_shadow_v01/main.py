#!/usr/bin/env python3
"""Read-only Yuanta SPARK streaming smoke test.

This module intentionally exposes no order API and writes no credentials or
market data.  It is the first connectivity check before an append-only shadow
collector is implemented.
"""

from __future__ import annotations

import argparse
import getpass
import os
import platform
import re
import shlex
import sys
import threading
import time
from pathlib import Path


DEFAULT_VENDOR_DIR = Path("/Users/linyunyan/Downloads/YuantaSparkAPI_osx-arm64_Python")


def _safe_text(value) -> str:
    try:
        return str(value)
    except Exception:
        return "(unavailable)"


def _dragged_path(value: str) -> Path:
    value = value.strip()
    try:
        parts = shlex.split(value)
    except ValueError:
        parts = []
    if len(parts) == 1:
        value = parts[0]
    return Path(value).expanduser().resolve()


def _normalise_account(value: str) -> str:
    digits = re.sub(r"[\s-]", "", value.strip().upper())
    if digits.startswith("S"):
        digits = digits[1:]
    if not re.fullmatch(r"[0-9]{11}", digits):
        raise ValueError("證券帳號必須是分公司4碼加證券帳號7碼；不要輸入CMA交割帳號。")
    return "S" + digits


def _load_api(vendor_dir: Path):
    if platform.machine() != "arm64":
        raise RuntimeError("目前不是 arm64 環境。")
    dotnet_dir = vendor_dir / ".dotnet"
    dll = vendor_dir / "YuantaSparkAPI.dll"
    for path in (dotnet_dir, dll, vendor_dir / "YuantaSparkAPI.runtimeconfig.json"):
        if not path.exists():
            raise FileNotFoundError(f"缺少元大元件：{path.name}")

    os.environ["DOTNET_ROOT"] = str(dotnet_dir)
    os.environ["DOTNET_ROOT_ARM64"] = str(dotnet_dir)
    sys.path.insert(0, str(vendor_dir))

    from pythonnet import load

    load("coreclr")
    import clr

    clr.AddReference("System.Collections")
    clr.AddReference(str(dll))
    from System.Collections.Generic import List
    from YuantaOneAPI import (
        FiveTickA,
        StockTick,
        YuantaSparkAPITrader,
        enumEnvironmentMode,
        enumLangType,
        enumLogType,
        enumMarketType,
        enumQuoteFiveTickIndexType,
    )

    return {
        "List": List,
        "FiveTickA": FiveTickA,
        "StockTick": StockTick,
        "Trader": YuantaSparkAPITrader,
        "Environment": enumEnvironmentMode,
        "Language": enumLangType,
        "LogType": enumLogType,
        "Market": enumMarketType,
        "FiveTickIndex": enumQuoteFiveTickIndexType,
    }


def _market_choice(market_enum):
    choices = {
        "1": ("上市整股", market_enum.TWSE),
        "2": ("上櫃整股", market_enum.TWOTC),
        "3": ("上市盤中零股", market_enum.TWSEODD),
        "4": ("上櫃盤中零股", market_enum.TWOTCODD),
    }
    print("\n行情市場：")
    for key, (name, _) in choices.items():
        print(f"  {key}. {name}")
    selected = input("請選擇 [預設 1]：").strip() or "1"
    if selected not in choices:
        raise ValueError("市場選項不正確。")
    return choices[selected]


def _book_side(result, attr: str):
    side = getattr(result, attr)
    prices = [_safe_text(getattr(side, f"Price{i}")) for i in range(1, 6)]
    volumes = [_safe_text(getattr(side, f"Vol{i}")) for i in range(1, 6)]
    return prices, volumes


def run_stream_test(api_types, seconds: int) -> int:
    print("\n連線環境：")
    print("  1. UAT測試環境")
    print("  2. PROD正式環境（只讀，預設）")
    choice = input("請選擇 [預設 2]：").strip() or "2"
    if choice not in {"1", "2"}:
        print("環境選項不正確。")
        return 2
    environment = api_types["Environment"].PROD if choice == "2" else api_types["Environment"].UAT
    if choice == "2" and input("請輸入 PROD 確認只讀正式環境：").strip() != "PROD":
        print("未確認PROD，已安全取消。")
        return 2

    try:
        market_name, market = _market_choice(api_types["Market"])
    except ValueError as exc:
        print(exc)
        return 2
    symbol = input("股票代碼 [預設 2330]：").strip().upper() or "2330"
    if not re.fullmatch(r"[0-9A-Z]{2,12}", symbol):
        print("股票代碼格式不正確。")
        return 2

    pfx = _dragged_path(input("請把.pfx憑證拖到此視窗後按Enter："))
    if not pfx.is_file() or pfx.suffix.lower() != ".pfx":
        print("找不到有效的.pfx憑證。")
        return 2
    pfx_password = getpass.getpass("憑證密碼（不會顯示）：")
    try:
        account = _normalise_account(input("元大證券帳號（不是CMA帳號）："))
    except ValueError as exc:
        print(exc)
        return 2
    trading_password = getpass.getpass("證券電子交易密碼（不會顯示）：")
    if not pfx_password or not trading_password:
        print("密碼不可空白。")
        return 2

    login_event = threading.Event()
    state = {
        "login_ok": False,
        "stock_ticks": 0,
        "live_stock_ticks": 0,
        "five_ticks": 0,
        "last_print": 0.0,
    }

    five_index = api_types["FiveTickIndex"]

    def on_response(int_mark, _index, response_name, _handle, value):
        name = _safe_text(response_name)
        try:
            if int(int_mark) == 1 and name == "Login":
                status = value.LoginStatus
                code = _safe_text(status.MsgCode)
                content = _safe_text(status.MsgContent)
                state["login_ok"] = code in {"0001", "00001"}
                print(f"\n登入結果：{code}｜{content}｜帳戶筆數 {_safe_text(status.Count)}")
                login_event.set()
                return

            if int(int_mark) == 2 and name in {"SubscribeStockTick", "SubscribeStocktick"}:
                state["stock_ticks"] += 1
                serial = int(value.SerialNo)
                if serial >= 0:
                    state["live_stock_ticks"] += 1
                tick_time = value.Time
                formatted = (
                    f"{int(tick_time.bytHour):02d}:{int(tick_time.bytMin):02d}:"
                    f"{int(tick_time.bytSec):02d}.{int(tick_time.ushtMSec):03d}"
                )
                print(
                    f"成交 {formatted} {value.StkCode}｜價 {_safe_text(value.DealPrice)}｜"
                    f"量 {_safe_text(value.DealVol)}｜買/賣 {_safe_text(value.BuyPrice)}/{_safe_text(value.SellPrice)}"
                )
                return

            if int(int_mark) == 2 and name == "SubscribeFiveTickA":
                state["five_ticks"] += 1
                now = time.monotonic()
                # Five-level events can be extremely frequent. Print at most one
                # compact snapshot every two seconds while still counting all.
                if now - float(state["last_print"]) < 2.0:
                    return
                flag = value.IndexFlag
                if flag == five_index.IndexFlag20:
                    prices, volumes = _book_side(value, "IndexFlag_20")
                    label = "買五檔"
                elif flag == five_index.IndexFlag21:
                    prices, volumes = _book_side(value, "IndexFlag_21")
                    label = "賣五檔"
                else:
                    return
                state["last_print"] = now
                levels = "、".join(f"{p}({v})" for p, v in zip(prices, volumes))
                print(f"{label} {value.StkCode}｜{levels}")
                return

            if int(int_mark) == 0:
                print("系統連線狀態已更新。")
        except Exception as exc:
            print(f"回應解析失敗：{type(exc).__name__}: {exc}")
            if name == "Login":
                login_event.set()

    api = None
    opened = logged_in = subscribed_tick = subscribed_book = False
    stock_list = book_list = None
    try:
        api = api_types["Trader"]()
        api.SetLogType(api_types["LogType"].NONE)
        api.OnResponse += on_response
        print("\n正在開啟連線……")
        api.Open(environment)
        opened = True
        time.sleep(2)

        accepted = bool(api.Login(str(pfx), pfx_password, account, trading_password))
        pfx_password = trading_password = ""
        if not accepted:
            print("API未接受登入請求。")
            return 1
        if not login_event.wait(20) or not state["login_ok"]:
            print("登入未成功或逾時。")
            return 1
        logged_in = True

        stock_list = api_types["List"][api_types["StockTick"]]()
        stock = api_types["StockTick"]()
        stock.MarketType = market
        stock.StockCode = symbol
        stock_list.Add(stock)

        book_list = api_types["List"][api_types["FiveTickA"]]()
        book = api_types["FiveTickA"]()
        book.MarketType = market
        book.StockCode = symbol
        book_list.Add(book)

        subscribed_tick = bool(api.SubscribeStockTick(account, stock_list, api_types["Language"].UTF8))
        subscribed_book = bool(api.SubscribeFiveTickA(account, book_list, api_types["Language"].UTF8))
        print(
            f"\n訂閱請求：{market_name} {symbol}｜逐筆 {'ACCEPTED' if subscribed_tick else 'REJECTED'}｜"
            f"五檔 {'ACCEPTED' if subscribed_book else 'REJECTED'}"
        )
        if not subscribed_tick or not subscribed_book:
            return 1

        print(f"只讀監看 {seconds} 秒；不寫檔、不模擬下單、不送正式委託。\n")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

        print(
            "\n測試摘要："
            f"逐筆事件 {state['stock_ticks']}（正常成交 {state['live_stock_ticks']}）、"
            f"五檔事件 {state['five_ticks']}。"
        )
        if state["live_stock_ticks"] == 0 and state["five_ticks"] == 0:
            print("訂閱已接受但沒有即時事件；若目前非交易時段，這是合理結果。")
        print("actual_orders=0｜actual_fills=0｜broker_order_calls=0")
        return 0
    except KeyboardInterrupt:
        print("\n使用者取消，正在安全解除訂閱。")
        return 130
    except Exception as exc:
        print(f"\n測試失敗：{type(exc).__name__}: {exc}")
        return 1
    finally:
        pfx_password = trading_password = ""
        if api is not None:
            if subscribed_tick and stock_list is not None:
                try:
                    api.UnSubscribeStockTick(account, stock_list, api_types["Language"].UTF8)
                except Exception:
                    pass
            if subscribed_book and book_list is not None:
                try:
                    api.UnSubscribeFiveTickA(account, book_list, api_types["Language"].UTF8)
                except Exception:
                    pass
            if logged_in:
                try:
                    api.LogOut()
                    time.sleep(1)
                except Exception:
                    pass
            if opened:
                try:
                    api.Close()
                except Exception:
                    pass
            try:
                api.Dispose()
            except Exception:
                pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="元大SPARK只讀逐筆與五檔測試")
    parser.add_argument("--seconds", type=int, default=60, help="監看秒數，預設60")
    parser.add_argument(
        "--vendor-dir",
        type=Path,
        default=Path(os.environ.get("YUANTA_SPARK_API_DIR", DEFAULT_VENDOR_DIR)),
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not 5 <= args.seconds <= 600:
        print("seconds必須介於5至600。")
        return 2
    print("元大SPARK即時行情只讀測試")
    print("安全邊界：沒有委託類別、沒有下單函式、帳密不寫入檔案。")
    try:
        api_types = _load_api(args.vendor_dir.resolve())
    except Exception as exc:
        print(f"環境載入失敗：{type(exc).__name__}: {exc}")
        return 1
    print(f"環境載入成功：Python {platform.python_version()}／{platform.machine()}／.NET 8")
    return run_stream_test(api_types, args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())
