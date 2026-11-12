"""離線測試：用假資料檢查 SEC 解析和否決欄邏輯。執行：python scripts/test_evaluate.py"""
import json
from pathlib import Path

from fetch_metrics import evaluate, extract_sec, merge_sources, score

RULES = json.loads((Path(__file__).resolve().parent.parent / "universe.json").read_text(encoding="utf-8"))["rules"]


def dur(vals):
    return {"units": {"USD": [{"form": "10-K", "start": f"{y}-01-01", "end": f"{y}-12-31", "val": v} for y, v in vals.items()]}}


def inst(vals):
    return {"units": {"USD": [{"form": "10-K", "end": f"{y}-12-31", "val": v} for y, v in vals.items()]}}


def test_extract_sec():
    facts = {"facts": {"us-gaap": {
        "NetCashProvidedByUsedInOperatingActivities": dur({2024: 100, 2025: 120}),
        "PaymentsToAcquirePropertyPlantAndEquipment": dur({2024: 10, 2025: 12}),
        "PaymentsToDevelopSoftware": dur({2025: 8}),
        "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
            {"form": "10-K", "start": "2024-01-01", "end": "2024-12-31", "val": 50},
            {"form": "10-K", "start": "2025-01-01", "end": "2025-12-31", "val": 49},
            {"form": "10-Q", "start": "2025-07-01", "end": "2025-09-30", "val": 48}]}},
        "LongTermDebtNoncurrent": inst({2025: 200}), "LongTermDebtCurrent": inst({2025: 20}),
        "OperatingLeaseLiability": inst({2025: 15}),
        "CashAndCashEquivalentsAtCarryingValue": inst({2025: 60}),
    }}}
    fin = extract_sec(facts)
    assert fin["fiscal_end"] == "2025-12-31"
    assert fin["cur"]["capex"] == 20 and fin["prev"]["capex"] == 10
    assert fin["cur"]["debt"] == 220 and fin["cur"]["leases"] == 15
    assert fin["cur"]["shares"] == 49 and fin["prev"]["shares"] == 50


def base(**over):
    cur = dict(ocf=120, capex=20, revenue=400, op_income=150, net_income=100, interest_expense=10, sbc=8,
               dividends=30, shares=49, cash_total=60, debt=220, leases=15, total_assets=900,
               total_liabilities=500, current_assets=200, current_liabilities=150, retained_earnings=300)
    cur.update(over)
    prev = dict(ocf=100, capex=10, revenue=360, shares=50)
    return {"fiscal_end": "2025-12-31", "cur": cur, "prev": prev}


MARKET = {"market_cap": 3000, "dividend_yield": 0.02, "change_52w": 0.1}


def verdict(fin, team="white", sector="payments", market=MARKET):
    return evaluate(fin, market, {"team": team, "sector": sector}, RULES)


def test_pass():
    assert verdict(base())["verdict"] == "pass"


def test_white_leverage_veto_blue_flag():
    fin = base(debt=600)
    assert verdict(fin)["verdict"] == "veto"
    assert verdict(fin, team="blue", sector="semi_design")["verdict"] == "flag"


def test_dilution_veto():
    fin = base(shares=60)
    assert verdict(fin, team="blue", sector="quantum")["verdict"] == "veto"


def test_burn_runway():
    fin = base(ocf=-50, cash_total=100, debt=0, dividends=None)
    out = verdict(fin, team="blue", sector="quantum")
    assert out["verdict"] == "veto" and out["metrics"]["cash_runway_years"] < 3
    fin = base(ocf=-50, cash_total=400, debt=0, dividends=None)
    assert verdict(fin, team="blue", sector="quantum")["verdict"] == "pass"


def test_utility_exception():
    fin = base(ocf=120, capex=300, debt=800, dividends=60)
    out = verdict(fin, sector="grid_utility")
    assert out["verdict"] == "pass", out["checks"]


def test_utility_dilution_uses_eps():
    fin = base(ocf=120, capex=300, debt=800, dividends=60, shares=52)        # 股數 +4%
    fin["prev"]["net_income"] = 90                                           # 每股盈餘 +6.8%
    assert verdict(fin, sector="grid_utility")["verdict"] == "pass"
    fin["prev"]["net_income"] = 99                                           # 每股盈餘 -2.9%
    assert verdict(fin, sector="grid_utility")["verdict"] == "veto"


def test_infra_no_runway_check():
    fin = base(ocf=120, capex=130, da=100, debt=600, dividends=None)
    assert verdict(fin, sector="midstream")["verdict"] == "pass"


def test_reit_rules():
    fin = base(da=100, debt=1200, dividends=150, shares=52)
    fin["prev"].update(net_income=90, da=90)
    out = verdict(fin, sector="dc_reit")
    assert out["verdict"] == "pass", out["checks"]          # 淨負債 1155 ÷ EBITDA 250 = 4.6
    assert abs(out["metrics"]["net_debt_to_ebitda"] - 4.62) < 0.01
    assert verdict(base(da=100, debt=1800), sector="dc_reit")["verdict"] == "veto"   # 7.0 倍
    assert verdict(base(), sector="dc_reit")["verdict"] == "insufficient"             # 沒有折舊資料


