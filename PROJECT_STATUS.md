# 📋 PROJECT_STATUS

> 最後更新：2026-05-05 ｜ 已合進 `main`（commit `79f2f51`）
> 涵蓋版本：`bot.py` 1184 行、`index.html` 451 行
> 監控標的：**0050（單一 ETF）**，部署：Railway + GitHub Pages

---

## 📁 一、檔案清單與實際功能

專案根目錄含 `README.md` 與 `.gitignore` 共 11 個檔案。下列 **9 個** 是實際運作的程式 / 狀態 / 部署檔案，分為三類：**程式碼**、**狀態資料**、**部署配置**。

### 1. `bot.py`（1184 行，主程式）

Discord Bot 的全部邏輯，包含 7 個區塊：

| 區塊 | 行數 | 內容 |
|------|------|------|
| 資料抓取 | 53–200 | `fetch_0050_realtime` 打 TWSE `mis.twse.com.tw` 即時 API；`fetch_twii` 抓 `tse_t00.tw` 大盤；`fetch_monthly_twse` 抓 `STOCK_DAY` 月資料、民國年→西元年轉換、**transient 失敗（5xx / ConnectionError / Timeout）重試 2 次 backoff 0.5s/1s**；`fetch_historical` 串接近 6 個月資料、找出近 60 日高點；`fetch_foreign_flow` 抓 `TWT38U` 外資買賣超 |
| 技術指標 | 205–241 | `calc_rsi`（EWM 14 期）、`calc_ma`、`calc_bias`、`calc_macd`（12/26/9）、`calc_all` 一次回傳所有指標 |
| 評分與燈號 | 252–306 | `convergence_score` 共振評分、`get_signal` 三層燈號、`historical_prob` 查歷史回檔表、`predict_correction` 過熱預測 |
| 子彈閒置 | 311–379 | `load_last_trigger` / `save_last_trigger` 同步 GitHub `last_trigger.json`、`bullet_idle_status` 計算閒置月數 |
| 資料快取 | 387–396 | `get_cache` 當日 `closes` 序列；換日或前次失敗時重抓 |
| 資料推送 | 401–488 | **`load_data_json_from_github`（啟動時從 GitHub 摹既有 data.json 當 fallback）**、`push_data_json` PUT 到 GitHub Contents API、`build_data_json` 組裝給網頁吃的 JSON |
| Discord 介面 | 494–1090 | `fmt_daily` / `fmt_alert` / `fmt_weekly` / `fmt_close_summary` 4 種訊息模板、Slash 指令、傳統 `!` 指令、頻道管理（`job_daily_report` 加上 hist 失敗 fallback：以 `_last_push_data` 推 stale + 發 Discord 延遲訊息）|
| 排程啟動 | 1093–1184 | `on_ready` 註冊 **6 個 cron job**（`daily_report` / `weekly_report` / `monthly_idle` / `push_interval_am` / `push_interval_pm` / `push_close`）+ `market_hour_check` 自訂 loop；啟動時先 `load_data_json_from_github` 預載 fallback，再依排程窗口（09:05–13:30 / 15:30–15:59）決定是否 force push |

**內部全域變數**：`_cache`（當日歷史資料）、`_cache_date`、`_last_alert_lvl`（避免同級警報重複）、`_last_push_data`（**啟動時從 GitHub 預載；後續成功推送時更新；hist 失敗時的 fallback 來源**）、`_market_open_today`（國定假日判斷）、`_last_trigger_cache`（避免每次打 GitHub）、`_initialized`（避免 `on_ready` 重複註冊排程）。

### 2. `index.html`（451 行，網頁儀表板）

純前端單頁，**無後端、無框架**，每 5 分鐘 `fetch('data.json?t=' + Date.now())` 抓 GitHub 上的 `data.json` 渲染。

