#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Hunter 2.0 — 每日自動掃描
- 台股：TWSE OpenAPI 全市場日線（STOCK_DAY_ALL）+ 加權指數（FMTQIK）+ 產業別（t187ap03_L）
- 美股：stooq 日線（失敗時保留前次資料）
- 輸出 data.json 供 index.html 渲染
- 歷史資料以長格式 CSV 存於 history/，每次執行追加當日並保留 130 個交易日

執行：python scanner.py            （線上模式，GitHub Actions 用）
     python scanner.py --offline  （只用 history/ 既有資料重算，測試用）
"""
import csv, json, math, os, sys, time, io
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(ROOT, "history")
os.makedirs(HIST, exist_ok=True)

TPE = timezone(timedelta(hours=8))
TODAY = datetime.now(TPE).strftime("%Y-%m-%d")

# ── 設定 ─────────────────────────────────────────────
# 波段追蹤清單（可自行增減）
TW_WATCH = ["2330","2317","2454","3231","2382","3661","2308","3017","2603","6669"]
US_WATCH = ["NVDA","MSFT","AMD","AVGO","META","GOOGL","AMZN","TSLA","PLTR","MU"]
TW_NAMES = {"2330":"台積電","2317":"鴻海","2454":"聯發科","3231":"緯創","2382":"廣達",
            "3661":"世芯-KY","2308":"台達電","3017":"奇鋐","2603":"長榮","6669":"緯穎"}
# 美股財報日（需手動維護；過期自動忽略）
US_EARNINGS = {"META":"2026-07-29","AMZN":"2026-07-30","PLTR":"2026-08-03","NVDA":"2026-08-26"}

# 當沖預篩參數
DT_MIN_VALUE   = 3e8    # 當日成交金額下限（元）
DT_MIN_PRICE   = 10.0   # 股價下限
DT_MIN_SCORE   = 50     # 入榜門檻
DT_MAX_LIST    = 10     # 清單上限
MA_TIGHT_PCT   = 2.0    # 均線糾結：max(5,10,20MA)/min - 1 <= 2%
MA_WIDE_PCT    = 6.0    # 均線發散（扣分）

# ── 工具 ─────────────────────────────────────────────
def fetch_json(url, tries=3):
    import requests
    for i in range(tries):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent":"alpha-hunter/2.0"})
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"  fetch fail ({i+1}/{tries}) {url}: {e}")
        time.sleep(2*(i+1))
    return None

def fetch_text(url, tries=3):
    import requests
    for i in range(tries):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent":"alpha-hunter/2.0"})
            if r.status_code == 200 and len(r.text) > 50:
                return r.text
        except Exception as e:
            print(f"  fetch fail ({i+1}/{tries}) {url}: {e}")
        time.sleep(2*(i+1))
    return None

def num(s):
    if s is None: return None
    s = str(s).replace(",","").replace("+","").strip()
    if s in ("","--","-","X","0.00--"): return None
    try: return float(s)
    except ValueError: return None

def roc_date(s):  # 兼容 民國 115/07/17 與 西元 20260717 / 2026-07-17
    s = str(s).strip()
    if "/" in s:                                  # 民國格式 115/07/17
        p = s.split("/")
        if len(p) == 3:
            return f"{int(p[0])+1911}-{int(p[1]):02d}-{int(p[2]):02d}"
    d = s.replace("-", "")
    if len(d) == 8 and d.isdigit():               # 西元 20260717
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return s

def sma(vals, n):
    if len(vals) < n: return None
    return sum(vals[-n:]) / n

def wilder_rsi(closes, n=14):
    if len(closes) < n+1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag, al = sum(gains[:n])/n, sum(losses[:n])/n
    for i in range(n, len(gains)):
        ag = (ag*(n-1)+gains[i])/n; al = (al*(n-1)+losses[i])/n
    if al == 0: return 100.0
    return round(100 - 100/(1+ag/al), 1)

def wilder_atr(rows, n=14):  # rows: (o,h,l,c)
    if len(rows) < n+1: return None
    trs = []
    for i in range(1, len(rows)):
        h,l,pc = rows[i][1], rows[i][2], rows[i-1][3]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr = sum(trs[:n])/n
    for t in trs[n:]:
        atr = (atr*(n-1)+t)/n
    return round(atr, 2)

def pivots(rows, k=3):
    """回傳 (最近擺動支撐, 最近擺動壓力)：±k 根的局部極值"""
    lows, highs = [], []
    for i in range(k, len(rows)-k):
        win = rows[i-k:i+k+1]
        if rows[i][2] == min(w[2] for w in win): lows.append((i, rows[i][2]))
        if rows[i][1] == max(w[1] for w in win): highs.append((i, rows[i][1]))
    last_close = rows[-1][3]
    sup = next((p for _,p in reversed(lows) if p < last_close), None)
    res = next((p for _,p in reversed(highs) if p > last_close), None)
    return sup, res

# ── 歷史資料存取（長格式 CSV：date,code,name,open,high,low,close,volume,value）──
def hist_path(mkt): return os.path.join(HIST, f"{mkt}_history.csv")

def load_hist(mkt):
    p = hist_path(mkt)
    data = {}
    if os.path.exists(p):
        for r in csv.DictReader(open(p, encoding="utf-8")):
            data.setdefault(r["code"], []).append(r)
    for c in data:
        data[c].sort(key=lambda r: r["date"])
    return data

def save_hist(mkt, data, keep=130):
    rows = []
    for c, rs in data.items():
        rows.extend(rs[-keep:])
    rows.sort(key=lambda r: (r["date"], r["code"]))
    with open(hist_path(mkt), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date","code","name","open","high","low","close","volume","value"])
        w.writeheader(); w.writerows(rows)

# ── 台股抓取 ─────────────────────────────────────────
def fetch_tw(data):
    fmt = fetch_json("https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK")
    if not fmt:
        print("TWSE FMTQIK 失敗"); return None, None
    last = fmt[-1]
    tdate = roc_date(last["Date"] if "Date" in last else last["日期"])
    taiex = num(last.get("TAIEX") or last.get("發行量加權股價指數"))
    print(f"台股最新交易日 {tdate} TAIEX {taiex}")

    allday = fetch_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    if not allday:
        print("STOCK_DAY_ALL 失敗"); return tdate, taiex
    n_new = 0
    for r in allday:
        code = r.get("Code") or r.get("證券代號")
        o,h,l,c = num(r.get("OpeningPrice")), num(r.get("HighestPrice")), num(r.get("LowestPrice")), num(r.get("ClosingPrice"))
        v, val = num(r.get("TradeVolume")), num(r.get("TradeValue"))
        if not code or c is None or o is None: continue
        if len(code) != 4 or not code.isdigit(): continue   # 只留普通股
        rs = data.setdefault(code, [])
        if rs and rs[-1]["date"] == tdate: continue          # 已有當日
        rs.append({"date":tdate,"code":code,"name":r.get("Name") or r.get("證券名稱") or "",
                   "open":o,"high":h or c,"low":l or c,"close":c,"volume":v or 0,"value":val or 0})
        n_new += 1
    print(f"  新增 {n_new} 檔當日資料")

    # 指數歷史
    idx = data.setdefault("_TAIEX", [])
    for row in fmt:
        d = roc_date(row.get("Date") or row.get("日期"))
        x = num(row.get("TAIEX") or row.get("發行量加權股價指數"))
        if x and (not idx or idx[-1]["date"] < d):
            idx.append({"date":d,"code":"_TAIEX","name":"加權指數","open":x,"high":x,"low":x,"close":x,"volume":0,"value":0})
    return tdate, taiex

def fetch_sectors():
    p = os.path.join(HIST, "tw_sectors.json")
    j = fetch_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L")
    if j:
        m = {}
        for r in j:
            code = r.get("公司代號"); sec = r.get("產業別")
            if code and sec: m[str(code)] = str(sec)
        json.dump(m, open(p,"w",encoding="utf-8"), ensure_ascii=False)
        return m
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return {}

# ── 美股抓取（stooq）────────────────────────────────
def fetch_us(data):
    for t in US_WATCH:
        txt = fetch_text(f"https://stooq.com/q/d/l/?s={t.lower()}.us&i=d")
        if not txt or "Date" not in txt.splitlines()[0]:
            print(f"  {t}: stooq 失敗，沿用既有資料"); continue
        rows = list(csv.DictReader(io.StringIO(txt)))[-130:]
        data[t] = [{"date":r["Date"],"code":t,"name":t,"open":float(r["Open"]),"high":float(r["High"]),
                    "low":float(r["Low"]),"close":float(r["Close"]),"volume":float(r.get("Volume") or 0),"value":0}
                   for r in rows if r.get("Close")]
        print(f"  {t}: {len(data[t])} bars → {data[t][-1]['date']}")

# ── 指標計算 ─────────────────────────────────────────
def indicators(rs):
    closes = [float(r["close"]) for r in rs]
    ohlc = [(float(r["open"]),float(r["high"]),float(r["low"]),float(r["close"])) for r in rs]
    vols = [float(r["volume"]) for r in rs]
    n = len(rs)
    if n < 22: return None
    sup, res = pivots(ohlc)
    out = {
        "last_date": rs[-1]["date"], "close": closes[-1],
        "chg1d": round((closes[-1]/closes[-2]-1)*100, 2) if n >= 2 else None,
        "ma5": sma(closes,5), "ma10": sma(closes,10), "ma20": sma(closes,20),
        "ma50": sma(closes,50) or sma(closes, max(20, n-1)),
        "atr14": wilder_atr(ohlc), "rsi14": wilder_rsi(closes),
        "hi20": max(c[1] for c in ohlc[-20:]), "lo20": min(c[2] for c in ohlc[-20:]),
        "hi60": max(c[1] for c in ohlc[-min(60,n):]), "lo60": min(c[2] for c in ohlc[-min(60,n):]),
        "support": sup, "resistance": res,
        "vol_ratio": round((sum(vols[-1:])/1) / (sum(vols[-6:-1])/5), 2) if n>=6 and sum(vols[-6:-1])>0 else None,
        "avg_val_20d": round(sum(float(r["value"]) for r in rs[-20:])/20, 0),
    }
    c, m20, m50 = closes[-1], out["ma20"], out["ma50"]
    out["trend"] = "up" if (m20 and m50 and c>m20>m50) else ("down" if (m20 and m50 and c<m20<m50) else "mixed")
    for k in ("ma5","ma10","ma20","ma50"):
        if out[k]: out[k] = round(out[k], 2)
    return out

# ── 波段訊號（規則式）────────────────────────────────
def swing_setup(code, name, ind, cur, is_tw):
    """規則：支撐帶承接。支撐存在且現價在支撐上方 8% 內、非下降趨勢才產生訊號。"""
    if not ind or ind["trend"] == "down": return None
    sup = ind["support"] or ind["lo20"]
    res = ind["resistance"] or ind["hi20"]
    px, atr = ind["close"], ind["atr14"] or 0
    if not sup or not atr or px <= 0: return None
    if px < sup or px > sup*1.08: return None
    entry = round(sup*1.015, 2); stop = round(sup - 0.6*atr, 2)
    t1 = ind["ma20"] if ind["ma20"] and ind["ma20"] > entry*1.01 else res
    t2 = res if res and res > (t1 or 0)*1.01 else ind["hi60"]
    if not t1 or not t2 or entry <= stop: return None
    risk = entry - stop
    rr1, rr2 = round((t1-entry)/risk,2), round((t2-entry)/risk,2)
    if rr2 < 1.5: return None
    grade = "A-" if (px > (ind["ma50"] or 0) and 42 <= (ind["rsi14"] or 0) <= 65 and rr2 >= 2) else \
            ("B+" if rr2 >= 2 else "B")
    ev = ""
    if not is_tw and code in US_EARNINGS and US_EARNINGS[code] >= TODAY:
        ev = f"🚨 財報 {US_EARNINGS[code]}，財報前 2 個交易日未達目標一 → 減半或出場"
    return {"code":code,"name":name,"cur":"NT$" if is_tw else "$",
            "price":px,"chg":ind["chg1d"],"rsi":ind["rsi14"],"atr":ind["atr14"],"trend":ind["trend"],
            "grade":grade,
            "trigger":f"收盤站回 {round(sup,2)} 之上且當日低點不破前日低點（止穩K），大盤非紅燈",
            "entryLo":round(sup,2),"entryHi":round(sup*1.03,2),"entry":entry,"stop":stop,
            "t1":round(t1,2),"t1l":"MA20/前壓","t2":round(t2,2),"t2l":"壓力/60日高",
            "rr1":rr1,"rr2":rr2,
            "invalid":f"收盤跌破 {round(stop,2)} → 訊號取消",
            "event":ev}

# ── 當沖預篩（使用者五規則的日線代理）────────────────
def daytrade_screen(data, sectors, taiex_chg):
    cands = []
    strong_by_sector = {}
    metrics = {}
    for code, rs in data.items():
        if code.startswith("_") or len(rs) < 21: continue
        r = rs[-1]
        c, o, h, l = float(r["close"]), float(r["open"]), float(r["high"]), float(r["low"])
        val = float(r["value"]); v = float(r["volume"])
        pc = float(rs[-2]["close"])
        if c < DT_MIN_PRICE or val < DT_MIN_VALUE: continue
        chg = (c/pc-1)*100
        vols = [float(x["volume"]) for x in rs[-6:-1]]
        vr = v/(sum(vols)/5) if sum(vols) > 0 else 0
        closes = [float(x["close"]) for x in rs]
        m5, m10, m20 = sma(closes,5), sma(closes,10), sma(closes,20)
        if not all((m5,m10,m20)): continue
        spread = (max(m5,m10,m20)/min(m5,m10,m20)-1)*100
        pos = (c-l)/(h-l) if h > l else 1.0
        sec = sectors.get(code, "?")
        metrics[code] = dict(chg=chg, vr=vr, spread=spread, pos=pos, sec=sec,
                             m5=m5, m10=m10, m20=m20, c=c, name=r["name"], val=val)
        if chg >= 3: strong_by_sector.setdefault(sec, []).append(code)

    for code, m in metrics.items():
        if m["chg"] < 3 or m["vr"] < 1.8: continue          # 基本門檻：強勢 + 有量
        if not (m["c"] > m["m5"] > m["m10"]): continue       # 短均多頭
        score, why = 0, []
        if m["vr"] >= 3: score += 30; why.append(f"量比{m['vr']:.1f}")
        elif m["vr"] >= 2: score += 20; why.append(f"量比{m['vr']:.1f}")
        else: score += 10; why.append(f"量比{m['vr']:.1f}")
        if m["chg"] >= 5: score += 20; why.append(f"漲{m['chg']:.1f}%")
        else: score += 12; why.append(f"漲{m['chg']:.1f}%")
        if m["pos"] >= 0.8: score += 15; why.append("收最高點附近")
        if m["spread"] <= MA_TIGHT_PCT: score += 20; why.append(f"均線糾結{m['spread']:.1f}%（剛起漲）")
        elif m["spread"] >= MA_WIDE_PCT: score -= 15; why.append(f"⚠均線發散{m['spread']:.1f}%（易回檔）")
        peers = [p for p in strong_by_sector.get(m["sec"], []) if p != code]
        if len(peers) >= 2:
            score += 15; why.append(f"族群同步（{len(peers)}檔齊強）")
        elif len(peers) == 1:
            score += 7; why.append("族群1檔同強")
        else:
            why.append("⚠獨漲（隔日沖風險）")
        if m["chg"] >= 9.5: why.append("⚠接近漲停，隔日開高追進風險大")
        if score >= DT_MIN_SCORE:
            cands.append({"code":code,"name":m["name"],"close":round(m["c"],2),"chg":round(m["chg"],2),
                          "vol_ratio":round(m["vr"],2),"ma_spread":round(m["spread"],2),
                          "value_e8":round(m["val"]/1e8,1),"sector":m["sec"],
                          "peers":len(peers),"score":score,"why":"、".join(why)})
    cands.sort(key=lambda x: -x["score"])
    return cands[:DT_MAX_LIST]

# ── 大盤紅綠燈 ───────────────────────────────────────
def regime(idx_rows):
    if not idx_rows or len(idx_rows) < 6:
        return {"level":"yellow","label":"資料不足","taiex":None,"note":"指數歷史不足 20 日，預設黃燈"}
    closes = [float(r["close"]) for r in idx_rows]
    c, m5 = closes[-1], sma(closes,5)
    m20 = sma(closes,20) or sma(closes, len(closes)-1)
    hi60 = max(closes[-min(60,len(closes)):])
    dd = round((c/hi60-1)*100, 1)
    if c > m20 and c > m5: lv, lb = "green", "正常執行 · 風險 1%"
    elif c < m5 and c < m20: lv, lb = "red", "暫停新倉 · 試單風險 ≤0.33%"
    else: lv, lb = "yellow", "半倉 · 風險 0.5% · 只做 A 級"
    return {"level":lv,"label":lb,"taiex":round(c,2),"ma5":round(m5,2),"ma20":round(m20,2),
            "drawdown_from_60d_high":dd,
            "note":f"TAIEX {round(c,0):,.0f}｜MA5 {m5:,.0f}｜MA20 {m20:,.0f}｜距 60 日高 {dd}%"}

# ── 主程式 ───────────────────────────────────────────
def main():
    offline = "--offline" in sys.argv
    tw = load_hist("tw"); us = load_hist("us")
    sectors = {}
    if not offline:
        fetch_tw(tw)
        sectors = fetch_sectors()
        fetch_us(us)
        save_hist("tw", tw); save_hist("us", us)
    else:
        p = os.path.join(HIST, "tw_sectors.json")
        if os.path.exists(p): sectors = json.load(open(p, encoding="utf-8"))
        print("離線模式：使用既有 history/")

    reg = regime(tw.get("_TAIEX"))
    idx_chg = 0.0

    tw_setups, tw_all = [], {}
    for code in TW_WATCH:
        rs = tw.get(code)
        ind = indicators(rs) if rs else None
        if ind:
            tw_all[code] = {**ind, "name": TW_NAMES.get(code, code)}
            s = swing_setup(code, TW_NAMES.get(code, code), ind, "NT$", True)
            if s: tw_setups.append(s)
    us_setups, us_all = [], {}
    for t in US_WATCH:
        rs = us.get(t)
        ind = indicators(rs) if rs else None
        if ind:
            us_all[t] = {**ind, "name": t}
            s = swing_setup(t, t, ind, "$", False)
            if s: us_setups.append(s)

    dt = daytrade_screen(tw, sectors, idx_chg)

    out = {
        "generated": datetime.now(TPE).strftime("%Y-%m-%d %H:%M %Z"),
        "regime": reg,
        "swing": {"tw": tw_setups, "us": us_setups},
        "overview": {"tw": tw_all, "us": us_all},
        "daytrade": {"date_basis": (tw.get("2330") or [{}])[-1].get("date",""),
                     "list": dt,
                     "note": "以最近收盤日線預篩，供『隔日』盤中依 SOP 執行；非即時訊號。"},
        "meta": {"mode": "offline" if offline else "live",
                 "universe_tw": len([k for k in tw if not k.startswith('_')]),
                 "earnings": US_EARNINGS}
    }
    with open(os.path.join(ROOT, "data.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"data.json 完成：波段 TW {len(tw_setups)} / US {len(us_setups)}，當沖清單 {len(dt)} 檔，紅綠燈={reg['level']}")

if __name__ == "__main__":
    main()
