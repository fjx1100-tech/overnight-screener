#!/usr/bin/env python3
"""
隔夜持股法改进版筛选脚本 — 尾盘选股 (14:30运行)
=====================================================
运行时段: 14:30筛选, 留出30分钟尾盘确认和买入
数据源自动切换: 东方财富优先, 失败则用新浪 → 腾讯
任一源挂了不影响整体, 只在数据完整度上扣分。
"""

import json, os, re, sys, time, subprocess
from datetime import datetime
from typing import Optional

OUTPUT_DIR = os.path.expanduser("~/overnight_screener_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Server酱推送 (微信通知)
# 优先读取环境变量(GitHub Actions), 其次硬编码(本地运行)
SC_KEY = os.environ.get("SC_KEY", "SCT347879TSQ0zp53ApDKc8P8jN3D6Zggx")
SC_URL = f"https://sctapi.ftqq.com/{SC_KEY}.send"

# 筛选阈值
MIN_MCAP, MAX_MCAP = 40, 230     # 亿 (40~230亿)
MIN_CHG, MAX_CHG = 3.0, 5.0      # %
MIN_TURN, MAX_TURN = 3.0, 9.0    # %
MIN_VR = 1.2
TIMEOUT = 10

def curl(url, timeout=TIMEOUT):
    try:
        r = subprocess.run(["curl", "-s", "--max-time", str(timeout), url],
                           capture_output=True, timeout=timeout+3)
        if r.returncode != 0 or not r.stdout: return None
        # 自动检测编码 (东方财富=UTF8, 腾讯=GBK, 新浪=UTF8)
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

# ================================================================
# 数据源 — 自动切换
# ================================================================

def try_eastmoney_list():
    """尝试东方财富批量列表 (最快最全)"""
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=6000&po=1&np=1&fltt=2&invt=2&fid=f3"
           "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
           "&fields=f2,f3,f5,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21")
    raw = curl(url, timeout=15)
    if not raw: return None, "no_response"
    try: data = json.loads(raw)
    except: return None, "json_error"
    items = data.get("data",{}).get("diff",[])
    if not items: return None, "empty"
    stocks = []
    for it in items:
        code, name = it.get("f12",""), it.get("f14","")
        if not code or "ST" in (name or ""): continue
        if code.startswith("92") or (name or "").startswith(("N","C")): continue
        vr = pf(it.get("f10"))
        stocks.append({"code":code,"name":name,
            "price":pf(it.get("f2")),"change_pct":pf(it.get("f3")),
            "turnover":pf(it.get("f8")),"vol_ratio":vr/100 if vr else None,
            "mcap_billion":billion(it.get("f20")),
            "high":pf(it.get("f15")),"low":pf(it.get("f16")),
            "open":pf(it.get("f17")),"prev_close":pf(it.get("f18")),
            "pe":pf(it.get("f9")),"volume_lots":pf(it.get("f5")),
            "amplitude":pf(it.get("f7")),
            "src_A":True,"src_B":False,"src_C":False})
    return stocks, "ok"

def try_sina_paginated(max_pages=30):
    """新浪分页获取涨幅榜 — 备选源, 按涨跌幅降序排列"""
    all_stocks = []
    for page in range(1, max_pages+1):
        # 按涨跌幅降序排列, 确保3-5%区间全覆盖
        url = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=80&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=init"
        raw = curl(url, timeout=12)
        if not raw: break
        try:
            # 清理JSON中的NaN
            raw_clean = re.sub(r':\s*NaN', ': null', raw)
            raw_clean = re.sub(r':\s*-NaN', ': null', raw_clean)
            items = json.loads(raw_clean)
        except: break
        if not items: break
        page_passed = 0
        for it in items:
            code, name = it.get("code",""), it.get("name","")
            if not code or "ST" in (name or ""): continue
            if code.startswith("92"): continue
            chg = pf(it.get("changepercent"))
            if chg is None: continue
            # 涨跌幅低于下限, 本页后面的更低, 整页跳过
            if chg < MIN_CHG: break
            if chg > MAX_CHG: continue  # 还没到目标区间
            if pf(it.get("turnoverratio"), 0) < MIN_TURN: continue
            page_passed += 1
            all_stocks.append({"code":code,"name":name,
                "price":pf(it.get("trade")),"change_pct":chg,
                "turnover":pf(it.get("turnoverratio")),
                "vol_ratio":None,
                "mcap_billion":None,
                "high":pf(it.get("high")),"low":pf(it.get("low")),
                "open":pf(it.get("open")),"prev_close":pf(it.get("settlement")),
                "volume_lots":pf(it.get("volume")),
                "src_A":True,"src_B":False,"src_C":False})
        # 本页出现低于MIN_CHG的, 后续页都不需要了
        last_chg = pf(items[-1].get("changepercent")) if items else None
        if last_chg is not None and last_chg < MIN_CHG:
            break
        if len(items) < 80: break
        time.sleep(0.3)
    return all_stocks