- **CSS（行 7–137）**：CSS Grid + 自訂屬性（`--gold/--green/--red/--yellow`），暗色背景 + 格線浮水印，響應式斷點 500px。
- **`dataStaleness(d)`（行 184–199，新增）**：依 `data.updated` 計算過時程度。**10 分鐘 grace**：時間戳在 10 分鐘內一律視為新鮮（與 5 分鐘排程一致），不警告；超過 10 分鐘才看「在交易時間 / `stale=true` / >24 小時」三條件判斷是否警告。回傳 `{isStale, ageMin, ageLabel}`。
- **`render(d)` 函式（行 196–402）**：6 個區塊渲染：
  1. 主燈號卡（價格、回檔進度條、共振評分圓環）
  2. 技術指標（RSI / Bias20 / Bias60 / MA20 / MA60 / MACD）
  3. 近期回檔機率（過熱預測，含等級徽章與信號標籤）
  4. 子彈加碼建議（5 段位，當前段位高亮 `bact`）
  5. 子彈閒置狀況（365 天進度條，180 天黃 / 365 天紅）
  6. 歷史回檔記錄（觸發門檻時才顯示，平均反彈 / 最大繼跌雙條）
- **`isMarketHours()`（行 405–412）**：用 UTC+8 偏移判斷台灣交易時間（週一~五 09:00–13:30），決定狀態指示燈。
- **`load()`（行 415–446）**：抓 `data.json`、呼叫 `render`；用 `dataStaleness` 結果決定指示燈：過時 → 黃色 `資料延遲・X 分/小時/天前`；盤中且新鮮 → 綠色 `盤中即時`；其他 → 灰色 `休市・最後收盤`。
- **錯誤處理**：`d.stale === true` 或資料逾 30 分鐘時最上方顯示黃色警告條（內容區分為「API 暫時無法取得最新資料」或「資料已逾 X 未更新」）；`d.etf_0050.drawdown == null` 顯示「資料不完整」紅色卡。

### 3. `data.json`（網頁讀取的市場資料快照）

Bot 在排程窗口內由 `push_data_json` 寫入（盤中 09:05–13:30 每 5 分鐘 + 收盤 15:30）。重啟時若不在窗口內**不會**再寫入（避免出現非排程時間戳，例如先前的 17:07 異常更新）。

**當前 repo 內快照**（2026-05-04 17:07，是修正前最後一次 force push 留下的）：
- `etf_0050.price = 94.6`、`drawdown = 0.0`（剛突破近期高點，自動 `adjust_high_for_price` 把 high60 調為 94.6）
- `pred.score = 80`（高過熱：RSI 76.2 + Bias20 +11.3% + 30 日漲 +24.5% + Bias60 +20.3%）
- `foreign_net = null`（外資 API 抓取失敗）
- `light = 🟢`（回檔未觸發任何門檻）

**完整欄位 schema**：`updated`、`market_open`、`twii.{price,chg}`、`etf_0050.{price,chg,label,high60,high60_date,high60_days,drawdown,rsi,bias20,bias60,ma20,ma60,macd,macd_hist,above_ma20,above_ma60,score,signals[],light,title,action,hist_prob,pred}`、`foreign_net`、`idle.{months,emoji,advice,last_date}`、`stale?`（fallback 時為 true）。

> 網頁端除了讀 `stale` 旗標外，也會自行檢查 `updated` 是否逾 30 分鐘，雙重保險。

### 4. `channels.json`（多伺服器頻道對應表）

格式：`{ "<guild_id>": <channel_id> }`，每個伺服器 **只能設一個** 接收頻道。
當前內容：`{"1496435208810139729": 1499550767252635849}` — 單一伺服器、單一頻道。
透過 `/設定頻道` 寫入，`load_channels` / `save_channels` 同時存本地與 GitHub（Railway 重啟後不會掉設定）。

### 5. `last_trigger.json`（上次觸發加碼門檻的日期）

