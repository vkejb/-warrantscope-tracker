"""Create a Traditional Chinese report from completed research artifacts only."""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path


def fmt(x):
    if x is None:return 'NA'
    if isinstance(x,float):return f'{x:,.2f}'
    return str(x)


def write_report(output:Path):
    tables=json.loads((output/'tables.json').read_text())
    manifest=json.loads((output/'run_manifest.json').read_text())
    selection=json.loads((output/'selection_before_holdout.json').read_text())
    rows=[x for x in tables['experiments'] if x.get('scenario')=='BASE_ASSUMED_LOTS' and x.get('experiment') in ['A','B','C','D'] and x.get('study_status')=='EXPLORATORY_CONDITIONAL']
    audit=tables['DATA_AUDIT']
    good=[x for x in audit if x['evaluation_grade']=='SIMPLIFIED_CONDITIONAL']
    lines=['# 現股當沖做空第一輪研究報告','',
      '## 結論與證據等級','',
      '本輪完成獨立離線資料盤點、規格凍結、逐筆因果訊號、延遲五檔整張容量情境、共用資金組合及合成測試。正式系統維持LONG-only；SHADOW_ONLY、NO_LIVE_ORDER、actual_orders=0。',
      '尚不能證明加入做空能改善真實可執行的成本後績效。嚴格可執行組因五檔量單位、歷史資格/進場時限制及部分時鐘未證，損益與回撤均為NA，不是0。以下數字僅為明示假設的情境結果，不是成交或上線證據。','',
      f'本輪可作簡化研究{len(good)}日；完整可執行性認證0日。來源資料截止2026/10/29。',
      f'空方版本：{selection["entry"]}；選擇狀態：{selection["status"]}。若無贏家，B/C/D只用事前固定S1_NONE參考，不能稱最佳空方。',
      '如某組沒有成交，其0元代表模型沒有部位，不是已證明安全或有空方優勢。','',
      '## 共用基準比較（主情境，單位新臺幣）','',
      '假設五檔原量為張、延遲1秒、單次五檔10%容量、整1000股、不利1tick；實際單位尚未證實。每組起始190000元，最多一倉/每日一筆，風險3500元；SHORT賣出款不增加可用額度。權益跨日累計，不將各日浮盈相加。','',
      '|組別|有效日|進場/完成|稅費前|稅費後|最大回撤|相對A增量|未回補股數|',
      '|---|---:|---:|---:|---:|---:|---:|---:|']
    for x in rows:
        lines.append(f'|{x["experiment"]}|{x["effective_days"]}|{x["entries"]}/{x["closed_trades"]}|{fmt(x["gross_pnl"])}|{fmt(x["net_pnl"])}|{fmt(x["max_drawdown_twd"])}|{fmt(x.get("common_day_incremental_net_vs_A"))}|{x["cover_incomplete_quantity"]}|')
    lines+=['','A=目前LONG訊號在本輪較嚴暖機及成交模型的重播；B=固定/選定SHORT；C=多空共用資金與次數；D=當下無合格LONG才允許SHORT。C/D沒有兩份190000元。',
      '原背景paper使用立即全額成交、固定每場資金，本輪有延遲/容量/費用資金預留與跨日權益，兩者損益差不可全歸因於加入SHORT。原始dated基準另見baseline_reference.csv。','',
      '## 各版本與較差情境','',
      '|實驗|情境|進場/完成|成本後損益|最大回撤|扣最佳一天後|',
      '|---|---|---:|---:|---:|---:|']
    for x in tables['experiments']:
        if x.get('study_status')!='EXPLORATORY_CONDITIONAL':continue
        lines.append(f'|{x["experiment"]}|{x["scenario"]}|{x["entries"]}/{x["closed_trades"]}|{fmt(x["net_pnl"])}|{fmt(x["max_drawdown_twd"])}|{fmt(x["net_ex_best_day"])}|')
    lines+=['','UNIT_SHARES與UNIT_LOTS只是敏感度假設，不是用哪個績效好來判定正確單位；深度不足造成0成交也不能證明策略穩健。',
      'experiments.csv保留所有失敗/無交易版本、平均/中位損益、勝率、PF、最大虧損、連虧、最差日、MFE/MAE、持有時間、浮盈回吐、跳過與未完成數量；零完成交易的勝率/PF為NA。','',
      '## 資料覆蓋與時點','',
      '|日期|切分|分級|原因/限制|',
      '|---|---|---|---|']
    for x in audit:
        reason='; '.join(x.get('exclusion_reasons',[])) or '單位/歷史資格/五檔交易所時間未證；只限條件情境'
        lines.append(f'|{x["day"]}|{x.get("split","AUDIT_ONLY")}|{x["evaluation_grade"]}|{reason}|')
    lines+=['','DATA_AUDIT.csv包含每股tick/book前後覆蓋、觀察間隔、原flag、暫停/恢復、header、重複/亂序、消息與序號、hash鏈及開盤前seal/watchlist時間。觀察無成交間隔不等於已證明斷線；沒有專門連線記錄的恢復時間保留未知。',
      '0050僅用已封存同步tick/book；2330與加權指數依有無分列，不以今日/日線回填。候選池廣度不作全市場廣度。歷史交易所六日清單只作資格線索，缺當時取得時間及券商限制，未自動升級為可成交。',
      'DealVol採SDK原生交易訊息量；以非單調診斷排除連續累積量的可能，不將數值大小當股/張證明。60秒flow與300秒P90需要真正逐筆訊息語義，若上游為重複快照/不完整串流，指標仍具限制。',
      '開盤auction實量計入VWAP、不計方向flow；SerialNo=-1不是成交。未知/定價/狀態方向不硬歸類。缺開盤不以第一筆補造完整VWAP/OR；>60秒觀察缺口須重暖機。','',
      '## 樣本、選擇與退出','',
      '訓練9/24–10/16，驗證10/19–22，回溯留出10/27–29；實際可用日期見上表，同一天不拆散。10/23因預先列明日曆衝突只稽核。留出LONG結果在本研究前已被看過，不能稱真正未觸碰樣本外。真正未來驗證保留10/30起PENDING。',
      '選擇只看訓練/驗證，至少10筆完成、5個獨立日且淨利正才選；這是防硬選規則，不是統計顯著門檻。樣本不足不提供誤導的逐筆bootstrap信賴區間、不年化、不宣稱正期望。',
      'E0沿用現行3500淨停損與MFE_V1、反向確認、13:20開始回補。E1結構失效、E2五分鐘低於0.25netR且買方flow重占優只作訓練/驗證診斷，未把它們的留出結果用來選擇。完整門檻與R定義見EXPERIMENT_SPEC.md及IMPLEMENTATION_NOTES.md。',
      '未完成回補仍保留股數與風險，完整淨利/DD為NA；observed_partial_max_drawdown只是已觀察局部下限，不是假造完整帳戶回撤。缺合法報價不拿最後成交價填平；13:25起不以連續撮合模型假設成交。','',
      '## 集中度、事件與未成交','',
      'grouped_diagnostics.csv分日期、股票、市場強弱與早盤/盤中；net_ex_best_day為扣除最佳一天的集中度診斷。missed_longs.csv是空方占用次數/資金後錯過的合格LONG候選，不代表每個候選原本必成交。',
      'event_cases.json保留最大/最差完成交易與首個異常附近行情和狀態；若沒有正收益，maximum_profit只是相對最大損益，不表示賺錢。fills.csv提供逐次部分成交及cover守恆。',
      'execution_issues.csv保留無報價、IOC未成交/剩量、深度不足、時鐘/序號/上下限未知、未回補及券差費用不可估部分。研究IOC自動取消餘量是模型假設，不是券商撤單證據。','',
      '## 成本、基準差異與後續','',
      '手續費沿用目前程式各邊0.000855、最低20元向上取整；現股當沖股票賣出稅0.0015，SHORT稅基在進場。沒有讀取帳號/憑證或券商帳戶費率；實際折扣/最低費及券差成本仍未確認。',
      '與舊對話的差異：已有short_side_study_v01、S4/account replay與後續UNIT_UNVERIFIED稽核，不能只依9/24TODO聲稱尚無研究。當前正式SHORT仍未啟用，現行LONG出場不是舊SPEC的5000停損。既有日線短波段研究完全未併入。',
      '值得繼續的是資料與執行證據收集，不是據此啟用SHORT。下一輪應先取得：歷史SDK/collector版本綁定、DealVol及五檔Vol單位/逐筆語義、逐日先賣後買及暫停/處置生效時間、callback原始市場/通道/序號、五檔交易所時間、連線斷復紀錄、實際帳戶費率、模擬委託受理/取消與成交延遲。不得為取得這些而自行登入/訂閱/送單。','',
      '## 重現與安全','',
      f'來源commit：`{manifest["source_commit"]}`；來源工作目錄：`{manifest["source_root"]}`。原工作區有未提交修改，詳本機freeze.json，未覆寫。',
      '`python3 -B -m cash_short_intraday_research_v01.run --output cash_short_intraday_research_v01/results_reproduction`',
      '再執行本模組report及export_tables.mjs。測試：`python3 -B -m unittest discover -s cash_short_intraday_research_v01/tests -v`。依manifest hashes需原封存資料，壓縮包不附全量原始行情或帳戶資料。',
      '保護原碼hash前後驗證、來源manifest與artifact hash、protocol/config凍結及無broker import的測試均可追查。測試通過只證明實作契約，不證明交易獲利。','',
      '官方來源：',
      '- https://www.twse.com.tw/zh/products/system/day-trading.html',
      '- https://www.twse.com.tw/zh/trading/day-trading.html',
      '- https://www.twse.com.tw/zh/products/system/trading.html',
      '- https://www.etax.nat.gov.tw/etwmain/tax-info/understanding/tax-knowledge/rwG2M1N','']
    (output/'RESEARCH_REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    return output/'RESEARCH_REPORT.md'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    print(write_report(parser.parse_args().output))
