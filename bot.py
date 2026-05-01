"""
📊 投資監控 Discord 機器人 v3
"""
import os
import json
from datetime import date
from dateutil.relativedelta import relativedelta
import discord
from discord.ext import commands
import pandas as pd
import requests
import asyncio
import random
import logging
import json
import os
from datetime import datetime
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── 設定 ──
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # 從 Railway Variables 讀取
TW_TZ     = pytz.timezone('Asia/Taipei')
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# ── Bot 初始化 ──
intents                 = discord.Intents.default()
intents.message_content = True
bot                     = commands.Bot(command_prefix='!', intents=intents)
scheduler               = AsyncIOScheduler(timezone=TW_TZ)

# ── 快取與狀態 ──
_hist_cache      = {}
_hist_cache_date = None
_last_alert_lvl  = 0
CHANNELS_FILE    = 'channels.json'

TWSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://mis.twse.com.tw/',
}

# ══════════════════════════════════════════
#  頻道管理
# ══════════════════════════════════════════
def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE) as f:
            return json.load(f)
    return {}

def save_channels(data):
    with open(CHANNELS_FILE, 'w') as f:
        json.dump(data, f)

async def get_all_channels():
    data   = load_channels()
    result = []
    for gid, cid in data.items():
        ch = bot.get_channel(int(cid))
        if ch:
            result.append(ch)
    return result

# ══════════════════════════════════════════
#  資料抓取
# ══════════════════════════════════════════
def fetch_0050_realtime():
    """
    抓取 0050 即時價格。
    盤中：回傳即時成交價。
    休市/收盤：TWSE 的 z 欄位為 '-'，改用 y（昨收）當作最後收盤價顯示。
    """
    url = ("https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
           "?ex_ch=tse_0050.tw&json=1&delay=0")
    try:
        r    = requests.get(url, headers=TWSE_HEADERS, timeout=10)
        data = r.json()
        if data.get('rtmessage') == 'OK' and data.get('msgArray'):
            item    = data['msgArray'][0]
            z       = item.get('z', '-')   # 最新成交價（盤中有值，休市為 '-'）
            y       = item.get('y', '0')   # 昨收價（永遠有值）
            is_open = z not in ('', '-', None)  # 是否有盤中成交

            price = float(z if is_open else y)
            ref   = float(y or 0)
            if price == 0:
                return None

            return {
                'price':   price,
                'ref':     ref,
                'chg':     (price - ref) / ref * 100 if ref else 0,
                'time':    item.get('t', '--') if is_open else '收盤價',
                'is_open': is_open,   # True=盤中, False=休市/收盤
            }
    except Exception as e:
        log.warning(f"TWSE API: {e}")
    return None

def fetch_twse_history(stock_no, months=6):
    """用 TWSE 官方 API 抓歷史日K，回傳 closes 和 dates 列表"""
    closes, dates = [], []
    now = datetime.now(TW_TZ)
    for i in range(months-1, -1, -1):
        d = datetime(now.year, now.month, 1, tzinfo=TW_TZ)
        # 往回推 i 個月
        m = d.month - i
        y = d.year
        while m <= 0:
            m += 12; y -= 1
        date_str = f"{y}{str(m).padStart if False else str(m).zfill(2)}01"
        url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
               f"?stockNo={stock_no}&date={date_str}&response=json")
        try:
            r = requests.get(url, headers=TWSE_HEADERS, timeout=10)
            data = r.json()
            if data.get('stat') == 'OK' and data.get('data'):
                for row in data['data']:
                    try:
                        c = float(row[6].replace(',', ''))
                        if c > 0:
                            closes.append(c)
                            dates.append(row[0])  # 民國年日期
                    except:
                        pass
        except Exception as e:
            log.warning(f"TWSE 歷史 {stock_no} {date_str}: {e}")
    return closes, dates

def twse_date_to_str(twse_date):
    """民國年轉西元年，例如 114/05/01 → 2025/05/01"""
    try:
        parts = twse_date.split('/')
        if len(parts) == 3:
            return f"{int(parts[0])+1911}/{parts[1]}/{parts[2]}"
    except:
        pass
    return twse_date

def fetch_historical_data():
    """用 TWSE 官方 API 抓 0050 歷史資料"""
    result = {}
    closes, dates = fetch_twse_history('0050', months=6)
    if closes:
        price  = closes[-1]
        prev   = closes[-2] if len(closes) > 1 else closes[-1]
        slice60= closes[-60:] if len(closes) >= 60 else closes
        d60    = dates[-60:] if len(dates) >= 60 else dates
        high60 = max(slice60)
        hi_idx = slice60.index(high60)
        hi_date= twse_date_to_str(d60[hi_idx]) if d60 else '--'
        hi_days= len(slice60) - 1 - hi_idx
        import pandas as pd
        close_series = pd.Series(closes, dtype=float)
        result['0050'] = {
            'price':       price,
            'prev':        prev,
            'chg':         (price - prev) / prev * 100,
            'high60':      high60,
            'high60_date': hi_date,
            'high60_days': hi_days,
            'drawdown':    (price - high60) / high60 * 100,
            'close':       close_series,
        }
        log.info(f"0050 歷史資料: {len(closes)}筆, 最新={price:.2f}")
    return result

