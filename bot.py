"""
📊 投資監控 Discord 機器人
功能：
  • 每日 09:00 自動日報
  • 每 13~17 分鐘靜默偵測，觸發才推播
  • 每週一週報
  • 每月第一個平日 子彈閒置提醒（≥90 天才發）
  • 盤中每5分鐘（09:05–13:30）/ 收盤後15:30 推送 data.json 到 GitHub（供網頁使用）
  • 多伺服器支援，/斜線指令
"""

import os
import json
import logging
import asyncio
import random
import time
import requests
import base64
import pytz
import pandas as pd
import numpy as np
import discord
from discord.ext import commands
from datetime import datetime, date
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
    """抓 TWSE 單月 0050 收盤資料；transient 失敗（網路錯誤、5xx）重試 2 次，backoff 0.5s/1s"""
    date_str = f"{year}{month:02d}01"
    url = (f'https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY'
           f'?stockNo=0050&date={date_str}&response=json')
    last_err = None
    for attempt in range(3):
        if attempt > 0:
            time.sleep(0.5 * (2 ** (attempt - 1)))  # 0.5s, 1s
        try:
            r = requests.get(url, headers=TWSE_HEADERS, timeout=10)
            if r.status_code >= 500:
                last_err = f'HTTP {r.status_code}'
                continue  # 5xx 重試
            if r.status_code != 200:
                return []  # 4xx 永久錯誤，不重試
            data = r.json()
            if data.get('stat') != 'OK' or not data.get('data'):
                return []
            result = []
            for row in data['data']:
                close_str = row[6].replace(',', '')
                row_date  = row[0]  # 格式: 114/05/01（民國年）
                if close_str and close_str not in ('--', 'X'):
                    try:
                        close = float(close_str)
                        # 民國年轉西元年
                        parts = row_date.split('/')
                        if len(parts) == 3:
                            ad_date = f"{int(parts[0])+1911}/{parts[1]}/{parts[2]}"
                        else:
                            ad_date = row_date
                        result.append({'close': close, 'date': ad_date})
                    except: pass
            return result
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            continue  # 網路錯誤重試
        except Exception as e:
            log.warning(f'TWSE 月資料 {year}/{month}: {e}')
            return []
    log.warning(f'TWSE 月資料 {year}/{month} 重試 3 次仍失敗: {last_err}')
    return []

def fetch_historical():
    """用 TWSE 官方 API 抓 0050 歷史日線（不依賴 yfinance）"""
    import time
    now = datetime.now(TW_TZ)  # 用台灣時區，避免 Railway(UTC) 在月底前8小時取錯月份
    all_data = []
    for i in range(5, -1, -1):
        month = now.month - i
        year  = now.year
        while month <= 0:
            month += 12; year -= 1
        rows = fetch_monthly_twse(year, month)
        all_data.extend(rows)
        if i > 0:
            time.sleep(0.5)  # 每次請求間隔 0.5s，避免 TWSE 速率限制

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

