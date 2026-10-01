# 新版觀測與 A/B 紙上比較部署準備

狀態：`OBSERVATION_DEPLOYMENT_PREPARED_NOT_APPLIED`

本文件只描述觀測及紙上報告的部署準備。它不授權啟動 LIVE、登入券商、
送單、解除 HALT、修改 LaunchAgent，或讓 B 版本接觸 broker routing。

## 2026-10-02 接線稽核

| 元件 | 真正載入位置 | 稽核時程序狀態 | 新版是否已載入 |
|---|---|---|---|
| 盤中只讀收集與收盤 paper 流程 | `/Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE/stock-strategy` | LaunchAgent 已載入、程序未運行、上次 exit code 1 | 否；正式 main 為 `fc0412a` |
| Telegram trading bot | `/Users/linyunyan/Downloads/-warrantscope-tracker/stock-strategy` | PID 46557，2026-10-01 00:28:36 起持續運行 | 否；checkout 為 `23d5e22` |
| LIVE/OBSERVE runtime 子程序 | 由 trading bot 以同一 Python 與 PYTHONPATH 啟動 `yuanta_live_runtime_v01.main` | 稽核時沒有 runtime 子程序；heartbeat 為 `STOPPED_CLEAN` | 否；下次啟動仍會讀取 alternate checkout 的磁碟程式 |
| 研究來源 | `codex/exit-profit-research` | 不屬於正式排程 | 是；`d5c5c2d` |
| 本部署準備 | `codex/observation-dual-track-deploy` | 不屬於正式排程 | 已準備、未套用 |

實際 shadow LaunchAgent 的 WorkingDirectory 與 PYTHONPATH 都是主工作區的
`stock-strategy`，Python 是 Yuanta venv 的 3.11.4；已安裝 plist 與 repo plist 的
SHA-256 相同：`43d071d756c2019f9bf481586b741db01abcb5ec8b6c38d47070b4f2dda0b3dd`。
trading bot 的 WorkingDirectory 與 PYTHONPATH 都是 alternate checkout 的
`stock-strategy`，Python 是 3.10.0b4。兩者的 stdout/stderr 與 runtime 輸出也各自
落在相同 checkout，不可把其中一份的結果當成另一份已部署證據。

不能從「Git 有檔案」推論執行中程序已載入。若 LIVE/OBSERVE runtime 在部署時
正在運行，必須先依正常停止流程結束，之後重新啟動才會載入新版；本輪不執行。
目前 trading bot 本身不需因本 patch 重啟，因為它沒有被修改，而 runtime 子程序
是新的 Python process。若未來改到 trading bot 本身，則不能視為熱更新。

## 真正呼叫鏈

### LIVE/OBSERVE 事件與決策證據

1. `yuanta_live_runtime_v01.main._Session._on_response` 收到 broker callback。
2. 實際 `LiveDirectionEngine.ingest_tick/ingest_book_*` 回傳 accepted/rejected 與原因。
3. 同一 callback 將 run ID、generation、exchange/callback time、全域 ingest sequence、
   raw serial 與實際處理結果寫入既有 quote stream。
4. `main.run_realtime` 在進場、持倉管理及生命週期決策邊界呼叫
   `archive_decision_evidence`。
5. `AppendOnlyRun.finalize` 把 evidence/failure counts、ledger hashes 與最後 flat
   證據寫入 manifest。

觀測寫入失敗會記為 `OBSERVATION_WRITE_FAILURE`；觀測例外會被隔離，不會阻擋
既有 EXIT 路徑。排程的純行情 collector 不執行正式策略引擎，因此不能用它的
raw archive 宣稱 actual LIVE decision parity。

### 收盤 A/B 紙上比較

1. `yuanta_intraday_shadow_v01.auto_runner` 收盤後先完成 `process_run`。
2. `publish_paper_day` 用同一筆正式合法進場產生 A 與 B 紙上出場。
3. 既有 `write_comparison` 完成後，新增 `publish_dual_track`。
4. 原子寫入：
   `paper_shadow_v01/runtime/reports/fixed_dual_exit_latest.json`。
5. 若沒有合法進場或缺任一固定軌，輸出零配對與明確原因，不補造交易。
6. 比較器或觀測驗收失敗不得被當成通過；B 永遠不送 broker order。

## 凍結契約

A：`PRODUCTION_ANTI_CHASE`，沿用目前正式 `LIVE_EXIT_POLICY`：

- policy ID：`HARD_3500_PLUS_MFE_V1_NO_LOSS_RECOVERY`
- hard stop：淨損益 -3,500 TWD
- MFE retention：1.5R/2R/3R 分別 60%/70%/75%
- policy hash：`0564a1550e5e6cfde2717ae55c453eb38eb58aa289fc0ea3f0778c2c69995c07`

B：`RECOVERY_NET_MFE_BUFFER_0_30_SHADOW`：

- activation 0.75R、initial lock 0.30R
- 120 秒 checkpoint：PnL <= -0.20R、MFE <= 0.10R、recovery <= 0.20R
- hard stop：淨損益 -3,500 TWD
- policy hash：`5c08129883967dc6d605f58dd4fdd02d52ced7debed64a0b24daa7e8bf299016`

