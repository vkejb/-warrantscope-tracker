"""Yuanta SPARK .NET loader used only by the live execution adapter."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import sys


REQUIRED_TRADER_METHODS = {
    "SendStockOrder": 3,
    "GetRealReport": 2,
    "GetRealReportMerge": 2,
    "GetStoreSummary": 2,
}

REQUIRED_STOCK_ORDER_PROPERTIES = {
    "Account",
    "APCode",
    "BasketNo",
    "BuySell",
    "Identify",
    "OrderNo",
    "OrderQty",
    "OrderType",
    "Price",
    "PriceFlag",
    "StkCode",
    "Time_in_force",
    "TradeDate",
    "TradeKind",
}


def validate_sdk_contract(api_types: dict[str, object]) -> dict[str, object]:
    """Fail before login if the installed DLL does not match this adapter."""

    import clr

    trader_type = clr.GetClrType(api_types["Trader"])
    stock_type = clr.GetClrType(api_types["StockOrder"])
    methods: dict[str, set[int]] = {}
    for method in trader_type.GetMethods():
        methods.setdefault(str(method.Name), set()).add(len(method.GetParameters()))
    missing_methods = {
        name: count
        for name, count in REQUIRED_TRADER_METHODS.items()
        if count not in methods.get(name, set())
    }
    members = {str(item.Name) for item in stock_type.GetProperties()}
    members.update(str(item.Name) for item in stock_type.GetFields())
    missing_members = sorted(REQUIRED_STOCK_ORDER_PROPERTIES - members)
    if missing_methods or missing_members:
        raise RuntimeError(
            "incompatible Yuanta SPARK SDK: "
            f"missing_method_overloads={missing_methods!r}; "
            f"missing_stock_order_members={missing_members!r}"
        )
    return {
        "method_parameter_counts": {
            name: sorted(methods.get(name, set())) for name in REQUIRED_TRADER_METHODS
        },
        "stock_order_properties": sorted(REQUIRED_STOCK_ORDER_PROPERTIES),
    }


def load_api_types(vendor_dir: str | Path) -> dict[str, object]:
    vendor_dir = Path(vendor_dir).expanduser().resolve()
    dll = vendor_dir / "YuantaSparkAPI.dll"
    if not dll.is_file():
        raise FileNotFoundError(f"missing YuantaSparkAPI.dll: {dll}")

    dotnet_dir = vendor_dir / ".dotnet"
    if dotnet_dir.is_dir():
        os.environ.setdefault("DOTNET_ROOT", str(dotnet_dir))
        if platform.machine().lower() in {"arm64", "aarch64"}:
            os.environ.setdefault("DOTNET_ROOT_ARM64", str(dotnet_dir))

    if str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))
    if sys.platform == "win32":
        os.add_dll_directory(str(vendor_dir))

    from pythonnet import load

    load("coreclr")
    import clr

    clr.AddReference("System.Collections")
    clr.AddReference(str(dll))

    from System.Collections.Generic import List
    from YuantaOneAPI import (
        StockOrder,
        YuantaSparkAPITrader,
        enumEnvironmentMode,
        enumLangType,
        enumLogType,
    )

    result = {
        "List": List,
        "StockOrder": StockOrder,
        "Trader": YuantaSparkAPITrader,
        "Environment": enumEnvironmentMode,
        "Language": enumLangType,
        "LogType": enumLogType,
    }
    validate_sdk_contract(result)
    return result