def test_infra_uses_ebitda():
    fin = base(da=100, debt=1000)                # 淨負債 955：÷ FCF 100 = 9.6 年，÷ EBITDA 250 = 3.8 倍
    assert verdict(fin)["verdict"] == "veto"
    assert verdict(fin, sector="midstream")["verdict"] == "pass"
    assert verdict(base(da=100, debt=1400), sector="midstream")["verdict"] == "veto"


def test_utility_coverage_two():
    fin = base(ocf=120, capex=300, debt=800, dividends=60, interest_expense=60)   # 2.5 倍
    assert verdict(fin, sector="grid_utility")["verdict"] == "pass"
    assert verdict(fin)["verdict"] == "veto"


def test_yield_trap():
    assert verdict(base(), market={**MARKET, "change_52w": -0.4})["verdict"] == "veto"


def test_tag_switch_and_merge():
    facts = {"facts": {"us-gaap": {
        "NetCashProvidedByUsedInOperatingActivities": dur({2024: 100, 2025: 120}),
        "Revenues": dur({2023: 300}),
        "SalesRevenueNet": dur({2024: 360, 2025: 400}),
        "LongTermDebtCurrent": inst({2025: 20}),
    }}}
    fin = extract_sec(facts)
    assert fin["cur"]["revenue"] == 400 and fin["prev"]["revenue"] == 360 and fin["basis"] == "年度"
    yahoo = {"fiscal_end": "2025-12-31", "cur": {"debt": 500, "cash_total": 80, "capex": 30, "shares": 49},
             "prev": {}, "growth_cur": {"shares": 49, "ocf": 120}, "growth_prev": {"shares": 50, "ocf": 100}}
    fin, used = merge_sources(fin, yahoo)
    assert fin["cur"]["debt"] == 500 and fin["cur"]["leases"] == 0 and fin["cur"]["capex"] == 30
    assert fin["growth_prev"]["shares"] == 50 and "debt" in used


def test_ttm():
    def q(tag_vals):
        rows = []
        for (start, end, form), val in tag_vals.items():
            rows.append({"form": form, "start": start, "end": end, "val": val, "filed": end})
        return {"units": {"USD": rows}}
    facts = {"facts": {"us-gaap": {
        "NetCashProvidedByUsedInOperatingActivities": q({
            ("2023-01-01", "2023-12-31", "10-K"): 80, ("2024-01-01", "2024-12-31", "10-K"): 100,
            ("2023-01-01", "2023-09-30", "10-Q"): 55, ("2024-01-01", "2024-09-30", "10-Q"): 70,
            ("2025-01-01", "2025-09-30", "10-Q"): 90, ("2025-07-01", "2025-09-30", "10-Q"): 33}),
        "WeightedAverageNumberOfDilutedSharesOutstanding": q({
            ("2025-01-01", "2025-09-30", "10-Q"): 49, ("2025-07-01", "2025-09-30", "10-Q"): 48,
            ("2024-07-01", "2024-09-30", "10-Q"): 50, ("2024-01-01", "2024-09-30", "10-Q"): 51}),
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            {"form": "10-Q", "end": "2025-09-30", "val": 66, "filed": "2025-10-30"},
            {"form": "10-K", "end": "2024-12-31", "val": 60, "filed": "2025-02-01"}]}},
    }}}
    fin = extract_sec(facts)
    assert fin["fiscal_end"] == "2025-09-30" and fin["basis"] == "近四季"
    assert fin["cur"]["ocf"] == 100 + 90 - 70 and fin["prev"]["ocf"] == 80 + 70 - 55
    assert fin["cur"]["shares"] == 48 and fin["prev"]["shares"] == 50 and fin["cur"]["cash"] == 66


def test_score():
    fin = base(equity=400, da=50)
    fin["trend"] = [{"end": "2023", "capex_to_revenue": 0.04}, {"end": "2024", "capex_to_revenue": 0.05},
                    {"end": "2025", "capex_to_revenue": 0.05}]
    entry = {"team": "blue", "sector": "semi_design", "proxy_score20": 18, "top_customer_pct": 0.3}
    m = evaluate(fin, MARKET, entry, RULES)["metrics"]
    out = score(fin, m, entry, RULES)
    # 三角：ROE 25%=1、負債權益比 0.55=1、保障 15 倍=1；指標：FCF÷資本支出 5=1、淨負債 1.75 年=0.5、大客戶 30%=0、趨勢 +1pp=0.5
    assert out["triangle_points"] == 3 and out["indicator_points"] == 2.0
    assert abs(out["total"] - (54 + 10 + 15)) < 1e-9 and out["passed"] is False
    entry2 = {**entry, "proxy_score20": None, "top_customer_pct": None}
    out2 = score(fin, m, entry2, RULES)
    assert out2["total"] is None and out2["indicator_items"] == 3 and out2["partial"] is not None


def test_bank_pending_and_prerevenue():
    assert verdict(base(), sector="banks")["verdict"] == "flag"
    fin = base(ocf=-50, cash_total=900, debt=0, dividends=None, revenue=None, shares=75)
    fin["prev"]["revenue"] = None
    assert verdict(fin, team="blue", sector="smr")["verdict"] == "veto"


def test_currency_mismatch_skips_altman():
    out = verdict(base(), market={**MARKET, "currency_mismatch": True})
    assert "altman_z" not in out["metrics"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