def adjust_high_for_price(hist, price):
    """即時價格超越近期高點時，臨時上調高點（僅影響當次計算，不寫快取）"""
    if hist and price > hist['high60']:
        now_str = datetime.now(TW_TZ).strftime('%Y/%m/%d')
        return {**hist, 'high60': float(price), 'high60_date': now_str, 'high60_days': 0}
    return hist

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
    macd_line, _, hist = calc_macd(closes)
    return {'rsi': float(rsi), 'bias20': float(b20), 'bias60': float(b60),
            'ma20': float(ma20), 'ma60': float(ma60), 'macd_hist': float(hist),
            'macd': float(macd_line),
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
    if mh > 0 and ind.get('macd', 0) < 0: score+=15; signals.append('MACD底部翻正 (+15)')
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
    """近期回檔機率預測（1% 間隔的線性貢獻模型）。

    每個過熱因子用 `max(0, 觀測值 − 啟動門檻) × 係數` 算出該因子的「預期回檔貢獻 %」，
    全部加總後 / 2.5 normalize 為預期回檔幅度（0~25%，1% 間隔）：

      因子           觀測值           啟動門檻   係數    上限例
      ───────        ───────         ────────  ─────   ──────
      RSI 過熱       RSI            50         0.40    RSI 75 → +10%
      20 日乖離      bias20         3 (%)      1.00    +10% bias → +10%
      30 日漲幅      r30            5 (%)      0.50    +25% 漲 → +10%
      60 日乖離      bias60         3 (%)      0.60    +15% bias → +7.2%
      連漲天數       近 10 日上漲   5 (天)     1.50    9/10 → +6%

    expected_drop = round(Σcontribs / 2.5)，0~25
    score         = clamp(expected_drop × 4, 1, 99)（1% 間隔，永遠不會 0 或 100）
    drop_low      = max(2, round(expected_drop × 0.83))   # 區間下緣，預期 × 5/6
    drop_high     = max(5, round(expected_drop × 1.25))   # 區間上緣，預期 × 5/4

    區間寬度隨 expected_drop 等比例增加（小幅預測較窄、高信心；大幅預測較寬、高不確定性），
    相鄰 expected_drop 的範圍會互相重疊：
        expected_drop=10 → -8%~-13%
        expected_drop=12 → -10%~-15%   ← 重疊區 -10%~-13%
        expected_drop=14 → -12%~-18%   ← 與上面重疊區 -12%~-15%
        expected_drop=16 → -13%~-20%   ← 與上面重疊區 -13%~-18%

    每個有貢獻的因子會在 signals 裡帶上「+X.X%」標示，使用者能直接看到誰貢獻多少。
    """
    rsi, b20, b60 = ind['rsi'], ind['bias20'], ind['bias60']
    price = closes[-1]

    r30 = ((price - closes[-30]) / closes[-30] * 100) if len(closes) >= 30 else 0.0
    if len(closes) >= 10:
        rec = closes[-10:]
        up_days = sum(1 for i in range(1, len(rec)) if rec[i] > rec[i-1])
    else:
        up_days = 0

    # (名稱, 觀測值, 啟動門檻, 係數)
    factors = [
        ('rsi',     rsi,      50.0, 0.40),
        ('bias20',  b20,       3.0, 1.00),
        ('r30',     r30,       5.0, 0.50),
        ('bias60',  b60,       3.0, 0.60),
        ('streak',  up_days,   5.0, 1.50),
    ]
    contribs = [(n, v, max(0.0, v - thr) * coef) for n, v, thr, coef in factors]

    expected_drop = max(0, min(round(sum(c[2] for c in contribs) / 2.5), 25))
    # 機率限制在 1~99：完全沒有訊號也給 1（永遠不可能 0，市場本來就有不確定性）
    # 因子全部破表也封頂 99（永遠不可能 100，避免「鐵定回檔」的誤導）
    score    = max(1, min(expected_drop * 4, 99))
    # 等比例範圍：寬度隨 expected_drop 增長，相鄰 expected_drop 的範圍會重疊
    drop_low = max(2, round(expected_drop * 0.83))
    drop_high = max(5, round(expected_drop * 1.25))
    range_str = f'-{drop_low}%~-{drop_high}%'

    signals = []
    for name, val, contrib in contribs:
        if contrib < 1.0:  # 貢獻不足 1% 不列
            continue
        c = round(contrib, 1)
        if name == 'rsi':
            desc = '嚴重過熱' if val > 75 else ('過熱' if val > 70 else ('偏熱' if val > 65 else '偏高'))
            signals.append(f'RSI {val:.1f} {desc} (+{c:.1f}%)')
        elif name == 'bias20':
            desc = '嚴重偏高' if val > 8 else ('偏高' if val > 5 else '略偏高')
            signals.append(f'乖離率 +{val:.1f}% {desc} (+{c:.1f}%)')
        elif name == 'r30':
            desc = '過大' if val > 20 else ('偏大' if val > 12 else '偏多')
            signals.append(f'近30日漲 +{val:.1f}% {desc} (+{c:.1f}%)')
        elif name == 'bias60':
            desc = '嚴重過高' if val > 10 else ('偏高' if val > 6 else '略偏高')
            signals.append(f'60日乖離 +{val:.1f}% {desc} (+{c:.1f}%)')
        elif name == 'streak':
            signals.append(f'近10日{int(val)}天上漲 (+{c:.1f}%)')

    if score >= 70:   lv, em, adv = '高',   '🔴', '子彈備妥，等真實觸發立刻行動'
    elif score >= 45: lv, em, adv = '中',   '🟡', '留意回檔訊號，子彈先別動'
    elif score >= 20: lv, em, adv = '低',   '🟢', '市場尚穩，繼續定額即可'
    else:             lv, em, adv = '極低', '🟢', '無明顯回測疑慮，正常持有'

    return {
        'score': score, 'signals': signals, 'range': range_str,
        'level': lv, 'emoji': em, 'advice': adv,
        'expected_drop': expected_drop,
        'drop_low': drop_low, 'drop_high': drop_high,
    }

# ══════════════════════════════════════════
#  💰  子彈閒置
# ══════════════════════════════════════════
def load_last_trigger():
    """優先從 GitHub 讀取，Railway 重啟後仍保留閒置計時"""
    global _last_trigger_cache
    if _last_trigger_cache:
        return _last_trigger_cache
    default = date(2026, 5, 1)
    if GITHUB_TOKEN:
        try:
            api_url = (f'https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}'
                       f'/contents/{IDLE_FILE}')
            headers = {'Authorization': f'token {GITHUB_TOKEN}',
                       'Accept': 'application/vnd.github.v3+json'}
            r = requests.get(api_url, headers=headers, timeout=10)
            if r.status_code == 200:
                content = base64.b64decode(r.json()['content']).decode('utf-8')
                result = date.fromisoformat(json.loads(content).get('date', str(default)))
                _last_trigger_cache = result
                return result
            elif r.status_code == 404:
                # 首次部署：GitHub 上尚無檔案，寫入預設日期
                save_last_trigger(default)
                return default
        except Exception as e:
            log.warning(f'讀取 last_trigger.json: {e}')
    if os.path.exists(IDLE_FILE):
        with open(IDLE_FILE) as f:
            result = date.fromisoformat(json.load(f).get('date', str(default)))
            _last_trigger_cache = result
            return result
    _last_trigger_cache = default
    return default

def save_last_trigger(trigger_date=None):
    """同時存本地與 GitHub，確保重啟後不流失"""
    global _last_trigger_cache
    if trigger_date is None:
        trigger_date = date.today()
    _last_trigger_cache = trigger_date
    payload_data = {'date': str(trigger_date)}
    with open(IDLE_FILE, 'w') as f:
        json.dump(payload_data, f)
    if not GITHUB_TOKEN:
        return
    try:
        api_url = (f'https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}'
                   f'/contents/{IDLE_FILE}')
        headers = {'Authorization': f'token {GITHUB_TOKEN}',
                   'Accept': 'application/vnd.github.v3+json'}
        r = requests.get(api_url, headers=headers, timeout=10)
        sha = r.json().get('sha', '') if r.status_code == 200 else ''
        content = base64.b64encode(
            json.dumps(payload_data, ensure_ascii=False).encode('utf-8')
        ).decode('utf-8')
        body = {'message': 'update last_trigger.json', 'content': content, 'branch': GITHUB_BRANCH}
        if sha:
            body['sha'] = sha
        requests.put(api_url, headers=headers, json=body, timeout=15)
        log.info('last_trigger.json 已同步到 GitHub')
    except Exception as e:
        log.warning(f'寫入 last_trigger.json: {e}')

def bullet_idle_status():
    """子彈閒置狀態（單位：天）。
    門檻：≥365 天 → 投 80%；≥180 天 → 投 50%；其餘繼續等。
    """
    last = load_last_trigger()
    days = max(0, (date.today() - last).days)
    if days >= 365:   em, adv = '⚠️', '市場長期無大回檔，建議投入子彈的 **80%**'
    elif days >= 180: em, adv = '⏰', '建議投入子彈的 **50%**，剩餘繼續等門檻'
    else:             em, adv = '🟢', '繼續等待，保留子彈'
    return days, em, adv, last

# ══════════════════════════════════════════
#  📦  資料快取
# ══════════════════════════════════════════
_cache = {}
_cache_date = None

def get_cache():
    global _cache, _cache_date
    today = date.today()
    # 換日或上次取得失敗（hist=None）時重新抓取，避免整天都用失敗快取
    if _cache_date != today or not _cache.get('hist'):
        log.info('更新資料快取...')
        hist = fetch_historical()
        _cache = {'hist': hist}
        _cache_date = today
    return _cache

# ══════════════════════════════════════════
#  📤  推送 data.json 到 GitHub
# ══════════════════════════════════════════
def load_data_json_from_github():
    """啟動時撈 GitHub 上既有 data.json，作為 _last_push_data 的初始 fallback。
    這樣即使本次重啟後第一次 fetch_historical 就失敗，仍能用上次成功推送的資料
    走 stale 路徑，不會讓網頁卡住整天。"""
    if not GITHUB_TOKEN:
        return None
    try:
        api_url = (f'https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}'
                   f'/contents/{DATA_FILE}')
        headers = {'Authorization': f'token {GITHUB_TOKEN}',
                   'Accept': 'application/vnd.github.v3+json'}
        r = requests.get(api_url, headers=headers, timeout=10)
        if r.status_code == 200:
            content = base64.b64decode(r.json()['content']).decode('utf-8')
            return json.loads(content)
    except Exception as e:
        log.warning(f'載入既有 data.json: {e}')
    return None

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
                     prob, pred, idle_days, idle_emoji, idle_advice, last_trigger):
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
            'days': idle_days,
            'emoji':  idle_emoji,
            'advice': idle_advice,
            'last_date': str(last_trigger),
        },
    }

