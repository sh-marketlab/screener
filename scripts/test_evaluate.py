"""離線測試：用假資料檢查 SEC 解析和否決欄邏輯。執行：python scripts/test_evaluate.py"""
import json
from pathlib import Path

from fetch_metrics import evaluate, extract_sec

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


def test_reit_pending():
    assert verdict(base(), sector="dc_reit")["verdict"] == "flag"


def test_yield_trap():
    assert verdict(base(), market={**MARKET, "change_52w": -0.4})["verdict"] == "veto"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