def try_tencent_batch(codes):
    """腾讯行情API — 批量获取市值/量比/换手率"""
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
                "circ_mcap_billion": pf(f[44]),  # 流通市值(亿)
                "mcap_billion": pf(f[45]),        # 总市值(亿)
                "vol_ratio": pf(f[49]),            # 量比
                "high": pf(f[33]), "low": pf(f[34]),
                "open": pf(f[5]), "prev_close": pf(f[4]),
            }
        time.sleep(0.2)
    return results

def try_enrich_mcap(stocks):
    """补充市值/量比 — 依次尝试腾讯API、东方财富"""
    # 找出缺数据的股票
    need = [s for s in stocks if not s.get("mcap_billion") or not s.get("vol_ratio")]
    if not need:
        print("  市值/量比已齐全, 跳过")
        return stocks

    codes = [s["code"] for s in need]
    print(f"  尝试腾讯API补充 {len(codes)} 只...")
    tx = try_tencent_batch(codes)
    fixed = 0
    for s in need:
        t = tx.get(s["code"])
        if not t: continue
        if not s.get("mcap_billion") and t.get("mcap_billion"):
            s["mcap_billion"] = t["mcap_billion"]
            s["circ_mcap_billion"] = t.get("circ_mcap_billion")
        if not s.get("vol_ratio") and t.get("vol_ratio"):
            s["vol_ratio"] = t["vol_ratio"]
        if not s.get("pe") and t.get("pe"):
            s["pe"] = t["pe"]
        s["src_B"] = True
        fixed += 1
    print(f"  腾讯API补到 {fixed} 只")

    # 还有缺的, 尝试东方财富
    still_need = [s for s in need if not s.get("mcap_billion")]
    if still_need:
        print(f"  尝试东方财富补充 {len(still_need)} 只...")
        for s in still_need:
            mid = market_id(s["code"])
            url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={mid}.{s['code']}&fields=f116,f117,f9,f10"
            raw = curl(url, timeout=5)
            if not raw: continue
            try: d = json.loads(raw).get("data",{})
            except: continue
            if not d: continue
            if not s.get("mcap_billion"):
                s["mcap_billion"] = billion(d.get("f116"))
            vr = pf(d.get("f10"))
            if vr and not s.get("vol_ratio"):
                s["vol_ratio"] = vr / 100
            s["src_B"] = True

    return stocks

def try_kline(code):
    """获取K线 — 均线+斐波那契 (腾讯API优先, 东方财富备用)"""
    prefix = "sh"+code if code.startswith("6") else "sz"+code

    # 源1: 腾讯K线 (前复权)
    tx_url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={prefix},day,,,30,qfq"
    raw = curl(tx_url, timeout=8)
    if raw and "qfqday" in raw:
        try:
            # 去掉 callback 包装: kline_dayqfq={...}
            start = raw.index("{")
            data = json.loads(raw[start:])
            klines = data.get("data",{}).get(prefix,{}).get("qfqday",[])
            if klines and len(klines) >= 20:
                closes, highs, lows, vols = [], [], [], []
                for row in klines[-30:]:  # 取最近30条
                    # [date, open, close, high, low, volume]
                    if len(row) < 6: continue
                    closes.append(pf(row[2])); highs.append(pf(row[3]))
                    lows.append(pf(row[4])); vols.append(pf(row[5]))
                return _calc_ma_fib(closes, highs, lows, vols)
        except: pass

    # 源2: 东方财富K线 (备用)
    mid = market_id(code)
    em_url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
              f"?secid={mid}.{code}&fields1=f1,f2,f3,f4,f5,f6"
              f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
              f"&klt=101&fqt=1&end=20500101&lmt=30")
    raw = curl(em_url, timeout=6)
    if raw:
        try:
            klines = json.loads(raw).get("data",{}).get("klines",[])
            if klines and len(klines)>=20:
                closes, highs, lows, vols = [], [], [], []
                for line in klines:
                    p = line.split(",")
                    if len(p)<6: continue
                    closes.append(pf(p[2])); highs.append(pf(p[3]))
                    lows.append(pf(p[4])); vols.append(pf(p[5]))
                return _calc_ma_fib(closes, highs, lows, vols)
        except: pass

    return None

