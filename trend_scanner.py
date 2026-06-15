#!/usr/bin/env python3
"""
趋势波段选股器 v3 — AkShare主数据源
Primary:  AkShare (stock_zh_a_spot_em + stock_zh_a_hist)
Fallback: 东方财富 → 新浪+腾讯
"""

import json, os, re, sys, time, urllib.request
from datetime import datetime

# ========== 配置 ==========
MIN_MCAP, MAX_MCAP = 40, 230       # 市值范围（亿）
MIN_CHG, MAX_CHG = 3.0, 5.0        # 涨跌幅
MIN_TURN, MAX_TURN = 3.0, 12.0     # 换手率
MIN_VR = 1.2                        # 最低量比
OUTPUT_DIR = os.path.expanduser("~/trend_scanner_output")
SC_KEY = os.environ.get("SC_KEY", "SCT347879TSQ0zp53ApDKc8P8jN3D6Zggx")

# ========== 工具函数 ==========
def ema(vals, n):
    k = 2/(n+1); r = [vals[0]]
    for x in vals[1:]: r.append(x*k + r[-1]*(1-k))
    return r

def pb(closes):
    """布林带 %b 序列"""
    res = [None]*20
    for i in range(20, len(closes)):
        ma = sum(closes[i-19:i+1])/20
        s = (sum((c-ma)**2 for c in closes[i-19:i+1])/20)**0.5
        u, l = ma+2*s, ma-2*s
        res.append((closes[i]-l)/(u-l) if u!=l else None)
    return res

# ========== 数据源1: AkShare (主力) ==========
def fetch_akshare():
    """AkShare: 全量A股实时行情"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        stocks = []
        for _, r in df.iterrows():
            code = str(r['代码'])
            if code.startswith(('300','301','688','920','8','4')): continue
            name = str(r['名称'])
            if 'ST' in name or name.startswith('N') or name.startswith('C'): continue
            stocks.append({
                'code': code, 'name': name,
                'price': float(r['最新价']), 'change_pct': float(r['涨跌幅']),
                'turnover': float(r['换手率']) if r['换手率'] else 0,
                'vol_ratio': float(r['量比']) if r['量比'] and r['量比']!='-' else 0,
                'mcap': float(r['总市值'])/1e8 if r['总市值'] else 0,
                'vol': float(r['成交量']),
                'src': 'akshare'
            })
        print(f"[AkShare] {len(stocks)} 主板候选")
        return stocks
    except Exception as e:
        print(f"[AkShare] 失败: {e}")
        return None

def fetch_akshare_kline(code):
    """AkShare: K线数据"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date='20260101', adjust='qfq')
        if df.empty: return None
        return {
            'closes': df['收盘'].tolist(),
            'highs': df['最高'].tolist(),
            'lows': df['最低'].tolist(),
            'vols': df['成交量'].tolist(),
        }
    except:
        return None

# ========== 数据源2: 东方财富 (备用) ==========
def fetch_eastmoney():
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get?cb=&fid=f3&po=1&pz=5000&pn=1&np=1&fltt=2&invt=2&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f2,f3,f8,f10,f12,f14,f20&ut=8b12c5e7a3e0c5a7"
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        stocks = []
        for s in data.get('data',{}).get('diff',[]):
            code = s.get('f12','')
            if code.startswith(('300','301','688','92')): continue
            name = s.get('f14','')
            if 'ST' in name or name.startswith('N'): continue
            stocks.append({
                'code': code, 'name': name,
                'price': s.get('f2',0), 'change_pct': s.get('f3',0) or 0,
                'turnover': s.get('f8',0) or 0,
                'vol_ratio': (s.get('f10',0) or 0),
                'mcap': (s.get('f20',0) or 0)/1e8,
                'src': 'eastmoney'
            })
        print(f"[东财] {len(stocks)} 主板候选")
        return stocks
    except Exception as e:
        print(f"[东财] 失败: {e}")
        return None

# ========== 数据源3: 新浪+腾讯 (兜底) ==========
def fetch_sina():
    stocks = []
    for page in range(1, 15):
        try:
            url = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=80&sort=changepercent&asc=0&node=hs_a"
            req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
            text = urllib.request.urlopen(req, timeout=10).read().decode('gbk','ignore').replace('NaN','null')
            data = json.loads(text)
            for s in data:
                code = s.get('code','')
                if code.startswith(('300','301','688','92','8','4')): continue
                name = s.get('name','')
                if 'ST' in name or name.startswith('N'): continue
                chg = s.get('changepercent',0) or 0
                if chg < 2: break
                stocks.append({
                    'code': code, 'name': name,
                    'price': s.get('trade',0), 'change_pct': chg,
                    'turnover': s.get('turnoverratio',0) or 0,
                    'src': 'sina'
                })
            last_chg = data[-1].get('changepercent',0) if data else 0
            if last_chg < 2: break
        except: break
    # 腾讯补市值+量比
    if stocks:
        enrich_tencent(stocks)
    print(f"[新浪+腾讯] {len(stocks)} 主板候选")
    return stocks

