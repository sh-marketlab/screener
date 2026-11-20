"""用假資料測試信用追蹤的計算和分類邏輯。不需要網路。"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_credit import classify, cds_view, naive_dd, norm_cdf, price_signals, value_ago  # noqa: E402

RULES = {"dd_alert": 2.0, "dd_alert_asset_heavy": 1.0, "dd_drop_4w": 1.0, "vol_ratio_flag": 1.5,
         "drawdown_flag": -0.2, "cds_alert_bps": 200, "cds_rise_flag": 0.3, "divergence_dd_drop": 0.5}


def close(a, b, tol=1e-6):
    assert abs(a - b) < tol, (a, b)


def test_dd():
    # 手算：E=100, D=100, σE=0.4 → σD=0.15, σV=0.275, DD=(ln2 + (0.04-0.0378125))/0.275
    expected = (math.log(2) + (0.04 - 0.5 * 0.275 ** 2)) / 0.275
    close(naive_dd(100, 100, 0.4, 0.04), expected)
    assert naive_dd(100, 1, 0.3, 0.04) > naive_dd(100, 50, 0.3, 0.04)  # 負債越少越安全
    assert naive_dd(100, 50, 0.6, 0.04) < naive_dd(100, 50, 0.3, 0.04)  # 波動越大越危險
    assert naive_dd(100, 0, 0.3, 0.04) is None
    assert naive_dd(None, 10, 0.3, 0.04) is None
    close(norm_cdf(0), 0.5)


def test_price_signals():
    flat = [100.0] * 300
    p = price_signals(flat)
    close(p["ret_1m"], 0)
    close(p["drawdown_52w"], 0)
    up = [100 * 1.001 ** i for i in range(300)]
    p = price_signals(up)
    assert p["ret_1m"] > 0 and abs(p["drawdown_52w"]) < 1e-9
    assert price_signals([1.0] * 10)["ret_1m"] is None


def test_value_ago():
    hist = [{"date": "2026-10-17", "dd": {"X": 5.0}}, {"date": "2026-10-24", "dd": {"X": 4.5}},
            {"date": "2026-11-14", "dd": {"X": 3.0}}]
    assert value_ago(hist, "dd", "X", "2026-11-21") == 4.5   # 28 天前 = 10-24
    assert value_ago(hist, "dd", "Y", "2026-11-21") is None
    assert value_ago(hist[-1:], "dd", "X", "2026-11-21") is None  # 太近的不算


def test_cds_view():
    v = cds_view([{"date": "2026-11-06", "bps": 139}, {"date": "2026-10-06", "bps": 79}])
    assert v["bps"] == 139 and v["prev_bps"] == 79
    close(v["change_pct"], 139 / 79 - 1)
    assert cds_view(None) is None


def test_classify():
    ok = {"dd": 8, "dd_change_4w": 0.2, "price": {"ret_1m": -0.02, "vol_ratio": 1.0, "drawdown_52w": -0.05}}
    assert classify(ok, RULES, False)[0] == "ok"
    low = {**ok, "dd": 1.5}
    assert classify(low, RULES, False)[0] == "alert"
    assert classify(low, RULES, True)[0] == "ok"  # 高槓桿板塊門檻較低
    drop = {**ok, "dd_change_4w": -1.2}
    assert classify(drop, RULES, False)[0] == "watch"
    div = {**ok, "dd_change_4w": -0.6, "price": {**ok["price"], "ret_1m": 0.08}}
    s, reasons, d = classify(div, RULES, False)
    assert s == "watch" and d
    cds = {**ok, "cds": {"bps": 298, "change_pct": 0.1}}
    assert classify(cds, RULES, False)[0] == "alert"
    bank = {"not_applicable": "銀行", "price": {}}
    assert classify(bank, RULES, False)[0] == "na"
    assert classify({"price": {}, "errors": ["抓不到股價"]}, RULES, False)[0] == "insufficient"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
