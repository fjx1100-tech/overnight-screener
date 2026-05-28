#!/usr/bin/env python3
"""
趋势波段选股器 — 尾盘选股 (14:30运行)
========================================
核心逻辑：均线多头 + 上升通道 + 放量确认 + MACD共振
持有周期：3~10天
适用人群：上班族，不看盘，尾盘看一眼
========================================
"""
import json, os, re, sys, time, subprocess, urllib.request, urllib.parse
from datetime import datetime
from typing import Optional

OUTPUT_DIR = os.path.expanduser("~/trend_scanner_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

SC_KEY = os.environ.get("SC_KEY", "SCT347879TSQ0zp53ApDKc8P8jN3D6Zggx")
SC_URL = f"https://sctapi.ftqq.com/{SC_KEY}.send"

# ── 阈值参数 ──
MIN_MCAP, MAX_MCAP = 40, 230      # 市值(亿)
MIN_CHG, MAX_CHG = 3.0, 5.0       # 涨跌幅(%)
MIN_TURN, MAX_TURN = 3.0, 12.0    # 换手率(%) 放宽到12%
MIN_VR = 1.2                       # 量比
MIN_VOL_RATIO = 1.5                # 当日量/前5日均量 ≥ 1.5(放量确认)
TIMEOUT = 10

# ── 工具函数 ──

def curl(url, timeout=TIMEOUT):
    try:
        r = subprocess.run(["curl", "-s", "--max-time", str(timeout), url],
                           capture_output=True, timeout=timeout+3)
        if r.returncode != 0 or not r.stdout: return None
        for enc in ['utf-8', 'gbk', 'gb2312', 'gb18030']:
            try: return r.stdout.decode(enc)
            except UnicodeDecodeError: continue
        return r.stdout.decode('utf-8', errors='replace')
    except: return None

def pf(v, default=None):
    if v is None or v=="" or v=="-": return default
    try: return float(v)
    except: return default

def billion(raw): return pf(raw)/1e8 if pf(raw) else None
def market_id(code): return "1" if code.startswith("6") else "0"

# ── 数据源 ──

def fetch_eastmoney():
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=6000&po=1&np=1&fltt=2&invt=2&fid=f3"
           "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
           "&fields=f2,f3,f5,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21")
    raw = curl(url, timeout=15)
    if not raw: return None
    try: data = json.loads(raw)
    except: return None
    items = data.get("data",{}).get("diff",[])
    if not items: return None
    stocks = []
    for it in items:
        code, name = it.get("f12",""), it.get("f14","")
        if not code or "ST" in (name or ""): continue
        if code.startswith(("92","300","301","688")): continue
        if (name or "").startswith(("N","C")): continue
        vr = pf(it.get("f10"))
        stocks.append({"code":code,"name":name,
            "price":pf(it.get("f2")),"change_pct":pf(it.get("f3")),
            "turnover":pf(it.get("f8")),"vol_ratio":vr/100 if vr else None,
            "mcap_billion":billion(it.get("f20")),
            "high":pf(it.get("f15")),"low":pf(it.get("f16")),
            "open":pf(it.get("f17")),"prev_close":pf(it.get("f18")),
            "volume_lots":pf(it.get("f5"))})
    return stocks

def fetch_sina_paginated(max_pages=30):
    all_stocks = []
    for page in range(1, max_pages+1):
        url = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=80&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=init"
        raw = curl(url, timeout=12)
        if not raw: break
        try:
            raw_clean = re.sub(r':\s*NaN', ': null', raw)
            raw_clean = re.sub(r':\s*-NaN', ': null', raw_clean)
            items = json.loads(raw_clean)
        except: break
        if not items: break
        for it in items:
            code, name = it.get("code",""), it.get("name","")
            if not code or "ST" in (name or ""): continue
            if code.startswith(("92","300","301","688")): continue
            chg = pf(it.get("changepercent"))
            if chg is None: continue
            if chg < MIN_CHG: break
            if chg > MAX_CHG: continue
            all_stocks.append({"code":code,"name":name,
                "price":pf(it.get("trade")),"change_pct":chg,
                "turnover":pf(it.get("turnoverratio")),
                "vol_ratio":None,"mcap_billion":None,
                "high":pf(it.get("high")), "low":pf(it.get("low")),
                "open":pf(it.get("open")), "prev_close":pf(it.get("settlement")),
                "volume_lots":pf(it.get("volume"))})
        last_chg = pf(items[-1].get("changepercent")) if items else None
        if last_chg is not None and last_chg < MIN_CHG: break
        if len(items) < 80: break
        time.sleep(0.3)
    return all_stocks