def _calc_ma_fib(closes, highs, lows, vols):
    """从OHLCV数组计算均线+上升通道判断"""
    if len(closes) < 20: return None
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    ma30 = sum(closes) / len(closes)
    h = max(highs[-20:]); l = min(lows[-20:])
    cp = closes[-1]
    ma_ok = (ma10 > ma20 > ma30) or (ma10 > ma20)
    vol_ok = False
    if len(vols) >= 6:
        vol_ok = sum(vols[-3:]) / 3 > sum(vols[-6:-1]) / 5
    # 上升通道判断: 当前价在MA20之上 + 近期底部抬升
    up_channel = False
    if len(closes) >= 40:
        # 前20日低点 vs 后20日低点 → 底部抬升
        low_prior = min(lows[:20])
        low_recent = min(lows[-20:])
        # 近20日低点 > 前20日低点 (底部抬高)
        # 且当前价 > MA20 (在均线上方运行)
        up_channel = (low_recent > low_prior) and (cp > ma20) and (cp > ma10)
    elif len(closes) >= 20:
        up_channel = (cp > ma20) and (cp > ma10)
    return {"ma10": round(ma10, 2), "ma20": round(ma20, 2), "ma30": round(ma30, 2),
            "ma_bullish": ma_ok, "up_channel": up_channel,
            "recent_high": round(h, 2), "recent_low": round(l, 2),
            "vol_increasing": vol_ok, "current_price": cp}

# ================================================================
# 筛选流水线
# ================================================================

def screen_initial(stocks, debug=False):
    """初筛"""
    ok=[]
    for s in stocks:
        code = s.get("code","")
        name = s.get("name","")
        # 排除300/301/688开头 (用户不能买创业板/科创板)
        if code.startswith(("300","301","688")):
            if debug: print(f"  ✗ {code} {name}: 300/301/688不可买")
            continue
        chg=s.get("change_pct"); mcap=s.get("mcap_billion")
        turn=s.get("turnover"); vr=s.get("vol_ratio")
        reason=[]
        if not chg: reason.append(f"缺涨跌幅")
        elif not (MIN_CHG<=chg<=MAX_CHG): reason.append(f"涨跌幅{chg:.1f}%越界")
        if not turn: reason.append("缺换手率")
        elif not (MIN_TURN<=turn<=MAX_TURN): reason.append(f"换手{turn:.1f}%越界")
        if mcap is not None and not (MIN_MCAP<=mcap<=MAX_MCAP): reason.append(f"市值{mcap:.0f}亿越界")
        if vr is not None and vr<MIN_VR: reason.append(f"量比{vr:.2f}<{MIN_VR}")
        if reason:
            if debug: print(f"  ✗ {code} {s['name']}: {', '.join(reason)}")
            continue
        ok.append(s)
    return ok

def enrich_kline(stocks):
    for i,s in enumerate(stocks):
        k=try_kline(s["code"])
        if k:
            s["src_C"]=True; s.update(k)
        if (i+1)%5==0: print(f"  K线 {i+1}/{len(stocks)}...")
        time.sleep(0.12)

def final_filter(stocks):
    """终筛 — 均线多头 + 量增 + 上升通道"""
    for s in stocks:
        score=0
        s["pass_ma"]=s.get("ma_bullish",False); score+=4 if s["pass_ma"] else 0
        s["pass_vol"]=s.get("vol_increasing",False); score+=3 if s["pass_vol"] else 0
        s["pass_uptrend"]=s.get("up_channel",False); score+=3 if s["pass_uptrend"] else 0
        # 数据完整度
        comp=0
        if s.get("src_A"): comp+=50
        if s.get("src_B"): comp+=20
        if s.get("src_C"): comp+=30
        s["completeness"]=min(100,comp)
        if s["completeness"]>=70: score+=1
        s["score"]=score
    stocks.sort(key=lambda x:x["score"],reverse=True)
    return stocks

# ================================================================
# 输出
# ================================================================

def print_top(stocks, n=15):
    if not stocks:
        print("\n今日无符合条件候选")
        return
    print(f"\n{'='*65}")
    print(f"  隔夜持股法改进版 — {datetime.now().strftime('%Y-%m-%d')} — {len(stocks)}只候选")
    print(f"{'='*65}")
    for i,s in enumerate(stocks[:n]):
        comp=s.get("completeness",0)
        icon="🟢" if comp>=90 else ("🟡" if comp>=70 else "🔴")
        tags=[]
        tags.append("MA✓" if s.get("pass_ma") else "MA✗")
        tags.append("量✓" if s.get("pass_vol") else "量✗")
        tags.append("通道✓" if s.get("pass_uptrend") else "通道✗")
        ma=f" MA10={s['ma10']} MA20={s['ma20']}" if s.get("ma10") else ""
        print(f"\n{icon} #{i+1} {s['code']} {s['name']}  评分:{s['score']}  数据:{comp}%")
        print(f"  涨{s['change_pct']:.1f}% 量比{s.get('vol_ratio','?')} 换手{s['turnover']}% 市值{s.get('mcap_billion','?')}亿")
        print(f"  现价{s.get('price','?')}{ma}  {'|'.join(tags)}")
        if s.get("recent_high"): print(f"  区间: 高{s['recent_high']}→低{s['recent_low']}")

