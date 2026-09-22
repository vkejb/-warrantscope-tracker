#!/usr/bin/env python3
"""Native macOS GUI for the read-only Stage A intraday shadow collector."""

from __future__ import annotations

from datetime import datetime, time
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from zoneinfo import ZoneInfo

from .collector import DEFAULT_RUNTIME_DIR, load_stage_a_watchlist
from .collector_main import run
from .main import DEFAULT_VENDOR_DIR, _load_api
from .postprocess import process_run


TAIPEI = ZoneInfo("Asia/Taipei")
MODES = {
    "5分鐘測試": "TEST",
    "早盤至10:30": "MORNING",
    "全天至13:35": "FULL_DAY",
    "自訂分鐘": "CUSTOM",
}


def duration_seconds(mode: str, custom_minutes: str, now: datetime | None = None) -> int:
    now = now or datetime.now(TAIPEI)
    if mode == "TEST":
        return 300
    if mode == "CUSTOM":
        minutes = int(custom_minutes)
        if not 1 <= minutes <= 300:
            raise ValueError("自訂分鐘必須介於1至300。")
        return minutes * 60
    target_clock = time(10, 30) if mode == "MORNING" else time(13, 35)
    target = datetime.combine(now.date(), target_clock, tzinfo=TAIPEI)
    seconds = int((target - now).total_seconds())
    if seconds < 60:
        raise ValueError(f"目前已超過本模式結束時間 {target_clock.strftime('%H:%M')}。")
    if seconds > 18000:
        raise ValueError("距離收集結束時間超過5小時，請在08:35後啟動。")
    return seconds


class IntradayApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("WarrantScope Stage A 盤中 Shadow")
        self.root.geometry("760x650")
        self.events: queue.Queue[dict] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.run_dir: Path | None = None

        self.pfx = tk.StringVar()
        self.pfx_password = tk.StringVar()
        self.account = tk.StringVar()
        self.trading_password = tk.StringVar()
        self.mode_label = tk.StringVar(value="5分鐘測試")
        self.custom_minutes = tk.StringVar(value="30")
        self.status = tk.StringVar(value="尚未啟動")
        self.counts = tk.StringVar(value="逐筆 0｜五檔 0｜解析錯誤 0")
        self.seal_info = tk.StringVar(value="正在檢查 Stage A seal…")
        self._build()
        self._load_seal()
        self.root.after(200, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self):
        frame = ttk.Frame(self.root, padding=18); frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Stage A Top30 盤中行情收集", font=("Helvetica", 20, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.seal_info).pack(anchor="w", pady=(3, 14))
        form = ttk.Frame(frame); form.pack(fill="x")
        ttk.Label(form, text="PFX 憑證").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.pfx, width=58).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(form, text="選擇…", command=self._browse).grid(row=0, column=2)
        ttk.Label(form, text="憑證密碼").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.pfx_password, show="●").grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Label(form, text="證券帳號").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.account).grid(row=2, column=1, sticky="ew", padx=8)
        ttk.Label(form, text="電子交易密碼").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.trading_password, show="●").grid(row=3, column=1, sticky="ew", padx=8)
        ttk.Label(form, text="收集模式").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Combobox(form, textvariable=self.mode_label, values=list(MODES), state="readonly").grid(row=4, column=1, sticky="ew", padx=8)
        ttk.Label(form, text="自訂分鐘").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.custom_minutes).grid(row=5, column=1, sticky="ew", padx=8)
        form.columnconfigure(1, weight=1)

        buttons = ttk.Frame(frame); buttons.pack(fill="x", pady=14)
        self.start_button = ttk.Button(buttons, text="開始只讀收集", command=self._start); self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="安全停止", command=self._stop, state="disabled"); self.stop_button.pack(side="left", padx=8)
        ttk.Label(frame, textvariable=self.status, font=("Helvetica", 13, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.counts).pack(anchor="w", pady=(3, 8))
        self.log = tk.Text(frame, height=17, wrap="word", state="disabled"); self.log.pack(fill="both", expand=True)
        ttk.Label(frame, text="安全邊界：SHADOW_ONLY｜不模擬下單｜不呼叫任何委託 API｜密碼不落盤").pack(anchor="w", pady=(10, 0))

    def _append(self, text: str):
        self.log.configure(state="normal"); self.log.insert("end", text + "\n"); self.log.see("end"); self.log.configure(state="disabled")

    def _browse(self):
        path = filedialog.askopenfilename(title="選擇元大 PFX 憑證", filetypes=(("PFX 憑證", "*.pfx"), ("所有檔案", "*")))
        if path: self.pfx.set(path)

    def _load_seal(self):
        try:
            seal, items, _ = load_stage_a_watchlist()
            self.seal_info.set(f"最新封存：{seal['signal_date']}｜Top30｜上市 {sum(x.market == 'TWSE' for x in items)}／上櫃 {sum(x.market == 'TPEX' for x in items)}")
        except Exception as exc:
            self.seal_info.set(f"Stage A readiness failed：{type(exc).__name__}: {exc}")

    def _start(self):
        if self.worker and self.worker.is_alive(): return
        try:
            seconds = duration_seconds(MODES[self.mode_label.get()], self.custom_minutes.get())
        except Exception as exc:
            messagebox.showerror("無法啟動", str(exc)); return
        values = {"pfx": self.pfx.get(), "pfx_password": self.pfx_password.get(), "account": self.account.get(), "trading_password": self.trading_password.get()}
        if not all(values.values()):
            messagebox.showerror("資料不足", "請完整填寫憑證、帳號與兩組密碼。"); return
        self.pfx_password.set(""); self.trading_password.set(""); self.account.set("")
        self.stop_event.clear(); self.start_button.configure(state="disabled"); self.stop_button.configure(state="normal")
        self.status.set("正在載入元大 API…"); self._append(f"預定收集 {seconds // 60} 分鐘；原始資料使用 gzip append-only 封存。")
        self.worker = threading.Thread(target=self._work, args=(seconds, values), daemon=False); self.worker.start()

    def _progress(self, value: dict):
        if value.get("type") == "FINAL":
            self.run_dir = Path(value["run_dir"])
        self.events.put(value)

    def _work(self, seconds: int, credentials: dict[str, str]):
        try:
            api = _load_api(DEFAULT_VENDOR_DIR)
            code = run(api, seconds=seconds, runtime_dir=DEFAULT_RUNTIME_DIR, credentials=credentials, stop_event=self.stop_event, progress_callback=self._progress, compress=True)
            credentials.clear()
            if code != 0:
                self.events.put({"type": "DONE", "code": code}); return
            if not self.run_dir:
                raise RuntimeError("collector completed without run directory")
            result = process_run(self.run_dir)
            self.events.put({"type": "ANALYSIS_COMPLETE", **result})
            self.events.put({"type": "DONE", "code": 0})
        except Exception as exc:
            credentials.clear()
            self.events.put({"type": "ERROR", "message": f"{type(exc).__name__}: {exc}"})
            self.events.put({"type": "DONE", "code": 1})

    def _poll(self):
        while True:
            try: event = self.events.get_nowait()
            except queue.Empty: break
            kind = event.get("type")
            if kind == "LOGIN": self.status.set("登入成功" if event["ok"] else f"登入失敗 {event['code']}")
            elif kind == "SUBSCRIBED": self.status.set(f"已訂閱 {event['watchlist_count']} 檔，正在收集")
            elif kind == "PROGRESS":
                self.counts.set(f"逐筆 {event['ticks']}｜五檔 {event['books']}｜解析錯誤 {event['callback_errors']}｜剩餘 {event['remaining_seconds']//60} 分")
            elif kind == "FINAL":
                self.run_dir = Path(event["run_dir"]); m = event["manifest"]
                self.counts.set(f"逐筆 {m['event_counts']['ticks']}｜五檔 {m['event_counts']['books']}｜解析錯誤 {m['event_counts']['callback_errors']}")
                self._append(f"原始封存：{m['status']}｜{m['run_id']}｜hash {m['manifest_hash'][:12]}")
            elif kind == "ANALYSIS_COMPLETE":
                self._append(f"分析完成：{event['session']['coverage_status']}｜mature outcomes {event['session']['mature_outcome_rows']}")
                self._append(f"通知：{event['notification']}")
            elif kind == "ERROR": self.status.set("執行失敗"); self._append(event["message"])
            elif kind == "DONE":
                self.status.set("已完成" if event["code"] == 0 else "已停止／未完成")
                self.start_button.configure(state="normal"); self.stop_button.configure(state="disabled")
        self.root.after(200, self._poll)

    def _stop(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set(); self.status.set("正在安全停止…")

    def _close(self):
        if self.worker and self.worker.is_alive():
            if messagebox.askyesno("仍在收集", "要安全停止後再關閉嗎？"):
                self.stop_event.set(); self.status.set("正在安全停止，完成後即可關閉。")
            return
        self.root.destroy()


def main() -> int:
    root = tk.Tk(); IntradayApp(root); root.mainloop(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
