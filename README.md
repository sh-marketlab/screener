# 白8藍12篩選儀表板

依「白守藍攻」結構（白 8 + 藍 W12）篩選候選標的。規則見 [`rules.html`](rules.html)。

## 檔案

| 檔案 | 用途 |
|---|---|
| `universe.json` | 候選池和所有門檻。人工維護，是唯一的名單來源 |
| `docs/universe-fields.md` | `universe.json` 的欄位定義 |
| `scripts/fetch_metrics.py` | 抓財務資料，計算第 3 步否決欄和第 4 步評分，輸出 `data/metrics.json` |
| `scripts/test_evaluate.py` | 用假資料測試解析和否決邏輯 |
| `index.html` | 儀表板頁面，含第 5 步組合檢查 |
| `rules.html` | 8 步規則手冊 |

## 第一次設定

1. Settings → Secrets and variables → Actions，新增 `SEC_CONTACT`，內容是 SEC 要求的聯絡信箱。沒有設定時全部改用 Yahoo Finance。
2. Settings → Pages，Source 選 Deploy from a branch，分支選 `main`、資料夾選 `/ (root)`。
3. Actions → 抓取財務資料 → Run workflow，手動跑第一次。

之後每月 3 日自動更新一次。

## 增刪標的或板塊

直接改 `universe.json`：

- 新增標的：在 `tickers` 加一筆，`stage` 填 `pool`。
- 移除標的：把 `stage` 改成 `disabled` 並填 `disabled_reason`，不要刪掉。
- 新增板塊：在 `sectors` 加一筆。停用板塊：`active` 改成 `false`。
- 調整門檻：改 `rules`。

## 目前的限制

- 財務數字是近四季合計。外國公司（`rules.veto.foreign_filers`）用 Yahoo Finance 的季報，稀釋比較用年度數字。
- 總分要有 20 項分數（`proxy_score20`）才算得出來；目前是代理版，而且只有部分標的有分數。
- 最大客戶占比（`top_customer_pct`）、潛在稀釋、近 2 年到期負債、REIT 的 AFFO 要人工補。
- 銀行的否決規則還沒寫，結果會顯示「注意」。
- 組合檢查只算席位數，資金占比類的上限需要部位金額，沒有計算。
- 第 6 步進場訊號還沒做。

本專案是個人研究工具，不是投資建議。
