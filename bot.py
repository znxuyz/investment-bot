"""
📊 投資監控 Discord 機器人
功能：
  • 每日 09:00 自動日報
  • 開盤時間（09:00~13:30）每15分鐘偵測，觸發才推播
  • 每日 14:00 收盤後更新 data.json
  • 每週一週報 / 每月1日子彈閒置提醒
  • 每30分鐘推送 data.json 到 GitHub（供網頁使用）
  • 多伺服器支援，/斜線指令
"""

import os
import json
import logging
import asyncio
import random
import requests
import base64
import pytz
import pandas as pd
import numpy as np
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ══════════════════════════════════════════
#  ⚙️  設定
# ══════════════════════════════════════════
BOT_TOKEN     = os.environ.get('BOT_TOKEN', '')
GITHUB_TOKEN  = os.environ.get('GITHUB_TOKEN', '')
GITHUB_USER   = 'znxuyz'
GITHUB_REPO   = 'investment-bot'
GITHUB_BRANCH = 'main'
DATA_FILE     = 'data.json'
CHANNELS_FILE = 'channels.json'
IDLE_FILE     = 'last_trigger.json'

TW_TZ = pytz.timezone('Asia/Taipei')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

TWSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://mis.twse.com.tw/',
}

# ══════════════════════════════════════════
#  📡  資料抓取
# ══════════════════════════════════════════
def fetch_0050_realtime():
    """TWSE 官方即時 API，盤中即時，休市用昨收"""
    try:
        url = ('https://mis.twse.com.tw/stock/api/getStockInfo.jsp'
               '?ex_ch=tse_0050.tw&json=1&delay=0')
        r = requests.get(url, headers=TWSE_HEADERS, timeout=10)
        data = r.json()
        if data.get('rtmessage') == 'OK' and data.get('msgArray'):
            item = data['msgArray'][0]
            z = item.get('z', '-')
            y = float(item.get('y', 0) or 0)
            is_open = z not in ('', '-', None)
            price = float(z) if is_open else y
            if price == 0: return None
            return {
                'price': price, 'ref': y,
                'chg': (price - y) / y * 100 if y else 0,
                'time': item.get('t', '--') if is_open else '收盤價',
                'is_open': is_open,
                'label': '盤中即時' if is_open else '最後收盤價（休市中）',
            }
    except Exception as e:
        log.warning(f'TWSE 即時 API: {e}')
    return None

def fetch_twii():
    """大盤即時"""
    try:
        url = ('https://mis.twse.com.tw/stock/api/getStockInfo.jsp'
               '?ex_ch=tse_t00.tw&json=1&delay=0')
        r = requests.get(url, headers=TWSE_HEADERS, timeout=10)
        data = r.json()
        if data.get('msgArray'):
            item = data['msgArray'][0]
            z = item.get('z', '-')
            y = float(item.get('y', 0) or 0)
            is_open = z not in ('', '-', None)
            price = float(z) if is_open else y
            return {'price': price, 'chg': (price - y) / y * 100 if y else 0}
    except Exception as e:
        log.warning(f'TWSE 大盤: {e}')
    return None

def fetch_monthly_twse(year, month):
    """抓 TWSE 單月 0050 收盤資料"""
    date_str = f"{year}{month:02d}01"
    url = (f'https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY'
           f'?stockNo=0050&date={date_str}&response=json')
    try:
        r = requests.get(url, headers=TWSE_HEADERS, timeout=10)
        if r.status_code != 200: return []
        data = r.json()
        if data.get('stat') != 'OK' or not data.get('data'): return []
        result = []
        for row in data['data']:
            close_str = row[6].replace(',', '')
            date_str  = row[0]  # 格式: 114/05/01（民國年）
            if close_str and close_str not in ('--', 'X'):
                try:
                    close = float(close_str)
                    # 民國年轉西元年
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        ad_date = f"{int(parts[0])+1911}/{parts[1]}/{parts[2]}"
                    else:
                        ad_date = date_str
                    result.append({'close': close, 'date': ad_date})
                except: pass
        return result
    except Exception as e:
        log.warning(f'TWSE 月資料 {year}/{month}: {e}')
    return []

