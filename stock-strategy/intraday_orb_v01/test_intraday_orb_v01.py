from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
import csv
import json
import subprocess
import sys
import tempfile
import unittest

from .analysis import forward_returns
from .config import CFG
from .data import (
    DataValidationError,
    load_daily,
    load_index_daily,
    load_index_minutes,
    load_minutes,
)
from .models import (
    DailyBar,
    IndexDailyBar,
    IndexMinuteBar,
    MinuteBar,
    OddLotQuote,
    Signal,
    TradingSession,
    UniverseMember,
)
from .shadow import SHADOW_STATUS, evaluate_quote
from .signals import generate_signals, select_daily_signals, tick_size, ticks_above
from .universe import build_universes


def member(day: date, symbol: str = "1234") -> UniverseMember:
    return UniverseMember(
        trade_date=day,
        symbol=symbol,
        name="測試股",
        market="TWSE",
        previous_close=99.0,
        previous_index_close=100.0,
        sma20=95.0,
        sma60=90.0,
        median_volume_20d=1_500_000,
        median_turnover_20d=150_000_000,
        rs5=0.03,
        rs20=0.06,
        close_to_60d_high=1.0,
        universe_rank=1,
    )


def minute_grid(
    day: date,
    *,
    symbol: str = "1234",
    history: bool,
    after_signal_price: float = 100.6,
) -> list[MinuteBar]:
    result = []
    end = datetime.combine(day, time(9, 1))
    final = datetime.combine(day, time(11, 0) if history else time(13, 30))
    while end <= final:
        if history:
            price = 99.0
            low = high = price
            volume = 1_000
        elif end.time() <= time(9, 15):
            price = 99.8
            low, high = 99.5, 100.0
            volume = 2_000
        elif end.time() in {time(9, 16), time(9, 17)}:
            price = 100.5
            low, high = 99.8, 100.5
            volume = 2_000
        else:
            price = after_signal_price
            low, high = price - 0.1, price + 0.1
            volume = 2_000
        result.append(
            MinuteBar(
                bar_end=end,
                symbol=symbol,
                market="TWSE",
                open=price,
                high=high,
                low=low,
                close=price,
                volume=volume,
                turnover=price * volume,
                limit_up=108.9,
            )
        )
        end += timedelta(minutes=1)
    return result


def signal_fixture(day: date, symbol: str = "1234", when: time = time(9, 17)) -> Signal:
    return Signal(
        strategy_id=CFG.strategy_id,
        config_hash=CFG.fingerprint(),
        trade_date=day,
        symbol=symbol,
        name="測試股",
        market="TWSE",
        signal_time=datetime.combine(day, when),
        signal_price=100.5,
        or_high=100.0,
        or_low=99.5,
        vwap=99.9,
        rvol=2.0,
        stock_return=100.5 / 99.0 - 1.0,
        index_return=0.0,
        intraday_relative_strength=100.5 / 99.0 - 1.0,
        median_turnover_20d=150_000_000,
    )


def signal_inputs(target: date, *, after_signal_price: float = 100.6):
    history_dates = [target - timedelta(days=value) for value in range(20, 0, -1)]
    minutes = []
    indexes = []
    trading_calendar = []
    for day in history_dates:
        minutes.extend(minute_grid(day, history=True))
        indexes.append(
            IndexMinuteBar(datetime.combine(day, time(11, 0)), "TWSE", "TAIEX", 100.0)
        )
        trading_calendar.append(TradingSession(day, "TWSE"))
    minutes.extend(
        minute_grid(target, history=False, after_signal_price=after_signal_price)
    )
    cursor = datetime.combine(target, time(9, 1))
    while cursor <= datetime.combine(target, time(11, 0)):
        indexes.append(IndexMinuteBar(cursor, "TWSE", "TAIEX", 100.0))
        cursor += timedelta(minutes=1)
    trading_calendar.append(TradingSession(target, "TWSE"))
    return minutes, indexes, trading_calendar