def save(stocks):
    ds=datetime.now().strftime("%Y-%m-%d")
    with open(os.path.join(OUTPUT_DIR,f"{ds}.json"),"w") as f:
        json.dump({"date":ds,"total":len(stocks),"results":stocks},f,ensure_ascii=False,indent=2)
    with open(os.path.join(OUTPUT_DIR,f"{ds}.txt"),"w") as f:
        f.write(f"隔夜持股法改进版 — {ds}\n{'='*50}\n")
        for i,s in enumerate(stocks[:15]):
            f.write(f"\n#{i+1} {s['code']} {s['name']} [数据:{s.get('completeness',0)}%]\n")
            f.write(f"  涨{s['change_pct']:.1f}% 量比{s.get('vol_ratio','?')} 换手{s['turnover']}% 市值{s.get('mcap_billion','?')}亿\n")
            if s.get("ma10"): f.write(f"  MA10={s['ma10']} MA20={s['ma20']} 多头:{s.get('pass_ma')} 通道:{s.get('pass_uptrend')}\n")
    print(f"\n📁 保存: {OUTPUT_DIR}/{ds}.json  + .txt")
    return stocks

def push_to_wechat(stocks):
    """Server酱推送到微信"""
    ds = datetime.now().strftime("%Y-%m-%d %H:%M")
    top_n = 8
    if not stocks:
        title = f"⚠️ 隔夜选股 {ds} — 无候选"
        desp = "今日筛选无符合条件的股票。"
    else:
        title = f"{ds} 尾盘选股: {len(stocks)}只候选"
        lines = []
        for i, s in enumerate(stocks[:top_n]):
            chg = s.get("change_pct", 0)
            vr = s.get("vol_ratio", "?")
            turn = s.get("turnover", "?")
            mcap = s.get("mcap_billion", "?")
            ma = f"MA10={s.get('ma10','?')} MA20={s.get('ma20','?')}" if s.get("ma10") else ""
            lines.append(
                f"{i+1}. {s['name']}({s['code']}) "
                f"涨{chg}% 量比{vr} 换手{turn}% 市值{mcap}亿 {ma}"
            )
        desp = "\n".join(lines)
        if len(stocks) > top_n:
            desp += f"\n...共{len(stocks)}只, 详见脚本输出目录"
    desp += "\n\n⚠️ 非投资建议, 尾盘确认后再决策"
    # URL编码desp (Server酱用application/x-www-form-urlencoded)
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
        req = urllib.request.Request(SC_URL, data=data, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        if result.get("code") == 0:
            print("✅ 微信推送成功")
        else:
            print(f"⚠️ 微信推送失败: {result.get('message','未知错误')}")
    except Exception as e:
        print(f"⚠️ 微信推送异常: {e}")

# ================================================================
# 主流程
# ================================================================

def main():
    print("隔夜持股法改进版筛选器")
    print("="*50)

    # 1. 尝试东方财富批量
    stocks, src_label = try_eastmoney_list()
    if stocks:
        print(f"[源A] 东方财富: {len(stocks)}只 (市值/量比/换手齐全)")
    else:
        print(f"[源A] 东方财富连不上 → 切换到新浪分页")
        stocks = try_sina_paginated(max_pages=8)
        if not stocks:
            print("❌ 新浪也无数据, 终止")
            return
        print(f"[源A] 新浪涨幅榜: {len(stocks)}只 (涨幅3-5%, 缺市值/量比)")
        # 补充市值
        print("[源B] 尝试补充市值数据...")
        try_enrich_mcap(stocks)
        enriched = sum(1 for s in stocks if s.get("mcap_billion"))
        print(f"  补到 {enriched}/{len(stocks)} 只的市值")

    # 2. 初筛
    candidates = screen_initial(stocks)
    print(f"\n[初筛] {len(stocks)} → {len(candidates)} 只")
    if not candidates:
        save([]); return

    # 3. K线
    print(f"[源C] 拉取K线...")
    enrich_kline(candidates)

    # 4. 终筛
    final = final_filter(candidates)
    print_top(final)
    save(final)

    hi=sum(1 for s in final if s.get("completeness",0)>=90)
    mi=sum(1 for s in final if 70<=s.get("completeness",0)<90)
    lo=sum(1 for s in final if s.get("completeness",0)<70)
    print(f"\n📊 置信度: 🟢{hi} 🟡{mi} 🔴{lo}")

    # 5. 推送微信
    push_to_wechat(final)

if __name__=="__main__":
    main()