def fetch_historical():
    """用 TWSE 官方 API 抓 0050 歷史日線（不依賴 yfinance）"""
    from datetime import datetime as dt
    now = dt.now()
    all_data = []
    for i in range(5, -1, -1):
        month = now.month - i
        year  = now.year
        while month <= 0:
            month += 12; year -= 1
        rows = fetch_monthly_twse(year, month)
        all_data.extend(rows)

    if len(all_data) < 20:
        log.warning(f'歷史資料不足: {len(all_data)}筆')
        return None

    closes = [d['close'] for d in all_data]
    dates  = [d['date']  for d in all_data]

    # 找近60日高點
    slice60 = closes[-60:] if len(closes) >= 60 else closes
    dates60 = dates[-60:]  if len(closes) >= 60 else dates
    max_idx  = slice60.index(max(slice60))
    high60   = slice60[max_idx]
    high60_date = dates60[max_idx]
    high60_days = len(slice60) - 1 - max_idx

    log.info(f'歷史資料: {len(closes)}筆，高點 {high60:.2f} ({high60_date})')
    return {
        'closes':     closes,
        'high60':     float(high60),
        'high60_date': high60_date,
        'high60_days': int(high60_days),
    }

def fetch_foreign_flow():
    """外資買賣超"""
    try:
        date_str = datetime.now(TW_TZ).strftime('%Y%m%d')
        url = (f'https://www.twse.com.tw/rwd/zh/fund/TWT38U'
               f'?date={date_str}&response=json')
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        data = r.json()
        if data.get('stat') == 'OK' and data.get('data'):
            row = data['data'][-1]
            buy = int(row[2].replace(',', ''))
            sell = int(row[3].replace(',', ''))
            return buy - sell
    except Exception as e:
        log.warning(f'外資: {e}')
    return None

# ══════════════════════════════════════════
#  📐  技術指標
# ══════════════════════════════════════════
def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    s = pd.Series(closes)
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period-1, min_periods=period).mean()
    rs = gain / loss
    return float((100 - 100 / (1 + rs)).iloc[-1])

def calc_ma(closes, period):
    if len(closes) < period: return closes[-1]
    return float(np.mean(closes[-period:]))

def calc_bias(closes, period):
    ma = calc_ma(closes, period)
    return (closes[-1] - ma) / ma * 100

def calc_macd(closes):
    s = pd.Series(closes)
    ema12 = s.ewm(span=12).mean()
    ema26 = s.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    hist = macd - signal
    return float(macd.iloc[-1]), float(signal.iloc[-1]), float(hist.iloc[-1])

def calc_all(closes):
    rsi = calc_rsi(closes)
    b20 = calc_bias(closes, 20)
    b60 = calc_bias(closes, 60)
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    _, _, hist = calc_macd(closes)
    return {'rsi': float(rsi), 'bias20': float(b20), 'bias60': float(b60),
            'ma20': float(ma20), 'ma60': float(ma60), 'macd_hist': float(hist),
            'above_ma20': bool(closes[-1] > ma20), 'above_ma60': bool(closes[-1] > ma60)}

# ══════════════════════════════════════════
#  🎯  評分與燈號
# ══════════════════════════════════════════
HIST_DATA = {
    20: {'count':5,  'rec':4,  'days':180, 'bounce':35.4, 'maxdrop':43.2},
    15: {'count':10, 'rec':7,  'days':90,  'bounce':18.6, 'maxdrop':30.2},
    8:  {'count':23, 'rec':19, 'days':60,  'bounce':11.2, 'maxdrop':18.5},
}

def convergence_score(drawdown, ind, foreign_net):
    score, signals = 0, []
    for thresh, pts in [(20,30),(15,25),(8,15),(5,8)]:
        if drawdown <= -thresh:
            score += pts; signals.append(f'回檔{drawdown:.1f}% (+{pts})'); break
    rsi, b20, mh = ind['rsi'], ind['bias20'], ind['macd_hist']
    if rsi < 30:   score+=25; signals.append(f'RSI {rsi:.1f} 超賣 (+25)')
    elif rsi < 40: score+=12; signals.append(f'RSI {rsi:.1f} 偏低 (+12)')
    if b20 < -5:   score+=20; signals.append(f'乖離率 {b20:.1f}% 大幅負乖離 (+20)')
    elif b20 < -3: score+=10; signals.append(f'乖離率 {b20:.1f}% 負乖離 (+10)')
    if mh > 0:     score+=15; signals.append('MACD底部翻正 (+15)')
    if foreign_net and foreign_net > 0: score+=10; signals.append(f'外資買超 (+10)')
    return int(min(score, 100)), signals