def fetch_tencent_batch(codes):
    results = {}
    prefix = lambda c: "sh"+c if c.startswith("6") else "sz"+c
    batches = [codes[i:i+10] for i in range(0, len(codes), 10)]
    for batch in batches:
        url = "https://qt.gtimg.cn/q=" + ",".join(prefix(c) for c in batch)
        raw = curl(url, timeout=8)
        if not raw: continue
        for line in raw.strip().split("\n"):
            m = re.search(r'v_\w+="(.+)"', line)
            if not m: continue
            f = m.group(1).split("~")
            if len(f) < 46: continue
            code = f[2]
            results[code] = {
                "price": pf(f[3]), "change_pct": pf(f[32]),
                "turnover": pf(f[38]), "pe": pf(f[39]),
                "mcap_billion": pf(f[45]),
                "vol_ratio": pf(f[49]),
                "high": pf(f[33]), "low": pf(f[34]),
                "open": pf(f[5]), "prev_close": pf(f[4]),
            }
        time.sleep(0.2)
    return results

def enrich_mcap(stocks):
    need = [s for s in stocks if not s.get("mcap_billion") or not s.get("vol_ratio")]
    if not need: return
    codes = [s["code"] for s in need]
    print(f"  腾讯补充 {len(codes)} 只...")
    tx = fetch_tencent_batch(codes)
    for s in need:
        t = tx.get(s["code"])
        if not t: continue
        if not s.get("mcap_billion") and t.get("mcap_billion"):
            s["mcap_billion"] = t["mcap_billion"]
        if not s.get("vol_ratio") and t.get("vol_ratio"):
            s["vol_ratio"] = t["vol_ratio"]
    still = [s for s in need if not s.get("mcap_billion") or not s.get("vol_ratio")]
    for s in still:
        mid = market_id(s["code"])
        url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={mid}.{s['code']}&fields=f116,f117,f10"
        raw = curl(url, timeout=5)
        if not raw: continue
        try: d = json.loads(raw).get("data",{})
        except: continue
        if not d: continue
        if not s.get("mcap_billion"): s["mcap_billion"] = billion(d.get("f116"))
        vr = pf(d.get("f10"))
        if vr and not s.get("vol_ratio"): s["vol_ratio"] = vr / 100

# ── K线 + 技术指标 ──

def fetch_kline(code):
    """获取30天K线，返回{closes, highs, lows, vols, dates}"""
    prefix = "sh"+code if code.startswith("6") else "sz"+code
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={prefix},day,,,40,qfq"
    raw = curl(url, timeout=8)
    if raw and "qfqday" in raw:
        try:
            start = raw.index("{")
            data = json.loads(raw[start:])
            klines = data.get("data",{}).get(prefix,{}).get("qfqday",[])
            if klines and len(klines) >= 25:
                closes, highs, lows, vols, dates = [], [], [], [], []
                for row in klines[-30:]:
                    if len(row) < 6: continue
                    dates.append(row[0])
                    closes.append(pf(row[2]))
                    highs.append(pf(row[3]))
                    lows.append(pf(row[4]))
                    vols.append(pf(row[5]))
                return {"closes":closes,"highs":highs,"lows":lows,"vols":vols,"dates":dates}
        except: pass
    # 备用：东方财富K线
    mid = market_id(code)
    url2 = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid={mid}.{code}&fields1=f1,f2,f3,f4,f5,f6"
            f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            f"&klt=101&fqt=1&end=20500101&lmt=30")
    raw = curl(url2, timeout=6)
    if raw:
        try:
            klines = json.loads(raw).get("data",{}).get("klines",[])
            if klines and len(klines) >= 25:
                closes, highs, lows, vols, dates = [], [], [], [], []
                for line in klines:
                    p = line.split(",")
                    if len(p) < 6: continue
                    dates.append(p[0])
                    closes.append(pf(p[2]))
                    highs.append(pf(p[3]))
                    lows.append(pf(p[4]))
                    vols.append(pf(p[5]))
                return {"closes":closes,"highs":highs,"lows":lows,"vols":vols,"dates":dates}
        except: pass
    return None