格式：`{"date": "YYYY-MM-DD"}`。當前：`{"date": "2026-05-01"}`。
`job_price_check` 偵測到回檔 ≤ -8% 並升級 `_last_alert_lvl` 時呼叫 `save_last_trigger()` 更新；`bullet_idle_status` 用 `(date.today() − last_trigger_date).days` 算閒置天數，超過 180/365 天觸發提醒。

### 6. `railway.json`（Railway 部署描述）

指定 `builder = DOCKERFILE`、`dockerfilePath = Dockerfile`、`startCommand = python bot.py`、`restartPolicyType = ON_FAILURE`、`restartPolicyMaxRetries = 10`。Railway 會優先讀這個檔案而非 `Procfile`。

### 7. `Dockerfile`（容器映像）

`python:3.12-slim` → `pip install --prefer-binary -r requirements.txt` → `CMD ["python", "bot.py"]`。
`--prefer-binary` 是為了避開 pandas/numpy 在 slim 映像上編譯失敗的問題。

### 8. `Procfile`（過渡用，與 railway.json 重複）

只有一行 `worker: python bot.py`。Railway 已設為走 Dockerfile，這個檔案實際上是 **冗餘**（Heroku 相容性遺留）。

### 9. `requirements.txt`（Python 套件鎖定）

```
discord.py==2.3.2     # Slash 指令、訊息發送
pandas==2.2.3         # RSI/MACD 的 EWM 計算
requests==2.32.3      # TWSE API + GitHub Contents API
APScheduler==3.10.4   # 6 個 cron job 排程
pytz==2024.2          # 台灣時區
python-dateutil==2.9.0.post0  # relativedelta 算月差
numpy==1.26.4         # MA 計算
```

> 注意：版本鎖死（`==`），升級需手動測。

---

## 🎯 二、選股邏輯細節

**目前不做選股，只監控 0050 一檔。** 程式中所有 `fetch_*` 都硬編碼 `0050` 與 `t00`（大盤代碼），沒有股票池或篩選器。

「選股」邏輯實際是 **「擇時加碼」邏輯**，分三層判斷：

### Layer 1 — 回檔幅度（drawdown，必要條件）

```
drawdown = (current_price - high60) / high60 × 100
high60 = 近 60 個交易日最高收盤價（fetch_historical）
```

**特例處理**：當即時價格突破 high60 時，`adjust_high_for_price` 會把 high60 即時上調為當前價（避免「破新高還顯示 -5%」的錯誤）。但這個調整 **只在當次計算用，不寫入快取**，下個交易日 `fetch_historical` 重新抓資料時才會固化。

### Layer 2 — 共振評分（score，加碼確認）

`convergence_score(drawdown, ind, foreign_net)` 是把回檔幅度 + 4 個技術指標 + 外資籌碼的訊號加總，**上限 100 分**（見第三節）。

### Layer 3 — 燈號決策（`get_signal`）

```python
if drawdown <= -20 and score >= 55:  🔴 100% 子彈
elif drawdown <= -15 and score >= 40: 🔴  70% 子彈
elif drawdown <= -8  and score >= 25: 🟡  30% 子彈
elif drawdown <= -5:                   🟡  子彈待命
else:                                  🟢  正常持有
```

**重點**：drawdown 與 score 是 **AND 條件**，回檔但沒共振訊號（例如純粹技術反彈中的下殺）不會觸發加碼。這個門檻組合是給 ETF 大盤股設計的，**不適用個股**。

### 過熱預測（`predict_correction`，反向判斷）

獨立於加碼邏輯，輸出 0~100 的「近期回檔機率分數」，給「現在該不該追高」的參考。當前 `data.json` 顯示 80 分（高過熱），但因為 drawdown = 0，主燈號仍是 🟢。

---

## ⚖️ 三、三層權重的實際配置

「三層權重」對應 **加碼子彈的階梯式投入**，由 `get_signal`（行 252–257）與 `convergence_score`（行 238–250）共同實作：

