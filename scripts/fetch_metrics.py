"""抓取第 3 步否決欄需要的數字，輸出 data/metrics.json。

資料來源：
- 美國公司：SEC XBRL companyfacts（最近一份 10-K 的年度數字），Yahoo Finance 做交叉檢查。
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
}


def _days(a: str, b: str) -> int:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


def _series(facts: dict, tag: str, duration: bool) -> dict[str, float]:
    """回傳某個科目的 {期末日: 值}。期間型只取 10-K 裡約一年長度的期間。"""
    out: dict[str, float] = {}
    for unit_rows in (facts.get(tag) or {}).get("units", {}).values():
        for r in unit_rows:
            if r.get("form") not in ("10-K", "10-K/A") or (("start" in r) != duration):
                continue
            if not duration or 350 <= _days(r["start"], r["end"]) <= 380:
                out[r["end"]] = r["val"]
    return out


def _annual_duration(facts: dict, tags: list[str]) -> dict[str, float]:
    """合併備選科目：每個期末日取第一個有值的科目（公司換科目時不會漏）。"""
    out: dict[str, float] = {}
    for tag in tags:
        for end, val in _series(facts, tag, True).items():
            out.setdefault(end, val)
    return out


def _instant(facts: dict, tags: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for tag in tags:
        for end, val in _series(facts, tag, False).items():
            out.setdefault(end, val)
    return out


def extract_sec(companyfacts: dict) -> dict | None:
    """把 SEC companyfacts 轉成統一格式 {fiscal_end, cur:{}, prev:{}}。"""
    facts = companyfacts.get("facts", {}).get("us-gaap", {})
    ocf = _annual_duration(facts, DURATION["ocf"])
    if not ocf:
        return None
    ends = sorted(ocf)
    end = ends[-1]
    prev_end = next((e for e in reversed(ends[:-1]) if 350 <= _days(e, end) <= 380), None)
    cur, prev = {}, {}
    for key, tags in DURATION.items():
        series = _annual_duration(facts, tags)
        cur[key] = series.get(end)
        prev[key] = series.get(prev_end) if prev_end else None
    for key, tags in INSTANT.items():
        cur[key] = _instant(facts, tags).get(end)
    # 資本支出加上資本化軟體
    if cur.get("capex") is not None:
        cur["capex"] += cur.get("capex_software") or 0
    if prev.get("capex") is not None:
        prev["capex"] += prev.get("capex_software") or 0
    parts = [cur.get("debt_noncurrent"), cur.get("debt_current"), cur.get("debt_short")]
    if any(p is not None for p in parts):
        cur["debt"] = sum(p or 0 for p in parts)
    else:
        cur["debt"] = cur.get("debt_total_alt")
    cur["leases"] = (cur.get("lease_operating") or 0) + (cur.get("lease_finance") or 0)
    cur["cash_total"] = (cur.get("cash") or 0) + (cur.get("st_investments") or 0) if cur.get("cash") is not None else None
    return {"fiscal_end": end, "cur": cur, "prev": prev}


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
    "total_assets": ("balance", ["Total Assets"]),
    "total_liabilities": ("balance", ["Total Liabilities Net Minority Interest"]),
    "current_assets": ("balance", ["Current Assets"]),
    "current_liabilities": ("balance", ["Current Liabilities"]),
    "retained_earnings": ("balance", ["Retained Earnings"]),
}


def extract_yahoo(tk) -> dict | None:
    """把 yfinance 的年度報表轉成統一格式。Yahoo 的 Total Debt 已含租賃負債。"""
    import pandas as pd

    frames = {"cashflow": tk.cashflow, "income": tk.income_stmt, "balance": tk.balance_sheet}
    cf = frames["cashflow"]
    if cf is None or cf.empty:
        return None
    cols = list(cf.columns)
    cur, prev = {}, {}
    for key, (frame, names) in YF_ROWS.items():
        df = frames[frame]
        for name in names:
            if df is not None and name in df.index:
                row = df.loc[name]
                for target, idx in ((cur, 0), (prev, 1)):
                    if len(row) > idx and pd.notna(row.iloc[idx]):
                        target[key] = float(row.iloc[idx])
                break
    for d in (cur, prev):
        for k in ("capex", "dividends", "interest_expense"):
            if d.get(k) is not None:
                d[k] = abs(d[k])
    cur["leases"] = 0
    return {"fiscal_end": str(cols[0].date()), "cur": cur, "prev": prev}


def merge_sources(sec: dict, yahoo: dict | None) -> tuple[dict, list[str]]:
    """以 SEC 為主，補上 Yahoo。回傳合併後的資料和「哪些欄位用了 Yahoo」。

    - SEC 缺的欄位用 Yahoo 補（僅在兩邊會計年度結束日相同時）。
    - 現金和負債用 Yahoo 的合計數：各家在 XBRL 裡的負債科目差異太大，常只抓到一部分。
      Yahoo 的 Total Debt 已含租賃，所以租賃歸零避免重複。
    - 資本支出取兩邊較大者：XBRL 常漏掉資本化軟體等項目，取大者較保守。
    """
    used: list[str] = []
    if not yahoo or yahoo.get("fiscal_end", "")[:7] != sec["fiscal_end"][:7]:
        return sec, used
    for period in ("cur", "prev"):
        for key, val in yahoo[period].items():
            if sec[period].get(key) is None and val is not None:
                sec[period][key] = val
                used.append(f"{key}({period})")
    cur, ycur = sec["cur"], yahoo["cur"]
    for key in ("cash_total", "debt"):
        if ycur.get(key) is not None and cur.get(key) != ycur[key]:
            cur[key] = ycur[key]
            used.append(key)
            if key == "debt":
                cur["leases"] = 0
    for period in ("cur", "prev"):
        a, b = sec[period].get("capex"), yahoo[period].get("capex")
        if a is not None and b is not None and b > a * 1.05:
            sec[period]["capex"] = b
            used.append(f"capex({period})")
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

    fcf = cur["ocf"] - (cur.get("capex") or 0) if cur.get("ocf") is not None else None
    fcf_prev = prev["ocf"] - (prev.get("capex") or 0) if prev.get("ocf") is not None else None
    m["fcf"] = fcf

    def _sum(d, a, b):
        return d[a] + d[b] if d.get(a) is not None and d.get(b) is not None else None

    ebitda = _sum(cur, "op_income", "da")
    ffo, ffo_prev = _sum(cur, "net_income", "da"), _sum(prev, "net_income", "da")
    m["ebitda"] = ebitda
    m["share_growth"] = _div(cur.get("shares"), prev.get("shares"))
    if m["share_growth"] is not None:
        m["share_growth"] -= 1

    # 稀釋：股數增加超過門檻才檢查
    sg = m["share_growth"]
    if sg is None:
        add("dilution", "已發生的稀釋", None, None, "missing")
    elif sg <= v["share_growth_check"]:
        add("dilution", "已發生的稀釋", sg, v["share_growth_check"], "pass")
    else:
        if sector in v["reit_sectors"] and ffo and ffo_prev and ffo > 0 and ffo_prev > 0:
            growth, basis = (ffo / cur["shares"]) / (ffo_prev / prev["shares"]) - 1, "每股 FFO 粗估"
        elif fcf and fcf_prev and fcf > 0 and fcf_prev > 0:
            growth, basis = (fcf / cur["shares"]) / (fcf_prev / prev["shares"]) - 1, "每股自由現金流"
        elif cur.get("revenue") and prev.get("revenue"):
            growth, basis = (cur["revenue"] / cur["shares"]) / (prev["revenue"] / prev["shares"]) - 1, "每股營收"
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

    capex_funded = sector in v["utility_sectors"] or sector in v["reit_sectors"]
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
        market["currency_mismatch"] = bool(market["currency"] and market["financial_currency"]
                                           and market["currency"] != market["financial_currency"])
        yahoo_fin = None
        if yt is not None:
            try:
                yahoo_fin = extract_yahoo(yt)
            except Exception as e:  # noqa: BLE001
                rec["errors"].append(f"yahoo statements: {e}")

        fin = None
        if tkr not in foreign and tkr in cik_map:
            try:
                r = session.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_map[tkr]}.json", timeout=60)
                r.raise_for_status()
                fin = extract_sec(r.json())
                time.sleep(0.2)
            except Exception as e:  # noqa: BLE001
                rec["errors"].append(f"sec: {e}")
        if fin:
            rec["source"] = "SEC"
            rec["crosscheck"] = crosscheck(fin, yahoo_fin)
            fin, rec["yahoo_filled"] = merge_sources(fin, yahoo_fin)
        elif yahoo_fin:
            fin, rec["source"] = yahoo_fin, "Yahoo"
        rec["market"] = market
        if fin and fin["cur"].get("ocf") is not None:
            rec["fiscal_end"] = fin["fiscal_end"]
            rec["raw"] = {k: fin["cur"].get(k) for k in
                          ("ocf", "capex", "revenue", "op_income", "net_income", "interest_expense", "sbc",
                           "dividends", "shares", "cash_total", "debt", "leases", "da")}
            try:
                rec.update(evaluate(fin, market, entry, rules))
            except Exception as e:  # noqa: BLE001
                rec["errors"].append(f"evaluate: {e}")
                rec["verdict"] = "insufficient"
        else:
            rec["verdict"] = "insufficient"
        result[tkr] = rec
        print(f"{tkr:6s} {rec.get('source') or '-':6s} {rec['verdict']}  {'; '.join(rec['errors'])}")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"generated": date.today().isoformat(), "basis": "最近一個完整會計年度",
                               "tickers": result}, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in result.values() if r["verdict"] != "insufficient")
    print(f"完成：{ok}/{len(result)} 檔有足夠資料")
    return 0


if __name__ == "__main__":
    sys.exit(main())