def calc_ma(data, n):
    if len(data) < n: return None
    return sum(data[-n:]) / n

def calc_ema(data, n):
    """指数移动平均"""
    result = []
    for i, v in enumerate(data):
        if i == 0:
            result.append(v)
        else:
            k = 2 / (n + 1)
            result.append(v * k + result[-1] * (1 - k))
    return result

def analyze_kline(code):
    """完整技术分析：均线 + 通道 + 量 + MACD"""
    k = fetch_kline(code)
    if not k: return None
    
    closes = k["closes"]
    highs = k["highs"]
    lows = k["lows"]
    vols = k["vols"]
    dates = k["dates"]
    cp = closes[-1]
    
    if len(closes) < 20: return None
    
    # ── 均线 ──
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma30 = calc_ma(closes, 30)
    ma_bullish = ma10 > ma20 > ma30 if ma30 else ma10 > ma20
    
    # ── 上升通道 ──
    up_channel = False
    if len(closes) >= 40:
        low_prior = min(lows[:20])
        low_recent = min(lows[-20:])
        up_channel = (low_recent > low_prior * 0.99) and (cp > ma20) and (cp > ma10)
    elif len(closes) >= 20:
        up_channel = (cp > ma20) and (cp > ma10)
    
    # ── 量能 ──
    vol_ok = False
    if len(vols) >= 6:
        vol_ok = sum(vols[-3:]) / 3 > sum(vols[-6:-1]) / 5
    
    # 当日量 / 前5日均量
    vol_ratio_5 = None
    if len(vols) >= 6:
        avg5 = sum(vols[-6:-1]) / 5
        if avg5 > 0:
            vol_ratio_5 = vols[-1] / avg5
    
    # ── MACD ──
    ema12_list = calc_ema(closes, 12)
    ema26_list = calc_ema(closes, 26)
    diff = [ema12_list[i] - ema26_list[i] for i in range(len(closes))]
    dea_list = calc_ema(diff, 9)
    macd_val = [(diff[i] - dea_list[i]) * 2 for i in range(len(closes))]
    
    # 当前MACD状态
    diff_now = diff[-1]
    dea_now = dea_list[-1]
    macd_now = macd_val[-1]
    
    # MACD判断
    macd_above_zero = diff_now > 0                # DIFF在零轴上方
    macd_golden_cross = diff_now > dea_now         # 当前金叉状态
    macd_dea_up = (dea_list[-1] > dea_list[-2]) if len(dea_list) >= 2 else False  # DEA拐头
    macd_bar_up = (macd_now > macd_val[-2]) if len(macd_val) >= 2 else False       # MACD柱放大
    
    # 二次金叉检测：最近20天内是否有过死叉然后又金叉
    double_golden = False
    for i in range(-20, 0):
        if i >= 1 and diff[i] < dea_list[i] and diff[i-1] >= dea_list[i-1]:
            # 发现死叉，看后面是否金叉
            for j in range(i+1, 0):
                if diff[j] > dea_list[j] and diff[j-1] <= dea_list[j-1]:
                    double_golden = True
                    break
    
    macd_score = 0
    macd_signals = []
    if macd_above_zero:
        macd_score += 2
        macd_signals.append("零轴上")
    if macd_golden_cross:
        macd_score += 2
        macd_signals.append("金叉")
    if macd_dea_up:
        macd_score += 2
        macd_signals.append("DEA拐头")
    if macd_bar_up:
        macd_score += 1
        macd_signals.append("柱放大")
    if double_golden:
        macd_score += 2
        macd_signals.append("二次金叉")
    
    return {
        "ma10": round(ma10,2), "ma20": round(ma20,2), "ma30": round(ma30,2) if ma30 else None,
        "ma_bullish": ma_bullish,
        "up_channel": up_channel,
        "vol_increasing": vol_ok,
        "vol_ratio_5": round(vol_ratio_5, 2) if vol_ratio_5 else None,
        "macd_diff": round(diff_now, 3),
        "macd_dea": round(dea_now, 3),
        "macd_bar": round(macd_now, 3),
        "macd_above_zero": macd_above_zero,
        "macd_golden_cross": macd_golden_cross,
        "macd_dea_up": macd_dea_up,
        "macd_bar_up": macd_bar_up,
        "macd_double_golden": double_golden,
        "macd_score": macd_score,
        "macd_signals": ";".join(macd_signals),
        "recent_high": round(max(highs[-20:]),2),
        "recent_low": round(min(lows[-20:]),2),
        "current_price": cp,
    }

