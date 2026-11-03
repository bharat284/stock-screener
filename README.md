# Daily Stock Screener: Documentation

A personal screener for NSE stocks. Every morning it scans a list of stocks, scores each one on
**trend, momentum, fresh catalysts and fundamentals**, and shows:

- **3 Large-cap, 3 Mid-cap and 3 Small-cap picks** expected to rise
- **Top 3 updates that may spike a stock**: fresh order wins, promoter buying, corporate actions, deals
- For every stock: a **1-year price chart with a 6-month moving average** and every event marked on it
  (past and upcoming), a full event timeline and a score breakdown

> ⚠️ **Research aid only, not investment advice.** Scores are rule-based. Momentum and news-driven
> moves can reverse quickly. Always do your own research and use stop-losses.

---

## 1. Using the app

### Tabs
| Tab | What it does |
|---|---|
| **Dashboard** | Stock cards are on the left: Large/Mid/Small picks, top catalysts and your watchlist. Click any card to see the expanded analysis on the right. |
| **Analyze** | Search for any stock. In the desktop app this searches **every listed company on screener.in** and analyses it on demand (~10 s). In the shared web version it searches the stocks in today's screen. Filter by cap, sorted by score. |
| **Watchlist** | Your starred stocks, opened the same way as any other card. |
| **Documentation** | This page. |

### Watchlist
Tap **☆** on any card, or **☆ Add to watchlist** in the expanded view.
- **Desktop app:** saved to `watchlist.json` and **included in every daily 8 AM run**, even stocks that aren't in `universe.txt`.
- **Shared web version:** saved in your own browser only (each friend has their own).

### Refresh button
- **Desktop app:** re-fetches everything from screener.in and the web (≈10 minutes for ~100 stocks; progress is shown in the top bar).
- **Shared web version:** loads the most recent published data.

### Expanded view
- **KPIs:** price, 1M/3M return, 6-month MA, 50-DMA, RSI, 52-week high, market cap, P/E, ROCE, promoter holding.
- **Chart:** price (solid), **6-month MA** (dashed, 126 trading days), 50-DMA (thin), plus event markers. Hover a marker to read the event. Upcoming events extend the chart to the right.
- **Events list:** upcoming first, then the last 12 months. Filter chips by type; ↗ opens the filing or news article.
- **Score breakdown:** every point added or removed, with the reason.

### Event markers
| Marker | Meaning | Source |
|---|---|---|
| ▲ Order wins | Orders, contracts, LoA/LoI, work orders. Value is parsed where stated. | screener.in filings; news (only when the company is the *receiver*) |
| ◆ Shareholding | Promoter holding change (any), FII/DII change ≥ 1 pp, SAST / insider disclosures | screener.in shareholding pattern + filings |
| ■ Results | Quarterly revenue and profit YoY. Dated on the filing day when known, otherwise ~45 days after quarter end. | screener.in quarterly results |
| ★ Corporate / deals / approvals | Bonus, split, buyback, dividend, QIP, acquisitions, JVs, MoUs, USFDA, capacity expansion, launches | screener.in filings |
| ✕ Risk flags | Resignations, penalties, SEBI/tax orders, pledges, downgrades, defaults, plant shutdowns | filings + news |
| ● Web news | Headlines, analyst targets/ratings, rallies and falls | Google News |
| ● Price signals | Volume spikes (≥3x the 50-day average with a ≥4% move), new 52-week-high breakouts | price data |
| ▢ Upcoming | Board meetings, results dates, investor calls, record dates, AGMs mentioned in filings | screener.in filings |

---

## 2. How stocks are scored

The score is the sum of the components below. Weights live in `score()` in `screener.py`.

### Trend (max ≈ 30)
| Rule | Points |
|---|---|
| Price above 50-DMA | +8 |
| 50-DMA above 200-DMA (uptrend) | +8 |
| Price below 200-DMA *and* 50-DMA below 200-DMA | −5 |
| Price above 6-month MA | +6 |
| 6-month MA rising over the last month | +4 |
| Within 10% of the 52-week high | +4 |

### Momentum (max ≈ 23)
| Rule | Points |
|---|---|
| 1-month return | 1 point per 1%, capped between −10 and +15 |
| RSI(14) between 50 and 70 | +5 |
| RSI above 75 (overbought) | −3 |
| Last-5-day volume ≥ 1.5x normal while price is rising | +3 |

### Catalysts, last 30 days (max ≈ 45)
| Rule | Points |
|---|---|
| Order announcement(s) | +12, +3 per additional order, plus up to +10 for size (4 pts per 1% of market cap); capped at +22. Filings are used first, news only if there are no filings. |
| Promoter holding up last quarter | +10 (+5 more if ≥ 0.5 pp) |
| Promoter holding down ≥ 0.5 pp | −10 |
| FII + DII holding up ≥ 0.5 pp | +4 |
| Fresh corporate action / deal / approval filing | +4 each, max +8 |
| SAST / insider disclosure | +2 |
| Risk-flag filing | −8 |
| News tone, last 14 days | +1 per positive headline, −2 per negative, capped at ±6 |

