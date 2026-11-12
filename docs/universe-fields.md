# universe.json 欄位定義

`universe.json` 只放人工判斷的欄位，是候選池的唯一來源。
財務數字、均線、擁擠分數由程式抓取後寫入 `metrics.json`，兩個檔案用 `ticker` 對應。

## 最上層

| 欄位 | 說明 |
|---|---|
| `version` | 檔案格式版本，改欄位結構時加 1 |
| `updated` | 最後修改日期 |
| `rules` | 席位數、過門檻分數、各種上限。改門檻只改這裡 |
| `factor_tags` | 共同因素標籤的代碼和中文名稱 |
| `sectors` | 板塊清單 |
| `tickers` | 標的清單 |

## sectors（板塊）

| 欄位 | 說明 |
|---|---|
| `id` | 英文代碼，標的用它對應板塊 |
| `name` | 顯示名稱 |
| `default_tag` | 這個板塊預設的共同因素標籤 |
| `active` | `false` 代表停用整個板塊，儀表板不顯示，但資料保留 |

新增板塊：在 `sectors` 加一筆。刪除板塊：把 `active` 改成 `false`，不要真的刪掉。

## tickers（標的）

| 欄位 | 可填的值 | 對應步驟 |
|---|---|---|
| `ticker` | 股票代碼，不可重複 | |
| `name` | 公司名稱 | |
| `sector` | 板塊的 `id` | |
| `team` | `white`、`blue` | 第 1 步 |
| `stage` | `pool` 候選池、`watch` 觀察名單、`seat` 席位、`disabled` 停用 | |
| `role.essential` | `yes`、`partial`、`no`、`unknown` | 第 1 步：必需性 |
| `role.bottleneck` | `yes`、`partial`、`no`、`unknown` | 第 1 步：瓶頸性 |
| `theme` | 要驗證的題材，一句話 | 第 2 步 |
| `verification.status` | `verified`、`partial`、`unverified`、`warning`、`unreviewed` | 第 2 步 |
| `verification.evidence_level` | 1 分部金額、2 營運數字、3 公司定性說法、4 外部說法 | 第 2 步 |
| `verification.note` | 依據的數字或說明 | 第 2 步 |
| `verification.source` | 文件連結或「哪份文件第幾頁」 | 第 2 步 |
| `verification.date` | 查證日期 | 第 2 步 |
| `tags.primary` | 一個主標籤 | 第 5 步 |
| `tags.secondary` | 最多兩個副標籤 | 第 5 步 |
| `tags.personal` | 個人標籤，例如 `taiwan_concentration` | 第 5 步 |
| `proxy_score20` | 代理版 20 項分數，沒有評過填 `null` | 第 4 步 |
| `top_customer_pct` | 最大客戶占比，小數（0.25 代表 25%），沒查過填 `null` | 第 4 步 |
| `yahoo_alt` | 選填。Yahoo 用主代碼抓不到報表時改用的代碼，例如 `ASML.AS` | |
| `disabled_reason` | `stage` 是 `disabled` 時必填 | 第 7 步 |
| `updated` | 這一筆最後修改日期 | |

新增標的：在 `tickers` 加一筆，`stage` 先填 `pool`、`verification.status` 填 `unreviewed`。
刪除標的：把 `stage` 改成 `disabled`，並填 `disabled_reason`。

## metrics.json（程式自動產生）

目前已實作稀釋、漏錢、Altman Z、黃金三角、4 個指標。價格均線、估值快照、擁擠、內部人還沒做。

| 欄位群 | 內容 | 對應步驟 | 更新頻率 |
|---|---|---|---|
| 稀釋 | 1 年股數變動、每股自由現金流成長、股票薪酬 ÷ 營收、潛在稀釋 | 第 3 步 | 每季 |
| 漏錢 | 利息保障倍數、含租賃淨負債 ÷ 自由現金流、股息 ÷ 自由現金流、近 2 年到期負債 | 第 3 步 | 每季 |
| Altman Z | Z 或 Z''，金融與公用事業留空 | 第 3 步 | 每季 |
| 黃金三角 | ROE、負債權益比、利息保障倍數 | 第 4 步 | 每季 |
| 4 個指標 | 自由現金流 ÷ 資本支出、含租賃淨負債、最大客戶占比（人工補）、資本支出 ÷ 營收趨勢 | 第 4 步 | 每季 |
| 價格 | 收盤價、MA200／240／365、52 週漲跌幅、Beta | 第 6 步 | 每日 |
| 估值快照 | 預估本益比、未來 12 個月獲利預估（每月存一次，供腰斬判定） | 第 6 步 | 每月 |
| 擁擠 | 機構持股比例、放空比重、擁擠分數 | 第 6 步 | 每月 |
| 內部人 | 90 天內公開市場買進人數、主管計畫外賣出 | 第 7 步 | 每週 |

## 由兩個檔案算出來的結果

| 結果 | 算法 |
|---|---|
| 否決欄結論 | 通過、注意、否決（白隊和藍隊標準不同） |
| 總分 | 20 項 60% + 三角與 4 指標依隊別加權 |
| 組合檢查 | 各標籤席位數和資金占比，對照 `rules.caps` |
| 進場訊號 | 碰線、腰斬判定、擁擠分數 |
