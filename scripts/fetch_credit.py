"""信用追蹤：每週抓一次觀察名單的信用風險代理指標，輸出 data/credit.json。

名單 = universe.json 裡 stage 不是 disabled 的標的 + data/credit_watch.json 的 extra。

個股 CDS 沒有免費來源，所以這裡用三種東西代替：
1. 違約距離（naive distance-to-default，Bharath & Shumway 2008 的簡化 Merton 模型）：
   用市值、股價波動和負債算出公司資產價值離「資不抵債」有幾個標準差。
   數字越小越危險，跟 CDS 高度相關，但不是 CDS。
2. 股價訊號：近 1 月報酬、52 週回檔、近 1 月 ÷ 近 1 年波動。
3. 手動 CDS：data/cds_manual.json，從新聞讀到的數字。

市場基準用 FRED 的 ICE BofA 公司債利差（投資級、BBB、高收益）。
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE = ROOT / "universe.json"
METRICS = ROOT / "data" / "metrics.json"
WATCH = ROOT / "data" / "credit_watch.json"
MANUAL = ROOT / "data" / "cds_manual.json"
HISTORY = ROOT / "data" / "credit_history.json"
OUT = ROOT / "data" / "credit.json"

FRED_SERIES = {
    "ig": ("BAMLC0A0CM", "投資級公司債利差"),
    "bbb": ("BAMLC0A4CBBB", "BBB 公司債利差"),
    "hy": ("BAMLH0A0HYM2", "高收益債利差"),
    "tbill": ("DTB3", "3 個月國庫券殖利率"),
}
HISTORY_KEEP = 110  # 約兩年的週資料


# ---------- 純計算（有測試） ----------

def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def naive_dd(equity: float, debt: float, sigma_e: float, r: float, t: float = 1.0) -> float | None:
    """簡化 Merton 違約距離。equity、debt 同幣別；sigma_e 是年化股價波動；r 是無風險利率（小數）。"""
    if not equity or not debt or equity <= 0 or debt <= 0 or not sigma_e or sigma_e <= 0:
        return None
    v = equity + debt
    sigma_d = 0.05 + 0.25 * sigma_e
    sigma_v = equity / v * sigma_e + debt / v * sigma_d
    return (math.log(v / debt) + (r - 0.5 * sigma_v ** 2) * t) / (sigma_v * math.sqrt(t))


def price_signals(closes: list[float]) -> dict:
    """closes：由舊到新的每日收盤價（已還原）。"""
    out = {"ret_1m": None, "drawdown_52w": None, "vol_1m": None, "vol_1y": None, "vol_ratio": None}
    c = [x for x in closes if x is not None and x > 0]
    if len(c) < 30:
        return out
    last = c[-1]
    out["ret_1m"] = last / c[-22] - 1
    out["drawdown_52w"] = last / max(c[-252:]) - 1
    logr = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]

    def vol(xs):
        if len(xs) < 2:
            return None
        m = sum(xs) / len(xs)
        return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) * math.sqrt(252)

    out["vol_1m"] = vol(logr[-21:])
    out["vol_1y"] = vol(logr[-252:])
    if out["vol_1m"] and out["vol_1y"]:
        out["vol_ratio"] = out["vol_1m"] / out["vol_1y"]
    return out


def value_ago(history: list[dict], key: str, ticker: str | None, today: str, days: int = 28):
    """從歷史快照找最接近 days 天前（至少 21 天前）的值。"""
    target = date.fromisoformat(today) - timedelta(days=days)
    best, gap = None, None
    for snap in history:
        d = date.fromisoformat(snap["date"])
        if (date.fromisoformat(today) - d).days < 21:
            continue
        val = snap.get(key, {}).get(ticker) if ticker else snap.get(key)
        if val is None:
            continue
        g = abs((d - target).days)
        if gap is None or g < gap:
            best, gap = val, g
    return best


def cds_view(entries: list[dict] | None) -> dict | None:
    if not entries:
        return None
    es = sorted(entries, key=lambda e: e["date"])
    last = es[-1]
    view = dict(last)
    if len(es) >= 2 and es[-2].get("bps"):
        prev = es[-2]
        view["prev_bps"], view["prev_date"] = prev["bps"], prev["date"]
        view["change_pct"] = last["bps"] / prev["bps"] - 1
    return view


def classify(row: dict, rules: dict, asset_heavy: bool) -> tuple[str, list[str], bool]:
    """回傳（狀態, 理由, 是否股債背離）。狀態：ok / watch / alert / na / insufficient。"""
    reasons: list[str] = []
    level = 0
    dd, d4 = row.get("dd"), row.get("dd_change_4w")
    p = row.get("price") or {}
    cds = row.get("cds")
    if row.get("not_applicable"):
        if not cds:
            return "na", [row["not_applicable"]], False
        reasons.append(row["not_applicable"])
    elif dd is None and not cds:
        return "insufficient", row.get("errors") or ["缺資料"], False

    th = rules["dd_alert_asset_heavy"] if asset_heavy else rules["dd_alert"]
    if dd is not None and dd < th:
        level = max(level, 2)
        reasons.append(f"違約距離 {dd:.1f} 低於 {th}")
    if d4 is not None and d4 <= -rules["dd_drop_4w"]:
        level = max(level, 1)
        reasons.append(f"違約距離 4 週內下降 {abs(d4):.1f}")
    vr, dw = p.get("vol_ratio"), p.get("drawdown_52w")
    if vr is not None and dw is not None and vr >= rules["vol_ratio_flag"] and dw <= rules["drawdown_flag"]:
        level = max(level, 1)
        reasons.append(f"波動放大 {vr:.1f} 倍，且距 52 週高點 {dw * 100:.0f}%")
    if cds:
        if cds.get("bps") is not None and cds["bps"] >= rules["cds_alert_bps"]:
            level = max(level, 2)
            reasons.append(f"CDS {cds['bps']:.0f} bps 高於 {rules['cds_alert_bps']}")
        if cds.get("change_pct") is not None and cds["change_pct"] >= rules["cds_rise_flag"]:
            level = max(level, 1)
            reasons.append(f"CDS 比上一筆上升 {cds['change_pct'] * 100:.0f}%")

    divergence = False
    r1 = p.get("ret_1m")
    if r1 is not None and r1 > 0:
        dd_worse = d4 is not None and d4 <= -rules["divergence_dd_drop"]
        cds_worse = bool(cds and cds.get("change_pct") is not None and cds["change_pct"] > 0)
        if dd_worse or cds_worse:
            divergence = True
            level = max(level, 1)
            reasons.append("股債背離：股價近 1 月上漲，信用指標卻變差")
    return ["ok", "watch", "alert"][level], reasons, divergence


# ---------- 抓資料 ----------

def fred_series(session, sid: str) -> list[tuple[str, float]]:
    """有 FRED_API_KEY 時走官方 API（GitHub 主機比較穩），沒有時退回 fredgraph.csv，各重試 3 次。"""
    import time
    import pandas as pd
    key = os.environ.get("FRED_API_KEY")
    start = (date.today() - timedelta(days=400)).isoformat()
    last_err = None
    for attempt in range(3):
        try:
            if key:
                r = session.get("https://api.stlouisfed.org/fred/series/observations",
                                params={"series_id": sid, "api_key": key, "file_type": "json",
                                        "observation_start": start}, timeout=45)
                r.raise_for_status()
                out = []
                for o in r.json()["observations"]:
                    try:
                        out.append((o["date"], float(o["value"])))
                    except ValueError:
                        pass  # FRED 用 "." 表示缺值
                return out
            r = session.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                            params={"id": sid, "cosd": start}, timeout=45)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = ["date", "value"]
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            df = df.dropna()
            return [(str(d)[:10], float(v)) for d, v in zip(df["date"], df["value"])]
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{'API' if key else 'fredgraph'} 重試 3 次失敗：{last_err}")


def fred_block(series: list[tuple[str, float]]) -> dict:
    if not series:
        return {}
    d_last, v_last = series[-1]
    target = date.fromisoformat(d_last) - timedelta(days=28)
    prev = None
    for d, v in series:
        if date.fromisoformat(d) <= target:
            prev = (d, v)
    out = {"date": d_last, "value": v_last}
    if prev:
        out["prev_date"], out["prev_value"] = prev
        out["change_4w"] = v_last - prev[1]
    out["spark"] = [v for _, v in series[-130:]]
    return out


def main() -> int:
    import requests
    import yfinance as yf

    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    metrics = json.loads(METRICS.read_text(encoding="utf-8")).get("tickers", {}) if METRICS.exists() else {}
    watch = json.loads(WATCH.read_text(encoding="utf-8"))
    manual = json.loads(MANUAL.read_text(encoding="utf-8")).get("entries", {}) if MANUAL.exists() else {}
    history = json.loads(HISTORY.read_text(encoding="utf-8")).get("snapshots", []) if HISTORY.exists() else []
    rules = watch["rules"]
    vrules = universe["rules"]["veto"]
    heavy = set(vrules.get("utility_sectors", []) + vrules.get("reit_sectors", []) + vrules.get("infra_sectors", []))
    banks = set(vrules.get("bank_sectors", []))
    foreign = set(vrules.get("foreign_filers", []))
    sector_name = {s["id"]: s["name"] for s in universe["sectors"]}

    names: list[dict] = []
    seen = set()
    for t in universe["tickers"]:
        if t["stage"] == "disabled":
            continue
        names.append({"ticker": t["ticker"], "name": t["name"], "list": "screener", "team": t["team"],
                      "stage": t["stage"], "sector": t["sector"], "sector_name": sector_name.get(t["sector"], t["sector"])})
        seen.add(t["ticker"])
    for e in watch.get("extra", []):
        if e["ticker"] not in seen:
            names.append({"ticker": e["ticker"], "name": e.get("name", e["ticker"]), "list": "extra",
                          "why": e.get("why"), "team": None, "stage": None, "sector": None, "sector_name": None})
            seen.add(e["ticker"])

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (sh-marketlab-screener credit tracker)"
    market, fred_err = {}, []
    prev_market = {}
    if OUT.exists():
        try:
            prev_market = json.loads(OUT.read_text(encoding="utf-8")).get("market", {})
        except Exception:  # noqa: BLE001
            pass
    for key, (sid, label) in FRED_SERIES.items():
        try:
            market[key] = {"id": sid, "label": label, **fred_block(fred_series(session, sid))}
        except Exception as e:  # noqa: BLE001
            fred_err.append(f"{sid}: {e}")
            if key in prev_market:  # 抓不到時沿用上一次的數字，頁面標示為舊資料
                market[key] = {**prev_market[key], "stale": True}
    r = (market.get("tbill", {}).get("value") or 4.0) / 100

    symbols = [n["ticker"] for n in names]
    prices = yf.download(symbols, period="400d", auto_adjust=True, progress=False, group_by="ticker", threads=True)
    price_date = None
    fx_cache: dict[str, float | None] = {}

    def fx(src: str, dst: str):
        key = f"{src}{dst}"
        if key not in fx_cache:
            try:
                h = yf.Ticker(f"{key}=X").history(period="10d")["Close"].dropna()
                fx_cache[key] = float(h.iloc[-1]) if len(h) else None
            except Exception:  # noqa: BLE001
                fx_cache[key] = None
        return fx_cache[key]

    today = date.today().isoformat()
    rows = []
    for n in names:
        tkr = n["ticker"]
        row = {**n, "errors": []}
        try:
            closes = prices[tkr]["Close"].dropna() if len(symbols) > 1 else prices["Close"].dropna()
        except Exception:  # noqa: BLE001
            closes = None
        if closes is None or len(closes) < 30:
            row["errors"].append("抓不到股價")
            row["price"] = {}
        else:
            row["price"] = price_signals([float(x) for x in closes])
            row["price"]["last"] = float(closes.iloc[-1])
            d = str(closes.index[-1])[:10]
            row["price"]["date"] = d
            price_date = max(price_date or d, d)

        if n["sector"] in banks:
            row["not_applicable"] = "銀行的負債以存款為主，違約距離不適用"
        elif row["price"].get("last"):
            m = metrics.get(tkr) or {}
            raw, mk = m.get("raw") or {}, m.get("market") or {}
            shares = raw.get("shares")
            debt = (raw.get("debt") or 0) + (raw.get("leases") or 0) if raw.get("debt") is not None else None
            cur, fin_cur = mk.get("currency"), mk.get("financial_currency")
            # 外國公司（ADR 一股可能等於多股普通股）改用 Yahoo 的市值，避免股數和股價對不上
            adr = tkr in foreign or bool(mk.get("currency_mismatch"))
            equity = row["price"]["last"] * shares if shares and not adr else None
            row["debt_source"] = "metrics.json" if debt is not None else "Yahoo"
            if equity is None or debt is None:
                try:
                    info = yf.Ticker(tkr).info or {}
                    equity = equity or info.get("marketCap")
                    if debt is None:
                        debt = info.get("totalDebt")
                    cur, fin_cur = cur or info.get("currency"), fin_cur or info.get("financialCurrency")
                except Exception as e:  # noqa: BLE001
                    row["errors"].append(f"yahoo info: {e}")
            if debt and cur and fin_cur and cur != fin_cur:
                rate = fx(fin_cur, cur)
                if rate:
                    debt = debt * rate
                    row["fx_note"] = f"負債由 {fin_cur} 換成 {cur}"
                else:
                    debt = None
                    row["errors"].append(f"抓不到 {fin_cur}/{cur} 匯率")
            row["equity"], row["debt"] = equity, debt
            if debt == 0:
                row["dd"] = None
                row["no_debt"] = True
            else:
                row["dd"] = naive_dd(equity, debt, row["price"].get("vol_1y"), r)
            if row["dd"] is not None:
                row["pd_1y"] = norm_cdf(-row["dd"])
                prev = value_ago(history, "dd", tkr, today)
                if prev is not None:
                    row["dd_change_4w"] = row["dd"] - prev
            elif not row.get("no_debt") and not row["errors"]:
                row["errors"].append("缺市值或負債")
        if row.get("no_debt"):
            row["not_applicable"] = "沒有負債，違約距離沒有意義"

        row["cds"] = cds_view(manual.get(tkr))
        row["dd_spark"] = [s["dd"][tkr] for s in history[-25:] if s.get("dd", {}).get(tkr) is not None]
        if row.get("dd") is not None:
            row["dd_spark"].append(row["dd"])
        row["status"], row["reasons"], row["divergence"] = classify(row, rules, n["sector"] in heavy)
        rows.append(row)
        print(f"{tkr:6s} {row['status']:12s} dd={row.get('dd')}  {'; '.join(row['reasons'])}")

    snap = {"date": today, "dd": {r_["ticker"]: round(r_["dd"], 3) for r_ in rows if r_.get("dd") is not None},
            "oas": {k: market[k]["value"] for k in ("ig", "bbb", "hy") if k in market and not market[k].get("stale")}}
    history = [s for s in history if s["date"] != today] + [snap]
    HISTORY.write_text(json.dumps({"snapshots": history[-HISTORY_KEEP:]}, ensure_ascii=False, indent=1), encoding="utf-8")

    OUT.write_text(json.dumps({
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "price_date": price_date,
        "risk_free": r,
        "market": market,
        "errors": fred_err,
        "rules": rules,
        "tickers": rows,
    }, ensure_ascii=False, indent=1, default=lambda o: None), encoding="utf-8")
    counts = {s: sum(1 for x in rows if x["status"] == s) for s in ("alert", "watch", "ok", "na", "insufficient")}
    print("完成", counts, fred_err or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