def get_signal(drawdown, score):
    if drawdown<=-20 and score>=55: return '🔴','回檔20%+｜動用100%子彈','多指標強力共振，全力加碼'
    if drawdown<=-15 and score>=40: return '🔴','回檔15%｜動用70%子彈', '指標共振確認，積極加碼'
    if drawdown<=-8  and score>=25: return '🟡','回檔8%｜動用30%子彈',  '初步觸發，保守加碼'
    if drawdown<=-5:                return '🟡','接近門檻｜子彈待命',    '尚未觸發，準備好等訊號'
    return                                 '🟢','正常持有｜繼續定額',    '無需動作，每月定額照常'

def historical_prob(drawdown):
    for k in [20,15,8]:
        if drawdown <= -k:
            d = HIST_DATA[k]; pct = round(d['rec']/d['count']*100)
            return {'thresh':k,'pct':pct,**d}
    return None

def predict_correction(closes, ind):
    rsi, b20, b60 = ind['rsi'], ind['bias20'], ind['bias60']
    score, signals = 0, []
    price = closes[-1]
    if rsi>75:   score+=25; signals.append(f'RSI {rsi:.1f} 嚴重過熱')
    elif rsi>70: score+=15; signals.append(f'RSI {rsi:.1f} 過熱')
    elif rsi>65: score+=8;  signals.append(f'RSI {rsi:.1f} 偏熱')
    if b20>8:    score+=20; signals.append(f'乖離率 +{b20:.1f}% 嚴重偏高')
    elif b20>5:  score+=13; signals.append(f'乖離率 +{b20:.1f}% 偏高')
    elif b20>3:  score+=6;  signals.append(f'乖離率 +{b20:.1f}% 略偏高')
    if len(closes)>=30:
        r30 = (price-closes[-30])/closes[-30]*100
        if r30>20:   score+=20; signals.append(f'近30日漲 +{r30:.1f}% 過大')
        elif r30>12: score+=12; signals.append(f'近30日漲 +{r30:.1f}% 偏大')
        elif r30>7:  score+=5;  signals.append(f'近30日漲 +{r30:.1f}%')
    if b60>10:   score+=15; signals.append(f'60日乖離 +{b60:.1f}% 嚴重過高')
    elif b60>6:  score+=8;  signals.append(f'60日乖離 +{b60:.1f}% 偏高')
    if len(closes)>=10:
        rec=closes[-10:]; up=sum(1 for i in range(1,len(rec)) if rec[i]>rec[i-1])
        if up>=8:   score+=15; signals.append(f'近10日{up}天連漲過度')
        elif up>=6: score+=7;  signals.append(f'近10日{up}天上漲')
    score = int(min(score,100))
    if score>=70:   lv,em,rng,adv='高','🔴','-12%~-20%','子彈備妥，等真實觸發立刻行動'
    elif score>=45: lv,em,rng,adv='中','🟡','-8%~-15%','留意回檔訊號，子彈先別動'
    elif score>=20: lv,em,rng,adv='低','🟢','-5%~-10%','市場尚穩，繼續定額即可'
    else:           lv,em,rng,adv='極低','🟢','-3%~-7%','無明顯回測疑慮，正常持有'
    return {'score':score,'signals':signals,'range':rng,'level':lv,'emoji':em,'advice':adv}

# ══════════════════════════════════════════
#  💰  子彈閒置
# ══════════════════════════════════════════
def load_last_trigger():
    if os.path.exists(IDLE_FILE):
        with open(IDLE_FILE) as f:
            return date.fromisoformat(json.load(f).get('date', str(date.today())))
    return date.today()