def fetch_foreign_flow():
    try:
        date_str = datetime.now(TW_TZ).strftime('%Y%m%d')
        url  = (f"https://www.twse.com.tw/rwd/zh/fund/TWT38U"
                f"?date={date_str}&response=json")
        r    = requests.get(url, headers=TWSE_HEADERS, timeout=10)
        data = r.json()
        if data.get('stat') == 'OK' and data.get('data'):
            row  = data['data'][-1]
            buy  = int(row[2].replace(',', ''))
            sell = int(row[3].replace(',', ''))
            return buy - sell
    except Exception as e:
        log.warning(f"外資資料: {e}")
    return None

def get_hist_cached():
    global _hist_cache, _hist_cache_date
    today = datetime.now(TW_TZ).date()
    if _hist_cache_date != today or not _hist_cache:
        log.info("更新歷史資料快取...")
        _hist_cache      = fetch_historical_data()
        _hist_cache_date = today
    return _hist_cache

# ══════════════════════════════════════════
#  技術指標
# ══════════════════════════════════════════
def calc_rsi(s, period=14):
    d  = s.diff()
    ag = d.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    al = (-d.clip(upper=0)).ewm(com=period-1, min_periods=period).mean()
    return float((100 - 100 / (1 + ag / al)).iloc[-1])

def calc_bias(s, ma=20):
    mv = s.rolling(ma).mean().iloc[-1]
    return float((s.iloc[-1] - mv) / mv * 100)

def calc_macd(s):
    m   = s.ewm(span=12).mean() - s.ewm(span=26).mean()
    sig = m.ewm(span=9).mean()
    return float(m.iloc[-1]), float(sig.iloc[-1]), float((m - sig).iloc[-1])

def calc_indicators(close):
    rsi        = calc_rsi(close)
    bias20     = calc_bias(close, 20)
    bias60     = calc_bias(close, 60)
    macd, sig, hist = calc_macd(close)
    ma20       = float(close.rolling(20).mean().iloc[-1])
    ma60       = float(close.rolling(60).mean().iloc[-1])
    price      = float(close.iloc[-1])
    return {
        'RSI': rsi, 'BIAS20': bias20, 'BIAS60': bias60,
        'MACD': macd, 'MACDsig': sig, 'MACDhist': hist,
        'MA20': ma20, 'MA60': ma60,
        'above_MA20': price > ma20, 'above_MA60': price > ma60,
    }

# ══════════════════════════════════════════
#  評分與燈號
# ══════════════════════════════════════════
def convergence_score(drawdown, ind, foreign_net):
    score, signals = 0, []
    for thresh, pts in [(20, 30), (15, 25), (8, 15), (5, 8)]:
        if drawdown <= -thresh:
            score += pts
            signals.append(f"回檔 {drawdown:.1f}% 觸發 {thresh}% 門檻 (+{pts})")
            break
    if ind['RSI'] < 30:
        score += 25; signals.append(f"RSI {ind['RSI']:.1f} 超賣 (+25)")
    elif ind['RSI'] < 40:
        score += 12; signals.append(f"RSI {ind['RSI']:.1f} 偏低 (+12)")
    if ind['BIAS20'] < -5:
        score += 20; signals.append(f"乖離率 {ind['BIAS20']:.1f}% 大幅負乖離 (+20)")
    elif ind['BIAS20'] < -3:
        score += 10; signals.append(f"乖離率 {ind['BIAS20']:.1f}% 負乖離 (+10)")
    if ind['MACDhist'] > 0 and ind['MACD'] < 0:
        score += 15; signals.append("MACD 底部翻正 (+15)")
    if foreign_net is not None and foreign_net > 0:
        score += 10; signals.append(f"外資買超 {foreign_net:,} 張 (+10)")
    return min(score, 100), signals

def get_signal(drawdown, score):
    if drawdown <= -20 and score >= 55:
        return "🔴", "回檔20%+｜動用100%子彈", "多指標強力共振，全力加碼"
    elif drawdown <= -15 and score >= 40:
        return "🔴", "回檔15%｜動用70%子彈",   "指標共振確認，積極加碼"
    elif drawdown <= -8 and score >= 25:
        return "🟡", "回檔8%｜動用30%子彈",    "初步觸發，保守加碼，其餘子彈保留"
    elif drawdown <= -5:
        return "🟡", "接近門檻｜子彈待命",      "尚未觸發，準備好等訊號"
    else:
        return "🟢", "正常持有｜繼續定額",      "無需動作，每月定額照常執行"

HIST = {
    20: {'count': 5,  'rec': 4,  'days': 180, 'bounce': 35.4, 'maxdrop': 43.2},
    15: {'count': 10, 'rec': 7,  'days': 90,  'bounce': 18.6, 'maxdrop': 30.2},
    8:  {'count': 23, 'rec': 19, 'days': 60,  'bounce': 11.2, 'maxdrop': 18.5},
}