# ══════════════════════════════════════════
#  📝  DC 訊息格式
# ══════════════════════════════════════════

def fmt_daily(rt, twii, ind, drawdown, score, signals, light, title, action,
              prob, pred, foreign_net, idle_days, idle_emoji, idle_advice, now):
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
        f"    RSI：{ind['rsi']:.1f}  {'✅ 超賣（買入訊號）' if ind['rsi']<30 else '⚠️ 超買' if ind['rsi']>70 else '✅ 正常'}",
        f"    乖離率（20日）：{ind['bias20']:+.2f}%",
        f"    乖離率（60日）：{ind['bias60']:+.2f}%",
        f"    MACD：{'↗️ 底部翻正' if ind['macd_hist']>0 and ind.get('macd',0)<0 else ('↗️ 正值' if ind['macd_hist']>0 else '↘️ 負值')}",
        f"    MA20：{'上方✅' if ind['above_ma20'] else '下方⚠️'} ｜ MA60：{'上方✅' if ind['above_ma60'] else '下方⚠️'}",

        '',
        f"**🎯 多指標共振評分：{score}/100**",
        *([f'    • {s}' for s in signals] if signals else ['    • 無顯著訊號']),
        '',
        '**📜 歷史回檔記錄**',
        f'    {prob_txt}',
        '',
        f"**{light} {title}**",
        f"    {action}",
        '',
        '**🔭 近期回檔機率**',
        f"    {pred['emoji']} 機率：**{pred['level']}**（{pred['score']}/100）",
        f"    可能幅度：`{pred['range']}`",
        f"    建議：{pred['advice']}",
        *([f"    依據：" + ' ｜ '.join(pred['signals'])] if pred['signals'] else ['    依據：目前無過熱訊號']),
        '',
        f"**💰 子彈閒置**｜{idle_emoji} {idle_days} 天未觸發｜{idle_advice}",
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