def enrich_tencent(stocks):
    for i in range(0, len(stocks), 10):
        batch = stocks[i:i+10]
        codes = ','.join([f"sh{s['code']}" if s['code'].startswith('6') else f"sz{s['code']}" for s in batch])
        try:
            url = f"https://web.sqt.gtimg.cn/q={codes}"
            req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
            data = urllib.request.urlopen(req, timeout=8).read().decode('gbk','ignore')
            for line in data.split('\n'):
                m = re.search(r'~(\d{6})~', line)
                if not m: continue
                code = m.group(1)
                f = line.split('~')
                for s in batch:
                    if s['code'] == code:
                        if len(f) > 49: s['vol_ratio'] = float(f[49]) if f[49] else 0
                        if len(f) > 45: s['mcap'] = float(f[45]) if f[45] else 0
        except: pass

def fetch_tencent_kline(code):
    try:
        prefix = 'sh' if code.startswith('6') else 'sz'
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,40,qfq"
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        d = json.loads(urllib.request.urlopen(req, timeout=8).read().decode())
        key = [k for k in d['data'] if k.startswith(prefix)][0]
        entries = [x for x in d['data'][key]['qfqday'] if isinstance(x, list)]
        return {
            'closes': [float(e[2]) for e in entries],
            'highs': [float(e[3]) for e in entries],
            'lows': [float(e[4]) for e in entries],
            'vols': [float(e[5]) for e in entries],
        }
    except: 
        return None

# ========== 初筛 ==========
def screen_initial(stocks):
    """基础条件过滤"""
    result = []
    for s in stocks:
        chg = s.get('change_pct', 0)
        turnover = s.get('turnover', 0)
        mcap = s.get('mcap', 0)
        vr = s.get('vol_ratio', 0)
        if not (MIN_CHG <= chg <= MAX_CHG): continue
        if not (MIN_TURN <= turnover <= MAX_TURN): continue
        if mcap and not (MIN_MCAP <= mcap <= MAX_MCAP): continue
        if vr and vr < MIN_VR: continue
        result.append(s)
    print(f"初筛: {len(stocks)} → {len(result)}只")
    return result

# ========== 技术分析 ==========
def analyze_kline(kl):
    """计算MA+斐波那契+量能+MACD"""
    c, h, l, v = kl['closes'], kl['highs'], kl['lows'], kl['vols']
    if len(c) < 20: return None
    cp = c[-1]
    ma10 = sum(c[-10:])/10; ma20 = sum(c[-20:])/20
    ma30 = sum(c[-30:])/30 if len(c) >= 30 else ma20
    ma_bull = ma10 > ma20 > ma30
    
    # 上升通道
    up_ch = False
    if len(c) >= 40:
        up_ch = min(l[-20:]) > min(l[:-20])*0.99 and cp > ma20 and cp > ma10
    else:
        up_ch = cp > ma20 and cp > ma10
    
    # 斐波那契(近40天)
    high_40 = max(h[-40:]) if len(h) >= 40 else max(h)
    low_40 = min(l[-40:]) if len(l) >= 40 else min(l)
    fib_range = high_40 - low_40
    fib_382 = low_40 + fib_range*0.382
    fib_500 = low_40 + fib_range*0.5
    fib_618 = low_40 + fib_range*0.618
    fib_pos = ">0.618"
    if cp < fib_382: fib_pos = "<0.382"
    elif cp < fib_500: fib_pos = "0.382-0.5"
    elif cp <= fib_618: fib_pos = "0.5-0.618"
    fib_ok = cp <= fib_618  # 不高于0.618回调位
    
    # 量能
    vol_inc = sum(v[-3:])/3 > sum(v[-8:-3])/5
    vol_ratio5 = v[-1] / (sum(v[-6:-1])/5) if len(v) >= 6 else 0
    
    # MACD
    cr = list(reversed(c))
    e12 = ema(cr, 12); e26 = ema(cr, 26)
    diff = [e12[i]-e26[i] for i in range(len(cr))]
    dea = ema(diff, 9)
    macd_bar = [(diff[i]-dea[i])*2 for i in range(len(cr))]
    macd_golden = diff[-1] > dea[-1]
    macd_above0 = diff[-1] > 0
    dea_rising = len(dea) >= 2 and dea[-1] > dea[-2]
    bar_rising = len(macd_bar) >= 2 and macd_bar[-1] > macd_bar[-2]
    
    # 二次金叉
    double_golden = False
    for i in range(-20, 0):
        if diff[i] < dea[i] and diff[i+1] >= dea[i+1]:
            for j in range(i+1, 0):
                if diff[j] > dea[j] and diff[j-1] <= dea[j-1]:
                    double_golden = True; break
            break
    
    # MACD评分
    macd_score = 0
    if macd_above0: macd_score += 2
    if macd_golden: macd_score += 2
    if dea_rising: macd_score += 2
    if bar_rising: macd_score += 1
    if double_golden: macd_score += 2
    
    return {
        'ma10': ma10, 'ma20': ma20, 'ma30': ma30, 'ma_bull': ma_bull,
        'up_channel': up_ch, 'fib_pos': fib_pos, 'fib_ok': fib_ok,
        'vol_inc': vol_inc, 'vol_ratio5': vol_ratio5,
        'macd_golden': macd_golden, 'macd_above0': macd_above0,
        'macd_score': min(macd_score, 6), 'double_golden': double_golden,
    }