class UniverseTests(unittest.TestCase):
    def test_universe_uses_t_minus_1_and_deterministic_rank(self):
        start = date(2026, 1, 1)
        dates = [start + timedelta(days=i) for i in range(62)]
        calendar = [
            TradingSession(day, market)
            for day in dates
            for market in ("TWSE", "TPEX")
        ]
        indexes = [
            IndexDailyBar(
                day,
                market,
                "TAIEX" if market == "TWSE" else "TPEX",
                (100 if market == "TWSE" else 200) + i * 0.05,
            )
            for i, day in enumerate(dates[:60])
            for market in ("TWSE", "TPEX")
        ]
        daily = []
        for symbol, slope in (("1234", 0.22), ("5678", 0.20)):
            for i, day in enumerate(dates[:60]):
                daily.append(
                    DailyBar(
                        date=day,
                        symbol=symbol,
                        name=symbol,
                        market="TWSE",
                        security_type="COMMON_STOCK",
                        trading_status="NORMAL",
                        close=20 + i * slope,
                        volume=1_500_000,
                        turnover=150_000_000,
                    )
                )
        for day in dates[:60]:
            daily.append(
                DailyBar(
                    date=day,
                    symbol="9998",
                    name="上櫃低價測試股",
                    market="TPEX",
                    security_type="COMMON_STOCK",
                    trading_status="NORMAL",
                    close=10.0,
                    volume=1_500_000,
                    turnover=150_000_000,
                )
            )
        target = dates[60]
        rows, audit = build_universes(
            daily, indexes, calendar, start_date=target, end_date=target
        )
        self.assertEqual([row.symbol for row in rows], ["1234", "5678"])
        self.assertEqual([row.universe_rank for row in rows], [1, 2])
        self.assertTrue(all(row.trade_date == target for row in rows))
        self.assertEqual(audit["selected_rows"], 2)
        incomplete_indexes = [
            row
            for row in indexes
            if not (row.market == "TPEX" and row.date == dates[10])
        ]
        with self.assertRaises(ValueError):
            build_universes(
                daily,
                incomplete_indexes,
                calendar,
                start_date=target,
                end_date=target,
            )
        incomplete_daily = [
            row
            for row in daily
            if not (row.market == "TPEX" and row.date == dates[10])
        ]
        with self.assertRaises(ValueError):
            build_universes(
                incomplete_daily,
                indexes,
                calendar,
                start_date=target,
                end_date=target,
            )