def fmt_close_summary(rt, twii, drawdown, light, title, action, now):
    """15:30 收盤後推播的簡短 Discord 摘要"""
    price = rt['price'] if rt else 0
    chg   = rt['chg']   if rt else 0
    lines = [
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"📊 **收盤快報** ｜ {now.strftime('%Y/%m/%d')}",
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"📈 **0050**：{price:.2f} 元  ({chg:+.2f}%)",
        (f"🇹🇼 **大盤**：{twii.get('price',0):,.0f} 點  ({twii.get('chg',0):+.2f}%)" if twii else ''),
        f"📉 距近期高點：{drawdown:.2f}%",
        f"{light} {title}",
        f"> {action}",
        '━━━━━━━━━━━━━━━━━━━━━━━━',
    ]
    return '\n'.join(l for l in lines if l)

# ══════════════════════════════════════════
#  🔧  多伺服器頻道管理
# ══════════════════════════════════════════
def load_channels():
    """從 GitHub 讀取 channels.json，Railway 重啟後仍能保留設定"""
    if not GITHUB_TOKEN:
        if os.path.exists(CHANNELS_FILE):
            with open(CHANNELS_FILE) as f: return json.load(f)
        return {}
    try:
        api_url = (f'https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}'
                   f'/contents/{CHANNELS_FILE}')
        headers = {'Authorization': f'token {GITHUB_TOKEN}',
                   'Accept': 'application/vnd.github.v3+json'}
        r = requests.get(api_url, headers=headers, timeout=10)
        if r.status_code == 200:
            content = base64.b64decode(r.json()['content']).decode('utf-8')
            return json.loads(content)
    except Exception as e:
        log.warning(f'讀取 channels.json: {e}')
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE) as f: return json.load(f)
    return {}

def save_channels(data):
    """同時存到本地和 GitHub"""
    # 存本地
    with open(CHANNELS_FILE, 'w') as f: json.dump(data, f)
    # 推到 GitHub
    if not GITHUB_TOKEN: return
    try:
        api_url = (f'https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}'
                   f'/contents/{CHANNELS_FILE}')
        headers = {'Authorization': f'token {GITHUB_TOKEN}',
                   'Accept': 'application/vnd.github.v3+json'}
        r = requests.get(api_url, headers=headers, timeout=10)
        sha = r.json().get('sha', '') if r.status_code == 200 else ''
        content = base64.b64encode(
            json.dumps(data, ensure_ascii=False).encode('utf-8')
        ).decode('utf-8')
        payload = {'message': 'update channels.json',
                   'content': content, 'branch': GITHUB_BRANCH}
        if sha: payload['sha'] = sha
        requests.put(api_url, headers=headers, json=payload, timeout=15)
        log.info('channels.json 已同步到 GitHub')
    except Exception as e:
        log.warning(f'寫入 channels.json: {e}')

async def get_all_channels():
    data = load_channels()
    result = []
    for gid, cid in data.items():
        ch = bot.get_channel(int(cid))
        if ch:
            result.append(ch)
        else:
            log.warning(f'找不到頻道 {cid}（伺服器 {gid}），可能已被移除')
    return result

# ══════════════════════════════════════════
#  🤖  Bot
# ══════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
scheduler = AsyncIOScheduler(timezone=TW_TZ)
_last_alert_lvl = 0
_initialized = False
_market_open_today = False  # 今日市場是否曾開盤（用於判斷國定假日）
_today_open_date   = None
_last_push_data    = None   # API 失敗時用來重推的上次快取
_last_trigger_cache = None  # 避免每次都打 GitHub API

# ── 排程任務 ──

