"""
Personal daily stock screener built on screener.in data.

For every symbol in universe.txt it pulls (from screener.in only):
  - company page: market cap, ratios, quarterly results, shareholding pattern,
    latest announcements (orders, board meetings, corporate actions...), credit ratings
  - price chart API: ~2 years of daily price, 50/200 DMA and volume

It then scores each stock on trend, momentum, fresh catalysts (order wins,
promoter buying, corporate actions) and fundamentals, buckets it as Large/Mid/Small
cap by live market cap, and writes an HTML dashboard to output/.

Usage:
    .venv\\Scripts\\python screener.py            # full run (uses today's cache if present)
    .venv\\Scripts\\python screener.py --refresh  # ignore cache, refetch everything
    .venv\\Scripts\\python screener.py --limit 10 # quick test on first 10 symbols

Optional: put your screener.in "sessionid" cookie in screener_session.txt (or env
SCREENER_SESSIONID) to also pull the logged-in "Important announcements" feed,
which goes back much further than the 5 public ones.

This is a research aid, not investment advice.
"""
import argparse
import calendar
import datetime as dt
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
OUT_DIR = ROOT / "output"
BASE = "https://www.screener.in"

# Market-cap buckets in Rs crore (approximation of AMFI's rank-based cut-offs).
LARGE_CAP_CR = 100_000
MID_CAP_CR = 30_000
PICKS_PER_BUCKET = 3
TOP_CATALYSTS = 3
MA_6M = 126            # ~6 months of trading days
CATALYST_WINDOW = 30   # days: how "fresh" an event must be to count as a catalyst
REQUEST_DELAY = float(os.environ.get("SCREENER_DELAY", "1.5"))    # seconds between network calls; be polite to screener.in

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) personal-screener/1.0"}
TODAY = dt.date.today()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- fetch

class Fetcher:
    def __init__(self, refresh=False):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        sid = os.environ.get("SCREENER_SESSIONID")
        sid_file = ROOT / "screener_session.txt"
        if not sid and sid_file.exists():
            sid = sid_file.read_text().strip()
        self.logged_in = bool(sid)
        if sid:
            self.session.cookies.set("sessionid", sid, domain="www.screener.in")
        self.refresh = refresh
        self.day_dir = CACHE_DIR / TODAY.isoformat()
        self.day_dir.mkdir(parents=True, exist_ok=True)

    def prune(self, keep_days=7):
        for d in CACHE_DIR.iterdir():
            try:
                if d.is_dir() and (TODAY - dt.date.fromisoformat(d.name)).days > keep_days:
                    for f in d.iterdir():
                        f.unlink()
                    d.rmdir()
            except ValueError:
                pass

    def get(self, path, cache_name):
        cache = self.day_dir / cache_name
        if cache.exists() and not self.refresh:
            return cache.read_text(encoding="utf-8")
        for attempt, wait in enumerate([0, 15, 45]):
            if wait:
                print(f"    rate-limited, waiting {wait}s...")
                time.sleep(wait)
            r = self.session.get(path if path.startswith("http") else BASE + path, timeout=30)
            time.sleep(REQUEST_DELAY)
            if r.status_code == 429:
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            cache.write_text(r.text, encoding="utf-8")
            return r.text
        return None


# --------------------------------------------------------------------------- parsing helpers

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTH_RE = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"


def num(text):
    if text is None:
        return None
    t = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    try:
        return float(t) if t not in ("", "-", ".") else None
    except ValueError:
        return None


def month_end(label):
    """'Sep 2026' -> date(2026, 9, 30)"""
    m = re.match(r"([A-Za-z]{3})\w*\s+(\d{4})", label.strip())
    if not m:
        return None
    mon, yr = MONTHS[m.group(1).lower()], int(m.group(2))
    return dt.date(yr, mon, calendar.monthrange(yr, mon)[1])