class SignalTests(unittest.TestCase):
    def test_second_completed_bar_triggers_at_0917(self):
        target = date(2026, 4, 1)
        minutes, indexes, trading_calendar = signal_inputs(target)
        evaluations, raw, selected, _ = generate_signals(
            [member(target)], minutes, indexes, trading_calendar
        )
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0].signal_time, datetime.combine(target, time(9, 17)))
        self.assertEqual(len(selected), 1)
        passed = [row for row in evaluations if row.passed]
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0].bar_end.time(), time(9, 17))
        self.assertAlmostEqual(passed[0].rvol, 2.0)

    def test_future_bars_do_not_change_existing_signal(self):
        target = date(2026, 4, 1)
        first_minutes, indexes, trading_calendar = signal_inputs(target, after_signal_price=101.0)
        _, first, _, _ = generate_signals(
            [member(target)], first_minutes, indexes, trading_calendar
        )
        changed = [
            replace(bar, high=150.0, low=50.0, close=50.0, turnover=bar.volume * 50.0)
            if bar.date == target and bar.bar_end.time() > time(9, 17)
            else bar
            for bar in first_minutes
        ]
        _, second, _, _ = generate_signals(
            [member(target)], changed, indexes, trading_calendar
        )
        self.assertEqual(first[0].signal_time, second[0].signal_time)
        self.assertEqual(first[0].signal_price, second[0].signal_price)

    def test_missing_one_rvol_day_fails_closed(self):
        target = date(2026, 4, 1)
        minutes, indexes, trading_calendar = signal_inputs(target)
        missing_day = target - timedelta(days=7)
        minutes = [bar for bar in minutes if bar.date != missing_day]
        evaluations, raw, selected, audit = generate_signals(
            [member(target)], minutes, indexes, trading_calendar
        )
        self.assertEqual(raw, [])
        self.assertEqual(selected, [])
        self.assertEqual(evaluations[0].rejection_reason, "MISSING_RVOL_SESSION")
        self.assertEqual(audit["data_issue_counts"]["MISSING_RVOL_SESSION"], 1)

    def test_missing_target_index_minute_censors_whole_stock_day(self):
        target = date(2026, 4, 1)
        minutes, indexes, trading_calendar = signal_inputs(target)
        indexes = [
            row
            for row in indexes
            if row.bar_end != datetime.combine(target, time(9, 17))
        ]
        evaluations, raw, selected, audit = generate_signals(
            [member(target)], minutes, indexes, trading_calendar
        )
        self.assertEqual(raw, [])
        self.assertEqual(selected, [])
        self.assertEqual(
            evaluations[-1].rejection_reason,
            "MISSING_PREVIOUS_OR_INDEX_MINUTE",
        )
        self.assertEqual(
            audit["data_issue_counts"]["MISSING_PREVIOUS_OR_INDEX_MINUTE"], 1
        )

    def test_any_censored_top30_member_cancels_daily_selection(self):
        target = date(2026, 4, 1)
        minutes, indexes, trading_calendar = signal_inputs(target)
        second_symbol = [replace(row, symbol="5678") for row in minutes]
        missing_history_day = target - timedelta(days=7)
        second_symbol = [
            row for row in second_symbol if row.date != missing_history_day
        ]
        evaluations, raw, selected, audit = generate_signals(
            [member(target, "1234"), member(target, "5678")],
            minutes + second_symbol,
            indexes,
            trading_calendar,
        )
        self.assertTrue(evaluations)
        self.assertEqual(len(raw), 1)
        self.assertEqual(
            raw[0].selection_reason,
            "DAY_CENSORED_INCOMPLETE_UNIVERSE_DATA",
        )
        self.assertEqual(selected, [])
        self.assertEqual(audit["censored_trade_date_count"], 1)

    def test_zero_volume_bar_cannot_create_fake_breakout(self):
        target = date(2026, 4, 1)
        minutes, indexes, trading_calendar = signal_inputs(target)
        fake_time = datetime.combine(target, time(9, 16))
        minutes = [
            replace(
                row,
                open=100.5,
                high=100.5,
                low=100.5,
                close=100.5,
                volume=0,
                turnover=0.0,
            )
            if row.bar_end == fake_time
            else row
            for row in minutes
        ]
        evaluations, raw, selected, audit = generate_signals(
            [member(target)], minutes, indexes, trading_calendar
        )
        self.assertEqual(raw, [])
        self.assertEqual(selected, [])
        self.assertEqual(
            evaluations[-1].rejection_reason,
            "ZERO_VOLUME_MINUTE_CHANGED_PRICE",
        )
        self.assertEqual(
            audit["data_issue_counts"]["ZERO_VOLUME_MINUTE_CHANGED_PRICE"], 1
        )

    def test_same_minute_ranking_and_one_daily_selection(self):
        target = date(2026, 4, 1)
        weaker = signal_fixture(target, "1234")
        stronger = replace(
            signal_fixture(target, "5678"), intraday_relative_strength=0.03
        )
        later = signal_fixture(target, "9999", time(9, 18))
        raw, selected = select_daily_signals([weaker, later, stronger])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].symbol, "5678")
        reasons = {row.symbol: row.selection_reason for row in raw}
        self.assertEqual(reasons["1234"], "NOT_TOP_RANKED_SAME_MINUTE")
        self.assertEqual(reasons["9999"], "LATER_THAN_DAILY_SELECTION")

    def test_tick_size_boundaries(self):
        self.assertEqual(tick_size(9.99), 0.01)
        self.assertEqual(tick_size(10), 0.05)
        self.assertEqual(tick_size(50), 0.10)
        self.assertEqual(tick_size(100), 0.50)
        self.assertEqual(tick_size(500), 1.00)
        self.assertEqual(tick_size(1000), 5.00)
        self.assertEqual(ticks_above(49.95, 2), 50.1)
        self.assertEqual(ticks_above(99.9, 2), 100.5)