async def job_push_data(is_close_push=False, force=False):
    """推送 data.json 到 GitHub。

    force=True          啟動時強制推送，不受時間限制。
    is_close_push=True  15:30 收盤推送：今天市場沒開過（國定假日）則跳過，
                        並強制刷新快取以納入今日收盤資料。
    預設（盤中）        週一~五 09:00~13:59、且 TWSE 確認市場開盤才推送。
    """
    global _market_open_today, _today_open_date
    now_tw = datetime.now(TW_TZ)
    today  = date.today()

    if _today_open_date != today:
        _market_open_today = False
        _today_open_date   = today

    if not force:
        if is_close_push:
            # 國定假日（今天沒開盤）跳過
            if now_tw.weekday() >= 5 or not _market_open_today:
                return
        else:
            # 盤中：僅週一~五 09:00~13:59
            if now_tw.weekday() >= 5 or not (9 <= now_tw.hour < 14):
                return

    if is_close_push:
        global _cache_date
        _cache_date = None  # 強制刷新，確保 15:30 能拿到今日收盤的歷史資料

    global _last_push_data

    cache = get_cache()
    hist  = cache.get('hist')
    if not hist:
        # API 取得歷史資料失敗：重推上次快取，網頁顯示警告
        if _last_push_data:
            stale = dict(_last_push_data)
            stale['stale'] = True
            stale['updated'] = datetime.now(TW_TZ).isoformat()
            push_data_json(stale)
            log.warning('歷史資料 API 失敗，重推上次快取（stale=True）')
        else:
            log.warning('歷史資料不足且無 fallback 快取，跳過推送')
        return

    rt = fetch_0050_realtime()

    if not is_close_push:
        if not rt or not rt.get('is_open'):
            # rt 失效統一處理：有 fallback 就推 stale 刷新時間戳，
            # 無論 force/cron/manual 觸發都一致行為。
            if _last_push_data:
                stale = dict(_last_push_data)
                stale['stale'] = True
                stale['updated'] = datetime.now(TW_TZ).isoformat()
                push_data_json(stale)
                ctx_parts = []
                if force: ctx_parts.append('force')
                if _market_open_today: ctx_parts.append('已開盤')
                else: ctx_parts.append('尚未確認開盤')
                log.warning(f'TWSE realtime 失效（{",".join(ctx_parts)}），重推上次快取（stale=True）')
                return
            # 沒有 fallback（首次部署、GitHub data.json 也還沒有）：
            #   force 在盤中跳過，避免用「最後收盤價」覆蓋潛在的新資料；
            #   force 在盤外（夜間 / 收盤後）繼續走完整路徑用 closes[-1] 當價格；
            #   cron 直接 return。
            if force:
                h = datetime.now(TW_TZ).hour
                m = datetime.now(TW_TZ).minute
                if h == 9 or (10 <= h <= 12) or (h == 13 and m <= 30):
                    log.info('盤中 force 推送但無 fallback 且 TWSE 尚無即時報價，跳過')
                    return
                # 非盤中：fall through，繼續執行（但不標記 _market_open_today）
            else:
                return
        else:
            _market_open_today = True  # 確認今天市場有開（is_open=True 才設定）

    twii    = fetch_twii()
    foreign = fetch_foreign_flow()
    closes  = hist['closes']
    ind     = calc_all(closes)
    price   = rt['price'] if rt else closes[-1]
    hist    = adjust_high_for_price(hist, price)
    drawdown = float((price - hist['high60']) / hist['high60'] * 100)

    score, signals = convergence_score(drawdown, ind, foreign)
    light, title, action = get_signal(drawdown, score)
    prob = historical_prob(drawdown)
    pred = predict_correction(closes, ind)
    idle_days, idle_emoji, idle_advice, last_trigger = bullet_idle_status()

    data = build_data_json(
        rt, twii, hist, foreign, ind,
        drawdown, score, signals, light, title, action,
        prob, pred, idle_days, idle_emoji, idle_advice, last_trigger
    )
    push_data_json(data)
    _last_push_data = data  # 儲存成功資料，供下次 API 失敗時 fallback

    # 收盤推送後額外發 Discord 摘要
    if is_close_push:
        channels = await get_all_channels()
        if channels:
            now = datetime.now(TW_TZ)
            msg = fmt_close_summary(rt, twii, drawdown, light, title, action, now)
            for ch in channels:
                try:
                    await ch.send(msg)
                except Exception as e:
                    log.warning(f'收盤摘要推送失敗: {e}')