# ── GitHub 資料推送 ──
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "znxuyz/investment-bot")
GITHUB_FILE  = "data.json"

def push_to_github(data: dict) -> bool:
    """把計算好的市場資料推到 GitHub data.json"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.warning("GITHUB_TOKEN 或 GITHUB_REPO 未設定，跳過推送")
        return False
    try:
        import base64
        content = json.dumps(data, ensure_ascii=False, indent=2)
        encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }
        r = requests.get(api_url, headers=headers, timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload = {
            "message": f"data: {datetime.now(TW_TZ).strftime('%Y/%m/%d %H:%M')}",
            "content": encoded,
        }
        if sha:
            payload["sha"] = sha
        r2 = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if r2.status_code in (200, 201):
            log.info("data.json 推送成功")
            return True
        log.warning(f"推送失敗: {r2.status_code}")
        return False
    except Exception as e:
        log.warning(f"GitHub 推送錯誤: {e}")
        return False

def build_data_json(hist, rt, ind, score, signals, light, title, action,
                    pred, stocks_data, idle_months, idle_emoji,
                    idle_advice, last_trigger_str, foreign_net, now):
    """組裝 data.json"""
    p0   = hist.get('0050', {})
    twii = hist.get('大盤', {})
    spy  = hist.get('SPY',  {})
    qqq  = hist.get('QQQ',  {})
    vix  = hist.get('VIX',  {})
    sox  = hist.get('費半', {})
    price= rt['price'] if rt else p0.get('price', 0)
    chg  = rt['chg']   if rt else p0.get('chg', 0)
    return {
        "updated_at":  now.strftime('%Y/%m/%d %H:%M'),
        "market_open": bool(rt.get('is_open', False)) if rt else False,
        "0050": {
            "price":       round(float(price), 2),
            "chg":         round(float(chg), 2),
            "high60":      round(float(p0.get('high60', 0)), 2),
            "high60_date": p0.get('high60_date', '--'),
            "high60_days": int(p0.get('high60_days', 0)),
            "drawdown":    round(float(p0.get('drawdown', 0)), 2),
        },
        "twii": {
            "price": round(float(twii.get('price', 0)), 0),
            "chg":   round(float(twii.get('chg', 0)), 2),
        },
        "indicators": {
            "RSI":        round(float(ind['RSI']), 1),
            "BIAS20":     round(float(ind['BIAS20']), 2),
            "BIAS60":     round(float(ind['BIAS60']), 2),
            "MACDhist":   round(float(ind['MACDhist']), 4),
            "above_MA20": bool(ind['above_MA20']),
            "above_MA60": bool(ind['above_MA60']),
            "MA20":       round(float(ind['MA20']), 2),
            "MA60":       round(float(ind['MA60']), 2),
        },
        "convergence": {"score": int(score), "signals": signals},
        "signal":      {"light": light, "title": title, "action": action},
        "prediction": {
            "score":   int(pred['score']),
            "level":   pred['level'],
            "emoji":   pred['emoji'],
            "range":   pred['range'],
            "advice":  pred['advice'],
            "signals": pred['signals'],
        },
        "stocks": {
            code: {
                "name":        sd['name'],
                "price":       round(float(sd['price']), 2),
                "high60":      round(float(sd['high60']), 2),
                "high60_date": sd['high60_date'],
                "high60_days": int(sd['high60_days']),
                "drawdown":    round(float(sd['drawdown']), 2),
            }
            for code, sd in stocks_data.items()
        },
        "idle": {
            "months":       int(idle_months),
            "emoji":        idle_emoji,
            "advice":       idle_advice,
            "last_trigger": last_trigger_str,
        },
        "us_market": {
            name: {"price": round(float(d.get('price',0)),2), "chg": round(float(d.get('chg',0)),2)}
            for name, d in [("SPY",spy),("QQQ",qqq),("VIX",vix),("費半",sox)] if d
        },
        "foreign_net": int(foreign_net) if foreign_net is not None else None,
    }

async def job_push_data():
    """每15分鐘推一次市場資料到 GitHub data.json"""
    try:
        hist    = get_hist_cached()
        rt      = fetch_0050_realtime()
        foreign = fetch_foreign_flow()
        if '0050' not in hist:
            return
        p0        = hist['0050']
        ind       = calc_indicators(p0['close'])
        actual_dd = (rt['price']-p0['high60'])/p0['high60']*100 if rt else p0['drawdown']
        score, signals = convergence_score(actual_dd, ind, foreign)
        light, title, action = get_signal(actual_dd, score)
        pred = predict_correction(list(p0['close']), ind['RSI'], ind['BIAS20'], ind['BIAS60'])
        stocks_data = {}
        for st in WATCH_STOCKS:
            sd = fetch_stock_data(st['code'])
            if sd:
                stocks_data[st['code']] = {**st, **sd}
        idle_months, idle_emoji, idle_advice, last_trigger = bullet_idle_status()
        now  = datetime.now(TW_TZ)
        data = build_data_json(
            hist, rt, ind, score, signals, light, title, action,
            pred, stocks_data, idle_months, idle_emoji,
            idle_advice, str(last_trigger), foreign, now
        )
        push_to_github(data)
        log.info(f"data.json 推送完成")
    except Exception as e:
        log.warning(f"job_push_data 錯誤: {e}")

# ── 個股追蹤 ──
WATCH_STOCKS = [
    {'name': '台積電', 'code': '2330'},
    {'name': '聯發科', 'code': '2454'},
    {'name': '日月光', 'code': '3711'},
]

def stock_signal(drawdown):
    if drawdown <= -30: return '🔴', '清倉警示'
    if drawdown <= -25: return '🟠', '高度警戒'
    if drawdown <= -15: return '🟡', '注意觀察'
    return '🟢', '正常'

def fetch_stock_data(code):
    """用 TWSE 官方 API 抓個股歷史資料"""
    try:
        closes, dates = fetch_twse_history(code, months=3)
        if not closes:
            return None
        price  = closes[-1]
        slice_ = closes[-60:] if len(closes) >= 60 else closes
        d_     = dates[-60:]  if len(dates)  >= 60 else dates
        high60 = max(slice_)
        hi_idx = slice_.index(high60)
        hi_date= twse_date_to_str(d_[hi_idx]) if d_ else '--'
        hi_days= len(slice_) - 1 - hi_idx
        return {
            'price':       price,
            'high60':      high60,
            'high60_date': hi_date,
            'high60_days': hi_days,
            'drawdown':    (price - high60) / high60 * 100,
            'closes':      closes,
        }
    except Exception as e:
        log.warning(f"個股 {code}: {e}")
        return None

def max_drawdown_since(closes, days_back=120):
    slice_ = closes[-days_back:] if len(closes)>=days_back else closes
    peak = slice_[0]; max_dd = 0.0
    for c in slice_:
        if c>peak: peak=c
        dd=(c-peak)/peak*100
        if dd<max_dd: max_dd=dd
    return max_dd

# ── 子彈閒置追蹤 ──
IDLE_FILE = 'last_trigger.json'

def load_last_trigger():
    if os.path.exists(IDLE_FILE):
        with open(IDLE_FILE) as f:
            data = json.load(f)
            return date.fromisoformat(data.get('date', str(date.today())))
    return date.today()

def save_last_trigger():
    with open(IDLE_FILE,'w') as f:
        json.dump({'date':str(date.today())},f)

def bullet_idle_status():
    last = load_last_trigger()
    now  = date.today()
    diff = relativedelta(now, last)
    months = diff.years*12 + diff.months
    if months>=12: emoji,advice='⚠️',f'市場長期無大回檔，建議投入子彈的 **80%**'
    elif months>=6: emoji,advice='⏰',f'建議投入子彈的 **50%**，剩餘繼續等門檻'
    else: emoji,advice='🟢','繼續等待，保留子彈'
    return months, emoji, advice, last

def predict_correction(closes, rsi, bias20, bias60):
    """根據技術指標預測近期回測機率"""
    score = 0
    signals = []
    price = closes[-1]

    # RSI 過熱
    if rsi > 75:   score += 25; signals.append(f"RSI {rsi:.1f} 嚴重過熱")
    elif rsi > 70: score += 15; signals.append(f"RSI {rsi:.1f} 過熱")
    elif rsi > 65: score += 8;  signals.append(f"RSI {rsi:.1f} 偏熱")

    # 乖離率過高
    if bias20 > 8:   score += 20; signals.append(f"乖離率 +{bias20:.1f}% 嚴重偏高")
    elif bias20 > 5: score += 13; signals.append(f"乖離率 +{bias20:.1f}% 偏高")
    elif bias20 > 3: score += 6;  signals.append(f"乖離率 +{bias20:.1f}% 略偏高")

    # 近30日漲幅
    if len(closes) >= 30:
        rise30 = (price - closes[-30]) / closes[-30] * 100
        if rise30 > 20:   score += 20; signals.append(f"近30日漲 +{rise30:.1f}% 過大")
        elif rise30 > 12: score += 12; signals.append(f"近30日漲 +{rise30:.1f}% 偏大")
        elif rise30 > 7:  score += 5;  signals.append(f"近30日漲 +{rise30:.1f}%")

    # 60日乖離
    if bias60 > 10:  score += 15; signals.append(f"60日乖離 +{bias60:.1f}% 嚴重過高")
    elif bias60 > 6: score += 8;  signals.append(f"60日乖離 +{bias60:.1f}% 偏高")

    # 近10日連漲
    if len(closes) >= 10:
        recent = closes[-10:]
        up_days = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1])
        if up_days >= 8:   score += 15; signals.append(f"近10日 {up_days} 天連漲過度")
        elif up_days >= 6: score += 7;  signals.append(f"近10日 {up_days} 天上漲")

    score = min(score, 100)

    if score >= 70:   level, emoji, rng, advice = '高',  '🔴', '-12%~-20%', '子彈備妥，等真實觸發立刻行動'
    elif score >= 45: level, emoji, rng, advice = '中',  '🟡', '-8%~-15%',  '留意回檔訊號，子彈先別動'
    elif score >= 20: level, emoji, rng, advice = '低',  '🟢', '-5%~-10%',  '市場尚穩，繼續定額即可'
    else:             level, emoji, rng, advice = '極低','🟢', '-3%~-7%',   '無明顯回測疑慮，正常持有'

    return {'score': score, 'signals': signals, 'range': rng,
            'level': level, 'emoji': emoji, 'advice': advice}


def historical_prob(drawdown):
    for k in [20, 15, 8]:
        if drawdown <= -k:
            d   = HIST[k]
            pct = d['rec'] / d['count'] * 100
            return (f"歷史跌超{k}%共 **{d['count']}次** ｜ "
                    f"{d['days']}日內回前高：{d['rec']}/{d['count']}次 "
                    f"(**{pct:.0f}%**) ｜ "
                    f"平均反彈：+{d['bounce']}% ｜ 最大繼跌：-{d['maxdrop']}%")
    return "目前回檔未達 8% 門檻，持續觀察"

def us_market_comment(spy_chg, qqq_chg, sox_chg, vix_val):
    lines = []
    if spy_chg <= -2:
        lines.append(f"⚠️ 美股昨大跌 {spy_chg:.1f}%，台股今日可能承壓")
    elif spy_chg >= 2:
        lines.append(f"✅ 美股昨大漲 {spy_chg:.1f}%，台股今日偏多")
    else:
        lines.append(f"😐 美股昨小幅 {spy_chg:+.1f}%，影響有限")
    if sox_chg <= -3:
        lines.append(f"⚠️ 費半昨跌 {sox_chg:.1f}%，台積電/聯發科留意")
    elif sox_chg >= 3:
        lines.append(f"✅ 費半昨漲 {sox_chg:.1f}%，半導體族群偏多")
    if vix_val > 30:
        lines.append(f"😱 VIX {vix_val:.1f}｜極度恐慌，往往是好買點")
    elif vix_val > 20:
        lines.append(f"⚠️ VIX {vix_val:.1f}｜市場情緒緊張")
    else:
        lines.append(f"😊 VIX {vix_val:.1f}｜市場情緒穩定")
    return "\n".join(f"    {l}" for l in lines)

# ══════════════════════════════════════════
#  訊息格式
# ══════════════════════════════════════════
def fmt_daily(hist_data, rt, ind, score, signals,
              light, title, action, hist_prob, foreign_net, now, pred=None):
    twii = hist_data.get('大盤', {})
    spy  = hist_data.get('SPY',  {})
    sox  = hist_data.get('費半', {})
    vix  = hist_data.get('VIX',  {})
    price    = rt['price'] if rt else hist_data.get('0050', {}).get('price', 0)
    chg      = rt['chg']   if rt else hist_data.get('0050', {}).get('chg', 0)
    rt_time  = f"（{rt['time']}）" if rt else "（收盤後）"
    drawdown = hist_data.get('0050', {}).get('drawdown', 0)
    us = us_market_comment(spy.get('chg',0), 0, sox.get('chg',0), vix.get('price',16))
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 **每日市場快報** ｜ {now.strftime('%Y/%m/%d %H:%M')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🇹🇼 **大盤**：{twii.get('price',0):,.0f} 點  ({twii.get('chg',0):+.2f}%)",
        f"📈 **0050**：{price:.2f} 元  ({chg:+.2f}%) {rt_time}",
        f"    距近期高點回檔：**{drawdown:.2f}%**",
        f"    近期高點：{hist_data.get('0050',{}).get('high60',0):.2f} 元｜{hist_data.get('0050',{}).get('high60_date','--')}｜距今 {hist_data.get('0050',{}).get('high60_days',0)} 個交易日",
        "",
        "**🔬 技術指標**",
        f"    RSI：{ind['RSI']:.1f}  {'⚠️ 超賣' if ind['RSI']<30 else '⚠️ 超買' if ind['RSI']>70 else '✅ 正常'}",
        f"    乖離率（20日）：{ind['BIAS20']:+.2f}%",
        f"    乖離率（60日）：{ind['BIAS60']:+.2f}%",
        f"    MACD 柱狀：{ind['MACDhist']:+.4f}  {'↗️ 翻正' if ind['MACDhist']>0 else '↘️ 負值'}",
        f"    均線：MA20 {'上方✅' if ind['above_MA20'] else '下方⚠️'} ｜ MA60 {'上方✅' if ind['above_MA60'] else '下方⚠️'}",
        "",
        "**🌏 美股→台股預判**",
        us,
        f"    外資買賣超：{f'{foreign_net:+,} 張' if foreign_net is not None else '待更新'}",
        "",
        f"**🎯 多指標共振評分：{score}/100**",
        *(["    • " + s for s in signals] if signals else ["    • 無顯著訊號"]),
        "",
        "**📜 歷史回檔機率**",
        f"    {hist_prob}",
        "",
        f"**{light} {title}**",
        f"    {action}",
        "",
        "**🔭 近期回測機率預測**",
    ]
    if pred:
        lines += [
            f"    {pred['emoji']} 機率：**{pred['level']}**（{pred['score']}/100）",
            f"    可能幅度：`{pred['range']}`",
            f"    建議：{pred['advice']}",
        ]
        if pred['signals']:
            lines.append("    依據：" + " ｜ ".join(pred['signals']))
        else:
            lines.append("    依據：目前無過熱訊號，市場相對健康")
        lines.append("    ⚠️ 統計機率，不保證發生。仍需等真實觸發門檻才行動。")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)

def fmt_alert(price, drawdown, ind, score, signals,
              light, title, action, hist_prob, rt_time, now):
    return "\n".join([
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🚨 **加碼警報** ｜ {now.strftime('%H:%M')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"0050：**{price:.2f} 元** （{rt_time}）",
        f"距近期高點：**{drawdown:.2f}%**",
        f"（詳細高點資訊請見每日日報）",
        "",
        f"RSI：{ind['RSI']:.1f} ｜ 乖離率：{ind['BIAS20']:+.2f}%",
        f"MACD：{'↗️ 翻正' if ind['MACDhist']>0 else '↘️ 負值'}",
        "",
        f"**🎯 共振評分：{score}/100**",
        *(["  • " + s for s in signals]),
        "",
        f"**📜 {hist_prob}**",
        "",
        f"**{light} {title}**",
        f"    {action}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ])

def fmt_weekly(hist_data, ind, score, light, title, now):
    p0   = hist_data.get('0050', {})
    twii = hist_data.get('大盤', {})
    return "\n".join([
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 **本週市場摘要** ｜ {now.strftime('%Y/%m/%d')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"大盤：{twii.get('price',0):,.0f} 點",
        f"0050：{p0.get('price',0):.2f} 元",
        f"距近期高點：{p0.get('drawdown',0):.2f}%",
        f"RSI：{ind['RSI']:.1f} ｜ 乖離率(20日)：{ind['BIAS20']:+.2f}%",
        f"整體燈號：{light} {title}",
        f"共振評分：{score}/100",
        "",
        "**📌 本週行動清單**",
        "    □ 每月定額 0050 是否已執行？",
        "    □ 本週有觸發加碼訊號嗎？",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ])

# ══════════════════════════════════════════
#  排程任務
# ══════════════════════════════════════════
async def job_daily_report():
    channels = await get_all_channels()
    if not channels:
        log.warning("尚未有任何伺服器設定頻道")
        return
    now     = datetime.now(TW_TZ)
    hist    = get_hist_cached()
    rt      = fetch_0050_realtime()
    foreign = fetch_foreign_flow()
    if '0050' not in hist:
        for ch in channels:
            await ch.send("⚠️ 今日無法取得市場資料，請稍後使用 `/check`。")
        return
    p0        = hist['0050']
    ind       = calc_indicators(p0['close'])
    actual_dd = (rt['price'] - p0['high60']) / p0['high60'] * 100 if rt else p0['drawdown']
    score, signals = convergence_score(actual_dd, ind, foreign)
    light, title, action = get_signal(actual_dd, score)
    hist_prob = historical_prob(actual_dd)
    closes_list = list(p0['close'])
    pred = predict_correction(closes_list, ind['RSI'], ind['BIAS20'], ind['BIAS60'])
    msg = fmt_daily(hist, rt, ind, score, signals, light, title, action, hist_prob, foreign, now, pred)

    # 個股監控
    stock_lines = ["", "**📌 個股持倉監控**"]
    alert_msgs  = []
    for st in WATCH_STOCKS:
        sd = fetch_stock_data(st['code'])
        if sd:
            ls, ll = stock_signal(sd['drawdown'])
            stock_lines.append(f"{st['name']} {st['code']}｜{sd['price']:.2f} 元｜{sd['drawdown']:.2f}%｜{ls} {ll}")
            if sd['drawdown'] <= -25:
                alert_msgs.append("\n".join([
                    "━━━━━━━━━━━━━━━━━━━━━━━━",
                    f"{'🚨' if sd['drawdown']<=-30 else '⚠️'} **個股{ll}** ｜ {now.strftime('%H:%M')}",
                    "━━━━━━━━━━━━━━━━━━━━━━━━",
                    f"**{st['name']} {st['code']}** 現價：{sd['price']:.2f} 元",
                    f"距近期高點：**{sd['drawdown']:.2f}%**",
                    f"近期高點：{sd['high60']:.2f} 元｜{sd['high60_date']}｜距今 {sd['high60_days']} 個交易日",
                    f"{ls} **{ll}**｜個股無自動汰換機制，建議重新評估基本面",
                    "━━━━━━━━━━━━━━━━━━━━━━━━",
                ]))
        else:
            stock_lines.append(f"{st['name']} {st['code']}｜資料取得失敗")

    # 子彈閒置
    idle_months, idle_emoji, idle_advice, _ = bullet_idle_status()
    idle_line = f"\n**💰 子彈閒置**｜{idle_emoji} {idle_months}個月無觸發｜{idle_advice}"
    full_msg = msg + "\n".join(stock_lines) + idle_line + "\n━━━━━━━━━━━━━━━━━━━━━━━━"

    # 推送 data.json 到 GitHub
    try:
        stocks_dict = {}
        for st in WATCH_STOCKS:
            sd = fetch_stock_data(st['code'])
            if sd:
                stocks_dict[st['code']] = {**st, **sd}
        idle_m, idle_e, idle_a, last_t = bullet_idle_status()
        data_json = build_data_json(
            hist, rt, ind, score, signals, light, title, action,
            pred, stocks_dict, idle_m, idle_e, idle_a, str(last_t), foreign, now
        )
        push_to_github(data_json)
    except Exception as e:
        log.warning(f"日報推送 data.json 失敗: {e}")

    for ch in channels:
        if alert_msgs:
            for am in alert_msgs:
                await ch.send(am)
        await ch.send(full_msg)
        log.info(f"日報發送至 {ch.guild.name}")

async def job_price_check():
    global _last_alert_lvl
    rt = fetch_0050_realtime()
    if not rt:
        return
    hist = get_hist_cached()
    if '0050' not in hist:
        return
    high60    = hist['0050']['high60']
    actual_dd = (rt['price'] - high60) / high60 * 100
    if actual_dd <= -20:
        level = 3
    elif actual_dd <= -15:
        level = 2
    elif actual_dd <= -8:
        level = 1
    else:
        _last_alert_lvl = 0
        return
    if level <= _last_alert_lvl:
        return
    _last_alert_lvl = level
    save_last_trigger()
    channels = await get_all_channels()
    if not channels:
        return
    p0      = hist['0050']
    ind     = calc_indicators(p0['close'])
    foreign = fetch_foreign_flow()
    score, signals = convergence_score(actual_dd, ind, foreign)
    light, title, action = get_signal(actual_dd, score)
    hist_prob = historical_prob(actual_dd)
    now = datetime.now(TW_TZ)
    msg = fmt_alert(rt['price'], actual_dd, ind, score, signals,
                    light, title, action, hist_prob, rt['time'], now)
    for ch in channels:
        await ch.send(msg)
    log.info(f"警報發送: {light} {title} 回檔{actual_dd:.1f}%")

async def job_weekly_report():
    channels = await get_all_channels()
    if not channels:
        return
    now  = datetime.now(TW_TZ)
    hist = get_hist_cached()
    if '0050' not in hist:
        return
    p0      = hist['0050']
    ind     = calc_indicators(p0['close'])
    foreign = fetch_foreign_flow()
    score, _ = convergence_score(p0['drawdown'], ind, foreign)
    light, title, _ = get_signal(p0['drawdown'], score)
    msg = fmt_weekly(hist, ind, score, light, title, now)
    for ch in channels:
        await ch.send(msg)
    log.info(f"週報發送至 {len(channels)} 個伺服器")

# ══════════════════════════════════════════
#  共用回覆邏輯
# ══════════════════════════════════════════
async def _do_set_channel(send, guild_id, channel_id, channel_name, guild_name):
    data = load_channels()
    data[str(guild_id)] = channel_id
    save_channels(data)
    await send(
        f"✅ **已設定！**\n"
        f"此頻道（{channel_name}）將接收每日日報和加碼警報。\n"
        f"每日 09:00 自動發送，有大跌立即推播。"
    )
    log.info(f"伺服器 {guild_name} 設定頻道: {channel_name}")

async def _do_remove_channel(send, guild_id):
    data = load_channels()
    if str(guild_id) in data:
        del data[str(guild_id)]
        save_channels(data)
        await send("✅ 已取消，此伺服器不再接收日報和警報。")
    else:
        await send("⚠️ 此伺服器尚未設定頻道。")

async def _do_check(send):
    rt   = fetch_0050_realtime()
    hist = get_hist_cached()
    if not rt or '0050' not in hist:
        await send("⚠️ 無法取得資料，請稍後再試。")
        return
    high60    = hist['0050']['high60']
    actual_dd = (rt['price'] - high60) / high60 * 100
    ind       = calc_indicators(hist['0050']['close'])
    score, _  = convergence_score(actual_dd, ind, None)
    light, title, _ = get_signal(actual_dd, score)
    is_open   = rt.get('is_open', True)
    status    = "盤中即時" if is_open else "最後收盤價（休市中）"
    await send("\n".join([
        f"📈 **0050 狀況** ｜ {status}",
        f"價格：{rt['price']:.2f} 元  ({rt['chg']:+.2f}%)",
        f"距近期高點：**{actual_dd:.2f}%**",
        f"近期高點：{hist.get('0050',{}).get('high60',0):.2f} 元｜{hist.get('0050',{}).get('high60_date','--')}｜距今 {hist.get('0050',{}).get('high60_days',0)} 個交易日",
        f"RSI：{ind['RSI']:.1f} ｜ 乖離率：{ind['BIAS20']:+.2f}%",
        f"共振評分：{score}/100",
        f"{light} **{title}**",
    ]))

async def _do_help(send):
    await send("\n".join([
        "**📊 投資監控機器人 指令**",
        "`/設定頻道` — 將此頻道設為日報/警報接收頻道",
        "`/取消頻道` — 取消此伺服器的日報和警報",
        "`/report`   — 手動觸發今日完整日報",
        "`/check`    — 快速查看當前 0050 狀況",
        "`/說明`     — 顯示此說明",
        "",
        "**⏰ 自動排程**",
        "每日 09:00（週一至五） — 日報",
        "每週一 09:00           — 週報",
        "每 13~17 分鐘          — 靜默偵測（觸發才推播）",
    ]))

# ══════════════════════════════════════════
#  Slash 指令（/ 開頭）
# ══════════════════════════════════════════
@bot.tree.command(name="設定頻道", description="將此頻道設為每日日報和加碼警報的接收頻道")
async def slash_set(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_set_channel(
        interaction.followup.send,
        interaction.guild_id, interaction.channel_id,
        interaction.channel.name, interaction.guild.name
    )

@bot.tree.command(name="取消頻道", description="取消此伺服器的日報和加碼警報")
async def slash_remove(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_remove_channel(interaction.followup.send, interaction.guild_id)

@bot.tree.command(name="report", description="手動觸發今日完整市場日報")
async def slash_report(interaction: discord.Interaction):
    await interaction.response.defer()
    await interaction.followup.send("⏳ 正在抓取資料...")
    await job_daily_report()
    await job_push_data()

@bot.tree.command(name="check", description="快速查看當前 0050 即時狀況")
async def slash_check(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_check(interaction.followup.send)

@bot.tree.command(name="個股", description="查看台積電、聯發科、日月光即時持倉狀況")
async def slash_stocks(interaction: discord.Interaction):
    await interaction.response.defer()
    lines = ["**📌 個股持倉監控**", ""]
    for st in WATCH_STOCKS:
        sd = fetch_stock_data(st['code'])
        if sd:
            ls, ll = stock_signal(sd['drawdown'])
            lines += [
                f"**{st['name']} {st['code']}**",
                f"現價：{sd['price']:.2f} 元",
                f"距高點：{sd['drawdown']:.2f}%｜高點 {sd['high60']:.2f}（{sd['high60_date']}，{sd['high60_days']}日前）",
                f"狀態：{ls} {ll}", "",
            ]
        else:
            lines += [f"**{st['name']}**：無法取得資料", ""]
    await interaction.followup.send("\n".join(lines))

@bot.command(name='個股')
async def cmd_stocks(ctx):
    lines = ["**📌 個股持倉監控**", ""]
    for st in WATCH_STOCKS:
        sd = fetch_stock_data(st['code'])
        if sd:
            ls, ll = stock_signal(sd['drawdown'])
            lines += [f"**{st['name']} {st['code']}** {sd['price']:.2f}元｜{sd['drawdown']:.2f}%｜{ls} {ll}", ""]
        else:
            lines += [f"**{st['name']}**：無法取得資料", ""]
    await ctx.send("\n".join(lines))

@bot.tree.command(name="說明", description="顯示所有指令說明")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_help(interaction.followup.send)

# ══════════════════════════════════════════
#  啟動
# ══════════════════════════════════════════
@bot.event
async def job_monthly_idle_check():
    channels = await get_all_channels()
    if not channels: return
    idle_months, idle_emoji, idle_advice, last_trigger = bullet_idle_status()
    if idle_months < 3: return
    hist = get_hist_cached()
    max_dd_txt = '--'
    if '0050' in hist:
        closes = list(hist['0050']['close'])
        max_dd = max_drawdown_since(closes, idle_months*21)
        max_dd_txt = f'{max_dd:.2f}%'
    msg = "\n".join([
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{idle_emoji} **子彈閒置提醒** ｜ {date.today().strftime('%Y/%m/%d')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"距上次觸發加碼門檻：**{idle_months} 個月**",
        f"（上次觸發：{last_trigger.strftime('%Y/%m/%d')}）",
        f"這段期間 0050 最大回檔：{max_dd_txt}",
        "",
        idle_advice,
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ])
    for ch in channels:
        await ch.send(msg)
    log.info(f"每月閒置提醒：{idle_months}個月")

async def on_ready():
    log.info(f"Bot 上線：{bot.user}")

    scheduler.add_job(job_daily_report,  'cron', hour=9, minute=0, day_of_week='mon-fri')
    scheduler.add_job(job_weekly_report, 'cron', hour=9, minute=0, day_of_week='mon')
    scheduler.add_job(job_monthly_idle_check, 'cron', hour=9, minute=0, day=1)

    async def random_check():
        while True:
            await asyncio.sleep(random.randint(13*60, 17*60))
            await job_price_check()
            await job_push_data()  # 同時推資料到 GitHub

    asyncio.create_task(random_check())
    scheduler.start()

    try:
        synced = await bot.tree.sync()
        log.info(f"已同步 {len(synced)} 個 slash 指令")
    except Exception as e:
        log.error(f"Slash 同步失敗: {e}")

    channels = await get_all_channels()
    for ch in channels:
        await ch.send("✅ **投資監控機器人已上線！**\n輸入 `/說明` 查看指令")
    log.info(f"已通知 {len(channels)} 個伺服器，Bot 運行中")

if __name__ == '__main__':
    bot.run(BOT_TOKEN)