### 子彈投入階梯（決策層）

| 燈號 | drawdown 門檻 | 共振分數門檻 | 投入比例 | 對應訊息 |
|------|--------------|------------|---------|---------|
| 🔴 | ≤ −20% | ≥ 55 | **100%** | 「多指標強力共振，全力加碼」 |
| 🔴 | ≤ −15% | ≥ 40 | **70%** | 「指標共振確認，積極加碼」 |
| 🟡 | ≤ −8%  | ≥ 25 | **30%** | 「初步觸發，保守加碼」 |
| 🟡 | ≤ −5%  | （不檢查） | 0%（待命） | 「尚未觸發，準備好等訊號」 |
| 🟢 | > −5% | — | 0% | 「無需動作，每月定額照常」 |

### 共振分數的權重組成（評分層，上限 100）

| 因子 | 條件 | 加分 | 來源行 |
|------|------|------|--------|
| 回檔幅度 | ≤ −20% | +30 | 240 |
| 回檔幅度 | ≤ −15% | +25 | 240 |
| 回檔幅度 | ≤ −8%  | +15 | 240 |
| 回檔幅度 | ≤ −5%  | +8  | 240 |
| RSI | < 30（超賣）| +25 | 244 |
| RSI | < 40（偏低）| +12 | 245 |
| Bias20 | < −5%（大幅負乖離）| +20 | 246 |
| Bias20 | < −3%（負乖離）| +10 | 247 |
| MACD | hist > 0 且 macd < 0（底部翻正）| +15 | 248 |
| 外資 | 買超（net > 0）| +10 | 249 |

> 同類因子互斥（用 `if/elif`），最高分上限 `min(score, 100)`。
> 例：drawdown −22% + RSI 28 + Bias20 −6% + MACD 翻正 + 外資買超 = 30+25+20+15+10 = **100 分**。

### 子彈閒置補強（時間層）

當 drawdown 長期未觸發 −8%，`bullet_idle_status` 會反向建議投入（單位：天）：

| 閒置天數 | 建議投入 | Emoji |
|---------|---------|-------|
| ≥ 365 天 | **80%** 子彈 | ⚠️ |
| ≥ 180 天 | **50%** 子彈 | ⏰ |
| < 180 天 | 繼續等待 | 🟢 |

> 計算：`days = (date.today() − last_trigger_date).days`。每月排程的「子彈閒置提醒」要 `days ≥ 90` 才發訊息。
> 設計理由：避免「市場一直不回檔，子彈擺到貶值」。

---

## 🎯 四、評分系統的因子

完整列出 **共振評分**（買入導向，`convergence_score`）與 **過熱評分**（賣出/警示導向，`predict_correction`）的全部因子。

### A. 共振評分（買入訊號，最高 100）

| 類別 | 因子 | 計算方式 | 權重區間 |
|------|------|---------|---------|
| 價格 | drawdown | (price − high60) / high60 | 8 ~ 30 |
| 動能 | RSI(14) | EWM 平滑，`com=period-1` | 0 / 12 / 25 |
| 乖離 | Bias20 | (price − MA20) / MA20 | 0 / 10 / 20 |
| 趨勢 | MACD hist | EMA12 − EMA26，再減 signal(9) | 0 / 15（限底部翻正）|
| 籌碼 | 外資買賣超 | TWT38U `buy − sell` | 0 / 10 |

### B. 過熱評分（警示，最高 100，`predict_correction`）

**1% 間隔的線性貢獻模型**：每個因子用 `max(0, 觀測值 − 啟動門檻) × 係數` 算出該因子的「預期回檔貢獻 %」，全部加總後 `/ 2.5` normalize 為預期回檔幅度（0~25%，1% 整數）。