def save_last_trigger():
    with open(IDLE_FILE, 'w') as f:
        json.dump({'date': str(date.today())}, f)

def bullet_idle_status():
    last = load_last_trigger()
    diff = relativedelta(date.today(), last)
    months = diff.years * 12 + diff.months
    if months>=12: em,adv='⚠️','市場長期無大回檔，建議投入子彈的 **80%**'
    elif months>=6: em,adv='⏰','建議投入子彈的 **50%**，剩餘繼續等門檻'
    else: em,adv='🟢','繼續等待，保留子彈'
    return months, em, adv, last

# ══════════════════════════════════════════
#  📦  資料快取
# ══════════════════════════════════════════
_cache = {}
_cache_date = None

def get_cache():
    global _cache, _cache_date
    today = date.today()
    if _cache_date != today or not _cache:
        log.info('更新資料快取...')
        hist = fetch_historical()
        _cache = {'hist': hist}
        _cache_date = today
    return _cache

# ══════════════════════════════════════════
#  📤  推送 data.json 到 GitHub
# ══════════════════════════════════════════
def push_data_json(data: dict):
    """把資料推送到 GitHub repo 的 data.json"""
    if not GITHUB_TOKEN:
        log.warning('沒有 GITHUB_TOKEN，跳過推送')
        return False
    try:
        api_url = (f'https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}'
                   f'/contents/{DATA_FILE}')
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json',
        }
        # 先取得現有檔案的 SHA（更新需要）
        r = requests.get(api_url, headers=headers, timeout=10)
        sha = r.json().get('sha', '') if r.status_code == 200 else ''

        content = base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        ).decode('utf-8')

        payload = {
            'message': f'update data.json {datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")}',
            'content': content,
            'branch': GITHUB_BRANCH,
        }
        if sha: payload['sha'] = sha

        r2 = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if r2.status_code in (200, 201):
            log.info('data.json 推送成功')
            return True
        else:
            log.warning(f'data.json 推送失敗: {r2.status_code}')
            return False
    except Exception as e:
        log.warning(f'data.json 推送錯誤: {e}')
        return False

def build_data_json(rt, twii, hist_data, foreign_net, ind,
                     drawdown, score, signals, light, title, action,
                     prob, pred, idle_months, idle_emoji, idle_advice, last_trigger):
    """組合 data.json 的完整資料結構"""
    return {
        'updated': datetime.now(TW_TZ).isoformat(),
        'market_open': rt.get('is_open', False) if rt else False,
        'twii': twii if twii else {},
        'etf_0050': {
            'price': rt['price'] if rt else hist_data.get('closes', [0])[-1],
            'chg':   rt['chg']   if rt else 0,
            'label': rt['label'] if rt else '最後收盤價',
            'high60':      hist_data.get('high60', 0),
            'high60_date': hist_data.get('high60_date', '--'),
            'high60_days': hist_data.get('high60_days', 0),
            'drawdown': drawdown,
            **ind,
            'score': score, 'signals': signals,
            'light': light, 'title': title, 'action': action,
            'hist_prob': prob,
            'pred': pred,
        },

        'foreign_net': foreign_net,
        'idle': {
            'months': idle_months,
            'emoji':  idle_emoji,
            'advice': idle_advice,
            'last_date': str(last_trigger),
        },
    }

# ══════════════════════════════════════════
#  📝  DC 訊息格式
# ══════════════════════════════════════════