共同 entry spec hash：
`093c359e96409c7461373dc0a02142a7b1e945a967473536d966cda6992fad1c`

共同 paper contract hash：
`7828a8231e6be05a66e77058b3fb9deb4122221cc85ffac8b98e1e63ec2ef3ad`

稽核時正式主工作區與研究來源的上述 hash 完全相同，因此 A 的規則確實對應
當時正式版本；alternate checkout 的 A policy hash 也相同。部署後仍須由報告內
的 frozen contract 與 program hashes 再核對。

## 最小必要檔案

- `stock-strategy/yuanta_live_runtime_v01/main.py`
- `stock-strategy/yuanta_live_runtime_v01/strategy.py`
- `stock-strategy/yuanta_live_runtime_v01/tests/test_observation_evidence.py`
- `stock-strategy/yuanta_intraday_shadow_v01/collector.py`
- `stock-strategy/yuanta_intraday_shadow_v01/auto_runner.py`
- `stock-strategy/yuanta_intraday_shadow_v01/tests/test_automation.py`
- `stock-strategy/paper_shadow_v01/runner.py`
- `stock-strategy/paper_shadow_v01/dual_track.py`
- `stock-strategy/paper_shadow_v01/tests/test_dual_track.py`
- `stock-strategy/exit_profit_research_v01/__init__.py`
- `stock-strategy/exit_profit_research_v01/readonly_acceptance.py`
- 本文件

沒有修改正式進場、正式出場、下單數量、風控門檻、broker adapter、routing、
LIVE gates、Keychain、runtime state 或任何 plist。

## 預設 dry-run

以下命令只顯示計畫與差異，不修改兩個正式 checkout：

```bash
git -C /Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE \
  status --short --branch
git -C /Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE \
  diff --stat main..codex/observation-dual-track-deploy
git -C /Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE \
  diff --name-status main..codex/observation-dual-track-deploy
git -C /Users/linyunyan/Downloads/-warrantscope-tracker status --short --branch
```

套用前必須再確認上述最小必要路徑沒有未提交變更，且 LIVE/OBSERVE runtime
沒有運行。主工作區有其他不相關變更時，不得 reset、清除或覆蓋。

## 經核准後的部署順序（本輪不要執行）

1. 記錄兩個 checkout 的舊 HEAD 與相關檔案清單。
2. 對最小必要檔案建立外部備份。
3. 主工作區使用 `--ff-only` 前進至部署準備分支。
4. 驗證後，讓 alternate checkout 以主工作區分支為來源做 `--ff-only`。
5. 不改 plist；下一次 shadow 排程與下一個新 runtime process 會從各自 checkout
   載入磁碟上的新版。

```bash
DEPLOY_BACKUP=/tmp/warrantscope-observation-backup-$(date +%Y%m%dT%H%M%S)
mkdir "$DEPLOY_BACKUP"
git -C /Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE \
  diff --name-only main..codex/observation-dual-track-deploy > "$DEPLOY_BACKUP/files.txt"
git -C /Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE \
  bundle create "$DEPLOY_BACKUP/before.bundle" main

git -C /Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE \
  merge --ff-only codex/observation-dual-track-deploy
git -C /Users/linyunyan/Downloads/-warrantscope-tracker \
  fetch /Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE \
  codex/observation-dual-track-deploy
git -C /Users/linyunyan/Downloads/-warrantscope-tracker \
  merge --ff-only FETCH_HEAD
```

## 撤回（本輪不要執行）

不使用 `reset --hard`，也不刪除 runtime 證據。若部署後需要撤回，在主工作區
對部署 commit 建立 revert commit，再讓 alternate checkout fast-forward 到同一
撤回版本：

```bash
git -C /Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE \
  revert --no-edit codex/observation-dual-track-deploy
git -C /Users/linyunyan/Downloads/-warrantscope-tracker \
  fetch /Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE main
git -C /Users/linyunyan/Downloads/-warrantscope-tracker \
  merge --ff-only FETCH_HEAD
```

若撤回時 LIVE/OBSERVE runtime 已運行，先依正常安全停止流程處理；不要在持倉
生命週期中替換已載入模組。已生成的 evidence/report 保留供稽核，不偽裝成新版
仍在運行。

## 下一個已收集交易日的只讀驗收

```bash
PYTHONPATH=stock-strategy python3 -B -m exit_profit_research_v01.readonly_acceptance \
  --run-dir /absolute/path/to/yuanta_intraday_shadow_v01/runtime/runs/RUN_ID
```

```bash
PYTHONPATH=stock-strategy python3 -B -m paper_shadow_v01.dual_track \
  --runtime-dir /absolute/path/to/paper_shadow_v01/runtime \
  --source-runtime-dir /absolute/path/to/yuanta_intraday_shadow_v01/runtime \
  --output /tmp/fixed_dual_exit_validation.json
```

驗收必須分開查看 recording integrity、decision-boundary availability、actual LIVE
parity。零配對、UNKNOWN、observation failure、缺 ledger 或 hash mismatch 都不能
宣稱新版已驗證成功。