# ========== 评分 ==========
def score_stock(stock, ta):
    """满分12分：数据完整2+MA多头2+上升通道2+斐波位置2+量能2+MACD共振2"""
    score = 0
    if stock.get('mcap') and stock.get('vol_ratio'): score += 2  # 数据完整
    if ta['ma_bull']: score += 2
    if ta['up_channel']: score += 2
    if ta['fib_ok']: score += 2
    if ta['vol_inc']: score += 1
    if ta['vol_ratio5'] >= 1.5: score += 1
    score += min(ta['macd_score'], 2)  # MACD贡献最多2分
    return score

# ========== K线获取 ==========
def get_kline(code):
    """AkShare优先 → 腾讯兜底"""
    kl = fetch_akshare_kline(code)
    if kl and len(kl.get('closes',[])) >= 20:
        return kl
    return fetch_tencent_kline(code)

# ========== 通知 ==========
def push_wechat(title, content):
    try:
        data = urllib.parse.urlencode({'text': title, 'desp': content}).encode()
        urllib.request.urlopen(f"https://sctapi.ftqq.com/{SC_KEY}.send", data=data, timeout=5)
    except: pass

# ========== 主流程 ==========
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 1. 获取全量数据（三级降级）
    stocks = fetch_akshare()
    if not stocks:
        stocks = fetch_eastmoney()
    if not stocks:
        stocks = fetch_sina()
    if not stocks:
        print("❌ 所有数据源失败")
        return
    
    # 2. 初筛
    candidates = screen_initial(stocks)
    
    # 3. K线+技术分析+评分
    results = []
    for i, s in enumerate(candidates):
        if i % 10 == 0: 
            print(f"  技术分析 {i+1}/{len(candidates)}...")
        kl = get_kline(s['code'])
        if not kl: continue
        ta = analyze_kline(kl)
        if not ta: continue
        
        s['ta'] = ta
        s['score'] = score_stock(s, ta)
        results.append(s)
        time.sleep(0.15)  # 防封
    
    # 4. 排序
    results.sort(key=lambda x: -x['score'])
    
    # 5. 输出
    print(f"\n{'='*70}")
    print(f"  {today} 趋势选股结果 ({len(results)}只)")
    print(f"{'='*70}")
    
    for r in results[:15]:
        ta = r['ta']
        label = '🟢' if r['score'] >= 10 else ('🟡' if r['score'] >= 7 else '⚪')
        print(f"{label} {r['code']} {r['name']:<8s} ↑{r['change_pct']:+.1f}% "
              f"得分{r['score']:>2}/12 MA多头={'✓' if ta['ma_bull'] else '✗'} "
              f"斐波={ta['fib_pos']} MACD={'金叉' if ta['macd_golden'] else '死叉'}")
    
    # 保存JSON
    out = [{'code':r['code'],'name':r['name'],'price':r['price'],
            'change_pct':r['change_pct'],'turnover':r['turnover'],
            'mcap':r['mcap'],'vol_ratio':r.get('vol_ratio',0),
            'score':r['score'],'ma_bull':r['ta']['ma_bull'],
            'fib_pos':r['ta']['fib_pos'],'macd_golden':r['ta']['macd_golden']}
           for r in results]
    with open(f"{OUTPUT_DIR}/{today}.json", 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    
    # 保存TXT
    with open(f"{OUTPUT_DIR}/{today}.txt", 'w') as f:
        for r in results[:15]:
            ta = r['ta']
            f.write(f"{r['code']} {r['name']} 得分{r['score']}/12 | "
                   f"涨{r['change_pct']:+.1f}% MA多头={'✓' if ta['ma_bull'] else '✗'} "
                   f"斐波={ta['fib_pos']} MACD={'金叉' if ta['macd_golden'] else '死叉'}\n")
    
    # 微信推送（前8只）
    push_content = '\n'.join([
        f"{r['code']} {r['name']} 得分{r['score']}/12 ↑{r['change_pct']:+.1f}%"
        for r in results[:8]
    ])
    push_wechat(f"{today} 趋势选股: {len(results)}只", push_content)
    
    # 统计
    high = sum(1 for r in results if r['score'] >= 10)
    mid = sum(1 for r in results if 7 <= r['score'] < 10)
    low = sum(1 for r in results if r['score'] < 7)
    print(f"\n🟢≥10: {high}只 | 🟡7-9: {mid}只 | ⚪<7: {low}只")
    print(f"结果已存: {OUTPUT_DIR}/{today}.json")

if __name__ == '__main__':
    main()