def fmt_daily(rt, twii, ind, drawdown, score, signals, light, title, action,
              prob, pred, foreign_net, idle_months, idle_emoji, idle_advice, now):
    price = rt['price'] if rt else 0
    chg   = rt['chg']   if rt else 0
    label = rt['label'] if rt else '--'
    prob_txt = '目前回檔未達 8% 門檻' if not prob else (
        f"歷史跌超{prob['thresh']}%共 **{prob['count']}次** ｜ "
        f"{prob['days']}日內回前高 **{prob['pct']}%** ｜ "
        f"平均反彈 +{prob['bounce']}%"
    )

    lines = [
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"📊 **每日市場快報** ｜ {now.strftime('%Y/%m/%d %H:%M')}",
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"🇹🇼 **大盤**：{twii.get('price',0):,.0f} 點  ({twii.get('chg',0):+.2f}%)" if twii else '',
        f"📈 **0050**：{price:.2f} 元  ({chg:+.2f}%) ｜ {label}",
        f"    距近期高點回檔：**{drawdown:.2f}%**",
        '',
        '**🔬 技術指標**',
        f"    RSI：{ind['rsi']:.1f}  {'⚠️ 超賣' if ind['rsi']<30 else '⚠️ 超買' if ind['rsi']>70 else '✅ 正常'}",
        f"    乖離率（20日）：{ind['bias20']:+.2f}%",
        f"    乖離率（60日）：{ind['bias60']:+.2f}%",
        f"    MACD：{'↗️ 翻正' if ind['macd_hist']>0 else '↘️ 負值'}",
        f"    MA20：{'上方✅' if ind['above_ma20'] else '下方⚠️'} ｜ MA60：{'上方✅' if ind['above_ma60'] else '下方⚠️'}",

        '',
        f"**🎯 多指標共振評分：{score}/100**",
        *([f'    • {s}' for s in signals] if signals else ['    • 無顯著訊號']),
        '',
        '**📜 歷史回檔機率**',
        f'    {prob_txt}',
        '',
        f"**{light} {title}**",
        f"    {action}",
        '',
        '**🔭 近期回測機率預測**',
        f"    {pred['emoji']} 機率：**{pred['level']}**（{pred['score']}/100）",
        f"    可能幅度：`{pred['range']}`",
        f"    建議：{pred['advice']}",
        *([f"    依據：" + ' ｜ '.join(pred['signals'])] if pred['signals'] else ['    依據：目前無過熱訊號']),
        '',
        f"**💰 子彈閒置**｜{idle_emoji} {idle_months}個月無觸發｜{idle_advice}",
        '━━━━━━━━━━━━━━━━━━━━━━━━',
    ]
    return '\n'.join(l for l in lines if l is not None)

def fmt_alert(price, drawdown, ind, score, signals, light, title, action,
              prob, high60, high60_date, high60_days, rt_time, now):
    prob_txt = '歷史機率詳見日報' if not prob else (
        f"跌超{prob['thresh']}%，{prob['days']}日內回前高 **{prob['pct']}%**"
    )
    return '\n'.join([
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"🚨 **加碼警報** ｜ {now.strftime('%H:%M')}",
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"0050：**{price:.2f} 元** （{rt_time}）",
        f"距近期高點：**{drawdown:.2f}%**",
        f"近期高點：{high60:.2f} 元｜{high60_date}｜距今 {high60_days} 個交易日",
        '',
        f"RSI：{ind['rsi']:.1f} ｜ 乖離率：{ind['bias20']:+.2f}%",
        f"**🎯 共振評分：{score}/100**",
        *([f'  • {s}' for s in signals]),
        '',
        f"📜 {prob_txt}",
        '',
        f"**{light} {title}**",
        f"    {action}",
        '━━━━━━━━━━━━━━━━━━━━━━━━',
    ])

def fmt_weekly(twii, price, drawdown, ind, score, light, title, now):
    return '\n'.join([
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"📅 **本週市場摘要** ｜ {now.strftime('%Y/%m/%d')}",
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"大盤：{twii.get('price',0):,.0f} 點" if twii else '',
        f"0050：{price:.2f} 元",
        f"距近期高點：{drawdown:.2f}%",
        f"RSI：{ind['rsi']:.1f} ｜ 乖離率(20日)：{ind['bias20']:+.2f}%",
        f"整體燈號：{light} {title}",
        f"共振評分：{score}/100",
        '',
        '**📌 本週行動清單**',
        '    □ 每月定額 0050 是否已執行？',
        '    □ 本週有觸發加碼訊號嗎？',
        '━━━━━━━━━━━━━━━━━━━━━━━━',
    ])

# ══════════════════════════════════════════
#  🔧  多伺服器頻道管理
# ══════════════════════════════════════════
def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE) as f: return json.load(f)
    return {}

def save_channels(data):
    with open(CHANNELS_FILE, 'w') as f: json.dump(data, f)