| 類別 | 因子 | 觀測值 | 啟動門檻 | 係數 | 上限例 |
|------|------|--------|---------|------|--------|
| 動能 | RSI | RSI(14) | 50 | 0.40 | RSI 75 → +10% |
| 乖離 | Bias20 | (price − MA20)/MA20 % | +3% | 1.00 | +13% bias → +10% |
| 漲幅 | 30 日累積漲幅 | (P − P₋₃₀)/P₋₃₀ % | +5% | 0.50 | +25% 漲 → +10% |
| 趨勢 | Bias60 | (price − MA60)/MA60 % | +3% | 0.60 | +15% bias → +7.2% |
| 連漲 | 近 10 日上漲天數 | up_days | 5 | 1.50 | 9/10 → +6% |

**關鍵公式**：
```
expected_drop = round(Σcontribs / 2.5)             # 0~25 整數，1% 間隔
score         = clamp(expected_drop × 4, 1, 99)    # 1~99 整數（永遠不 0 或 100）
drop_low      = max(2, round(expected_drop × 0.83)) # 中位數 × 5/6
drop_high     = max(5, round(expected_drop × 1.25)) # 中位數 × 5/4
range         = "-{drop_low}%~-{drop_high}%"
```

> 例：RSI=77.6 / Bias20=+10% / R30=+29% / Bias60=+20.6% → 貢獻 11.04+7.0+12.0+10.56=40.6 → expected_drop=16 → score=64 → range "-13%~-20%"。
>
> **score 限制在 [1, 99]** 的設計理由：完全無訊號時市場仍有基本不確定性，給 1 而非 0；指標全部破表時實務上也不會「鐵定回檔」，給 99 而非 100，避免使用者誤以為機率學意義上的絕對。
>
> **範圍寬度等比例縮放、且重疊**：低 expected_drop 區間窤（高信心，例：3 → `-2%~-5%`、5 → `-4%~-6%`）、高 expected_drop 區間寬（高不確定性，例：20 → `-17%~-25%`、25 → `-21%~-31%`）。相鄰 expected_drop 的範圍會互相重疊（如20 → `-17%~-25%`、16 → `-13%~-20%`，重疊區 `-17%~-20%`），符合「指標差一點、實際回檔幅度仍可能落在類似區段」的統計直觀。

過熱等級門檻：`>=70 高 🔴`、`>=45 中 🟡`、`>=20 低 🟢`、其餘極低 🟢。

`pred.signals` 每個訊號末尾會帶 `(+X.X%)` 標示該因子對 `expected_drop` 的具體貢獻，方便使用者一眼看出哪些指標主導當前的過熱判斷。

### C. 歷史回檔機率表（硬編碼，行 232–236）

```python
HIST_DATA = {
    20: {count:5,  rec:4,  days:180, bounce:35.4, maxdrop:43.2},
    15: {count:10, rec:7,  days:90,  bounce:18.6, maxdrop:30.2},
    8:  {count:23, rec:19, days:60,  bounce:11.2, maxdrop:18.5},
}
```

`historical_prob` 依 drawdown 分檔回查，產出「歷史 X 次跌超 Y%、Z 日內回前高 N%」的訊息。
**這份表是靜態的**，不會隨時間自動更新。

---

## 📌 五、待辦清單

按優先級排序，標註 **影響範圍**。

### ✅ 已修（本批次）