### Fundamentals (max ≈ 13)
| Rule | Points |
|---|---|
| Latest-quarter net profit growth YoY > 15% | +6 (below −15%: −4) |
| Latest-quarter revenue growth YoY > 10% | +4 |
| ROCE > 15% | +3 |

### Picks and catalysts
- **Picks:** the 3 highest scores in each cap bucket.
- **Cap buckets** use live market cap: **Large ≥ ₹1,00,000 Cr**, **Mid ≥ ₹30,000 Cr**, **Small below that**. This approximates AMFI's rank-based lists and can be changed at the top of `screener.py`.
- **Top 3 catalysts:** each event in the last 30 days gets a strength score:
  - Base points: order 10, corporate action 7, deal 7, approval 6, promoter/insider buying 6.
  - Order size relative to market cap is added on top.
  - News is weighted at 0.6x, so the exchange filing is preferred over a report of the same thing.
  - Strength decays with age.

  The 3 stocks with the strongest events are shown.

---

## 3. Data sources

| Data | Source | Notes |
|---|---|---|
| Announcements: orders, board meetings, corporate actions, SAST, results filings | **screener.in** company page | The public page shows the **5 latest** filings. Logged in (see §5), the full "Important" feed is used too. |
| Quarterly results, shareholding (promoter/FII/DII), ratios, credit ratings | **screener.in** company page | |
| Daily price, 50/200-DMA, volume | **screener.in** chart API | Daily for the last 365 days. The year before uses weekly closes so the 6-month MA covers the full chart. |
| Company search (desktop app) | **screener.in** search API | |
| Web news: headlines, analyst calls, order news | **Google News RSS** | ~60 headlines per company per year. Classified by keyword. |

Requests are rate-limited (1.5 s apart) and cached per day in `cache\YYYY-MM-DD\` (7 days are kept).

---

## 4. Running it

### Folder layout (`E:\screener`)
| File | Purpose |
|---|---|
| `screener.py` | Fetches, scores and builds `output\index.html` + `output\data.json` |
| `app.py` | Desktop app: local server with live search, on-demand analysis, refresh, watchlist |
| `template.html` | The dashboard UI |
| `universe.txt` | Stocks scanned daily (one NSE symbol per line) |
| `watchlist.json` | Your watchlist (desktop app) |
| `run_daily.bat` | What the 8 AM scheduled task runs |
| `open_screener.bat` | Double-click: starts the desktop app and opens it in your browser |
| `output\` | `index.html`, `data.json`, dated reports, `picks_YYYY-MM-DD.csv` |
| `logs\run.log` | Log of each scheduled run |

### Commands
```
open_screener.bat                          desktop app at http://127.0.0.1:8765
.venv\Scripts\python screener.py            run the screen (uses today's cache)
.venv\Scripts\python screener.py --refresh  ignore cache, refetch everything
.venv\Scripts\python screener.py --limit 10 quick test on first 10 symbols
```

### Daily 8 AM refresh
The Windows Task Scheduler task **"Daily Stock Screener"** runs `run_daily.bat` at 08:00 every day.
If the PC is off at 8 AM, it runs at the next startup. An open dashboard tab reloads its data every hour.

### Changing the stock list
Edit `universe.txt`. Buckets come from market cap, so the headings in the file are only for you.
Symbols must match screener.in URLs (`screener.in/company/<SYMBOL>/`).

---

## 5. Optional: deeper filing history

Without a login, screener.in shows only the 5 latest announcements per company. To use the full
"Important announcements" feed:
1. Log in to screener.in in your browser.
2. Open DevTools → Application → Cookies → `https://www.screener.in` and copy the **`sessionid`** value.
3. Paste it into `E:\screener\screener_session.txt`. **Keep this file private** and never share or publish it.

---

## 6. Limitations
- **Keyword classification:** event types are rule-based. Expect the occasional mislabelled headline (e.g. an order the company *placed* rather than won is filtered out, but other edge cases slip through).
- **Results dates:** dates are approximate (quarter end + 45 days) unless the results filing is in the visible feed.
- **Shareholding timing:** changes are dated at quarter end. They are disclosed about 3 weeks later.
- **Order sizes:** only parsed when the rupee or dollar amount appears in the filing summary or headline.
- **BSE/NSE APIs:** not used. Both block automated requests.
- **Shared web version:** shows the data as of the last publish. Live search and full refresh need the desktop app.