async def get_all_channels():
    data = load_channels()
    result = []
    for gid, cid in data.items():
        ch = bot.get_channel(int(cid))
        if ch: result.append(ch)
    return result

# ══════════════════════════════════════════
#  🤖  Bot
# ══════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
scheduler = AsyncIOScheduler(timezone=TW_TZ)
_last_alert_lvl = 0

# ── 排程任務 ──

async def job_push_data():
    """每30分鐘：計算所有資料並推送 data.json"""
    cache = get_cache()
    hist = cache.get('hist')
    if not hist: log.warning('歷史資料不足，跳過推送'); return

    rt      = fetch_0050_realtime()
    twii    = fetch_twii()
    foreign = fetch_foreign_flow()
    closes  = hist['closes']
    ind     = calc_all(closes)
    price   = rt['price'] if rt else closes[-1]
    drawdown = float((price - hist['high60']) / hist['high60'] * 100)

    score, signals = convergence_score(drawdown, ind, foreign)
    light, title, action = get_signal(drawdown, score)
    prob = historical_prob(drawdown)
    pred = predict_correction(closes, ind)
    idle_months, idle_emoji, idle_advice, last_trigger = bullet_idle_status()

    data = build_data_json(
        rt, twii, hist, foreign, ind,
        drawdown, score, signals, light, title, action,
        prob, pred, idle_months, idle_emoji, idle_advice, last_trigger
    )
    push_data_json(data)

async def job_daily_report():
    """每日 09:00 日報"""
    channels = await get_all_channels()
    if not channels: return

    cache = get_cache()
    hist  = cache.get('hist')
    if not hist:
        for ch in channels: await ch.send('⚠️ 今日無法取得市場資料，請稍後使用 `/check` 查詢。')
        return

    rt      = fetch_0050_realtime()
    twii    = fetch_twii()
    foreign = fetch_foreign_flow()
    closes  = hist['closes']
    ind     = calc_all(closes)
    price   = rt['price'] if rt else closes[-1]
    drawdown = float((price - hist['high60']) / hist['high60'] * 100)

    score, signals = convergence_score(drawdown, ind, foreign)
    light, title, action = get_signal(drawdown, score)
    prob = historical_prob(drawdown)
    pred = predict_correction(closes, ind)
    idle_months, idle_emoji, idle_advice, _ = bullet_idle_status()
    now  = datetime.now(TW_TZ)

    msg = fmt_daily(rt, twii, ind, drawdown, score, signals, light, title, action,
                    prob, pred, foreign, us, idle_months, idle_emoji, idle_advice, now)
    for ch in channels:
        await ch.send(msg)
        log.info(f'日報發送至 {ch.guild.name}')

    # 同步推送 data.json
    data = build_data_json(
        rt, twii, hist, foreign, ind,
        drawdown, score, signals, light, title, action,
        prob, pred, idle_months, idle_emoji, idle_advice, _
    )
    push_data_json(data)

async def job_price_check():
    """開盤時間（09:00~13:30）每15分鐘偵測，觸發才推播"""
    global _last_alert_lvl
    rt = fetch_0050_realtime()
    if not rt: return

    cache = get_cache()
    hist  = cache.get('hist')
    if not hist: return

    price    = rt['price']
    drawdown = (price - hist['high60']) / hist['high60'] * 100

    if drawdown <= -20: level = 3
    elif drawdown <= -15: level = 2
    elif drawdown <= -8:  level = 1
    else:
        _last_alert_lvl = 0
        return

    if level <= _last_alert_lvl: return
    _last_alert_lvl = level
    save_last_trigger()

    channels = await get_all_channels()
    if not channels: return

    closes  = hist['closes']
    foreign = fetch_foreign_flow()
    ind     = calc_all(closes)
    score, signals = convergence_score(drawdown, ind, foreign)
    light, title, action = get_signal(drawdown, score)
    prob = historical_prob(drawdown)
    now  = datetime.now(TW_TZ)

    msg = fmt_alert(
        price, drawdown, ind, score, signals, light, title, action,
        prob, hist['high60'], hist['high60_date'], hist['high60_days'],
        rt['time'], now
    )
    for ch in channels:
        await ch.send(msg)
    log.info(f'警報發送: {light} 回檔{drawdown:.1f}%')

    # 推送更新的 data.json
    await job_push_data()

