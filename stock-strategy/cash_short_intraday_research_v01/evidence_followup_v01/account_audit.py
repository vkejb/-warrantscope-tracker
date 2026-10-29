"""Read-only arithmetic reconciliation of frozen round-two ledgers.

No strategy/runtime, SDK, network or broker module is imported.  Archived books
only supply causal signal-side prices for an explicitly labelled reconstruction
of the existing sizing formula; original fills and PnL are never regenerated.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path


VARIANTS = ("E0", "E1", "E2")
SAFETY = {"mode": "SHADOW_ONLY", "NO_LIVE_ORDER": True, "actual_orders": 0,
          "broker_connections": 0, "market_subscriptions": 0}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def dec(value):
    return Decimal(str(value))


def total(rows, field="net_pnl"):
    values = [r.get(field) for r in rows]
    return None if any(v is None for v in values) else float(sum(map(dec, values), Decimal(0)))


def fee(notional, cfg):
    # Exactly the original replay.fee convention, including its six-place input.
    return max(int(cfg["minimum_commission"]), int((dec(round(float(notional), 6)) *
               dec(cfg["commission_rate"])).to_integral_value(rounding=ROUND_CEILING)))


def tick(price):
    p = dec(price)
    for ceiling, step in ((10, ".01"), (50, ".05"), (100, ".1"),
                          (500, ".5"), (1000, "1")):
        if p < ceiling:
            return Decimal(step)
    return Decimal(5)


def shift(price, count, direction):
    p = dec(price)
    for _ in range(count):
        p += direction * tick(p if direction > 0 else p - Decimal(".00000001"))
    return float(p)


def structural_risk(entry, exit_, quantity, cfg):
    ev = (dec(entry) * quantity).quantize(Decimal(".000001"))
    xv = (dec(exit_) * quantity).quantize(Decimal(".000001"))
    commission = fee(ev, cfg) + fee(xv, cfg)
    tax = int((ev * dec(cfg["sell_tax_rate"])).to_integral_value(rounding=ROUND_CEILING))
    return float(xv - ev + commission + tax)


def sizing_reconstruction(signal, book, equity, cfg):
    """Audit-only transcription of original sizing, not a new trading rule."""
    cap = min(float(cfg["capital_twd"]), float(equity))
    base = {"sizing_evidence": "RECONSTRUCTED_EXISTING_FORMULA_NOT_ORIGINAL_SIZING_LEDGER",
            "sizing_equity_input": equity, "capital_ceiling": cfg["capital_twd"],
            "sizing_available_capital": cap, "risk_budget_twd": cfg["risk_per_trade_twd"],
            "daily_trade_limit": cfg["maximum_trades_per_day"],
            "maximum_positions": cfg["maximum_positions"],
            "real_broker_cash": None, "real_broker_cash_status": "UNKNOWN"}
    if not book or not book.get("bids"):
        return {**base, "reconstructed_requested_quantity": None,
                "sizing_reason": "NO_SIGNAL_SIDE_QUOTE", "sizing_trace": []}
    observed = float(book["bids"][0][0])
    high, low = shift(observed, 2, 1), shift(observed, 2, -1)
    lot = int(cfg["board_lot_shares"])
    quantity = int(cap // (high * lot)) * lot
    capital_quantity = quantity
    while quantity > 0 and high * quantity + fee(high * quantity, cfg) > cap:
        quantity -= lot
    fee_quantity = quantity
    structure = signal.get("structure_high")
    risk_exit = (max(low, float(structure)) + float(tick(structure))) if structure else None
    trace = []
    while quantity > 0 and risk_exit is not None:
        risk = structural_risk(low, risk_exit, quantity, cfg)
        trace.append({"quantity": quantity, "estimated_structure_net_loss": risk,
                      "within_3500_budget": risk <= cfg["risk_per_trade_twd"]})
        if risk <= cfg["risk_per_trade_twd"]:
            break
        quantity -= lot
    one_lot_reserve = high * lot + fee(high * lot, cfg)
    one_lot_risk = structural_risk(low, risk_exit, lot, cfg) if risk_exit is not None else None
    return {**base, "signal_best_bid": observed, "entry_sell_limit": low,
            "sizing_worst_notional_price": high, "sizing_worst_risk_entry_price": low,
            "structure_high": structure, "sizing_structure_cover_price": risk_exit,
            "capital_only_quantity": capital_quantity, "capital_plus_fee_quantity": fee_quantity,
            "reconstructed_requested_quantity": quantity,
            "sizing_reason": "OK" if quantity else "MINIMUM_BOARD_LOT_EXCEEDS_CAP_OR_RISK",
            "one_lot_required_principal_plus_entry_fee": one_lot_reserve,
            "one_lot_funding_shortfall": max(0, one_lot_reserve - cap),
            "one_lot_estimated_structure_net_loss": one_lot_risk,
            "one_lot_within_risk_budget": one_lot_risk <= cfg["risk_per_trade_twd"] if one_lot_risk is not None else None,
            "requested_principal_plus_entry_fee": high * quantity + fee(high * quantity, cfg) if quantity else 0,
            "requested_structure_net_loss": structural_risk(low, risk_exit, quantity, cfg) if quantity and risk_exit else None,
            "sizing_trace": trace}


def causal_books(round2, archive_root, signals):
    """Extract only 46 signal snapshots, preserving native quantities/receipt."""
    found, sources = {}, []
    byday = defaultdict(list)
    for signal in signals:
        byday[signal["day"]].append(signal)
    for day, candidates in sorted(byday.items()):
        day_doc = json.loads((Path(round2) / ("day_" + day + ".json")).read_text())
        run_ids = day_doc["source_run_id"]
        if len(run_ids) != 1:
            raise ValueError("AMBIGUOUS_CANONICAL_RUN")
        run = Path(archive_root) / run_ids[0]
        path = next((run / name for name in ("books.jsonl.gz", "books.jsonl") if (run / name).is_file()), None)
        if path is None:
            raise FileNotFoundError("Missing canonical books for " + day)
        bysymbol = defaultdict(list)
        for s in candidates:
            bysymbol[s["symbol"]].append((datetime.fromisoformat(s["time"]), s["signal_id"]))
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as f:
            for line_number, text in enumerate(f, 1):
                row = json.loads(text)
                symbol = str(row.get("stock_id", ""))
                if symbol not in bysymbol or row.get("ingest_accepted") is False:
                    continue
                try:
                    bids = [(float(p), float(v)) for p, v in zip(row["buy_prices"], row["buy_volumes"])]
                    asks = [(float(p), float(v)) for p, v in zip(row["sell_prices"], row["sell_volumes"])]
                    if (len(row["buy_prices"]) != len(row["buy_volumes"]) or
                        len(row["sell_prices"]) != len(row["sell_volumes"]) or
                        any(p < 0 or v < 0 for p, v in bids + asks)):
                        continue
                    receipt = datetime.fromisoformat(row["received_at"].replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    continue
                sequence = row.get("ingest_sequence")
                key = (receipt, sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else line_number, line_number)
                for at, signal_id in bysymbol[symbol]:
                    previous = found.get(signal_id)
                    if receipt <= at and (previous is None or key > previous["_key"]):
                        found[signal_id] = {"_key": key, "symbol": symbol, "bids": bids, "asks": asks,
                          "received_at": receipt.isoformat(), "exchange_time": row.get("exchange_time"),
                          "quote_time": row.get("quote_time"), "source_run_id": run_ids[0],
                          "source_file": str(path), "source_line": line_number,
                          "raw_buy_prices": row["buy_prices"], "raw_buy_volumes": row["buy_volumes"],
                          "raw_sell_prices": row["sell_prices"], "raw_sell_volumes": row["sell_volumes"],
                          "raw_unit": "UNKNOWN_SDK_NATIVE", "conversion_performed": False,
                          "book_evidence": "ARCHIVED_RAW_SNAPSHOT_RECONSTRUCTED_CAUSAL_SELECTION"}
        sources.append({"day": day, "run_id": run_ids[0], "path": str(path), "sha256": sha(path)})
    for b in found.values():
        b.pop("_key")
    return found, sources


def _rows(tables, name, scenario, variant, phase="ACCOUNT_EXIT"):
    return [r for r in tables[name] if r.get("scenario") == scenario and
            r.get("phase") == phase and r.get("variant") == variant]


def isolated_sizer(isolated_root):
    """Load exact pinned pure functions, without importing replay/strategy."""
    root = Path(isolated_root).resolve()
    module = root / "cash_short_intraday_research_v01"
    frozen = json.loads((module / "round2/freeze.json").read_text())
    evidence = []
    for name in ("execution.py", "replay.py", "config.json"):
        relative = "stock-strategy/cash_short_intraday_research_v01/" + name
        path = module / name
        expected = frozen["files"][relative]
        if sha(path) != expected:
            raise ValueError("ISOLATED_SOURCE_NOT_MATCHING_ORIGINAL_FREEZE:" + name)
        evidence.append({"path": str(path), "sha256": expected, "original_body_present": True})
    spec = importlib.util.spec_from_file_location("_frozen_account_audit_execution", module / "execution.py")
    execution = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(execution)
    tree = ast.parse((module / "replay.py").read_text())
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {"fee", "tax", "target_quantity"}]
    if {n.name for n in selected} != {"fee", "tax", "target_quantity"}:
        raise ValueError("MISSING_ORIGINAL_PURE_SIZING_BODY")
    namespace = {"math": math, "Decimal": Decimal, "ROUND_CEILING": ROUND_CEILING,
       "shift_price": execution.shift_price, "tick_size": execution.tick_size, "cost": execution.cost}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(module / "replay.py"), "exec"), namespace)
    return namespace["target_quantity"], evidence


def reconcile_gap(tables, scenario, variant):
    a0 = _rows(tables, "trades", scenario, "E0")
    av = _rows(tables, "trades", scenario, variant)
    f0 = _rows(tables, "trades", scenario, "E0", "FIXED_EXIT")
    fv = _rows(tables, "trades", scenario, variant, "FIXED_EXIT")
    indices = [{r["signal_id"]: r for r in rows} for rows in (a0, av, f0, fv)]
    bridge = []
    for sid in sorted(set().union(*(i.keys() for i in indices))):
        rows = [index.get(sid) for index in indices]
        nets = [r["marked_net_pnl"] if r else 0 for r in rows]
        if any(n is None for n in nets):
            delta = None
        else:
            delta = float(dec(nets[1]) - dec(nets[0]) - dec(nets[3]) + dec(nets[2]))
        sample = next(r for r in rows if r)
        bridge.append({"scenario": scenario, "variant": variant, "day": sample["day"],
           "symbol": sample["symbol"], "signal_id": sid,
           "account_E0_net": nets[0], "account_variant_net": nets[1],
           "fixed_E0_net": nets[2], "fixed_variant_net": nets[3],
           "account_increment": float(dec(nets[1])-dec(nets[0])) if None not in nets[:2] else None,
           "fixed_increment": float(dec(nets[3])-dec(nets[2])) if None not in nets[2:] else None,
           "account_minus_fixed_increment": delta,
           "account_E0_quantity": rows[0]["quantity"] if rows[0] else 0,
           "account_variant_quantity": rows[1]["quantity"] if rows[1] else 0,
           "fixed_variant_quantity": rows[3]["quantity"] if rows[3] else 0,
           "bridge_reason": "ADDITIONAL_ACCOUNT_ENTRY_NOT_IN_FIXED_COHORT" if rows[1] and not rows[3]
             else "SAME_FIXED_AND_ACCOUNT_PNL" if delta == 0 else "OTHER_DIFFERENCE_REQUIRES_REVIEW"})
    na0, nav, nf0, nfv = (total(rows, "marked_net_pnl") for rows in (a0, av, f0, fv))
    account_delta = float(dec(nav)-dec(na0)) if None not in (nav, na0) else None
    fixed_delta = float(dec(nfv)-dec(nf0)) if None not in (nfv, nf0) else None
    summary = {"scenario": scenario, "variant": variant, "account_E0_net": na0,
        "account_variant_net": nav, "fixed_E0_net": nf0, "fixed_variant_net": nfv,
        "account_increment": account_delta, "fixed_increment": fixed_delta,
        "account_minus_fixed_increment": account_delta-fixed_delta if None not in (account_delta, fixed_delta) else None,
        "bridge_sum": total(bridge, "account_minus_fixed_increment"),
        "account_entries": len(av), "fixed_entries": len(fv),
        "remaining_quantity": sum(r["remaining_quantity"] for r in av),
        "unvalued_trade_count": sum(r["marked_net_pnl"] is None for r in av)}
    summary["unexplained_residual"] = (summary["account_minus_fixed_increment"]-summary["bridge_sum"]
        if None not in (summary["account_minus_fixed_increment"], summary["bridge_sum"]) else None)
    if summary["unexplained_residual"] not in (0, None):
        raise AssertionError("UNRECONCILED_ACCOUNT_FIXED_GAP")
    return summary, sorted(bridge, key=lambda x: (x["day"], x["signal_id"]))


def check_shared_account_fixed_contracts(tables):
    checks = []
    trade_fields = ("entry_time", "entry_price", "quantity", "exit_time", "exit_price",
                    "commission", "sell_tax", "net_pnl", "exit_reason", "remaining_quantity")
    fill_fields = ("fill_phase", "time", "price", "quantity", "book_sequence", "level", "order_id")
    scenarios = sorted({t["scenario"] for t in tables["trades"]})
    for scenario in scenarios:
      for variant in VARIANTS:
        accounts = {t["signal_id"]: t for t in _rows(tables, "trades", scenario, variant)}
        fixed = {t["signal_id"]: t for t in _rows(tables, "trades", scenario, variant, "FIXED_EXIT")}
        for sid in sorted(accounts.keys() & fixed.keys()):
            if any(accounts[sid].get(k) != fixed[sid].get(k) for k in trade_fields):
                raise AssertionError("SHARED_FIXED_ACCOUNT_TRADE_CONTRACT_MISMATCH")
            def fills(phase):
                return [{k: f.get(k) for k in fill_fields} for f in _rows(tables, "fills", scenario, variant, phase)
                        if f["signal_id"] == sid]
            if fills("ACCOUNT_EXIT") != fills("FIXED_EXIT"):
                raise AssertionError("SHARED_FIXED_ACCOUNT_FILL_CONTRACT_MISMATCH")
            checks.append({"scenario": scenario, "variant": variant, "signal_id": sid,
                           "same_filled_quantity_entry_exit_cost_and_fill_events": True,
                           "requested_quantity_intentionally_compared_separately": True})
    return checks


def build_audit(tables, signals, books, cfg, original_sizer=None):
    scenarios = [s["name"] for s in cfg["stress_scenarios"]]
    signal_index = {s["signal_id"]: s for s in signals}
    daily_out, events, decisions, diffs, gaps, bridges, costs = [], [], [], [], [], [], []
    for scenario in scenarios:
      for variant in VARIANTS:
        trades = _rows(tables, "trades", scenario, variant)
        trade_by_id = {t["signal_id"]: t for t in trades}
        daily = sorted(_rows(tables, "daily_results", scenario, variant), key=lambda x: x["day"])
        disposition = _rows(tables, "portfolio_dispositions", scenario, variant)
        issues = _rows(tables, "execution_issues", scenario, variant)
        previous = cfg["capital_twd"]
        for d in daily:
            day_trades = [t for t in trades if t["day"] == d["day"]]
            if dec(d["starting_equity"]) != dec(previous):
                raise AssertionError("DAILY_OPENING_EQUITY_DOES_NOT_CHAIN")
            day_net = total(day_trades, "marked_net_pnl")
            if day_net != d["net_pnl"] or (day_net is not None and
                dec(d["starting_equity"])+dec(day_net) != dec(d["equity"])):
                raise AssertionError("DAILY_EQUITY_ACCOUNTING_MISMATCH")
            if d["external_cashflow"] != 0:
                raise AssertionError("UNEXPECTED_EXTERNAL_CASHFLOW")
            previous = d["equity"]
            daily_out.append({**d, "evidence": "ORIGINAL_ROUND2_DAILY_LEDGER_ARITHMETIC_VERIFIED",
                "net_from_trade_ledger": day_net, "equity_reconciled": True,
                "daily_trade_count": len(day_trades), "unexplained_equity_delta": 0})
            events.append({"scenario": scenario, "variant": variant, "day": d["day"],
                "time": datetime.strptime(d["day"], "%Y%m%d").replace(hour=9).isoformat()+"+08:00",
                "event_type": "DAY_OPEN", "equity": d["starting_equity"], "event_evidence": "ORIGINAL_DAILY_LEDGER_TIME_IS_SESSION_BOUNDARY_LABEL"})
        daily_index = {d["day"]: d for d in daily}
        bytime = defaultdict(list)
        for p in disposition:
            bytime[(p["day"], p["time"])].append(p)
        for (day, at), items in sorted(bytime.items()):
            items.sort(key=lambda p: (-float(signal_index[p["signal_id"]]["score"]), p["symbol"]))
            for rank, p in enumerate(items, 1):
                sid, d = p["signal_id"], daily_index[day]
                t, s, book = trade_by_id.get(sid), signal_index[sid], books.get(sid)
                relevant = [i for i in issues if i.get("signal_id") == sid]
                sizing = sizing_reconstruction(s, book, d["starting_equity"], cfg)
                if original_sizer and book:
                    exact_quantity, exact_reason = original_sizer(s, book, d["starting_equity"], cfg)
                    if exact_quantity != sizing["reconstructed_requested_quantity"] or exact_reason != sizing["sizing_reason"]:
                        raise AssertionError("TRANSCRIPTION_DIFFERS_FROM_PINNED_ORIGINAL_FUNCTION")
                    sizing["pinned_original_sizing_function_parity"] = True
                if t and sizing["reconstructed_requested_quantity"] != t["requested_quantity"]:
                    raise AssertionError("RECONSTRUCTED_SIZING_DIFFERS_FROM_ORIGINAL_REQUESTED:"+scenario+variant+sid)
                attempted = p["reason"] in {"ADOPTED", "ADOPTED_PARTIAL", "NO_FILL_OR_CAP_RISK"}
                requested = t["requested_quantity"] if t else next((i["requested_quantity"] for i in relevant if "requested_quantity" in i),
                    0 if any(i["code"] == "MINIMUM_BOARD_LOT_EXCEEDS_CAP_OR_RISK" for i in relevant) else None)
                entry_issues = [i["code"] for i in relevant if i.get("phase") != "EXIT"]
                row = {"scenario": scenario, "variant": variant, "day": day, "time": at,
                    "event_type": "SIGNAL_DECISION", "signal_id": sid, "symbol": s["symbol"],
                    "score": s["score"], "candidate_rank_at_decision": rank,
                    "same_decision_candidate_count": len(items), "day_starting_equity": d["starting_equity"],
                    "original_disposition": p["reason"], "ioc_attempted": attempted,
                    "requested_quantity_original": requested,
                    "filled_quantity_original": t["quantity"] if t else 0,
                    "entry_unfilled_quantity_original": t["entry_unfilled_quantity"] if t else requested,
                    "entry_issue_codes": entry_issues,
                    "remaining_quantity_after_exit": t["remaining_quantity"] if t else 0,
                    "no_cover_reason": t.get("last_untriggered_reason") if t else None,
                    "original_exit_reason": t["exit_reason"] if t else None,
                    "net_pnl_original": t["marked_net_pnl"] if t else 0,
                    "new_entry_quota_available": p["reason"] not in {"DAILY_TRADE_LIMIT", "PRIOR_DAY_RESIDUAL_EXPOSURE_BLOCKS_NEW_ENTRY"},
                    "original_requested_evidence": "ORIGINAL_TRADE_OR_EXECUTION_ISSUE" if requested is not None else "NOT_RECORDED_OR_NOT_ATTEMPTED",
                    "original_sizing_ledger_exists": False, **sizing,
                    "signal_book_received_at": book["received_at"] if book else None,
                    "signal_book_source_file": book["source_file"] if book else None,
                    "signal_book_source_line": book["source_line"] if book else None,
                    "signal_book_age_seconds": (datetime.fromisoformat(at)-datetime.fromisoformat(book["received_at"])).total_seconds() if book else None,
                    "reconstructed_size_is_actual_request": attempted and requested is not None,
                    "sizing_applicability": "ORIGINAL_ATTEMPT_PARITY" if attempted else "COUNTERFACTUAL_ONLY_NOT_A_REQUEST"}
                decisions.append(row)
                events.append({k: v for k, v in row.items() if k not in {"sizing_trace"}})
        for t in trades:
            fills = [f for f in _rows(tables, "fills", scenario, variant) if f["signal_id"] == t["signal_id"]]
            entry_fills = [f for f in fills if f["fill_phase"] == "ENTRY"]
            exit_fills = [f for f in fills if f["fill_phase"] == "EXIT"]
            if sum(f["quantity"] for f in entry_fills) != t["quantity"] or sum(f["quantity"] for f in exit_fills)+t["remaining_quantity"] != t["quantity"]:
                raise AssertionError("FILL_QUANTITY_CONSERVATION_FAILED")
            entry_value = sum(dec(f["price"])*f["quantity"] for f in entry_fills)
            exit_value = sum(dec(f["price"])*f["quantity"] for f in exit_fills)
            groups = defaultdict(list)
            for f in exit_fills:
                groups[str(f.get("order_id", f["time"]))].append(f)
            entry_fee = fee(entry_value, cfg)
            exit_fee = sum(fee(sum(dec(f["price"])*f["quantity"] for f in fs), cfg) for fs in groups.values())
            sell_tax = int((entry_value*dec(cfg["sell_tax_rate"])).to_integral_value(rounding=ROUND_CEILING))
            net = float(entry_value-exit_value-entry_fee-exit_fee-sell_tax) if t["remaining_quantity"] == 0 else None
            if t["commission"] != entry_fee+exit_fee or t["sell_tax"] != sell_tax or net != t["net_pnl"]:
                raise AssertionError("FILL_COST_OR_PNL_ARITHMETIC_MISMATCH")
            costs.append({"scenario": scenario, "variant": variant, "day": t["day"], "symbol": t["symbol"],
                "signal_id": t["signal_id"], "entry_notional_from_fills": float(entry_value),
                "exit_notional_from_fills": float(exit_value), "entry_commission": entry_fee,
                "exit_commission": exit_fee, "exit_order_count": len(groups), "sell_tax": sell_tax,
                "gross_pnl_from_fills": float(entry_value-exit_value), "net_pnl_from_fills": net,
                "ledger_net_pnl": t["net_pnl"], "arithmetic_residual": net-t["net_pnl"] if net is not None else None,
                "cost_evidence": "RECONSTRUCTED_FROZEN_PROXY_FORMULA_NOT_BROKER_BILL"})
            for f in fills:
                events.append({"scenario": scenario, "variant": variant, "day": t["day"],
                    "time": f["time"], "event_type": f["fill_phase"]+"_FILL", "signal_id": t["signal_id"],
                    "symbol": t["symbol"], "price": f["price"], "quantity": f["quantity"],
                    "order_id": f.get("order_id"), "book_sequence": f.get("book_sequence"),
                    "event_evidence": "ORIGINAL_ROUND2_FILL_LEDGER_MODEL_NOT_REAL_FILL"})
            events.append({"scenario": scenario, "variant": variant, "day": t["day"],
                "time": t["exit_trigger_time"], "event_type": "EXIT_TRIGGER", "signal_id": t["signal_id"],
                "symbol": t["symbol"], "exit_reason": t["exit_reason"],
                "event_evidence": "ORIGINAL_TRADE_LEDGER_TRIGGER_NOT_GUARANTEED_FILL"})
            events.append({"scenario": scenario, "variant": variant, "day": t["day"],
                "time": t["exit_time"] or t["mark_time"], "event_type": "TRADE_FINAL_ACCOUNTING",
                "signal_id": t["signal_id"], "symbol": t["symbol"], "gross_pnl": t["gross_pnl"],
                "commission": t["commission"], "sell_tax": t["sell_tax"], "net_pnl": t["marked_net_pnl"],
                "realized_pnl": t["realized_pnl"], "unrealized_pnl": t["unrealized_pnl"],
                "remaining_quantity": t["remaining_quantity"], "exit_reason": t["exit_reason"],
                "event_evidence": "ORIGINAL_TRADE_LEDGER_ACCOUNTING_AT_FINAL_FILL_OR_LAST_MARK"})
        for d in daily:
            events.append({"scenario": scenario, "variant": variant, "day": d["day"],
                "time": datetime.strptime(d["day"], "%Y%m%d").replace(hour=13, minute=30).isoformat()+"+08:00",
                "event_type": "DAY_CLOSE", "equity": d["equity"], "net_pnl": d["net_pnl"],
                "realized_pnl": d["realized_pnl"], "unrealized_pnl": d["unrealized_pnl"],
                "entry_quantity": d["entry_quantity"], "remaining_quantity": d["end_quantity"],
                "event_evidence": "ORIGINAL_DAILY_LEDGER_TIME_IS_SESSION_BOUNDARY_LABEL"})
      for variant in ("E1", "E2"):
        summary, bridge = reconcile_gap(tables, scenario, variant)
        gaps.append(summary); bridges.extend(bridge)
        base_trades = {t["signal_id"]: t for t in _rows(tables, "trades", scenario, "E0")}
        alt_trades = {t["signal_id"]: t for t in _rows(tables, "trades", scenario, variant)}
        base_decisions = {d["signal_id"]: d for d in decisions if d["scenario"] == scenario and d["variant"] == "E0"}
        alt_decisions = {d["signal_id"]: d for d in decisions if d["scenario"] == scenario and d["variant"] == variant}
        for sid in sorted(set(base_decisions)|set(alt_decisions), key=lambda sid: (signal_index[sid]["time"], sid)):
            a, b = base_trades.get(sid), alt_trades.get(sid)
            da, db = base_decisions[sid], alt_decisions[sid]
            category = "SAME_SIGNAL_SAME_FILLED_QUANTITY_EXIT_ONLY" if a and b and a["quantity"] == b["quantity"] else (
                "SAME_SIGNAL_DIFFERENT_FILLED_QUANTITY" if a and b else "ADDITIONAL_VARIANT_FILL" if b else "LOST_VARIANT_FILL" if a else "NO_FILL_BOTH")
            diffs.append({"scenario": scenario, "variant": variant, "day": da["day"], "time": da["time"],
                "signal_id": sid, "symbol": da["symbol"], "category": category,
                "E0_disposition": da["original_disposition"], "variant_disposition": db["original_disposition"],
                "E0_starting_equity": da["day_starting_equity"], "variant_starting_equity": db["day_starting_equity"],
                "E0_requested_quantity": da["requested_quantity_original"], "variant_requested_quantity": db["requested_quantity_original"],
                "E0_reconstructed_quantity": da["reconstructed_requested_quantity"], "variant_reconstructed_quantity": db["reconstructed_requested_quantity"],
                "E0_filled_quantity": a["quantity"] if a else 0, "variant_filled_quantity": b["quantity"] if b else 0,
                "E0_exit_time": a["exit_time"] if a else None, "variant_exit_time": b["exit_time"] if b else None,
                "E0_exit_price": a["exit_price"] if a else None, "variant_exit_price": b["exit_price"] if b else None,
                "E0_net": a["marked_net_pnl"] if a else 0, "variant_net": b["marked_net_pnl"] if b else 0,
                "net_difference": float(dec(b["marked_net_pnl"] if b else 0)-dec(a["marked_net_pnl"] if a else 0)),
                "E0_remaining": a["remaining_quantity"] if a else 0, "variant_remaining": b["remaining_quantity"] if b else 0,
                "one_lot_required_capital": da.get("one_lot_required_principal_plus_entry_fee"),
                "E0_one_lot_shortfall": da.get("one_lot_funding_shortfall"),
                "variant_one_lot_shortfall": db.get("one_lot_funding_shortfall"),
                "one_lot_structure_risk": da.get("one_lot_estimated_structure_net_loss"),
                "requested_changed_but_filled_unchanged": a is not None and b is not None and a["requested_quantity"] != b["requested_quantity"] and a["quantity"] == b["quantity"]})
    order = {name: i for i, name in enumerate(("DAY_OPEN", "SIGNAL_DECISION", "ENTRY_FILL", "EXIT_TRIGGER", "EXIT_FILL", "TRADE_FINAL_ACCOUNTING", "DAY_CLOSE"))}
    events.sort(key=lambda e: (e["scenario"], e["variant"], e["day"], e.get("time") or "", order[e["event_type"]],
                               e.get("candidate_rank_at_decision", 0), e.get("signal_id", "")))
    for n, event in enumerate(events, 1):
        event["audit_event_sequence"] = n
    return {"account_daily": daily_out, "account_events": events,
            "account_decisions": decisions, "account_differences": diffs,
            "account_gap_summary": gaps, "account_gap_bridge": bridges,
            "account_cost_reconciliation": costs}


def report(result):
    t = result["tables"]
    scenarios = list(dict.fromkeys(g["scenario"] for g in t["account_gap_summary"]))
    scenario = "BASE_ASSUMED_LOTS" if "BASE_ASSUMED_LOTS" in scenarios else next(iter(scenarios), None)
    base = [g for g in t["account_gap_summary"] if g["scenario"] == scenario]
    differences = [d for d in t["account_differences"] if d["scenario"] == scenario]
    decisions = [d for d in t["account_decisions"] if d["scenario"] == scenario]
    events = [e for e in t["account_events"] if e["scenario"] == scenario]
    bridges = [b for b in t["account_gap_bridge"] if b["scenario"] == scenario and b["account_minus_fixed_increment"] != 0]
    out = ["# E1／E2 出場配對與帳戶分歧對帳", "", "SHADOW_ONLY、NO_LIVE_ORDER、actual_orders=0。只讀既有第二輪帳本及封存五檔；沒有重跑策略。",
       "", "## 精確對帳", "", "帳本數值以原JSON精度讀入，費稅另外從原逐筆fills和凍結代理公式核對；未以摘要四捨五入值回推。",
       "", "|版本|E0帳戶淨損益|固定原進場淨損益|固定改善|完整帳戶淨損益|帳戶改善|帳戶−配對改善|未解釋差額|", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for g in base:
        out.append("|"+"|".join(str(g[k]) for k in ("variant", "account_E0_net", "fixed_variant_net", "fixed_increment", "account_variant_net", "account_increment", "account_minus_fixed_increment", "unexplained_residual"))+"|")
    out += ["", "以下逐筆橋接帳戶改善減固定配對改善，不把差額概括成未知資金路徑。"]
    for b in bridges:
        out.append(f"- {b['variant']}／{b['day']}／{b['symbol']}／signal_id={b['signal_id']}：帳戶增量 {b['account_increment']}，固定配對增量 {b['fixed_increment']}，差距 {b['account_minus_fixed_increment']}；原因 {b['bridge_reason']}。")
    if not bridges:
        out.append("- 此情境沒有非零橋接差額；這不代表其他情境亦相同。")
    checked = [c for c in result.get("shared_fixed_account_contract_checks", []) if c["scenario"] == scenario]
    out.append(f"- 本情境共享cohort交易契約核對 {len(checked)} 組；requested quantity另列，不能因實際filled相同就隱藏委託股數差異。")
    out += ["", "## 第一次分歧與傳遞", ""]
    for variant in ("E1", "E2"):
        rows = sorted([d for d in differences if d["variant"] == variant], key=lambda d: (d["time"], d["signal_id"]))
        first = next((d for d in rows if d["E0_filled_quantity"] and d["variant_filled_quantity"] and
                      (d["E0_exit_time"] != d["variant_exit_time"] or d["E0_net"] != d["variant_net"])), None)
        if first:
            out.append(f"- {variant}首個同訊號出場／權益分歧：{first['day']}、{first['symbol']}、signal_id={first['signal_id']}。E0／{variant}實際成交股數 {first['E0_filled_quantity']}／{first['variant_filled_quantity']}；淨損益 {first['E0_net']}／{first['variant_net']}。")
            for label in ("E0", variant):
                related = [e for e in events if e["variant"] == label and e.get("signal_id") == first["signal_id"]]
                entry = [e for e in related if e["event_type"] == "ENTRY_FILL"]
                quantity = sum(e["quantity"] for e in entry)
                price = sum(dec(e["price"])*e["quantity"] for e in entry)/quantity if quantity else None
                trigger = next((e for e in related if e["event_type"] == "EXIT_TRIGGER"), {})
                close = next((e for e in related if e["event_type"] == "TRADE_FINAL_ACCOUNTING"), {})
                out.append(f"  - {label}：進場VWAP {price}；觸發 {trigger.get('time')}；最後回補／估值 {close.get('time')}；出場原因 {close.get('exit_reason')}。觸發不等於保證成交。")
        else:
            out.append(f"- {variant}：沒有可辨識的同一已成交訊號出場分歧；不得憑空指定第一次。")
        request = next((d for d in rows if d["requested_changed_but_filled_unchanged"]), None)
        if request:
            out.append(f"- {variant}首次『委託量不同、成交量相同』：{request['day']}／{request['symbol']}／{request['signal_id']}；E0／{variant}日初權益 {request['E0_starting_equity']}／{request['variant_starting_equity']}；requested {request['E0_requested_quantity']}／{request['variant_requested_quantity']}；filled皆 {request['E0_filled_quantity']}。不能把未成交股數當額外曝險。")
        fill_change = next((d for d in rows if d["category"] in {"ADDITIONAL_VARIANT_FILL", "LOST_VARIANT_FILL", "SAME_SIGNAL_DIFFERENT_FILLED_QUANTITY"}), None)
        if fill_change:
            out.append(f"- {variant}首次實際進場差異：{fill_change['time']}／{fill_change['symbol']}／{fill_change['signal_id']}；類別 {fill_change['category']}；E0／{variant}股數 {fill_change['E0_filled_quantity']}／{fill_change['variant_filled_quantity']}；原處置 {fill_change['E0_disposition']}／{fill_change['variant_disposition']}。")
            following = [d for d in differences if d["variant"] == variant and d["day"] == fill_change["day"] and d["time"] > fill_change["time"]]
            for d in following:
                out.append(f"  - 後續候選 {d['time']}／{d['symbol']}／{d['signal_id']}：E0 {d['E0_disposition']}；{variant} {d['variant_disposition']}。")
        else:
            out.append(f"- {variant}：沒有成交股數或進場候選改變。")
    out += ["", "## 實際進場分歧的資金與風險門檻（重建，非原始sizing ledger）", ""]
    changed = {d["signal_id"] for d in differences if d["category"] in
               {"ADDITIONAL_VARIANT_FILL", "LOST_VARIANT_FILL", "SAME_SIGNAL_DIFFERENT_FILLED_QUANTITY"}}
    for d in decisions:
        if d["signal_id"] in changed:
            out.append(f"- {d['day']}／{d['symbol']}／{d['signal_id']}／{d['variant']}：原日初權益 {d['day_starting_equity']}；原訊號買一 {d.get('signal_best_bid')}；賣出限價 {d.get('entry_sell_limit')}，部位上限使用較高價格 {d.get('sizing_worst_notional_price')}；一張本金加進場費 {d.get('one_lot_required_principal_plus_entry_fee')}，資金缺口 {d.get('one_lot_funding_shortfall')}；結構風險估計 {d.get('one_lot_estimated_structure_net_loss')}／{d.get('risk_budget_twd')}；重建requested {d.get('reconstructed_requested_quantity')}，原filled {d['filled_quantity_original']}。")
    out += ["", "上述五檔選取只用signal時間前已收到的最後封存報價，保留原native數量，不作股／張轉換；完整book路徑、行號、receipt、雜湊附帳。沒有五檔交易所時間便不補回。",
      "", "## 每筆與逐日差額", "", "下列是帳戶相對E0差額；固定配對僅包含原cohort，新增帳戶進場不得混入配對。",
      "", "|日期／股票|signal_id|E0淨損益|E1淨損益|E2淨損益|E1−E0|E2−E0|", "|---|---|---:|---:|---:|---:|---:|"]
    diffindex = {(d["variant"], d["signal_id"]): d for d in differences}
    for sid in sorted({sid for v, sid in diffindex if diffindex[(v,sid)]["E0_filled_quantity"] or diffindex[(v,sid)]["variant_filled_quantity"]}, key=lambda sid: diffindex[("E1",sid)]["time"]):
        a, b = diffindex[("E1",sid)], diffindex[("E2",sid)]
        out.append(f"|{a['day']}／{a['symbol']}|{sid}|{a['E0_net']}|{a['variant_net']}|{b['variant_net']}|{a['net_difference']}|{b['net_difference']}|")
    out += ["", "逐日權益與每個候選（包括拒絕／部分成交／每日額度阻擋）見account_daily、account_decisions、account_events。無訊號日期仍保留每日0損益；這不將未評估時段變成沒有訊號。",
      "", "## 較差情境與替代單位", "", "|情境|出場|固定改善|帳戶改善|帳戶−配對改善|未解釋差額|", "|---|---|---:|---:|---:|---:|"]
    for g in t["account_gap_summary"]:
        out.append(f"|{g['scenario']}|{g['variant']}|{g['fixed_increment']}|{g['account_increment']}|{g['account_minus_fixed_increment']}|{g['unexplained_residual']}|")
    out += ["", "沒有成交時0僅為該假設下模型空手，不代表量單位獲證實，也不提供勝率或PF。各情境原cohort不一定相同，不能跨情境把差額混算。",
      "", "## 缺陷判斷與限制", ""]
    residuals = [g["unexplained_residual"] for g in t["account_gap_summary"]]
    out.append(f"- 情境／出場橋接共 {len(residuals)} 組，未解釋殘差 {residuals}；費稅及股數守恆檢查 {result.get('checks', {})}。這不是全面證明回測無缺陷。")
    out += ["- DOC_COMMENT_MISMATCH：原sizing註解說保留來回費用和一份風險預算，程式實際只以『較高價格本金＋進場費』檢查資金，再另以結構來回淨損檢查風險預算。這是已查得的註解與實作不符，不是本次橋接差額原因；本次不修改公式、不聲稱額外現金reserve存在。",
      "- 原帳本沒有逐候選sizing ledger，因此預算、限價及風險欄是以原碼公式＋原封存報價重建，與原日初權益及所有已採用requested quantity核對一致；不能冒充當時有記錄的決策證據。",
      "- ENTRY／ACCOUNT_EXIT部分重用E0結果帶有fixed_entry_sizing_injection等快取來源標籤；其意義是執行結果來源，不代表帳戶當時真的強制原cohort股數。phase、候選帳戶決策及本次requested parity為判別依據。標籤混淆屬provenance可讀性缺陷，不影響本次已對回金額。",
      "- 未登入券商，實際費率、歷史帳戶資格、單位與時鐘限制由EVIDENCE_AUDIT另列；此處費用算式相符不等於實際帳務已確認。3500風險預算不是最大損失保證。",
      "", "## 證據檔案", "", "- 原始來源：cash_short_intraday_research_v01/round2/results_round2_v1/tables.json（逐筆fills、trades、portfolio_dispositions、execution_issues及daily_results）。",
      "- 原始候選：同目錄signals_enriched.json與day_YYYYMMDD.json。",
      "- 對帳結果：account_tables.json（tables含account_events/account_decisions/account_daily/account_differences/account_gap_bridge/account_gap_summary/account_cost_reconciliation）。",
      "- 本次是只讀帳務對帳及明確標示的sizing重建，不是重新跑研究；未修改或覆寫第一、二輪檔案。", ""]
    return "\n".join(out)


def run(round2, config_path, archive_root, output, isolated_root):
    output = Path(output)
    targets = [output / "account_tables.json", output / "ACCOUNT_DIVERGENCE_REPORT.md"]
    if any(p.exists() for p in targets):
        raise FileExistsError("Account audit outputs already exist; use a new directory")
    tables_path = Path(round2) / "tables.json"
    signals_path = Path(round2) / "signals_enriched.json"
    tables = json.loads(tables_path.read_text())
    signals = json.loads(signals_path.read_text())
    cfg = json.loads(Path(config_path).read_text())
    original_sizer, original_bodies = isolated_sizer(isolated_root)
    books, sources = causal_books(round2, archive_root, signals)
    audited = build_audit(tables, signals, books, cfg, original_sizer=original_sizer)
    shared_checks = check_shared_account_fixed_contracts(tables)
    result = {"audit_version": "ACCOUNT_DIVERGENCE_ORIGINAL_LEDGER_ARITHMETIC_V01",
      "created_at": datetime.now(timezone.utc).isoformat(), **SAFETY,
      "performance_replay_executed": False, "original_strategy_imported": False,
      "source_round2_tables": {"path": str(tables_path), "sha256": sha(tables_path)},
      "source_round2_signals": {"path": str(signals_path), "sha256": sha(signals_path)},
      "sizing_config": {"path": str(config_path), "sha256": sha(config_path)},
      "original_pure_function_bodies": original_bodies,
      "pure_function_loading": "EXACT_AST_FUNCTION_BODIES_FROM_HASH_MATCHED_ISOLATED_REPLAY_NO_STRATEGY_IMPORT",
      "book_sources": sources, "causal_signal_book_snapshots": books,
      "shared_fixed_account_contract_checks": shared_checks,
      "tables": audited,
      "checks": {"all_daily_equity_chains": True, "all_fill_quantity_conservation": True,
        "all_adopted_requested_quantity_reconstruction": True, "all_fill_cost_net_arithmetic": True,
        "shared_fixed_account_contract_and_fill_parity": True,
        "all_account_fixed_gap_residuals_zero": all(x["unexplained_residual"] == 0 for x in audited["account_gap_summary"]),
        "original_round1_round2_overwritten": False}}
    output.mkdir(parents=True, exist_ok=True)
    targets[0].write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
    targets[1].write_text(report(result), encoding="utf-8")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--isolated-root", required=True, type=Path)
    p.add_argument("--round2-results", type=Path)
    p.add_argument("--round1-config", type=Path)
    p.add_argument("--archive-root", type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    module = args.isolated_root / "cash_short_intraday_research_v01"
    result = run(args.round2_results or module / "round2/results_round2_v1",
        args.round1_config or module / "config.json",
        args.archive_root or args.isolated_root / "yuanta_intraday_shadow_v01/runtime/runs",
        args.output, args.isolated_root)
    print(json.dumps({"checks": result["checks"], "table_counts": {k: len(v) for k, v in result["tables"].items()}, **SAFETY}))


if __name__ == "__main__":
    main()