- [x] **`_last_push_data` 重啟即遺失**：on_ready 改為先從 GitHub 摹現有 `data.json` 灌入，重啟後第一次 TWSE 失敗仍有 fallback 可用。`load_data_json_from_github` (`bot.py`)。
- [x] **`fetch_monthly_twse` 無重試**：加入 2 次 backoff (0.5s/1s) 重試，5xx 與 ConnectionError/Timeout 會重試，4xx 直接返回。
- [x] **網頁誤標「盤中即時」**：新增 `dataStaleness()`，`updated > 30 分鐘` 或 `stale=true` 時改顯示黃色「資料延遲・X 分/小時/天前」。
- [x] **啟動 force push 無視排程時間**：on_ready 啟動推送加排程窗口闘門（09:05–13:30 / 15:30–15:59 才推），17:07/夜間/週末重啟不再推送。
- [x] **日報 hist 失敗時整個 abort**：改為使用 `_last_push_data` 組 fallback 訊息發 Discord，並把 stale 版本推回 GitHub 讓網頁同步刷新狀態。
- [x] **TWSE realtime 整個早上壞掉時 cron silent skip**（Fix F）：`job_push_data` 原本在「rt 失效 + `_market_open_today=False`」時走 `else: return` 直接跳過，導致 09:05–13:30 期間每 5 分鐘的 cron 全部沒推、`data.json` 卡在 09:00 的 daily_report fallback。修正為：只要有 `_last_push_data`，不論今天是否確認過開盤都推 stale 並刷新時間戳。
- [x] **網頁非交易時間誤報「資料延遲」**（Fix G）：原本 `ageMin > 30` 一律標 stale，導致 14:00–15:30（13:30 收盤後到 15:30 close push 之間）會錯誤亮黃燈。修正為：只在「**交易時間內**（週一~五 09:00–13:30）資料 > 30 分鐘」或「資料 > 24 小時」或「`stale=true`」三種情況才標延遲。週末看週五 15:30 收盤、凌晨看昨天 15:30 等情境正確顯示灰色「休市・最後收盤」。
- [x] **`stale=true` 但時間戳剛刷新還是亮黃**：原本只要 `data.stale === true` 就亮黃，導致 Bot 每 5 分鐘推一次 stale 也每次都亮黃，使用者誤以為東西壞了。改為 **10 分鐘 grace**：時間戳 ≤10 分鐘一律視為新鮮，不警告（與 5 分鐘排程一致）；>10 分鐘才看「交易時間 / `stale=true` / >24 小時」判定。Bot 剛推完就看的場景現在會顯示綠燈而非黃色警告。
- [x] **`predict_correction` 改成 1% 間隔線性貢獻模型**：原本是 `score>=70 → -12%~-20%` 這種 4 檔粗略區間，無法區分「RSI 70」與「RSI 90」的差別。改為每個因子用 `max(0, val − threshold) × coef` 算出 1% 整數的回檔貢獻，加總 / 2.5 normalize 為 expected_drop（0~25%），score = expected_drop × 4，range = expected_drop ± 3~4。`signals` 末尾每項加上 `(+X.X%)` 標示因子貢獻量。新增 `pred.expected_drop / drop_low / drop_high` 三個欄位給網頁顯示中位數預測。
- [x] **子彈閒置單位改為天**：`bullet_idle_status` 從 `relativedelta` 月差改成 `(today − last).days`，門檻從 6/12 個月改為 180/365 天，月度提醒從「3 個月以上」改為「90 天以上」。data.json 的 `idle.months` 改為 `idle.days`；網頁進度條以 365 天為 100%、180 天黃 / 365 天紅。網頁端保留向後相容（舊 `months` × 30 估算成天）。

### 🔴 高優先（影響功能正確性）

- [ ] **外資 API 失效**：當前 `data.json` 的 `foreign_net = null`，`fetch_foreign_flow` 抓 `TWT38U` 沒成功。需驗證是 TWSE URL 變動、headers 不足、還是 IP 被擋。影響：共振分數固定少 10 分上限。
- [ ] **HIST_DATA 永遠不更新**：歷史回檔次數寫死在 `bot.py:246`，不會隨真實新事件累加。需要：（a）長期持久化新觸發事件、（b）排程每年重新統計。
- [ ] **`get_cache` 一天只刷一次歷史**：`_cache_date` 比對日期，整個交易日 `closes` 不會變。但盤中 `job_push_data` 拿不到當日盤中價來計算 MA/RSI（用前一日收盤序列 + 即時價拼湊）。確認 `calc_all` 的輸入是否符合預期。
- [ ] **`adjust_high_for_price` 不寫入快取**：突破新高的當下，下個 5 分鐘排程仍用舊 high60 重新計算 drawdown，看起來會「跳動」。考慮把臨時新高寫回 `_cache`。