async def job_daily_report(extra_send=None):
    """每日 09:00 日報。extra_send 可傳入 callback 使結果也回覆給指令觸發者。"""
    global _last_push_data
    channels = await get_all_channels()
    if not channels and not extra_send: return

    cache = get_cache()
    hist  = cache.get('hist')
    if not hist:
        # TWSE 取資料失敗：用最後一次成功的 _last_push_data 給 fallback
        if _last_push_data:
            last = _last_push_data
            try:
                last_dt = datetime.fromisoformat(last.get('updated', '')).strftime('%Y/%m/%d %H:%M')
            except Exception:
                last_dt = last.get('updated', '--') or '--'
            e_last  = last.get('etf_0050') or {}
            tw_last = last.get('twii') or {}
            lines = [
                '━━━━━━━━━━━━━━━━━━━━━━━━',
                '⚠️ **今日 TWSE 取資料失敗** ｜ 改顯示最後一次成功更新',
                '━━━━━━━━━━━━━━━━━━━━━━━━',
                f"最後更新：{last_dt}",
                f"📈 0050：{e_last.get('price', 0):.2f} 元 ({e_last.get('chg', 0):+.2f}%)",
            ]
            if tw_last.get('price'):
                lines.append(f"🇹🇼 大盤：{tw_last.get('price', 0):,.0f} 點 ({tw_last.get('chg', 0):+.2f}%)")
            lines.extend([
                f"📉 距近期高點：{e_last.get('drawdown', 0):.2f}%",
                f"🎯 共振評分：{e_last.get('score', 0)}/100",
                f"{e_last.get('light', '')} {e_last.get('title', '')}",
                '稍後請使用 `/check` 重試。網頁已同步標記為延遲。',
                '━━━━━━━━━━━━━━━━━━━━━━━━',
            ])
            msg = '\n'.join(l for l in lines if l)
            for ch in channels: await ch.send(msg)
            if extra_send: await extra_send(msg)
            # 把 stale 版本推回 GitHub，刷新 updated 讓網頁感知到延遲
            stale = dict(last)
            stale['stale'] = True
            stale['updated'] = datetime.now(TW_TZ).isoformat()
            push_data_json(stale)
        else:
            err = '⚠️ 今日無法取得市場資料，請稍後使用 `/check` 查詢。'
            for ch in channels: await ch.send(err)
            if extra_send: await extra_send(err)
        return

    rt      = fetch_0050_realtime()
    twii    = fetch_twii()
    foreign = fetch_foreign_flow()
    closes  = hist['closes']
    ind     = calc_all(closes)
    price   = rt['price'] if rt else closes[-1]
    hist    = adjust_high_for_price(hist, price)
    drawdown = float((price - hist['high60']) / hist['high60'] * 100)

    score, signals = convergence_score(drawdown, ind, foreign)
    light, title, action = get_signal(drawdown, score)
    prob = historical_prob(drawdown)
    pred = predict_correction(closes, ind)
    idle_days, idle_emoji, idle_advice, last_trigger = bullet_idle_status()
    now  = datetime.now(TW_TZ)

    msg = fmt_daily(rt, twii, ind, drawdown, score, signals, light, title, action,
                    prob, pred, foreign, idle_days, idle_emoji, idle_advice, now)
    for ch in channels:
        await ch.send(msg)
        log.info(f'日報發送至 {ch.guild.name}')
    if extra_send:
        await extra_send(msg)

    # 同步推送 data.json
    data = build_data_json(
        rt, twii, hist, foreign, ind,
        drawdown, score, signals, light, title, action,
        prob, pred, idle_days, idle_emoji, idle_advice, last_trigger
    )
    push_data_json(data)
    _last_push_data = data  # 確保 fallback 快取有最新資料

async def job_price_check():
    """每 13~17 分鐘靜默偵測"""
    global _last_alert_lvl
    rt = fetch_0050_realtime()
    if not rt: return

    cache = get_cache()
    hist  = cache.get('hist')
    if not hist: return

    price    = rt['price']
    hist     = adjust_high_for_price(hist, price)
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
    hist    = adjust_high_for_price(hist, price)
    drawdown = (price - hist['high60']) / hist['high60'] * 100
    foreign  = fetch_foreign_flow()
    score, _ = convergence_score(drawdown, ind, foreign)
    light, title, _ = get_signal(drawdown, score)
    now = datetime.now(TW_TZ)

    msg = fmt_weekly(twii, price, drawdown, ind, score, light, title, now)
    for ch in channels: await ch.send(msg)