# ── 筛选 ──

def screen(stocks):
    """基础条件过滤"""
    ok = []
    for s in stocks:
        chg = s.get("change_pct")
        mcap = s.get("mcap_billion")
        turn = s.get("turnover")
        vr = s.get("vol_ratio")
        reason = []
        if not chg or not (MIN_CHG <= chg <= MAX_CHG):
            reason.append(f"涨幅{chg}不达标")
        if not turn or not (MIN_TURN <= turn <= MAX_TURN):
            reason.append(f"换手{turn}不达标")
        if mcap is not None and not (MIN_MCAP <= mcap <= MAX_MCAP):
            reason.append(f"市值{mcap}越界")
        if vr is not None and vr < MIN_VR:
            reason.append(f"量比{vr}<{MIN_VR}")
        if reason: continue
        ok.append(s)
    return ok

# ── 评分 ──

def score_stock(s, tech):
    """综合评分（满分20分）"""
    score = 0
    details = []
    
    # 基础数据完整度 (2分)
    completeness = 0
    if s.get("mcap_billion"): completeness += 1
    if s.get("vol_ratio"): completeness += 1
    s["completeness"] = completeness
    if completeness >= 2: score += 2; details.append("数据完整")
    
    if not tech: 
        s["score"] = score
        s["score_detail"] = ";".join(details) if details else "无数据"
        return
    
    # 均线多头 (4分)
    if tech["ma_bullish"]: score += 4; details.append("均线多头")
    
    # 上升通道 (4分)
    if tech["up_channel"]: score += 4; details.append("上升通道")
    
    # 成交量递增 (2分)
    if tech["vol_increasing"]: score += 2; details.append("量增")
    
    # 放量确认 (2分)
    if tech["vol_ratio_5"] and tech["vol_ratio_5"] >= MIN_VOL_RATIO:
        score += 2; details.append(f"放量{tech['vol_ratio_5']:.1f}x")
    
    # MACD共振 (6分)
    score += min(tech["macd_score"], 6)
    if tech["macd_signals"]:
        details.append(f"MACD:{tech['macd_signals']}")
    
    s.update(tech)
    s["score"] = score
    s["score_detail"] = ";".join(details)

# ── 输出 ──

def print_results(stocks):
    if not stocks:
        print("\n今日无符合条件候选")
        return
    
    # 按评分排序
    stocks.sort(key=lambda x: x.get("score",0), reverse=True)
    
    print(f"\n{'='*70}")
    print(f"  趋势波段选股器 — {datetime.now().strftime('%Y-%m-%d')} — {len(stocks)}只候选")
    print(f"{'='*70}")
    
    for i, s in enumerate(stocks[:15]):
        sc = s.get("score", 0)
        icon = "🟢" if sc >= 15 else ("🟡" if sc >= 10 else ("⚪" if sc >= 5 else "⚫"))
        ma = f"MA10={s.get('ma10','?')} MA20={s.get('ma20','?')}" if s.get("ma10") else ""
        macd = f"DIFF={s.get('macd_diff','?')} DEA={s.get('macd_dea','?')}" if s.get("macd_diff") else ""
        
        print(f"\n{icon} #{i+1} {s['code']} {s['name']}  评分:{sc}/20")
        print(f"  涨{s['change_pct']:.1f}% 量比{s.get('vol_ratio','?')} 换手{s['turnover']}% 市值{s.get('mcap_billion','?')}亿")
        print(f"  现价{s.get('price','?')}  {ma}")
        print(f"  量比5日={s.get('vol_ratio_5','?')}x  |  {macd}")
        print(f"  {s.get('score_detail','')}")