class ForwardAndShadowTests(unittest.TestCase):
    def test_forward_window_excludes_signal_bar(self):
        target = date(2026, 4, 1)
        signal = signal_fixture(target).selected_for_day()
        bars = [
            MinuteBar(
                signal.signal_time,
                signal.symbol,
                signal.market,
                100.5,
                999.0,
                1.0,
                100.5,
                1_000,
                100_500,
                108.9,
            )
        ]
        for offset in range(1, 61):
            price = 100.5 + offset * 0.01
            bars.append(
                MinuteBar(
                    signal.signal_time + timedelta(minutes=offset),
                    signal.symbol,
                    signal.market,
                    price,
                    price + 0.02,
                    price - 0.02,
                    price,
                    1_000,
                    price * 1_000,
                    108.9,
                )
            )
        rows = forward_returns([signal], bars)
        five = next(row for row in rows if row["horizon"] == "5m")
        self.assertTrue(five["complete"])
        self.assertLess(five["mfe"], 0.01)
        self.assertIn("NOT_EXECUTABLE_FILL", five["reference_basis"])

    def test_close_forward_return_requires_complete_post_signal_grid(self):
        target = date(2026, 4, 1)
        signal = signal_fixture(target).selected_for_day()
        bars = []
        cursor = signal.signal_time + timedelta(minutes=1)
        close_time = datetime.combine(target, time(13, 30))
        while cursor <= close_time:
            if cursor.time() != time(10, 0):
                bars.append(
                    MinuteBar(
                        cursor,
                        signal.symbol,
                        signal.market,
                        100.5,
                        100.6,
                        100.4,
                        100.5,
                        1_000,
                        100_500,
                        108.9,
                    )
                )
            cursor += timedelta(minutes=1)
        rows = forward_returns([signal], bars)
        close = next(row for row in rows if row["horizon"] == "close")
        self.assertFalse(close["complete"])
        self.assertIsNone(close["forward_return"])

    def test_eligible_quote_never_claims_order_or_fill(self):
        target = date(2026, 4, 1)
        signal = signal_fixture(target).selected_for_day()
        quote = OddLotQuote(
            exchange_time=signal.signal_time + timedelta(seconds=1),
            received_time=signal.signal_time + timedelta(seconds=2),
            symbol=signal.symbol,
            market=signal.market,
            bid1=100.4,
            ask1=100.5,
            ask1_quantity=600,
            ask2_quantity=600,
            regular_last=100.5,
            limit_up=108.9,
        )
        decision = evaluate_quote(signal, quote)
        self.assertEqual(decision.status, SHADOW_STATUS)
        self.assertFalse(decision.is_actual_order)
        self.assertFalse(decision.is_actual_fill)

    def test_stale_quote_is_rejected(self):
        target = date(2026, 4, 1)
        signal = signal_fixture(target).selected_for_day()
        quote = OddLotQuote(
            exchange_time=signal.signal_time + timedelta(seconds=1),
            received_time=signal.signal_time + timedelta(seconds=5),
            symbol=signal.symbol,
            market=signal.market,
            bid1=100.4,
            ask1=100.5,
            ask1_quantity=600,
            ask2_quantity=600,
            regular_last=100.5,
            limit_up=108.9,
        )
        decision = evaluate_quote(signal, quote)
        self.assertEqual(decision.rejection_reason, "QUOTE_STALE")


