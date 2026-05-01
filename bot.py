"""
📊 投資監控 Discord 機器人 v3
"""
import discord
from discord.ext import commands
import yfinance as yf
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
BOT_TOKEN = "MTQ5OTU0ODg5NTM0MzE1MzI2NA.GZ2F2V.B5Bs_mJ8VYn-B2n9uizveIzVlNvGbb5j-QFSpo"
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

def fetch_historical_data():
    symbols = {
        '0050': '0050.TW', '大盤': '^TWII',
        'SPY': 'SPY', 'QQQ': 'QQQ',
        'VIX': '^VIX', '費半': '^SOX',
    }
    result = {}
    for name, ticker in symbols.items():
        try:
            df = yf.download(ticker, period='6mo', interval='1d',
                             progress=False, auto_adjust=True)
            if not df.empty:
                p    = float(df['Close'].iloc[-1])
                prev = float(df['Close'].iloc[-2])
                h60  = float(df['Close'].rolling(60).max().iloc[-1])
                result[name] = {
                    'price':    p,
                    'prev':     prev,
                    'chg':      (p - prev) / prev * 100,
                    'high60':   h60,
                    'drawdown': (p - h60) / h60 * 100,
                    'close':    df['Close'],
                }
        except Exception as e:
            log.warning(f"yfinance {name}: {e}")
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
              light, title, action, hist_prob, foreign_net, now):
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
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)

def fmt_alert(price, drawdown, ind, score, signals,
              light, title, action, hist_prob, rt_time, now):
    return "\n".join([
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🚨 **加碼警報** ｜ {now.strftime('%H:%M')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"0050：**{price:.2f} 元** （{rt_time}）",
        f"距近期高點：**{drawdown:.2f}%**",
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
    msg = fmt_daily(hist, rt, ind, score, signals, light, title, action, hist_prob, foreign, now)
    for ch in channels:
        await ch.send(msg)
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

@bot.tree.command(name="check", description="快速查看當前 0050 即時狀況")
async def slash_check(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_check(interaction.followup.send)

@bot.tree.command(name="說明", description="顯示所有指令說明")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.defer()
    await _do_help(interaction.followup.send)

# ══════════════════════════════════════════
#  啟動
# ══════════════════════════════════════════
@bot.event
async def on_ready():
    log.info(f"Bot 上線：{bot.user}")

    scheduler.add_job(job_daily_report,  'cron', hour=9, minute=0, day_of_week='mon-fri')
    scheduler.add_job(job_weekly_report, 'cron', hour=9, minute=0, day_of_week='mon')

    async def random_check():
        while True:
            await asyncio.sleep(random.randint(13*60, 17*60))
            await job_price_check()

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