def save(stocks):
    ds = datetime.now().strftime("%Y-%m-%d")
    with open(os.path.join(OUTPUT_DIR, f"{ds}.json"), "w") as f:
        json.dump({"date":ds, "total":len(stocks), "results":stocks},
                  f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUTPUT_DIR, f"{ds}.txt"), "w") as f:
        f.write(f"趋势波段选股器 — {ds}\n{'='*50}\n")
        for i, s in enumerate(stocks[:15]):
            f.write(f"\n#{i+1} {s['code']} {s['name']} 评分{s.get('score',0)}/20\n")
            f.write(f"  涨{s['change_pct']:.1f}% 量比{s.get('vol_ratio','?')} 换手{s['turnover']}% 市值{s.get('mcap_billion','?')}亿\n")
            if s.get("ma10"):
                f.write(f"  MA10={s['ma10']} MA20={s['ma20']} 多头:{s.get('ma_bullish')} 通道:{s.get('up_channel')}\n")
            if s.get("macd_diff"):
                f.write(f"  MACD: DIFF={s['macd_diff']} DEA={s['macd_dea']} 金叉:{s.get('macd_golden_cross')} 二次金叉:{s.get('macd_double_golden')}\n")

def push_to_wechat(stocks):
    ds = datetime.now().strftime("%Y-%m-%d %H:%M")
    top_n = 8
    if not stocks:
        title = f"⚠️ 趋势选股 {ds} — 无候选"
        desp = "今日无符合条件股票。"
    else:
        title = f"{ds} 趋势选股: {len(stocks)}只候选"
        lines = []
        for i, s in enumerate(stocks[:top_n]):
            sc = s.get("score", 0)
            chg = s.get("change_pct", 0)
            vr = s.get("vol_ratio", "?")
            turn = s.get("turnover", "?")
            mcap = s.get("mcap_billion", "?")
            macd_sig = s.get("macd_signals", "")
            lines.append(
                f"{i+1}. {s['name']}({s['code']}) 评分{sc} "
                f"涨{chg}% 量比{vr} 换手{turn}% 市值{mcap}亿 "
                f"{macd_sig}"
            )
        desp = "\n".join(lines)
        if len(stocks) > top_n:
            desp += f"\n...共{len(stocks)}只"
    desp += "\n\n⚠️ 非投资建议，尾盘确认后再决策"
    
    try:
        data = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
        req = urllib.request.Request(SC_URL, data=data, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        if result.get("code") == 0:
            print("✅ 微信推送成功")
        else:
            print(f"⚠️ 推送失败: {result.get('message','?')}")
    except Exception as e:
        print(f"⚠️ 推送异常: {e}")

# ── 主流程 ──

def main():
    print("趋势波段选股器 v1.0")
    print("="*50)
    print(f"条件: 涨{MIN_CHG}-{MAX_CHG}% 换手{MIN_TURN}-{MAX_TURN}% 量比>{MIN_VR} 市值{MIN_MCAP}-{MAX_MCAP}亿")
    print(f"技术: 均线多头 + 上升通道 + 量增 + MACD共振")
    print(f"排除: ST/300/301/688/新股")
    print()
    
    # 1. 获取数据
    stocks = fetch_eastmoney()
    if stocks:
        print(f"[源A] 东方财富: {len(stocks)}只")
    else:
        print("[源A] 东方财富不通 → 新浪分页")
        stocks = fetch_sina_paginated(max_pages=8)
        if not stocks:
            print("❌ 无数据，终止"); return
        print(f"[源A] 新浪: {len(stocks)}只 (缺市值/量比)")
        enrich_mcap(stocks)
    
    # 2. 初筛
    candidates = screen(stocks)
    print(f"\n[初筛] {len(stocks)} → {len(candidates)}只")
    if not candidates:
        save([]); push_to_wechat([]); return
    
    # 3. 技术分析（均线+通道+量+MACD）
    print(f"\n[技术分析] 拉取K线+计算MACD...")
    results = []
    for i, s in enumerate(candidates):
        tech = analyze_kline(s["code"])
        score_stock(s, tech)
        results.append(s)
        if (i+1) % 5 == 0:
            print(f"  {i+1}/{len(candidates)}...")
        time.sleep(0.15)
    
    # 4. 排序输出
    results.sort(key=lambda x: x.get("score",0), reverse=True)
    print_results(results)
    save(results)
    push_to_wechat(results)
    
    # 5. 统计
    high = sum(1 for s in results if s.get("score",0) >= 15)
    mid = sum(1 for s in results if 10 <= s.get("score",0) < 15)
    low = sum(1 for s in results if s.get("score",0) < 10)
    print(f"\n📊 评分分布: 🟢高分{high} 🟡中分{mid} ⚪低分{low}")

if __name__ == "__main__":
    main()