class SafetyAndInputTests(unittest.TestCase):
    def test_header_only_daily_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily.csv"
            path.write_text(
                "date,symbol,name,market,security_type,trading_status,close,volume,turnover\n",
                encoding="utf-8",
            )
            with self.assertRaises(DataValidationError):
                load_daily([path])

    def test_missing_actual_turnover_column_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily.csv"
            path.write_text(
                "date,symbol,name,market,security_type,trading_status,close,volume\n"
                "2026-01-01,1234,測試,TWSE,COMMON_STOCK,NORMAL,20,1000000\n",
                encoding="utf-8",
            )
            with self.assertRaises(DataValidationError):
                load_daily([path])

    def test_benchmark_index_ids_are_canonical_for_both_markets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            daily_path = root / "index_daily.csv"
            daily_path.write_text(
                "date,market,index_id,close\n"
                "2026-01-01,TWSE,TAIEX,20000\n"
                "2026-01-01,TPEX,TPEX,250\n",
                encoding="utf-8",
            )
            rows, _ = load_index_daily([daily_path])
            self.assertEqual({row.index_id for row in rows}, {"TAIEX", "TPEX"})

            minute_path = root / "index_minutes.csv"
            minute_path.write_text(
                "bar_end,market,index_id,close\n"
                "2026-01-02 09:01,TWSE,TPEX,20001\n",
                encoding="utf-8",
            )
            with self.assertRaises(DataValidationError):
                load_index_minutes([minute_path])

    def test_zero_volume_minute_must_carry_previous_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "minutes.csv"
            path.write_text(
                "bar_end,symbol,market,open,high,low,close,volume,turnover,limit_up\n"
                "2026-01-02 09:01,1234,TWSE,100,100,100,100,1000,100000,110\n"
                "2026-01-02 09:02,1234,TWSE,101,101,101,101,0,0,110\n",
                encoding="utf-8",
            )
            with self.assertRaises(DataValidationError):
                load_minutes([path])

    def test_package_contains_no_broker_order_entrypoint(self):
        package = Path(__file__).resolve().parent
        forbidden = ("Send" + "StockOrder", "Yuanta" + "SparkAPITrader")
        for path in package.glob("*.py"):
            if path == Path(__file__).resolve():
                continue
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, source, f"{token} unexpectedly present in {path}")

    def test_research_cli_runs_end_to_end_on_causal_fixture(self):
        target = date(2026, 4, 1)
        calendar = [target - timedelta(days=value) for value in range(60, -1, -1)]
        daily = []
        index_daily = []
        trading_calendar = []
        for i, day in enumerate(calendar):
            trading_calendar.extend(
                {"date": day.isoformat(), "market": market}
                for market in ("TWSE", "TPEX")
            )
            if day < target:
                index_daily.extend(
                    {
                        "date": day.isoformat(),
                        "market": market,
                        "index_id": "TAIEX" if market == "TWSE" else "TPEX",
                        "close": (100 if market == "TWSE" else 200) + i * 0.05,
                    }
                    for market in ("TWSE", "TPEX")
                )
                daily.append(
                    {
                        "date": day.isoformat(),
                        "symbol": "1234",
                        "name": "測試股",
                        "market": "TWSE",
                        "security_type": "COMMON_STOCK",
                        "trading_status": "NORMAL",
                        "close": 69.5 + i * 0.5,
                        "volume": 1_500_000,
                        "turnover": 150_000_000,
                    }
                )
                daily.append(
                    {
                        "date": day.isoformat(),
                        "symbol": "9998",
                        "name": "上櫃低價測試股",
                        "market": "TPEX",
                        "security_type": "COMMON_STOCK",
                        "trading_status": "NORMAL",
                        "close": 10.0,
                        "volume": 1_500_000,
                        "turnover": 150_000_000,
                    }
                )
        minutes, index_minutes, _ = signal_inputs(target)
        minute_rows = [
            {
                "bar_end": row.bar_end.isoformat(sep=" "),
                "symbol": row.symbol,
                "market": row.market,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "turnover": row.turnover,
                "limit_up": row.limit_up,
            }
            for row in minutes
        ]
        index_minute_rows = [
            {
                "bar_end": row.bar_end.isoformat(sep=" "),
                "market": row.market,
                "index_id": row.index_id,
                "close": row.close,
            }
            for row in index_minutes
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            daily_path = root / "daily.csv"
            index_daily_path = root / "index_daily.csv"
            trading_calendar_path = root / "trading_calendar.csv"
            minute_path = root / "minutes.csv"
            index_minute_path = root / "index_minutes.csv"
            _write_rows(daily_path, daily)
            _write_rows(index_daily_path, index_daily)
            _write_rows(trading_calendar_path, trading_calendar)
            _write_rows(minute_path, minute_rows)
            _write_rows(index_minute_path, index_minute_rows)
            output = root / "output"
            command = [
                sys.executable,
                "-B",
                str(Path(__file__).with_name("main.py")),
                "research",
                "--daily",
                str(daily_path),
                "--index-daily",
                str(index_daily_path),
                "--trading-calendar",
                str(trading_calendar_path),
                "--minutes",
                str(minute_path),
                "--index-minutes",
                str(index_minute_path),
                "--start-date",
                target.isoformat(),
                "--end-date",
                target.isoformat(),
                "--bootstrap-iterations",
                "20",
                "--output-dir",
                str(output),
            ]
            completed = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            validation = json.loads(
                (output / "validation_summary.json").read_text(encoding="utf-8")
            )
            self.assertTrue(validation["passed"])
            self.assertEqual(validation["checks"]["selected_signals"], 1)
            report = (output / "research_report.md").read_text(encoding="utf-8")
            self.assertIn("真實委託：0；真實成交：0", report)
            self.assertTrue((output / "forward_returns.csv").exists())
            manifest = json.loads(
                (output / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "COMPLETE")
            self.assertEqual(manifest["mode"], "research")


def _write_rows(path: Path, rows: list[dict]):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