async def job_weekly_report():
    """每週一 09:00 週報"""
    channels = await get_all_channels()
    if not channels: return

    cache = get_cache()
    hist  = cache.get('hist')
    if not hist: return

    rt      = fetch_0050_realtime()
    twii    = fetch_twii()
    closes  = hist['closes']
    ind     = calc_all(closes)
    price   = rt['price'] if rt else closes[-1]
    drawdown = (price - hist['high60']) / hist['high60'] * 100
    foreign  = fetch_foreign_flow()
    score, _ = convergence_score(drawdown, ind, foreign)
    light, title, _ = get_signal(drawdown, score)
    now = datetime.now(TW_TZ)

    msg = fmt_weekly(twii, price, drawdown, ind, score, light, title, now)
    for ch in channels: await ch.send(msg)

async def job_monthly_idle():
    """每月1日 子彈閒置提醒"""
    channels = await get_all_channels()
    if not channels: return

    idle_months, idle_emoji, idle_advice, last_trigger = bullet_idle_status()
    if idle_months < 3: return

    msg = '\n'.join([
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"{idle_emoji} **子彈閒置提醒** ｜ {date.today().strftime('%Y/%m/%d')}",
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"距上次觸發加碼門檻：**{idle_months} 個月**",
        f"（上次觸發：{last_trigger.strftime('%Y/%m/%d')}）",
        '',
        idle_advice,
        '━━━━━━━━━━━━━━━━━━━━━━━━',
    ])
    for ch in channels: await ch.send(msg)

# ── 共用回覆邏輯 ──
async def _do_check(send):
    rt    = fetch_0050_realtime()
    cache = get_cache()
    hist  = cache.get('hist')
    if not rt or not hist:
        await send('⚠️ 無法取得資料（可能是休市或網路問題），請稍後再試。')
        return
    closes   = hist['closes']
    price    = rt['price']
    drawdown = (price - hist['high60']) / hist['high60'] * 100
    foreign  = fetch_foreign_flow()
    ind      = calc_all(closes)
    score, _ = convergence_score(drawdown, ind, foreign)
    light, title, _ = get_signal(drawdown, score)
    await send('\n'.join([
        f"📈 **0050 狀況** ｜ {rt['label']}",
        f"價格：{price:.2f} 元  ({rt['chg']:+.2f}%)",
        f"距近期高點：**{drawdown:.2f}%**",
        f"近期高點：{hist['high60']:.2f} 元｜{hist['high60_date']}｜距今 {hist['high60_days']} 個交易日",
        f"RSI：{ind['rsi']:.1f} ｜ 乖離率：{ind['bias20']:+.2f}%",
        f"共振評分：{score}/100",
        f"{light} **{title}**",
    ]))

async def _do_set_channel(send, guild_id, channel_id, channel_name, guild_name):
    data = load_channels()
    data[str(guild_id)] = channel_id
    save_channels(data)
    await send(f"✅ **已設定！**\n此頻道（{channel_name}）將接收每日日報和加碼警報。\n每日 09:00 自動發送，有大跌立即推播。")
    log.info(f'頻道設定：{guild_name} #{channel_name}')

async def _do_remove_channel(send, guild_id):
    data = load_channels()
    if str(guild_id) in data:
        del data[str(guild_id)]; save_channels(data)
        await send('✅ 已取消，此伺服器不再接收日報和警報。')
    else:
        await send('⚠️ 此伺服器尚未設定頻道。')

async def _do_help(send):
    await send('\n'.join([
        '**📊 投資監控機器人 指令**',
        '`/設定頻道` — 將此頻道設為日報/警報頻道',
        '`/取消頻道` — 取消此伺服器的日報和警報',
        '`/report`   — 手動觸發今日完整日報',
        '`/check`    — 快速查看當前 0050 狀況',
        '`/說明`     — 顯示此說明',
        '',
        '**⏰ 自動排程**',
        '每日 09:00（週一至五）— 日報',
        '每週一 09:00         — 週報',
        '開盤時間 09:00~13:30  — 每15分鐘偵測（觸發才推播）',
        '每30分鐘              — 更新網頁資料',
        '每月1日               — 子彈閒置提醒',
        '',
        '**💡 新伺服器加入後**',
        '先輸入 `/設定頻道` 才會開始收到通知',
    ]))