### 🟡 中優先（穩定性與維護性）

- [ ] **`Procfile` 與 `railway.json` 冗餘**：Railway 走 Dockerfile，`worker: python bot.py` 從未被使用。建議刪除以避免新人混淆。
- [ ] **`cmd_help` 註冊為 `help2`**：因為 discord.py 內建 `help` 已存在，目前用 `!help2` 觸發。Slash 版的 `/說明` 沒問題，但 `!` 版本對使用者不直覺。
- [ ] **`channels.json` 一伺服器只能一頻道**：`save_channels` 用 `data[str(guild_id)] = channel_id` 直接覆寫。如果使用者想同時推到「市場頻道」+「警報頻道」需擴充為 list。
- [ ] **`_last_alert_lvl` 重啟後重置**：Railway 重啟後變 0，當天若已推過 −8% 警報，重啟後再達 −8% 會 **再推一次**。應與 `last_trigger.json` 一樣持久化。
- [ ] **`market_hour_check` 與 cron `push_interval_*` 重疊**：盤中 09:05~13:30 有兩條路徑（`market_hour_check` loop 與 cron）都會觸發資料抓取，造成 TWSE API 多餘請求。確認是否需合併。

### 🟢 低優先（功能擴充）

- [ ] **多標的支援**：目前硬編碼 0050。若要加 0056、00878、006208，需把 `fetch_*`、`HIST_DATA`、`channels.json` 改為以股票代碼為 key 的字典。
- [ ] **回測模組**：HIST_DATA 是手動統計，可寫一個 `backtest.py` 跑近 10 年資料驗證三層門檻的勝率。
- [ ] **網頁 PWA / 推播**：目前 `index.html` 只能網頁刷新，可加 Service Worker 做離線快取與瀏覽器通知。
- [ ] **`/settings` 指令**：讓伺服器管理員自訂閒置提醒月數、共振門檻等，存到 `channels.json` 的 value 物件。
- [ ] **單元測試**：`calc_rsi` / `convergence_score` / `get_signal` 都是純函式，補幾個 pytest case 防止重構回歸。
- [ ] **錯誤通知**：TWSE API 連續 N 次失敗時，應主動推一則訊息到頻道，而非只寫 log。
- [ ] **GitHub Actions CI**：requirements 鎖死版本，建議加個 weekly job 跑 `pip install` + `python -c "import bot"` 確保套件還能裝起來。

### 📝 文件待補

- [ ] CHANGELOG.md：目前完全沒有版本記錄。
- [ ] 環境變數完整列表（`BOT_TOKEN`、`GITHUB_TOKEN`、`GITHUB_USER`、`GITHUB_REPO`、`GITHUB_BRANCH`），README 只提到前兩個。
- [ ] data.json schema 文件：前端與 Bot 兩邊各自維護欄位，需要單一 source of truth。

---

## 🔗 相關連結

- 主程式入口：`bot.py:1184-1185`（`if __name__ == '__main__': bot.run(BOT_TOKEN)`）
- 排程註冊：`bot.py:1113-1126`（6 個 cron job）
- 啟動 force push 排程闘門：`bot.py:1168-1180`
- 燈號決策：`bot.py:266-271`
- 共振評分：`bot.py:252-264`
- 過熱預測：`bot.py:280-306`
- 日報 fallback 路徑：`bot.py:791-829`
- TWSE 月資料重試：`bot.py:96-138`
- GitHub data.json 預載：`bot.py:401-418`
- 網頁過時偵測：`index.html:184-194`
- 網頁渲染主體：`index.html:196-402`
