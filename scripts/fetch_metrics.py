"""抓取第 3 步否決欄需要的數字，輸出 data/metrics.json。

資料來源：
- 美國公司：SEC XBRL companyfacts（近四季，沒有季報時用最近年度），Yahoo Finance 做交叉檢查。
- 外國公司（universe.json 的 rules.veto.foreign_filers）或 SEC 抓不到時：Yahoo Finance 年度報表。
- 股價、市值、Beta、殖利率、機構持股、放空比重：Yahoo Finance。

環境變數 SEC_CONTACT：SEC 要求在 User-Agent 附上聯絡資訊。沒有設定時全部改走 Yahoo。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE = ROOT / "universe.json"
OUT = ROOT / "data" / "metrics.json"

# 每個欄位的備選 XBRL 科目，依序嘗試，取到第一個有值的
DURATION = {
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquirePropertyAndEquipmentAndIntangibleAssets",
              "PaymentsToAcquireOtherPropertyPlantAndEquipment"],
    "capex_software": ["PaymentsToDevelopSoftware", "PaymentsForSoftware", "PaymentsToAcquireSoftware"],
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "op_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt",
                         "InterestAndDebtExpense", "InterestPaidNet"],
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "da": ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
           "DepreciationAndAmortization", "DepreciationAmortizationAndOther"],
    "dividends": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding",
               "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
               "WeightedAverageNumberOfSharesOutstandingBasic"],
}
INSTANT = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "st_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                       "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "debt_noncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "debt_current": ["LongTermDebtCurrent", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "debt_short": ["ShortTermBorrowings", "CommercialPaper"],
    "debt_total_alt": ["DebtLongtermAndShorttermCombinedAmount", "LongTermDebt"],
    "lease_operating": ["OperatingLeaseLiability"],
    "lease_finance": ["FinanceLeaseLiability"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}


ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
QUARTER_FORMS = {"10-Q", "10-Q/A"}


def _d(s: str) -> date:
    return datetime.fromisoformat(s).date()


def _days(a: str, b: str) -> int:
    return (_d(b) - _d(a)).days


def _rows(facts: dict, tag: str, duration: bool) -> list[dict]:
    """某個科目的所有申報值。同一期間被重複申報時，取最晚申報的那一筆。"""
    best: dict[tuple, dict] = {}
    for unit_rows in (facts.get(tag) or {}).get("units", {}).values():
        for r in unit_rows:
            if r.get("form") not in ANNUAL_FORMS | QUARTER_FORMS or (("start" in r) != duration):
                continue
            key = (r.get("start"), r["end"])
            if key not in best or r.get("filed", "") >= best[key].get("filed", ""):
                best[key] = r
    return list(best.values())


def _ttm_one(rows: list[dict], end: str):
    """算出截至 end 的近四季合計：最近年度 + 今年累計 - 去年同期累計。"""
    cands = [r for r in rows if r["end"] == end]
    if not cands:
        return None
    ytd = max(cands, key=lambda r: _days(r["start"], r["end"]))
    span = _days(ytd["start"], ytd["end"])
    if 350 <= span <= 380:
        return ytd["val"]
    if span > 380:
        return None
    fy = [r for r in rows if 350 <= _days(r["start"], r["end"]) <= 380 and abs(_days(r["end"], ytd["start"]) - 1) <= 6]
    py = [r for r in rows if abs(_days(r["end"], end) - 365) <= 12 and abs(_days(r["start"], r["end"]) - span) <= 12]
    if fy and py:
        return fy[0]["val"] + ytd["val"] - py[0]["val"]
    return None


def _ttm(facts: dict, tags: list[str], end: str | None):
    if not end:
        return None
    for tag in tags:
        val = _ttm_one(_rows(facts, tag, True), end)
        if val is not None:
            return val
    return None


def _latest_period(facts: dict, tags: list[str], end: str | None):
    """不能加總的期間型數字（加權平均股數）：取截至 end 最短的那一期。"""
    if not end:
        return None
    for tag in tags:
        cands = [r for r in _rows(facts, tag, True) if r["end"] == end]
        if cands:
            return min(cands, key=lambda r: _days(r["start"], r["end"]))["val"]
    return None


def _instant_at(facts: dict, tags: list[str], end: str | None):
    if not end:
        return None
    for tag in tags:
        for r in _rows(facts, tag, False):
            if r["end"] == end:
                return r["val"]
    return None


def _annual(facts: dict, tags: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for tag in tags:
        for r in _rows(facts, tag, True):
            if 350 <= _days(r["start"], r["end"]) <= 380:
                out.setdefault(r["end"], r["val"])
    return out


def extract_sec(companyfacts: dict) -> dict | None:
    """把 SEC companyfacts 轉成統一格式。有 10-Q 時用近四季，否則用最近年度。"""
    facts = companyfacts.get("facts", {}).get("us-gaap", {})
    ocf_rows = [r for tag in DURATION["ocf"] for r in _rows(facts, tag, True)]
    if not ocf_rows:
        return None
    ends = sorted({r["end"] for r in ocf_rows})
    end = next((e for e in reversed(ends) if _ttm(facts, DURATION["ocf"], e) is not None), None)
    if not end:
        return None
    prev_end = next((e for e in reversed(ends) if abs(_days(e, end) - 365) <= 12
                     and _ttm(facts, DURATION["ocf"], e) is not None), None)
    latest = max((r for r in ocf_rows if r["end"] == end), key=lambda r: _days(r["start"], r["end"]))
    basis = "年度" if 350 <= _days(latest["start"], latest["end"]) <= 380 else "近四季"

    def period(e):
        d = {}
        for key, tags in DURATION.items():
            d[key] = _latest_period(facts, tags, e) if key == "shares" else _ttm(facts, tags, e)
        if d.get("capex") is not None:
            d["capex"] += d.get("capex_software") or 0
        return d

    cur, prev = period(end), period(prev_end)
    for key, tags in INSTANT.items():
        cur[key] = _instant_at(facts, tags, end)
    parts = [cur.get("debt_noncurrent"), cur.get("debt_current"), cur.get("debt_short")]
    cur["debt"] = sum(p or 0 for p in parts) if any(p is not None for p in parts) else cur.get("debt_total_alt")
    cur["leases"] = (cur.get("lease_operating") or 0) + (cur.get("lease_finance") or 0)
    cur["cash_total"] = ((cur.get("cash") or 0) + (cur.get("st_investments") or 0)) if cur.get("cash") is not None else None

    capex_a, soft_a, rev_a = (_annual(facts, DURATION[k]) for k in ("capex", "capex_software", "revenue"))
    trend = []
    for e in sorted(set(capex_a) & set(rev_a))[-3:]:
        if rev_a[e]:
            trend.append({"end": e, "capex_to_revenue": (capex_a[e] + soft_a.get(e, 0)) / rev_a[e]})
    return {"fiscal_end": end, "basis": basis, "cur": cur, "prev": prev, "trend": trend, "latest_fact_end": ends[-1]}


YF_ROWS = {
    "ocf": ("cashflow", ["Operating Cash Flow"]),
    "capex": ("cashflow", ["Capital Expenditure"]),
    "sbc": ("cashflow", ["Stock Based Compensation"]),
    "da": ("cashflow", ["Depreciation And Amortization", "Depreciation Amortization Depletion"]),
    "dividends": ("cashflow", ["Cash Dividends Paid", "Common Stock Dividend Paid"]),
    "revenue": ("income", ["Total Revenue"]),
    "op_income": ("income", ["Operating Income", "EBIT"]),
    "net_income": ("income", ["Net Income"]),
    "interest_expense": ("income", ["Interest Expense", "Interest Expense Non Operating"]),
    "shares": ("income", ["Diluted Average Shares", "Basic Average Shares"]),
    "cash_total": ("balance", ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"]),
    "debt": ("balance", ["Total Debt"]),
    "equity": ("balance", ["Stockholders Equity", "Common Stock Equity"]),
    "total_assets": ("balance", ["Total Assets"]),
    "total_liabilities": ("balance", ["Total Liabilities Net Minority Interest"]),
    "current_assets": ("balance", ["Current Assets"]),
    "current_liabilities": ("balance", ["Current Liabilities"]),
    "retained_earnings": ("balance", ["Retained Earnings"]),
}
YF_ABS = ("capex", "dividends", "interest_expense")


def _yf_row(df, names):
    if df is None or getattr(df, "empty", True):
        return None
    for name in names:
        if name in df.index:
            return df.loc[name]
    return None


def yahoo_frames(tk) -> dict:
    return {"annual": {"cashflow": tk.cashflow, "income": tk.income_stmt, "balance": tk.balance_sheet},
            "quarterly": {"cashflow": tk.quarterly_cashflow, "income": tk.quarterly_income_stmt,
                          "balance": tk.quarterly_balance_sheet}}


def extract_yahoo(frames: dict) -> dict | None:
    """把 yfinance 報表轉成統一格式。季報滿四季時用近四季，否則用年度。
    Yahoo 通常只給四到五季，算不出一年前的近四季，所以成長率比較一律用年度（growth_cur／growth_prev）。
    Yahoo 的 Total Debt 已含租賃負債。"""
    import pandas as pd

    def ok(x):
        return x is not None and pd.notna(x)

    ann, qtr = frames["annual"], frames["quarterly"]
    a_cur, a_prev, ttm = {}, {}, {}
    for key, (frame, names) in YF_ROWS.items():
        row = _yf_row(ann[frame], names)
        if row is not None:
            for target, idx in ((a_cur, 0), (a_prev, 1)):
                if len(row) > idx and ok(row.iloc[idx]):
                    target[key] = float(row.iloc[idx])
        qrow = _yf_row(qtr[frame], names)
        if qrow is not None and len(qrow):
            if frame == "balance" or key == "shares":
                if ok(qrow.iloc[0]):
                    ttm[key] = float(qrow.iloc[0])
            elif len(qrow) >= 4 and all(ok(x) for x in qrow.iloc[:4]):
                ttm[key] = float(sum(qrow.iloc[:4]))
    for d in (a_cur, a_prev, ttm):
        for k in YF_ABS:
            if d.get(k) is not None:
                d[k] = abs(d[k])
    if a_cur.get("ocf") is None and ttm.get("ocf") is None:
        return None

    trend = []
    cap, rev = _yf_row(ann["cashflow"], YF_ROWS["capex"][1]), _yf_row(ann["income"], YF_ROWS["revenue"][1])
    if cap is not None and rev is not None:
        for col in list(cap.index)[:3][::-1]:
            if col in rev.index and ok(cap[col]) and ok(rev[col]) and rev[col]:
                trend.append({"end": str(col.date()), "capex_to_revenue": abs(float(cap[col])) / float(rev[col])})

    a_cur.setdefault("leases", 0)
    if ttm.get("ocf") is not None:
        cur = {**a_cur, **ttm, "leases": 0}
        end, basis = str(qtr["cashflow"].columns[0].date()), "近四季"
    else:
        cur, basis = dict(a_cur), "年度"
        end = str(ann["cashflow"].columns[0].date())
    return {"fiscal_end": end, "basis": basis, "cur": cur, "prev": {}, "trend": trend,
            "growth_cur": a_cur, "growth_prev": a_prev}


def merge_sources(sec: dict, yahoo: dict | None) -> tuple[dict, list[str]]:
    """以 SEC 為主，補上 Yahoo。回傳合併後的資料和「哪些欄位用了 Yahoo」。

    - 兩邊期末月份相同時：SEC 缺的欄位用 Yahoo 補；現金和負債用 Yahoo 的合計數
      （各家在 XBRL 裡的負債科目差異太大，常只抓到一部分；Yahoo 的 Total Debt 已含租賃）；
      資本支出取兩邊較大者（XBRL 常漏掉資本化軟體），去年同期按同比例調整。
    - SEC 抓不到股數時，稀釋檢查改用 Yahoo 的年度數字。
    """
    used: list[str] = []
    if not yahoo:
        return sec, used
    cur, ycur = sec["cur"], yahoo["cur"]
    if yahoo.get("fiscal_end", "")[:7] == sec["fiscal_end"][:7]:
        for key, val in ycur.items():
            if cur.get(key) is None and val is not None:
                cur[key] = val
                used.append(key)
        for key in ("cash_total", "debt"):
            if ycur.get(key) is not None and cur.get(key) != ycur[key]:
                cur[key] = ycur[key]
                used.append(key)
                if key == "debt":
                    cur["leases"] = 0
        a, b = cur.get("capex"), ycur.get("capex")
        if a and b and b > a * 1.05:
            cur["capex"] = b
            if sec["prev"].get("capex") is not None:
                sec["prev"]["capex"] *= b / a
            used.append("capex")
    if (cur.get("shares") is None or sec["prev"].get("shares") is None) and yahoo.get("growth_prev", {}).get("shares"):
        sec["growth_cur"], sec["growth_prev"] = yahoo["growth_cur"], yahoo["growth_prev"]
        used.append("稀釋比較用年度數字")
    if not sec.get("trend"):
        sec["trend"] = yahoo.get("trend", [])
    return sec, used


def _div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def evaluate(fin: dict, market: dict, entry: dict, rules: dict) -> dict:
    """依 universe.json 的門檻算出各項檢查和結論。純函式，方便離線測試。"""
    v = rules["veto"]
    team, sector = entry["team"], entry["sector"]
    if sector in v.get("bank_sectors", []):
        return {"metrics": {}, "verdict": "flag", "manual_todo": ["銀行要看資本適足率與信用損失"],
                "checks": [{"id": "bank", "label": "銀行：現金流框架不適用，規則待補", "value": None,
                            "threshold": None, "result": "pending"}]}
    cur, prev = fin["cur"], fin["prev"]
    m: dict = {}
    checks: list[dict] = []

    def add(cid, label, value, threshold, result):
        checks.append({"id": cid, "label": label, "value": value, "threshold": threshold, "result": result})

    def _fcf(d):
        return d["ocf"] - (d.get("capex") or 0) if d.get("ocf") is not None else None

    gc, gp = fin.get("growth_cur") or cur, fin.get("growth_prev") or prev
    fcf = _fcf(cur)
    fcf_g, fcf_prev = _fcf(gc), _fcf(gp)
    m["fcf"] = fcf

    def _sum(d, a, b):
        return d[a] + d[b] if d.get(a) is not None and d.get(b) is not None else None

    ebitda = _sum(cur, "op_income", "da")
    ffo = _sum(cur, "net_income", "da")
    ffo_g, ffo_prev = _sum(gc, "net_income", "da"), _sum(gp, "net_income", "da")
    m["ebitda"] = ebitda
    m["share_growth"] = _div(gc.get("shares"), gp.get("shares"))
    if m["share_growth"] is not None:
        m["share_growth"] -= 1

    # 稀釋：股數增加超過門檻才檢查
    sg = m["share_growth"]
    if sg is None:
        add("dilution", "已發生的稀釋", None, None, "missing")
    elif sg <= v["share_growth_check"]:
        add("dilution", "已發生的稀釋", sg, v["share_growth_check"], "pass")
    else:
        if sector in v["reit_sectors"] and ffo_g and ffo_prev and ffo_g > 0 and ffo_prev > 0:
            growth, basis = (ffo_g / gc["shares"]) / (ffo_prev / gp["shares"]) - 1, "每股 FFO 粗估"
        elif sector in v["utility_sectors"] and (gc.get("net_income") or 0) > 0 and (gp.get("net_income") or 0) > 0:
            growth, basis = (gc["net_income"] / gc["shares"]) / (gp["net_income"] / gp["shares"]) - 1, "每股盈餘"
        elif fcf_g and fcf_prev and fcf_g > 0 and fcf_prev > 0:
            growth, basis = (fcf_g / gc["shares"]) / (fcf_prev / gp["shares"]) - 1, "每股自由現金流"
        elif gc.get("revenue") and gp.get("revenue"):
            growth, basis = (gc["revenue"] / gc["shares"]) / (gp["revenue"] / gp["shares"]) - 1, "每股營收"
        else:
            growth, basis = None, None
        m["per_share_growth"], m["per_share_basis"] = growth, basis
        if growth is None:
            add("dilution", "已發生的稀釋（沒有可比的每股指標）", sg, v["share_growth_check"], "veto")
        else:
            add("dilution", f"已發生的稀釋（{basis}成長 {growth:.1%}）", sg, sg, "pass" if growth >= sg else "veto")

    m["sbc_to_revenue"] = _div(cur.get("sbc"), cur.get("revenue"))
    if m["sbc_to_revenue"] is not None:
        add("sbc", "股票薪酬 ÷ 營收", m["sbc_to_revenue"], v["sbc_to_revenue_flag"],
            "flag" if m["sbc_to_revenue"] > v["sbc_to_revenue_flag"] else "pass")

    capex_funded = sector in v["utility_sectors"] + v["reit_sectors"] + v["infra_sectors"]
    if fcf is not None and fcf < 0 and not capex_funded:
        m["cash_runway_years"] = _div(cur.get("cash_total"), -fcf)
        r = m["cash_runway_years"]
        add("runway", "現金跑道（年）", r, v["cash_runway_years_min"],
            "missing" if r is None else ("veto" if r < v["cash_runway_years_min"] else "pass"))

    is_utility = sector in v["utility_sectors"]
    is_reit = sector in v["reit_sectors"]
    soft = "veto" if team == "white" else "flag"

    is_infra = sector in v["infra_sectors"]
    if is_reit:
        ic_min, ic_label = v["reit_ebitda_coverage_min"], "REIT：EBITDA ÷ 利息"
        m["interest_coverage"] = _div(ebitda, cur.get("interest_expense"))
    else:
        ic_min = v["utility_interest_coverage_min"] if is_utility else v["interest_coverage_min"]
        ic_label = "利息保障倍數"
        m["interest_coverage"] = _div(cur.get("op_income"), cur.get("interest_expense"))
    ic = m["interest_coverage"]
    if ic is not None:
        add("coverage", ic_label, ic, ic_min, soft if ic < ic_min else "pass")
    elif not cur.get("interest_expense"):
        add("coverage", "利息保障倍數（無利息費用或未揭露）", None, v["interest_coverage_min"], "pass")

    debt = cur.get("debt")
    net_debt = (debt or 0) + (cur.get("leases") or 0) - (cur.get("cash_total") or 0) if debt is not None else None
    m["net_debt_incl_leases"] = net_debt

    if is_reit or is_infra:
        cap = v["reit_net_debt_to_ebitda_max"] if is_reit else v["infra_net_debt_to_ebitda_max"]
        label = "含租賃淨負債 ÷ EBITDA（倍）"
        if net_debt is None or ebitda is None:
            add("leverage", label, None, cap, "missing")
        elif net_debt <= 0:
            m["net_debt_to_ebitda"] = 0.0
            add("leverage", label, 0.0, cap, "pass")
        elif ebitda <= 0:
            add("leverage", label, None, cap, soft)
        else:
            m["net_debt_to_ebitda"] = net_debt / ebitda
            add("leverage", label, m["net_debt_to_ebitda"], cap, soft if m["net_debt_to_ebitda"] > cap else "pass")
        if is_reit:
            m["dividend_to_ffo"] = _div(cur.get("dividends"), ffo) if ffo and ffo > 0 else None
            if m["dividend_to_ffo"] is not None:
                add("reit_payout", "REIT：股息 ÷ FFO 粗估（淨利＋折舊攤銷）", m["dividend_to_ffo"], v["reit_payout_max"],
                    soft if m["dividend_to_ffo"] > v["reit_payout_max"] else "pass")
        elif team == "white" and cur.get("dividends") and fcf is not None:
            m["dividend_to_fcf"] = _div(cur["dividends"], fcf) if fcf > 0 else None
            bad = fcf <= 0 or m["dividend_to_fcf"] > v["dividend_to_fcf_max"]
            add("div_cover", "股息 ÷ 自由現金流", m["dividend_to_fcf"], v["dividend_to_fcf_max"], "veto" if bad else "pass")
    elif is_utility:
        m["payout_ratio"] = _div(cur.get("dividends"), cur.get("net_income"))
        m["ocf_to_debt"] = _div(cur.get("ocf"), debt)
        if m["payout_ratio"] is not None:
            add("u_payout", "公用事業：股息 ÷ 淨利", m["payout_ratio"], v["utility_payout_max"],
                soft if m["payout_ratio"] > v["utility_payout_max"] else "pass")
        if m["ocf_to_debt"] is not None:
            add("u_ocf_debt", "公用事業：營運現金流 ÷ 負債", m["ocf_to_debt"], v["utility_ocf_to_debt_min"],
                soft if m["ocf_to_debt"] < v["utility_ocf_to_debt_min"] else "pass")
    else:
        if net_debt is not None and fcf is not None:
            if net_debt <= 0:
                m["net_debt_to_fcf"] = 0.0
                add("leverage", "含租賃淨負債 ÷ 自由現金流（淨現金）", 0.0, v["net_debt_to_fcf_max_years"], "pass")
            elif fcf <= 0:
                add("leverage", "含租賃淨負債 ÷ 自由現金流（自由現金流為負）", None, v["net_debt_to_fcf_max_years"], soft)
            else:
                m["net_debt_to_fcf"] = net_debt / fcf
                add("leverage", "含租賃淨負債 ÷ 自由現金流（年）", m["net_debt_to_fcf"], v["net_debt_to_fcf_max_years"],
                    soft if m["net_debt_to_fcf"] > v["net_debt_to_fcf_max_years"] else "pass")
        if team == "white" and cur.get("dividends") and fcf is not None:
            m["dividend_to_fcf"] = _div(cur["dividends"], fcf) if fcf > 0 else None
            bad = fcf <= 0 or m["dividend_to_fcf"] > v["dividend_to_fcf_max"]
            add("div_cover", "股息 ÷ 自由現金流", m["dividend_to_fcf"], v["dividend_to_fcf_max"], "veto" if bad else "pass")

    if team == "white":
        y, chg = market.get("dividend_yield"), market.get("change_52w")
        if y is not None and chg is not None and y >= v["yield_min_for_price_drop_check"]:
            add("yield_trap", "高殖利率是否來自股價下跌（52 週漲跌）", chg, v["price_drop_1y"],
                "veto" if chg < v["price_drop_1y"] else "pass")

    if sector not in v["altman_exempt_sectors"] and not market.get("currency_mismatch"):
        ta, tl = cur.get("total_assets"), cur.get("total_liabilities")
        need = [ta, tl, cur.get("current_assets"), cur.get("current_liabilities"),
                cur.get("retained_earnings"), cur.get("op_income"), cur.get("revenue"), market.get("market_cap")]
        if all(x is not None for x in need) and ta and tl:
            z = (1.2 * (cur["current_assets"] - cur["current_liabilities"]) / ta + 1.4 * cur["retained_earnings"] / ta
                 + 3.3 * cur["op_income"] / ta + 0.6 * market["market_cap"] / tl + 1.0 * cur["revenue"] / ta)
            m["altman_z"] = z
            add("altman", "Altman Z（大量回購的公司可能誤判）", z, v["altman_z_distress"],
                soft if z < v["altman_z_distress"] else "pass")

    results = [c["result"] for c in checks]
    flags = results.count("flag")
    if "veto" in results or (team == "white" and flags >= v["white_flags_to_veto"]):
        verdict = "veto"
    elif fcf is None or "missing" in results:
        verdict = "insufficient"
    elif flags or "pending" in results:
        verdict = "flag"
    else:
        verdict = "pass"
    manual = ["潛在稀釋（權證、可轉債、ATM）", "近 2 年到期負債", "最大客戶占比"]
    if is_reit:
        manual.append("公司公布的每股 AFFO 與 AFFO 配息率")
    return {"metrics": m, "checks": checks, "verdict": verdict, "manual_todo": manual}


def score(fin: dict, m: dict, entry: dict, rules: dict) -> dict:
    """第 4 步：黃金三角、4 個指標，加上 20 項分數合成 100 分。"""
    sc = rules["score"]
    cur = fin["cur"]

    def band(val, good, mid, higher_is_better=True):
        if val is None:
            return None
        if higher_is_better:
            return 1.0 if val > good else (0.5 if val >= mid else 0.0)
        return 1.0 if val < good else (0.5 if val <= mid else 0.0)

    fcf = m.get("fcf")
    net_debt = m.get("net_debt_incl_leases")
    lev = None
    if net_debt is not None and fcf is not None:
        lev = 0.0 if net_debt <= 0 else (net_debt / fcf if fcf > 0 else float("inf"))
    lev_pts = band(lev, sc["net_debt_to_fcf_good"], sc["net_debt_to_fcf_mid"], False)

    equity, ni = cur.get("equity"), cur.get("net_income")
    tri = {}
    if equity is not None and equity > 0:
        roe = _div(ni, equity)
        de = _div(cur.get("debt"), equity)
        distorted = roe is not None and roe > sc["roe_distorted_above"]
        tri["roe"] = {"value": roe, "points": band(roe, sc["roe_good"], sc["roe_mid"]),
                      "note": "股東權益偏小，ROE 可能失真" if distorted else None}
        tri["debt_to_equity"] = ({"value": lev, "points": lev_pts, "note": "權益失真，改用淨負債 ÷ 自由現金流"} if distorted
                                 else {"value": de, "points": band(de, sc["de_good"], sc["de_mid"], False), "note": None})
    else:
        tri["roe"] = {"value": None, "points": (1.0 if (ni or 0) > 0 else 0.0) if equity is not None else None,
                      "note": "股東權益為負（多半是回購造成），以是否獲利給分" if equity is not None else None}
        tri["debt_to_equity"] = {"value": lev, "points": lev_pts if equity is not None else None,
                                 "note": "改用淨負債 ÷ 自由現金流" if equity is not None else None}
    ic = m.get("interest_coverage") if entry["sector"] not in rules["veto"]["reit_sectors"] else _div(
        cur.get("op_income"), cur.get("interest_expense"))
    if ic is None and not cur.get("interest_expense") and (cur.get("op_income") or 0) > 0:
        tri["interest_coverage"] = {"value": None, "points": 1.0, "note": "沒有利息費用"}
    else:
        tri["interest_coverage"] = {"value": ic, "points": band(ic, sc["coverage_good"], sc["coverage_mid"]), "note": None}

    ind = {}
    capex = cur.get("capex")
    fc = None if fcf is None or capex is None else (float("inf") if capex == 0 and fcf > 0 else _div(fcf, capex))
    ind["fcf_to_capex"] = {"value": None if fc in (None, float("inf")) else fc,
                           "points": band(fc, sc["fcf_to_capex_good"], sc["fcf_to_capex_mid"]), "note": None}
    ind["net_debt_to_fcf"] = {"value": None if lev in (None, float("inf")) else lev, "points": lev_pts,
                              "note": "自由現金流為負" if lev == float("inf") else None}
    top = entry.get("top_customer_pct")
    ind["top_customer"] = {"value": top, "points": band(top, sc["top_customer_good"], sc["top_customer_mid"], False),
                           "note": None if top is not None else "要人工填 top_customer_pct"}
    trend = fin.get("trend") or []
    chg = trend[-1]["capex_to_revenue"] - trend[0]["capex_to_revenue"] if len(trend) >= 3 else None
    ind["capex_trend"] = {"value": chg, "note": None if chg is not None else "年度資料不足三年",
                          "points": None if chg is None else (1.0 if chg <= 0 else (0.5 if chg < sc["capex_trend_mid"] else 0.0))}

    def total(group):
        pts = [x["points"] for x in group.values() if x["points"] is not None]
        return (sum(pts), len(pts))

    (tp, tn), (ip, inn) = total(tri), total(ind)
    w = sc["weights"][entry["team"]]
    s20 = entry.get("proxy_score20")
    parts_ok = tn == 3 and inn >= 3
    partial = (tp / tn * w["triangle"] if tn else 0) + (ip / inn * w["indicators"] if inn else 0)
    out = {"triangle": tri, "indicators": ind, "triangle_points": tp, "triangle_items": tn,
           "indicator_points": ip, "indicator_items": inn, "score20": s20,
           "partial": partial if (tn and inn) else None, "partial_max": w["triangle"] + w["indicators"],
           "total": None, "passed": None}
    if s20 is not None and parts_ok:
        out["total"] = s20 / 20 * w["s20"] + partial
        out["passed"] = out["total"] >= rules["pass_score"]
    return out


def crosscheck(sec: dict, yahoo: dict | None) -> list[dict]:
    """SEC 和 Yahoo 的關鍵數字差距超過 5% 就列出來。"""
    out = []
    if not yahoo:
        return out
    for key in ("ocf", "capex", "shares", "revenue"):
        a, b = sec["cur"].get(key), yahoo["cur"].get(key)
        if a and b and abs(a - b) / abs(a) > 0.05:
            out.append({"field": key, "sec": a, "yahoo": b, "diff": (b - a) / abs(a)})
    return out


def main() -> int:
    import requests
    import yfinance as yf

    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    rules = universe["rules"]
    foreign = set(rules["veto"]["foreign_filers"])
    contact = os.environ.get("SEC_CONTACT", "").strip()
    session = requests.Session()
    session.headers["User-Agent"] = f"sh-marketlab-screener {contact}"

    cik_map: dict[str, str] = {}
    if contact:
        try:
            r = session.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
            r.raise_for_status()
            cik_map = {row["ticker"]: f"{row['cik_str']:010d}" for row in r.json().values()}
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 讀不到 SEC 代碼對照表，全部改用 Yahoo：{e}")
    else:
        print("[警告] 沒有設定 SEC_CONTACT，全部改用 Yahoo。")

    result: dict = {}
    for entry in universe["tickers"]:
        tkr = entry["ticker"]
        if entry["stage"] == "disabled":
            continue
        rec: dict = {"source": None, "errors": []}
        try:
            yt = yf.Ticker(tkr)
            info = yt.info or {}
        except Exception as e:  # noqa: BLE001
            yt, info = None, {}
            rec["errors"].append(f"yahoo info: {e}")
        market = {
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "market_cap": info.get("marketCap"),
            "beta": info.get("beta"),
            "dividend_yield": (info.get("trailingAnnualDividendYield")),
            "change_52w": info.get("52WeekChange"),
            "forward_pe": info.get("forwardPE"),
            "inst_held": info.get("heldPercentInstitutions"),
            "short_float": info.get("shortPercentOfFloat"),
            "currency": info.get("currency"),
            "financial_currency": info.get("financialCurrency"),
        }
        market["currency_mismatch"] = False  # 下面確定報表來源後再判斷
        yahoo_fin = None
        for symbol in filter(None, [tkr, entry.get("yahoo_alt")]):
            try:
                yahoo_fin = extract_yahoo(yahoo_frames(yt if symbol == tkr else yf.Ticker(symbol)))
            except Exception as e:  # noqa: BLE001
                rec["errors"].append(f"yahoo statements {symbol}: {e}")
            if yahoo_fin:
                if symbol != tkr:
                    rec["yahoo_symbol"] = symbol
                break
        if yahoo_fin is None:
            rec["errors"].append("Yahoo 沒有回傳報表")

        fin = None
        if tkr not in foreign and tkr in cik_map:
            try:
                r = session.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_map[tkr]}.json", timeout=60)
                r.raise_for_status()
                fin = extract_sec(r.json())
                time.sleep(0.2)
            except Exception as e:  # noqa: BLE001
                rec["errors"].append(f"sec: {e}")
        if fin is None and yahoo_fin is None and tkr in cik_map:
            try:  # 外國公司 Yahoo 抓不到時，退回 SEC 的年報資料（20-F／40-F）
                r = session.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_map[tkr]}.json", timeout=60)
                r.raise_for_status()
                fin = extract_sec(r.json())
            except Exception as e:  # noqa: BLE001
                rec["errors"].append(f"sec fallback: {e}")
        market["currency_mismatch"] = bool(market["currency"] and market["financial_currency"]
                                           and market["currency"] != market["financial_currency"])
        if fin and yahoo_fin and _days(fin["fiscal_end"], yahoo_fin["fiscal_end"]) > 45:
            rec["note"] = f"SEC 算得出的近四季只到 {fin['fiscal_end']}，改用較新的 Yahoo 資料"
            rec["sec_latest_fact_end"] = fin.get("latest_fact_end")
            yahoo_fin.setdefault("trend", [])
            if not yahoo_fin["trend"]:
                yahoo_fin["trend"] = fin.get("trend", [])
            fin = None
        if fin:
            rec["source"] = "SEC"
            rec["crosscheck"] = crosscheck(fin, yahoo_fin)
            fin, rec["yahoo_filled"] = merge_sources(fin, yahoo_fin)
        elif yahoo_fin:
            fin, rec["source"] = yahoo_fin, "Yahoo"
        rec["market"] = market
        if entry["sector"] in rules["veto"].get("bank_sectors", []):
            rec.update(evaluate({"cur": {}, "prev": {}}, market, entry, rules))
        elif fin and fin["cur"].get("ocf") is not None:
            rec["fiscal_end"], rec["basis"] = fin["fiscal_end"], fin.get("basis")
            rec["trend"] = fin.get("trend")
            rec["raw"] = {k: fin["cur"].get(k) for k in
                          ("ocf", "capex", "revenue", "op_income", "net_income", "interest_expense", "sbc",
                           "dividends", "shares", "cash_total", "debt", "leases", "da", "equity")}
            try:
                rec.update(evaluate(fin, market, entry, rules))
                rec["score"] = score(fin, rec["metrics"], entry, rules)
            except Exception as e:  # noqa: BLE001
                rec["errors"].append(f"evaluate: {e}")
                rec["verdict"] = "insufficient"
        else:
            rec["verdict"] = "insufficient"
        result[tkr] = rec
        print(f"{tkr:6s} {rec.get('source') or '-':6s} {rec.get('basis') or '-':4s} {rec['verdict']}  {'; '.join(rec['errors'])}")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"generated": date.today().isoformat(), "basis": "近四季（抓不到季報時用最近年度）",
                               "tickers": result}, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in result.values() if r["verdict"] != "insufficient")
    print(f"完成：{ok}/{len(result)} 檔有足夠資料")
    return 0


if __name__ == "__main__":
    sys.exit(main())