# ── Slash 指令 ──
@bot.tree.command(name='設定頻道', description='將此頻道設為日報/警報接收頻道')
async def slash_set(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_set_channel(interaction.followup.send,
                          interaction.guild_id, interaction.channel_id,
                          interaction.channel.name, interaction.guild.name)

@bot.tree.command(name='取消頻道', description='取消此伺服器的日報和加碼警報')
async def slash_remove(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_remove_channel(interaction.followup.send, interaction.guild_id)

@bot.tree.command(name='report', description='手動觸發今日完整市場日報')
async def slash_report(interaction: discord.Interaction):
    await interaction.response.defer()
    await interaction.followup.send('⏳ 正在抓取資料...')
    await job_daily_report()

@bot.tree.command(name='check', description='快速查看當前 0050 即時狀況')
async def slash_check(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_check(interaction.followup.send)

@bot.tree.command(name='說明', description='顯示所有指令說明')
async def slash_help(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_help(interaction.followup.send)

# ── 傳統 ! 指令（相容用）──
@bot.command(name='設定頻道')
async def cmd_set(ctx):
    await _do_set_channel(ctx.send, ctx.guild.id, ctx.channel.id, ctx.channel.name, ctx.guild.name)

@bot.command(name='取消頻道')
async def cmd_remove(ctx):
    await _do_remove_channel(ctx.send, ctx.guild.id)

@bot.command(name='report')
async def cmd_report(ctx):
    await ctx.send('⏳ 正在抓取資料...')
    await job_daily_report()

@bot.command(name='check')
async def cmd_check(ctx):
    await _do_check(ctx.send)

@bot.command(name='help2')
async def cmd_help(ctx):
    await _do_help(ctx.send)

# ── 啟動 ──
@bot.event
async def on_ready():
    log.info(f'Bot 上線：{bot.user}')

    scheduler.add_job(job_daily_report,  'cron', hour=9,  minute=0, day_of_week='mon-fri')
    scheduler.add_job(job_weekly_report, 'cron', hour=9,  minute=0, day_of_week='mon')
    scheduler.add_job(job_monthly_idle,  'cron', hour=9,  minute=0, day=1)
    scheduler.add_job(job_push_data,     'interval', minutes=30)
    scheduler.add_job(job_push_data,     'cron', hour=14, minute=0, day_of_week='mon-fri')

    async def market_hour_check():
        """只在開盤時間（09:00~13:30）每15分鐘偵測一次"""
        while True:
            now_tw = datetime.now(TW_TZ)
            weekday = now_tw.weekday()  # 0=週一, 4=週五
            hour, minute = now_tw.hour, now_tw.minute
            is_market_open = (
                weekday < 5 and  # 週一到週五
                (hour == 9 and minute >= 0) or
                (10 <= hour <= 12) or
                (hour == 13 and minute <= 30)
            )
            if is_market_open:
                await job_price_check()
                await asyncio.sleep(random.randint(13*60, 17*60))
            else:
                # 非開盤時間，每10分鐘檢查一次是否到開盤時間
                await asyncio.sleep(10*60)

    asyncio.create_task(market_hour_check())
    scheduler.start()

    # 同步 slash 指令
    try:
        synced = await bot.tree.sync()
        log.info(f'已同步 {len(synced)} 個 slash 指令')
    except Exception as e:
        log.error(f'Slash 同步失敗: {e}')

    # 上線時立刻推送一次 data.json
    await asyncio.sleep(3)
    await job_push_data()

    channels = await get_all_channels()
    for ch in channels:
        await ch.send('✅ **投資監控機器人已上線！**\n輸入 `/說明` 查看指令 ｜ `/check` 查看當前狀況')
    log.info(f'已通知 {len(channels)} 個伺服器')

if __name__ == '__main__':
    bot.run(BOT_TOKEN)