def parse_loose_date(text):
    """'20 Feb' or '21 Feb 2025' -> date (yearless dates assumed within the last 12 months)."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\w*(?:\s+(\d{4}))?", text)
    if not m:
        return None
    day, mon = int(m.group(1)), MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    yr = int(m.group(3)) if m.group(3) else TODAY.year
    try:
        d = dt.date(yr, mon, day)
    except ValueError:
        return None
    if not m.group(3) and d > TODAY:
        d = d.replace(year=yr - 1)
    return d


def dates_in_text(text):
    """All explicit calendar dates mentioned in a piece of text."""
    found = []
    for m in re.finditer(r"(\d{1,2})(?:st|nd|rd|th)?\s+" + MONTH_RE + r",?\s+(\d{4})", text, re.I):
        found.append((int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1))))
    for m in re.finditer(MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text, re.I):
        found.append((int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
    for m in re.finditer(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", text):
        found.append((int(m.group(3)), int(m.group(2)), int(m.group(1))))
    out = []
    for y, mo, d in found:
        try:
            out.append(dt.date(y, mo, d))
        except ValueError:
            pass
    return out


def order_value_cr(text):
    """Extract an order/contract value in Rs crore from announcement text, if stated."""
    best = None
    for m in re.finditer(r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d+)?)\s*(lakh\s*crore|crores?|cr\.?|lakhs?|million|mn|billion|bn)\b", text, re.I):
        v, unit = float(m.group(1).replace(",", "")), m.group(2).lower()
        if unit.startswith("lakh crore") or unit.startswith("lakh crore"):
            v *= 100_000
        elif unit.startswith("lakh"):
            v /= 100
        elif unit in ("million", "mn"):
            v /= 10
        elif unit in ("billion", "bn"):
            v *= 100
        best = max(best or 0, v)
    for m in re.finditer(r"(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)\s*(million|mn|billion|bn)\b", text, re.I):
        v = float(m.group(1).replace(",", "")) * (1 if m.group(2).lower() in ("million", "mn") else 1000)
        best = max(best or 0, v * 8.8)  # ~Rs 88/USD -> 1 USD mn ≈ 8.8 cr
    return best


# --------------------------------------------------------------------------- announcement classification

BOILERPLATE = re.compile(r"newspaper publication|trading window|loss of share certificate|duplicate share|"
                         r"compliance certificate|certificate under reg|reg\.? 74|regulation 74|"
                         r"esop|esos|allotment of equity shares under|change in (company|registrar)|"
                         r"intimation of record date for interest|statement of investor complaints", re.I)
NEG_CONTEXT = re.compile(r"sebi|gst|tax|court|tribunal|nclt|nclat|penalt|show cause|adjudicat|demand|fine|"
                         r"regulatory action|enforcement", re.I)
RULES = [
    ("negative", re.compile(r"resign|penalt|show cause|fraud|default|insolvency|search (and|&) seizure|raid|"
                            r"downgrad|pledge|encumb|promoters? .{0,25}(sell|sold|offload|stake sale|block deal|trim)|"
                            r"suspension|cessation of (cfo|ceo|md|auditor)|litigation|"
                            r"forensic|qualified opinion|fire at|strike|lockout|plant shutdown", re.I)),
    ("order", re.compile(r"receipt of order|award of order|bag(s|ged)? .{0,40}(order|contract)|"
                         r"(secur|receiv|win|won|award)\w* .{0,60}\b(orders?|contracts?|LoA|LOI|project)\b|"
                         r"\borders? (worth|valued|of rs|aggregating|inflow)|letter of (award|intent|acceptance)|"
                         r"work order|purchase order|new orders?|order book", re.I)),
    ("holding", re.compile(r"sast|reg(ulation)?\.? ?29|regulation 7\(2\)|insider trading|"
                           r"promoter.{0,30}(acqui|purchas|buy|increas|creeping)|open offer", re.I)),
    ("corporate", re.compile(r"bonus|split|sub-?division|buy-?back|dividend|rights issue|record date|"
                             r"\bqip\b|preferential|fund ?rais|warrants", re.I)),
    ("deal", re.compile(r"acqui|merger|amalgamat|joint venture|\bjv\b|strategic (partner|alliance)|"
                        r"stake|mou\b|memorandum of understanding|collaborat|tie-?up|agreement", re.I)),
    ("approval", re.compile(r"usfda|us fda|approval|eir\b|clearance|licen[cs]e|patent|commission(ed|ing)|"
                            r"capacity|expansion|new plant|commercial (production|operation)|launch", re.I)),
    ("results", re.compile(r"financial results|outcome of board meeting", re.I)),
    ("meeting", re.compile(r"board meeting|analyst|investor meet|conference call|earnings call|agm|"
                           r"annual general meeting|egm|postal ballot", re.I)),
]

NEWS_POSITIVE = re.compile(r"\b(buy|accumulate|upgrade[sd]?|target (price|of)|outperform|overweight|"
                           r"rall(y|ies)|surge[sd]?|jump(s|ed)?|soar(s|ed)?|record high|hits? 52-week high)\b", re.I)
NEWS_NEGATIVE = re.compile(r"\b(sell rating|underperform|underweight|plunge[sd]?|crash(es|ed)?|slump(s|ed)?|"
                           r"tank(s|ed)?|probe|raid|52-week low|cut to sell|weak (q\d|results))\b", re.I)

TYPE_LABEL = {
    "news": "News", "analyst": "Analyst / market view",
    "order": "Order win", "holding": "Shareholding", "corporate": "Corporate action", "deal": "Deal / M&A",
    "approval": "Approval / expansion", "results": "Results", "meeting": "Meeting", "negative": "Risk flag",
    "rating": "Credit rating", "price": "Price signal", "upcoming": "Upcoming", "info": "Filing",
}


def classify(title, summary):
    text = f"{title} {summary}".replace("_", " ")
    if BOILERPLATE.search(text) and not summary:
        return None
    for kind, rx in RULES:
        if rx.search(text):
            if kind == "order" and NEG_CONTEXT.search(text):
                return "negative"
            return kind
    return "info"


# --------------------------------------------------------------------------- company page

def parse_company(html):
    s = BeautifulSoup(html, "lxml")
    info = s.select_one("[data-company-id]")
    if not info or not s.select_one("#top-ratios"):
        return None
    c = {
        "name": s.select_one("h1").get_text(strip=True),
        "company_id": info["data-company-id"],
        "ratios": {},
    }
    for li in s.select("#top-ratios li"):
        name = li.select_one(".name")
        val = li.select_one(".value")
        if name and val:
            c["ratios"][" ".join(name.get_text().split())] = " ".join(val.get_text(" ").split())
    c["mcap"] = num(c["ratios"].get("Market Cap"))
    c["roce"] = num(c["ratios"].get("ROCE"))
    c["pe"] = num(c["ratios"].get("Stock P/E"))

    # quarterly results
    c["quarters"] = {}
    t = s.select_one("#quarters table")
    if t:
        heads = [th.get_text(strip=True) for th in t.select("thead th")][1:]
        c["quarter_heads"] = heads
        for tr in t.select("tbody tr"):
            tds = tr.select("td")
            if not tds:
                continue
            label = tds[0].get_text(" ", strip=True).replace("+", "").strip()
            c["quarters"][label] = [num(td.get_text()) for td in tds[1:]]

    # shareholding (quarterly)
    c["shp"], c["shp_heads"] = {}, []
    t = s.select_one("#quarterly-shp table")
    if t:
        c["shp_heads"] = [th.get_text(strip=True) for th in t.select("thead th")][1:]
        for tr in t.select("tbody tr"):
            tds = tr.select("td")
            label = tds[0].get_text(" ", strip=True).replace("+", "").strip()
            c["shp"][label] = [num(td.get_text()) for td in tds[1:]]

    c["announcements"] = parse_announcements(s)
    c["ratings"] = []
    for box in s.select("#documents .documents"):
        h = box.find(["h3", "h4"])
        if h and "Credit" in h.get_text():
            for a in box.select("li a"):
                d = parse_loose_date(a.get_text(" "))
                if d:
                    src = a.select_one("div")
                    c["ratings"].append({"date": d, "title": "Credit rating update",
                                         "detail": " ".join(src.get_text().split()) if src else "",
                                         "url": a.get("href", "")})
    return c


def parse_announcements(soup):
    out = []
    for li in soup.select("ul.list-links li"):
        a, tm = li.find("a"), li.find("time")
        if not a or not tm or not tm.get("datetime"):
            continue
        title = " ".join("".join(a.find_all(string=True, recursive=False)).split())
        detail_el = a.find(["div", "span"], class_="ink-600")
        summary = ""
        if detail_el:
            summary = " ".join(detail_el.get_text(" ").split())
            summary = re.sub(r"^\S+\s+\S+\s*-\s*", "", summary) if " - " in summary else ""
        title = re.sub(r"^Announcement under Regulation 30 \(LODR\)-", "", title).replace("_", " ")
        out.append({"date": dt.datetime.fromisoformat(tm["datetime"]).date(),
                    "title": title, "summary": summary, "url": a.get("href", "")})
    return out


def _chart_frame(txt):
    cols = {}
    for ds in json.loads(txt).get("datasets", []):
        key = {"Price": "close", "DMA50": "dma50", "DMA200": "dma200", "Volume": "volume"}.get(ds["metric"])
        if key:
            cols[key] = {v[0]: float(v[1]) for v in ds["values"] if v[1] not in (None, "")}
    if "close" not in cols:
        return None
    df = pd.DataFrame(cols)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def fetch_prices(fetcher, sym, company_id):
    """Daily data for the last 365 days (screener's daily limit), with the year before
    filled in from weekly closes (forward-filled to business days) so the 6-month MA
    is defined across the whole visible year."""
    daily = fetcher.get(f"/api/company/{company_id}/chart/?q=Price-DMA50-DMA200-Volume&days=365",
                        f"{safe(sym)}_chart_daily.json")
    if not daily:
        return None
    df = _chart_frame(daily)
    if df is None:
        return None
    weekly = fetcher.get(f"/api/company/{company_id}/chart/?q=Price&days=730", f"{safe(sym)}_chart_weekly.json")
    wk = _chart_frame(weekly) if weekly else None
    if wk is not None:
        older = wk[wk.index < df.index[0]]["close"]
        if len(older):
            older = older.reindex(pd.bdate_range(older.index[0], df.index[0] - pd.Timedelta(days=1))).ffill()
            df = pd.concat([pd.DataFrame({"close": older}), df])
    df.attrs["daily_start"] = df.index[-1] - pd.Timedelta(days=366)
    return df


def news_query_name(name):
    n = re.sub(r"\b(Ltd|Limited|Ltd\.|Co\.?|Corporation|Company|India|\(India\)|The)\b\.?", " ", name, flags=re.I)
    n = " ".join(n.split()) or name
    return n if len(n) > 3 else name


def fetch_news(fetcher, sym, name):
    """Web news headlines for the past year from Google News RSS (free, no key)."""
    # Require market context so short names (e.g. "Trent") don't pull in unrelated stories.
    q = quote(f'"{news_query_name(name)}" (shares OR stock OR NSE OR BSE OR order OR results) when:1y')
    xml = fetcher.get(f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en",
                      f"{safe(sym)}_news.xml")
    if not xml:
        return []
    out, seen = [], set()
    for it in BeautifulSoup(xml, "xml").find_all("item"):
        title = (it.title.get_text() if it.title else "").strip()
        src = it.source.get_text().strip() if it.source else ""
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            d = dt.datetime.strptime(it.pubDate.get_text().strip(), "%a, %d %b %Y %H:%M:%S %Z").date()
        except (AttributeError, ValueError):
            continue
        d = min(d, TODAY)
        key = (d, title[:40].lower())
        if key in seen or not title:
            continue
        seen.add(key)
        kind = classify(title, "")
        if kind == "order":
            # Only count it if this company is the one winning the order ("X bags order"),
            # not placing it ("X awards order to Y").
            m = re.search(r"\b(bag|win|won|secur|receiv|land|get|grab|clinch|bags|wins)\w*\b", title, re.I)
            prefix = title[: m.start()].lower() if m else ""
            names = {news_query_name(name).lower(), news_query_name(name).split()[0].lower(), sym.lower()}
            if not m or not any(n and n in prefix for n in names) or re.search(r"\bawards?\b|\bplaces?\b", prefix, re.I):
                kind = "news"
        if kind in (None, "info", "meeting", "results"):
            kind = ("analyst" if NEWS_POSITIVE.search(title) else
                    "negative" if NEWS_NEGATIVE.search(title) else
                    "results" if kind == "results" else "news")
        impact = 1 if kind in ("order", "corporate", "deal", "approval", "analyst") else -1 if kind == "negative" else 0
        e = {"date": d, "type": kind, "title": title, "detail": src, "url": it.link.get_text() if it.link else "",
             "impact": impact, "source": "news"}
        if kind == "order":
            e["value_cr"] = order_value_cr(title)
        out.append(e)
    return out


def safe(sym):
    return re.sub(r"[^A-Za-z0-9_-]", "_", sym)


# --------------------------------------------------------------------------- analysis

def rsi(series, n=14):
    d = series.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def price_events(df):
    ev = []
    last_year = df[df.index >= pd.Timestamp(TODAY - dt.timedelta(days=365))]
    if "volume" in df:
        avg = df["volume"].rolling(50).mean().shift(1)
        chg = df["close"].pct_change() * 100
        last = None
        for ts in last_year.index:
            v, a, c = df.at[ts, "volume"], avg.get(ts), chg.get(ts)
            if a and a > 0 and v >= 3 * a and abs(c) >= 4 and (last is None or (ts - last).days > 7):
                ev.append({"date": ts.date(), "type": "price",
                           "title": f"Volume spike {c:+.1f}% ({v / a:.1f}x avg volume)", "impact": 1 if c > 0 else -1})
                last = ts
    prior_high = df["close"].rolling(252, min_periods=120).max().shift(1)
    last_break = None
    for ts in last_year.index:
        ph = prior_high.get(ts)
        if ph and df.at[ts, "close"] > ph:
            if last_break is None or (ts - last_break).days > 30:
                ev.append({"date": ts.date(), "type": "price", "title": "New 52-week high breakout", "impact": 1})
            last_break = ts
    return ev


def shareholding_events(c):
    ev, heads = [], c["shp_heads"]
    for label, short, thresh in (("Promoters", "Promoter", 0.01), ("FIIs", "FII", 1.0), ("DIIs", "DII", 1.0)):
        vals = c["shp"].get(label)
        if not vals:
            continue
        for i in range(1, len(vals)):
            d = month_end(heads[i])
            if not d or d < TODAY - dt.timedelta(days=400) or vals[i] is None or vals[i - 1] is None:
                continue
            delta = vals[i] - vals[i - 1]
            if abs(delta) >= thresh:
                ev.append({"date": d, "type": "holding", "impact": 1 if delta > 0 else -1,
                           "title": f"{short} holding {'up' if delta > 0 else 'down'} {delta:+.2f}pp to {vals[i]:.2f}%",
                           "detail": f"Quarter ended {heads[i]} (disclosed ~3 weeks later)"})
    return ev


def results_events(c):
    ev, heads, q = [], c.get("quarter_heads", []), c["quarters"]
    sales = next((v for k, v in q.items() if k.startswith(("Sales", "Revenue"))), None)
    profit = next((v for k, v in q.items() if k.startswith("Net Profit")), None)
    if not profit:
        return ev
    exact = [a["date"] for a in c["announcements"] if re.search(r"financial results", a["title"], re.I)]
    for i in range(4, len(heads)):
        qend = month_end(heads[i])
        if not qend or qend < TODAY - dt.timedelta(days=420):
            continue
        d = next((x for x in exact if qend < x <= qend + dt.timedelta(days=75)), None)
        approx = d is None
        d = d or qend + dt.timedelta(days=45)
        if d > TODAY:
            continue
        bits = []
        for name, series in (("Revenue", sales), ("Profit", profit)):
            if series and series[i] is not None and series[i - 4] not in (None, 0):
                g = (series[i] / series[i - 4] - 1) * 100 * (1 if series[i - 4] > 0 else -1)
                bits.append(f"{name} {g:+.0f}% YoY")
        if bits:
            g_profit = profit[i] / profit[i - 4] - 1 if profit[i] and profit[i - 4] and profit[i - 4] > 0 else 0
            ev.append({"date": d, "type": "results", "impact": 1 if g_profit > 0.1 else (-1 if g_profit < -0.1 else 0),
                       "title": f"{heads[i]} results: " + ", ".join(bits),
                       "detail": "approx. date (quarter end + 45d)" if approx else "result filing date"})
    return ev


def announcement_events(c):
    ev, upcoming = [], []
    for a in c["announcements"]:
        kind = classify(a["title"], a["summary"])
        if kind is None:
            continue
        text = f"{a['title']} {a['summary']}"
        e = {"date": a["date"], "type": kind, "title": a["summary"] or a["title"], "source": "filing",
             "detail": a["title"] if a["summary"] else "", "url": a["url"],
             "impact": -1 if kind == "negative" else (1 if kind in ("order", "corporate", "deal", "approval") else 0)}
        if kind == "order":
            e["value_cr"] = order_value_cr(text)
        ev.append(e)
        for d in dates_in_text(text):
            if TODAY <= d <= TODAY + dt.timedelta(days=120):
                what = ("Board meeting" if re.search(r"board meeting", text, re.I) else
                        "Investor / analyst call" if re.search(r"call|analyst|investor", text, re.I) else
                        "Record date" if re.search(r"record date", text, re.I) else
                        "AGM / EGM" if re.search(r"agm|egm|general meeting", text, re.I) else "Scheduled event")
                upcoming.append({"date": d, "type": "upcoming", "title": f"{what} on {d:%d %b %Y}",
                                 "detail": a["summary"] or a["title"], "url": a["url"], "impact": 0})
    seen, uniq = set(), []
    for u in upcoming:
        k = (u["date"], u["title"])
        if k not in seen:
            seen.add(k)
            uniq.append(u)
    return ev, uniq


def score(c, df, events):
    pts, why = 0.0, []

    def add(p, reason):
        nonlocal pts
        pts += p
        why.append((round(p, 1), reason))

    close = df["close"]
    px = close.iloc[-1]
    ma6 = close.rolling(MA_6M).mean()
    dma50 = df["dma50"].iloc[-1] if "dma50" in df and not math.isnan(df["dma50"].iloc[-1]) else close.rolling(50).mean().iloc[-1]
    dma200 = df["dma200"].iloc[-1] if "dma200" in df and not math.isnan(df["dma200"].iloc[-1]) else close.rolling(200).mean().iloc[-1]
    hi52 = close.iloc[-252:].max()
    r = rsi(close).iloc[-1]
    ret1m = px / close.iloc[-22] - 1 if len(close) > 22 else 0
    ret3m = px / close.iloc[-64] - 1 if len(close) > 64 else 0
    vol_ratio = None
    if "volume" in df and len(df) > 60:
        base = df["volume"].iloc[-55:-5].mean()
        vol_ratio = df["volume"].iloc[-5:].mean() / base if base else None

    # Trend (max 30)
    if px > dma50:
        add(8, "Price above 50-DMA")
    if dma50 > dma200:
        add(8, "50-DMA above 200-DMA (uptrend)")
    elif px < dma200:
        add(-5, "Price below 200-DMA (downtrend)")
    if not math.isnan(ma6.iloc[-1]):
        if px > ma6.iloc[-1]:
            add(6, "Price above 6-month MA")
        if len(ma6) > 21 and ma6.iloc[-1] > ma6.iloc[-21]:
            add(4, "6-month MA rising")
    if px >= 0.9 * hi52:
        add(4, f"Within {100 * (1 - px / hi52):.0f}% of 52-week high")

    # Momentum (max ~20)
    add(max(-10, min(15, ret1m * 100)), f"1-month return {ret1m * 100:+.1f}%")
    if 50 <= r <= 70:
        add(5, f"RSI {r:.0f} (healthy momentum)")
    elif r > 75:
        add(-3, f"RSI {r:.0f} (overbought)")
    if vol_ratio and vol_ratio >= 1.5 and ret1m > 0:
        add(3, f"Volume {vol_ratio:.1f}x normal with rising price")

    # Catalysts (max ~35)
    recent = [e for e in events if (TODAY - e["date"]).days <= CATALYST_WINDOW and e["date"] <= TODAY]
    fresh = [e for e in recent if e.get("source") != "news"]   # exchange filings via screener.in
    news = [e for e in recent if e.get("source") == "news"]
    orders = [e for e in fresh if e["type"] == "order"]
    if not orders:  # fall back to order wins reported in the news
        orders = [e for e in news if e["type"] == "order"]
    if orders:
        order_pts = 12 + 3 * (len(orders) - 1)
        total = sum(e.get("value_cr") or 0 for e in orders)
        if total and c["mcap"]:
            pct = total / c["mcap"] * 100
            order_pts += min(10, pct * 4)
            add(min(22, order_pts), f"{len(orders)} order announcement(s), ~₹{total:,.0f} Cr = {pct:.1f}% of mcap")
        else:
            add(min(22, order_pts), f"{len(orders)} order announcement(s) in last {CATALYST_WINDOW} days")
    prom = c["shp"].get("Promoters") or []
    if len(prom) >= 2 and prom[-1] is not None and prom[-2] is not None:
        d = prom[-1] - prom[-2]
        if d > 0.01:
            add(10 + (5 if d >= 0.5 else 0), f"Promoter holding up {d:+.2f}pp last quarter")
        elif d <= -0.5:
            add(-10, f"Promoter holding down {d:.2f}pp last quarter")
    inst = sum((c["shp"].get(k) or [0, 0])[-1] - (c["shp"].get(k) or [0, 0])[-2]
               for k in ("FIIs", "DIIs") if len(c["shp"].get(k) or []) >= 2 and None not in (c["shp"][k][-1], c["shp"][k][-2]))
    if inst >= 0.5:
        add(4, f"FII+DII holding up {inst:+.2f}pp last quarter")
    pos = [e for e in fresh if e["type"] in ("corporate", "deal", "approval")]
    if pos:
        add(min(8, 4 * len(pos)), "Fresh " + ", ".join(sorted({TYPE_LABEL[e['type']].lower() for e in pos})))
    if any(e["type"] == "holding" and e.get("url") for e in fresh):
        add(2, "Recent SAST / insider disclosure")
    neg = [e for e in fresh if e["type"] == "negative"]
    if neg:
        add(-8, "Risk flag: " + neg[0]["title"][:80])
    if news:
        good = sum(1 for e in news if e["impact"] > 0 and (TODAY - e["date"]).days <= 14)
        bad = sum(1 for e in news if e["impact"] < 0 and (TODAY - e["date"]).days <= 14)
        tone = max(-6, min(6, good - 2 * bad))
        if tone:
            add(tone, f"News flow (14d): {good} positive / {bad} negative headlines")

    # Fundamentals (max ~15)
    q = c["quarters"]
    profit = next((v for k, v in q.items() if k.startswith("Net Profit")), None)
    sales = next((v for k, v in q.items() if k.startswith(("Sales", "Revenue"))), None)
    if profit and len(profit) >= 5 and profit[-1] is not None and profit[-5]:
        g = profit[-1] / profit[-5] - 1 if profit[-5] > 0 else (1 if profit[-1] > 0 else -1)
        if g > 0.15:
            add(6, f"Latest quarter profit {g * 100:+.0f}% YoY")
        elif g < -0.15:
            add(-4, f"Latest quarter profit {g * 100:+.0f}% YoY")
    if sales and len(sales) >= 5 and sales[-1] and sales[-5]:
        g = sales[-1] / sales[-5] - 1
        if g > 0.10:
            add(4, f"Latest quarter revenue {g * 100:+.0f}% YoY")
    if c["roce"] and c["roce"] > 15:
        add(3, f"ROCE {c['roce']:.0f}%")

    metrics = {"price": px, "dma50": dma50, "dma200": dma200, "ma6m": ma6.iloc[-1], "rsi": r,
               "ret1m": ret1m * 100, "ret3m": ret3m * 100, "hi52": hi52, "vol_ratio": vol_ratio}
    return round(pts, 1), why, metrics


def catalyst_strength(e, mcap):
    """How likely an event is to move the stock (used for 'Top catalysts')."""
    age = (TODAY - e["date"]).days
    if age > CATALYST_WINDOW or age < 0:
        return 0
    base = {"order": 10, "corporate": 7, "deal": 7, "approval": 6, "holding": 6}.get(e["type"], 0)
    if e["type"] == "holding" and e.get("impact", 0) <= 0:
        return 0
    if e["type"] == "order" and e.get("value_cr") and mcap:
        base += min(15, e["value_cr"] / mcap * 100 * 5)
    if e.get("source") == "news":
        base *= 0.6  # prefer the exchange filing over a news report of the same thing
    return base * (1 - age / (CATALYST_WINDOW * 2))


# --------------------------------------------------------------------------- main

def bucket(mcap):
    if mcap is None:
        return None
    return "Large" if mcap >= LARGE_CAP_CR else "Mid" if mcap >= MID_CAP_CR else "Small"


def analyse(fetcher, sym):
    path = f"/company/{quote(sym)}/consolidated/"
    html = fetcher.get(path, f"{safe(sym)}_cons.html")
    c = parse_company(html) if html else None
    if c and not c["quarters"]:
        html = fetcher.get(f"/company/{quote(sym)}/", f"{safe(sym)}.html")
        c = parse_company(html) if html else c
        path = f"/company/{quote(sym)}/"
    if not c:
        return None
    if fetcher.logged_in:
        imp = fetcher.get(f"/announcements/important/{c['company_id']}/", f"{safe(sym)}_important.html")
        if imp:
            have = {(a["date"], a["title"]) for a in c["announcements"]}
            c["announcements"] += [a for a in parse_announcements(BeautifulSoup(imp, "lxml"))
                                   if (a["date"], a["title"]) not in have]
    df = fetch_prices(fetcher, sym, c["company_id"])
    if df is None or len(df) < 130:
        return None

    ann_ev, upcoming = announcement_events(c)
    events = ann_ev + shareholding_events(c) + results_events(c) + price_events(df)
    try:
        events += fetch_news(fetcher, sym, c["name"])
    except Exception as ex:  # news is a bonus: never drop a stock because the feed failed
        print(f"    news unavailable for {sym}: {ex}")
    events += [{**r, "type": "rating", "impact": 0} for r in c["ratings"]
               if r["date"] >= TODAY - dt.timedelta(days=365)]
    events = [e for e in events if e["date"] >= TODAY - dt.timedelta(days=365)]
    events.sort(key=lambda e: e["date"])
    sc, why, metrics = score(c, df, events)

    ma6 = df["close"].rolling(MA_6M).mean()
    show = df[df.index >= pd.Timestamp(TODAY - dt.timedelta(days=365))]
    chart = {
        "dates": [d.strftime("%Y-%m-%d") for d in show.index],
        "close": [round(v, 2) for v in show["close"]],
        "ma6m": [None if math.isnan(v) else round(v, 2) for v in ma6.loc[show.index]],
        "dma50": [None if math.isnan(v) else round(v, 2) for v in show["dma50"]] if "dma50" in show else [],
    }
    cats = sorted(((catalyst_strength(e, c["mcap"]), e) for e in events), key=lambda x: -x[0])
    return {
        "symbol": sym, "name": c["name"], "url": BASE + path, "mcap": c["mcap"], "bucket": bucket(c["mcap"]),
        "pe": c["pe"], "roce": c["roce"], "score": sc, "why": why, "metrics": metrics,
        "promoter": (c["shp"].get("Promoters") or [None])[-1],
        "events": events, "upcoming": sorted(upcoming, key=lambda e: e["date"]), "chart": chart,
        "top_catalyst": (cats[0][0], cats[0][1]) if cats and cats[0][0] > 0 else (0, None),
    }


def jsonable(o):
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o))


NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"


def nifty500():
    """Current Nifty 500 symbols; refreshed from niftyindices.com, falling back to the saved copy."""
    path = ROOT / "nifty500.csv"
    try:
        r = requests.get(NIFTY500_URL, headers=HEADERS, timeout=20)
        if r.ok and r.text.startswith("Company Name") and r.text.count("\n") > 400:
            path.write_text(r.text, encoding="utf-8")
    except requests.RequestException:
        pass
    if not path.exists():
        return []
    import csv
    return [row["Symbol"].strip() for row in csv.DictReader(path.read_text(encoding="utf-8").splitlines())
            if row.get("Symbol")]


def load_symbols():
    syms = nifty500()
    syms += [ln.strip() for ln in (ROOT / "universe.txt").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    wl = ROOT / "watchlist.json"
    if wl.exists():
        try:
            syms += [str(x).upper() for x in json.loads(wl.read_text())]
        except ValueError:
            pass
    return list(dict.fromkeys(syms))


def to_json(obj):
    return json.dumps(obj, default=jsonable)


def build(refresh=False, limit=None, progress=None):
    """Screen the universe (+ watchlist), write output/index.html, data.json, CSV. Returns the data dict."""
    symbols = load_symbols()[:limit]
    fetcher = Fetcher(refresh=refresh)
    fetcher.prune()
    print(f"Screening {len(symbols)} stocks (logged-in feed: {'yes' if fetcher.logged_in else 'no'})")

    results, failed = [], []
    for i, sym in enumerate(symbols, 1):
        if progress:
            progress(i, len(symbols), sym)
        try:
            r = analyse(fetcher, sym)
        except Exception as ex:  # keep going if one company page is odd
            r = None
            print(f"  [{i}/{len(symbols)}] {sym}: error {ex}")
        if r:
            results.append(r)
            print(f"  [{i}/{len(symbols)}] {sym:<12} {r['bucket'] or '?':<6} score {r['score']:>6}")
        else:
            failed.append(sym)
            print(f"  [{i}/{len(symbols)}] {sym}: skipped (not found / insufficient data)")

    picks = {}
    for b in ("Large", "Mid", "Small"):
        pool = sorted((r for r in results if r["bucket"] == b), key=lambda r: -r["score"])
        picks[b] = [r["symbol"] for r in pool[:PICKS_PER_BUCKET]]

    cats = sorted((r for r in results if r["top_catalyst"][1]), key=lambda r: -r["top_catalyst"][0])
    catalysts = [{"symbol": r["symbol"], "strength": round(r["top_catalyst"][0], 1), "event": r["top_catalyst"][1]}
                 for r in cats[:TOP_CATALYSTS]]
    for r in results:
        r.pop("top_catalyst")

    data = {"generated": dt.datetime.now().strftime("%d %b %Y, %H:%M"), "today": TODAY.isoformat(),
            "picks": picks, "catalysts": catalysts, "stocks": results, "failed": failed,
            "thresholds": {"large": LARGE_CAP_CR, "mid": MID_CAP_CR}, "type_label": TYPE_LABEL,
            "logged_in": fetcher.logged_in}
    write_outputs(data)

    print("\nPICKS")
    for b, syms in picks.items():
        print(f"  {b:<6}: {', '.join(syms) or '-'}")
    print("TOP CATALYSTS")
    for c in catalysts:
        print(f"  {c['symbol']}: {c['event']['title'][:100]}")
    print(f"\nReport: {OUT_DIR / 'index.html'}")
    return data


SUMMARY_KEYS = ("symbol", "name", "url", "mcap", "bucket", "pe", "roce", "score", "why", "metrics", "promoter")


def summary(r):
    """Light per-stock record for data.json; the full record (chart + events) goes in stocks/<SYM>.json."""
    closes = r["chart"]["close"][-120:]
    step = max(1, len(closes) // 40)
    s = {k: r[k] for k in SUMMARY_KEYS}
    s["spark"] = closes[::step] + ([closes[-1]] if (len(closes) - 1) % step else [])
    s["n_events"] = len(r["events"]) + len(r["upcoming"])
    return s


def write_outputs(data):
    OUT_DIR.mkdir(exist_ok=True)
    stock_dir = OUT_DIR / "stocks"
    stock_dir.mkdir(exist_ok=True)
    for f in stock_dir.glob("*.json"):
        f.unlink()
    for r in data["stocks"]:
        (stock_dir / f"{safe(r['symbol'])}.json").write_text(to_json(r), encoding="utf-8")
    light = {**data, "stocks": [summary(r) for r in data["stocks"]]}
    (OUT_DIR / "data.json").write_text(to_json(light), encoding="utf-8")

    docs = (ROOT / "README.md").read_text(encoding="utf-8")
    page = (ROOT / "template.html").read_text(encoding="utf-8")
    page = page.replace("/*__DOCS__*/null", json.dumps(docs).replace("</", "<\\/"))
    (OUT_DIR / "index.html").write_text(page, encoding="utf-8")
    for old in ("latest.html", *[p.name for p in OUT_DIR.glob("screener_*.html")]):
        (OUT_DIR / old).unlink(missing_ok=True)

    rows = []
    by_sym = {r["symbol"]: r for r in data["stocks"]}
    for b, syms in data["picks"].items():
        for s in syms:
            r = by_sym[s]
            rows.append({"bucket": b, "symbol": s, "name": r["name"], "score": r["score"],
                         "price": round(r["metrics"]["price"], 2), "mcap_cr": r["mcap"],
                         "reasons": "; ".join(f"{p:+g} {t}" for p, t in r["why"])})
    pd.DataFrame(rows).to_csv(OUT_DIR / f"picks_{TODAY.isoformat()}.csv", index=False, encoding="utf-8-sig")


def analyse_one(sym, fetcher=None):
    """Analyse a single symbol on demand (used by app.py search)."""
    r = analyse(fetcher or Fetcher(), sym)
    if r:
        r.pop("top_catalyst", None)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="ignore today's cache")
    ap.add_argument("--limit", type=int, help="only process first N symbols (testing)")
    args = ap.parse_args()
    build(refresh=args.refresh, limit=args.limit)


if __name__ == "__main__":
    main()