async def job_monthly_idle():
    """每月第一個平日 子彈閒置提醒（1 號是週末則順延到下週一）"""
    now_tw = datetime.now(TW_TZ)
    # cron 已過濾 day=1~3 且為平日。再 gate 一次：只在「該月第一個平日」觸發
    #   day=1：必為平日（cron 過濾），直接執行
    #   day=2：今天必為週一（=1 號為週日）才執行
    #   day=3：今天必為週一（=1 號為週六、2 號為週日）才執行
    if now_tw.day != 1 and now_tw.weekday() != 0:
        return

    channels = await get_all_channels()
    if not channels: return

    idle_days, idle_emoji, idle_advice, last_trigger = bullet_idle_status()
    if idle_days < 90: return  # 不到 90 天不發提醒（原本「3 個月」）

    msg = '\n'.join([
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"{idle_emoji} **子彈閒置提醒** ｜ {date.today().strftime('%Y/%m/%d')}",
        '━━━━━━━━━━━━━━━━━━━━━━━━',
        f"距上次觸發加碼門檻：**{idle_days} 天**",
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
    hist     = adjust_high_for_price(hist, price)
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
        '`/report`   — 手動觸發今日完整日報（直接回覆給你）',
        '`/check`    — 快速查看當前 0050 狀況',
        '`/refresh`  — 手動推送 `data.json`，更新網頁（不發日報）',
        '`/status`   — 查看機器人運作狀態',
        '`/說明`     — 顯示此說明',
        '',
        '**⏰ 自動排程**',
        '每日 09:00（週一至五）— 日報',
        '每週一 09:00         — 週報',
        '每 13~17 分鐘         — 靜默偵測（觸發才推播）',
        '每5分鐘（盤中09:05–13:30）/ 15:30（收盤）— 更新網頁資料',
        '每月第一個平日       — 子彈閒置提醒（≥90 天才發）',
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
    await interaction.followup.send('⏳ 正在抓取資料，請稍候...')
    await job_daily_report(extra_send=interaction.followup.send)

@bot.tree.command(name='check', description='快速查看當前 0050 即時狀況')
async def slash_check(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_check(interaction.followup.send)

@bot.tree.command(name='說明', description='顯示所有指令說明')
async def slash_help(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_help(interaction.followup.send)

@bot.tree.command(name='refresh', description='手動推送 data.json（更新網頁，不發 Discord 日報）')
async def slash_refresh(interaction: discord.Interaction):
    global _cache_date
    await interaction.response.defer()
    await interaction.followup.send('⏳ 強制重抓歷史資料 + 即時報價並推送中...')
    _cache_date = None  # 強制刷新 hist 快取
    try:
        await job_push_data(force=True)
        await interaction.followup.send(
            '✅ 已嘗試推送 `data.json`。約 5–10 秒後刷新網頁即可看到最新狀態。\n'
            '若 TWSE 即時 API 失效，網頁會顯示黃色「資料延遲」警告（這是預期行為，不是錯誤）。'
        )
    except Exception as e:
        log.error(f'/refresh 推送失敗: {e}')
        await interaction.followup.send(f'❌ 推送發生錯誤：`{e}`')

@bot.tree.command(name='status', description='查看機器人運作狀態')
async def slash_status(interaction: discord.Interaction):
    await interaction.response.defer()
    now_tw = datetime.now(TW_TZ)
    channel_map = load_channels()
    msg = '\n'.join([
        '**📊 機器人狀態**',
        f"目前時間：{now_tw.strftime('%Y/%m/%d %H:%M')} (台灣)",
        f"已設定頻道：{len(channel_map)} 個伺服器",
        f"GitHub Token：{'✅ 已設定' if GITHUB_TOKEN else '❌ 未設定（網頁/頻道設定將無法持久化）'}",
        f"資料快取：{'✅ 有上次推送資料' if _last_push_data else '⚠️ 尚無快取（本次重啟後未推送過）'}",
        f"Scheduler：{'✅ 執行中' if scheduler.running else '❌ 未啟動'}",
    ])
    await interaction.followup.send(msg)

# ── 傳統 ! 指令（相容用）──
@bot.command(name='設定頻道')
async def cmd_set(ctx):
    await _do_set_channel(ctx.send, ctx.guild.id, ctx.channel.id, ctx.channel.name, ctx.guild.name)

@bot.command(name='取消頻道')
async def cmd_remove(ctx):
    await _do_remove_channel(ctx.send, ctx.guild.id)

@bot.command(name='report')
async def cmd_report(ctx):
    await ctx.send('⏳ 正在抓取資料，請稍候...')
    await job_daily_report(extra_send=ctx.send)

@bot.command(name='check')
async def cmd_check(ctx):
    await _do_check(ctx.send)

@bot.command(name='refresh')
async def cmd_refresh(ctx):
    global _cache_date
    await ctx.send('⏳ 強制重抓資料中...')
    _cache_date = None
    try:
        await job_push_data(force=True)
        await ctx.send('✅ 已推送，請刷新網頁')
    except Exception as e:
        await ctx.send(f'❌ 推送錯誤：{e}')

@bot.command(name='help2')
async def cmd_help(ctx):
    await _do_help(ctx.send)

# ── 啟動 ──
@bot.event
async def on_ready():
    global _initialized, _last_push_data
    log.info(f'Bot 上線：{bot.user}')

    # on_ready 每次重連都會觸發，用旗標確保初始化只執行一次
    if _initialized:
        log.info('重新連線，跳過重複初始化')
        return
    _initialized = True

    # 啟動時先撈 GitHub 既有 data.json，作為本次啟動的 _last_push_data fallback。
    # 重啟後若第一次 fetch_historical 失敗，仍能走 stale 路徑而非整天無資料。
    existing = load_data_json_from_github()
    if existing:
        _last_push_data = existing
        log.info(f"已載入既有 data.json 作為 fallback (updated: {existing.get('updated', '--')})")
    else:
        log.info('GitHub 上尚無 data.json 或載入失敗，無 fallback')

    scheduler.add_job(job_daily_report,  'cron', hour=9,  minute=0, day_of_week='mon-fri')
    scheduler.add_job(job_weekly_report, 'cron', hour=9,  minute=0, day_of_week='mon')
    # 月度子彈閒置提醒：每月第一個平日發（≥90 天才實際送訊息，內部 gate 控制週末順延）
    scheduler.add_job(job_monthly_idle,  'cron', hour=9,  minute=0,
                      day='1-3', day_of_week='mon-fri', id='monthly_idle')
    # 盤中每 5 分鐘更新網頁資料：09:05–12:55（每 5 分鐘）+ 13:05–13:30（市場 13:30 收盤，之後不再推）
    scheduler.add_job(job_push_data, 'cron',
                      hour='9-12', minute='5,10,15,20,25,30,35,40,45,50,55',
                      day_of_week='mon-fri', id='push_interval_am')
    scheduler.add_job(job_push_data, 'cron',
                      hour=13, minute='5,10,15,20,25,30',
                      day_of_week='mon-fri', id='push_interval_pm')
    scheduler.add_job(job_push_data, 'cron', hour=15, minute=30, day_of_week='mon-fri', id='push_close',
                      kwargs={'is_close_push': True})

    async def market_hour_check():
        """只在開盤時間（09:00~13:30）每15分鐘偵測一次"""
        while True:
            try:
                now_tw = datetime.now(TW_TZ)
                weekday = now_tw.weekday()  # 0=週一, 4=週五
                hour, minute = now_tw.hour, now_tw.minute
                is_market_open = (
                    weekday < 5 and (
                        (hour == 9) or
                        (10 <= hour <= 12) or
                        (hour == 13 and minute <= 30)
                    )
                )
                if is_market_open:
                    await job_price_check()
                    await asyncio.sleep(random.randint(13*60, 17*60))
                else:
                    # 非開盤時間，每10分鐘檢查一次是否到開盤時間
                    await asyncio.sleep(10*60)
            except asyncio.CancelledError:
                raise  # 正常結束 task，不攔截
            except Exception as e:
                log.error(f'market_hour_check 發生錯誤: {e}')
                await asyncio.sleep(60)  # 出錯後等 1 分鐘再繼續，不讓 loop 死掉

    asyncio.create_task(market_hour_check())
    scheduler.start()

    # 同步 slash 指令
    try:
        synced = await bot.tree.sync()
        log.info(f'已同步 {len(synced)} 個 slash 指令')
    except Exception as e:
        log.error(f'Slash 同步失敗: {e}')

    # 上線時推送一次 data.json，但**只在排程窗口內**才推：
    #   平日 09:05–13:30：盤中即時資料
    #   平日 15:30–15:59：收盤資料窗口
    # 其他時間（例如夜間、週末重啟）：跳過，避免出現非排程時間的更新時間戳
    await asyncio.sleep(3)
    now_tw = datetime.now(TW_TZ)
    weekday, hour, minute = now_tw.weekday(), now_tw.hour, now_tw.minute
    in_intraday = weekday < 5 and (
        (hour == 9 and minute >= 5) or
        (10 <= hour <= 12) or
        (hour == 13 and minute <= 30)
    )
    in_close = weekday < 5 and hour == 15 and minute >= 30
    if in_intraday or in_close:
        await job_push_data(force=True)
    else:
        log.info(f'啟動時間 {now_tw.strftime("%a %H:%M")} 不在排程窗口，跳過 force 推送')

    log.info('Bot 初始化完成，開始監控')

if __name__ == '__main__':
    async def run_with_retry():
        """攔截 Discord 登入 429 (Cloudflare 1015 / IP rate-limit)。

        關鍵：每次 retry 前必須 await bot.close() 把上一次失敗留下的 aiohttp
        session 關乾淨。否則漏掉的 session 仍會背景 keep-alive 打 Discord，
        被 Cloudflare 視為 IP 持續叩門 → rate limit 計時器不會減，永遠不解。
        """
        backoff = 60                # 起 1 分鐘
        max_backoff = 60 * 60       # 上限 60 分鐘
        attempt = 0
        while True:
            attempt += 1
            try:
                await bot.start(BOT_TOKEN)
                return              # bot.close() 被呼叫，正常結束
            except discord.HTTPException as e:
                if e.status == 429:
                    log.error(f'[attempt {attempt}] Discord 登入被 rate-limit '
                              f'(HTTP 429 / Cloudflare 1015)，sleep {backoff}s 後重試')
                    # 收乾淨上次的 session，避免 Unclosed client session 背景叩門
                    try:
                        await bot.close()
                    except Exception as close_err:
                        log.warning(f'bot.close() 失敗（可忽略）：{close_err}')
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                    continue
                raise
            except Exception as e:
                log.error(f'Bot 啟動例外 [{type(e).__name__}]: {e}')
                try:
                    await bot.close()
                except Exception:
                    pass
                raise
    asyncio.run(run_with_retry())