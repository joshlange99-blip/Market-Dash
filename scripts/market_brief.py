"""Market briefs: one before the US open and one after the close.

Runs on GitHub Actions. Adapted from a Cloudflare Worker version:
  - Same data sources: Yahoo Finance chart data (query1 -> query2, browser UA),
    CNBC / MarketWatch / Yahoo RSS, Finnhub earnings (Nasdaq calendar fallback).
  - Same NYSE holiday / early-close calendar and brief windows.
  - Same AI rules: use only the supplied data, cite headlines by number,
    reply in a fixed JSON shape. Tables on the page come from the data, never the AI.
  - Instead of a step machine + database, one run builds the whole brief and
    writes briefs/<id>.json plus briefs/index.json (last ~2 weeks).

Usage:
  python3 scripts/market_brief.py --mode scheduled   # build only if a brief is due now
  python3 scripts/market_brief.py --mode latest      # build the most recent brief if missing
  python3 scripts/market_brief.py --mode am|pm [--force]

Environment (all optional):
  ANTHROPIC_API_KEY  -> write-up by Claude (model CLAUDE_MODEL, default claude-sonnet-5-5)
  CF_ACCOUNT_ID + CF_API_TOKEN -> otherwise, free write-up via Cloudflare Workers AI
  FINNHUB_KEY        -> earnings with actual vs estimate (else Nasdaq's public calendar)
  NTFY_TOPIC         -> phone notification via ntfy.sh when a brief is published
  SITE_URL           -> link opened from the notification
"""
import argparse, base64, datetime as dt, email.utils, html, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "briefs")
KEEP_DAYS = 15
NEWS_JSON = "https://raw.githubusercontent.com/joshlange99-blip/Market-Dash/data/news.json"

# kind: "idx" price index, "yld" yield in percent (changes shown in bp)
MKT = {
    "fut": [("ES=F", "S&P 500 futures"), ("NQ=F", "Nasdaq 100 futures"), ("YM=F", "Dow futures"), ("RTY=F", "Russell 2000 futures")],
    "us": [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow"), ("^RUT", "Russell 2000"), ("^VIX", "VIX (fear gauge)")],
    "macro": [("^TNX", "10-yr Treasury yield"), ("UST2Y", "2-yr Treasury yield"), ("DX-Y.NYB", "US dollar index"),
              ("CL=F", "Oil (WTI)"), ("GC=F", "Gold"), ("BTC-USD", "Bitcoin")],
    "global": [("^N225", "Nikkei 225 (Japan)"), ("^HSI", "Hang Seng (Hong Kong)"), ("^STOXX50E", "Euro Stoxx 50"),
               ("^FTSE", "FTSE 100 (UK)"), ("^GDAXI", "DAX (Germany)")],
    "sectors": [("XLK", "Technology"), ("XLC", "Communication"), ("XLY", "Consumer discretionary"), ("XLF", "Financials"),
                ("XLV", "Health care"), ("XLI", "Industrials"), ("XLE", "Energy"), ("XLP", "Consumer staples"),
                ("XLU", "Utilities"), ("XLRE", "Real estate"), ("XLB", "Materials")],
}
YIELDS = {"^TNX", "UST2Y"}
FEEDS = [
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("CNBC Earnings", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]
LOG = []


def log(*a):
    msg = " ".join(str(x) for x in a)
    LOG.append(msg)
    print(msg, flush=True)


# ---------------------------------------------------------------- calendar
def nth_weekday(y, m, wd, n):  # wd: Mon=0
    d = dt.date(y, m, 1)
    return d + dt.timedelta(days=(wd - d.weekday()) % 7 + 7 * (n - 1))


def last_weekday(y, m, wd):
    d = (dt.date(y, m + 1, 1) if m < 12 else dt.date(y + 1, 1, 1)) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - wd) % 7)


def easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mo = (h + l - 7 * m + 114) // 31
    da = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(y, mo, da)


def observed(d):
    return d - dt.timedelta(days=1) if d.weekday() == 5 else d + dt.timedelta(days=1) if d.weekday() == 6 else d


_CAL = {}


def nyse_calendar(y):
    if y in _CAL:
        return _CAL[y]
    H, E = set(), set()
    nyd = dt.date(y, 1, 1)
    if nyd.weekday() != 5:  # a Saturday New Year's isn't moved to Friday
        H.add(observed(nyd))
    H.add(nth_weekday(y, 1, 0, 3)); H.add(nth_weekday(y, 2, 0, 3))
    H.add(easter(y) - dt.timedelta(days=2)); H.add(last_weekday(y, 5, 0))
    if y >= 2022:
        H.add(observed(dt.date(y, 6, 19)))
    H.add(observed(dt.date(y, 7, 4))); H.add(nth_weekday(y, 9, 0, 1))
    tg = nth_weekday(y, 11, 3, 4); H.add(tg); H.add(observed(dt.date(y, 12, 25)))
    for d in (tg + dt.timedelta(days=1), dt.date(y, 12, 24), dt.date(y, 7, 3)):
        if d.weekday() < 5 and d not in H:
            E.add(d)
    _CAL[y] = (H, E)
    return _CAL[y]


def trading_day(d):
    return d.weekday() < 5 and d not in nyse_calendar(d.year)[0]


def close_min(d):
    return 13 * 60 if d in nyse_calendar(d.year)[1] else 16 * 60


def prev_trading_day(d):
    for _ in range(10):
        d -= dt.timedelta(days=1)
        if trading_day(d):
            return d
    return d


def next_trading_day(d):
    for _ in range(10):
        d += dt.timedelta(days=1)
        if trading_day(d):
            return d
    return d


AM_START, AM_END, PM_DELAY, PM_SPAN = 8 * 60 + 15, 9 * 60 + 29, 50, 240


def et_now():
    return dt.datetime.now(ET)


def due_brief(now):
    d, m = now.date(), now.hour * 60 + now.minute
    if not trading_day(d):
        return None
    if AM_START <= m <= AM_END:
        return (d, "am")
    c = close_min(d)
    if c + PM_DELAY <= m <= c + PM_DELAY + PM_SPAN:
        return (d, "pm")
    return None


def latest_brief(now):
    d, m = now.date(), now.hour * 60 + now.minute
    if trading_day(d) and m >= close_min(d) + PM_DELAY:
        return (d, "pm")
    if trading_day(d) and m >= AM_START:
        return (d, "am")
    return (prev_trading_day(d), "pm")


def et_dt(d, minutes):
    return dt.datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=ET)


# ---------------------------------------------------------------- http
try:  # Yahoo rejects plain server requests; curl_cffi presents a real browser's TLS fingerprint.
    from curl_cffi import requests as creq
    _CSESS = creq.Session(impersonate="chrome")
except Exception:  # noqa: BLE001
    _CSESS = None


def get_browser(url, timeout=15):
    if _CSESS is None:
        return get(url, timeout=timeout)
    r = _CSESS.get(url, timeout=timeout, headers={"Accept": "application/json,text/plain,*/*"})
    if r.status_code != 200:
        raise urllib.error.HTTPError(url, r.status_code, f"HTTP {r.status_code}", None, None)
    return r.text


def get(url, headers=None, timeout=15, raw=False):
    h = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
    return b if raw else b.decode("utf-8", "replace")


def get_json(url, headers=None, timeout=15):
    return json.loads(get(url, headers, timeout))


def r2(x):
    return None if x is None else round(float(x), 2)


def thin(arr, n):
    if len(arr) <= n:
        return arr
    st = (len(arr) - 1) / (n - 1)
    return [arr[round(i * st)] for i in range(n)]


# ---------------------------------------------------------------- quotes
def yahoo_quote(sym, prepost=False):
    last = None
    for host in ("query1", "query2"):
        try:
            u = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
                 f"?range=1d&interval={'5m' if prepost else '15m'}&includePrePost={'true' if prepost else 'false'}")
            j = json.loads(get_browser(u, timeout=12))
            res = (j.get("chart") or {}).get("result") or []
            if not res or not res[0].get("meta"):
                raise ValueError("no data")
            r, m = res[0], res[0]["meta"]
            q0 = ((r.get("indicators") or {}).get("quote") or [{}])[0]
            closes = [v for v in (q0.get("close") or []) if v is not None]
            p = m.get("regularMarketPrice")
            pc = m.get("chartPreviousClose", m.get("previousClose"))
            if p is None:
                raise ValueError("no price")
            q = {"s": sym, "n": m.get("shortName") or m.get("longName") or sym, "p": r2(p),
                 "pc": r2(pc) if pc is not None else None, "t": m.get("regularMarketTime") or 0,
                 "sp": [r2(v) for v in thin(closes, 32)]}
            if prepost and closes:
                xl = closes[-1]
                if abs(xl - p) > 1e-6:
                    q["x"] = r2(xl)  # pre/after-hours last trade
            return q
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.4)
    raise last or ValueError("no data")


def treasury_2y(name):
    """Official U.S. Treasury 2-yr par yield (latest close) from the dashboard's yields.json.
    Yahoo has no live 2-yr yield index, and its 2-yr yield future trades too thinly."""
    with open(os.path.join(ROOT, "yields.json")) as f:
        S = [r for r in json.load(f)["series"] if r.get("2Y") is not None]
    last, prev = S[-1], S[-2]
    t = int(et_dt(dt.date.fromisoformat(last["date"]), 16 * 60).timestamp())
    return {"s": "UST2Y", "n": name, "p": last["2Y"], "pc": prev["2Y"], "t": t, "sp": [r["2Y"] for r in S[-32:]],
            "y": 1, "src": "U.S. Treasury daily close"}


def get_quote(sym, name):
    if sym == "UST2Y":
        try:
            return treasury_2y(name)
        except Exception as e:  # noqa: BLE001
            log(f"  Treasury 2-yr failed: {type(e).__name__}")
            return {"s": sym, "n": name, "err": True}
    try:
        q = yahoo_quote(sym)
        q["n"] = name or q["n"]
    except Exception as e:  # noqa: BLE001
        log(f"  quote {sym} failed: {type(e).__name__}: {str(e)[:80]}")
        return {"s": sym, "n": name or sym, "err": True}
    if sym in YIELDS:
        q["y"] = 1
    if q.get("t") and time.time() - q["t"] > 3.5 * 86400:
        q["old"] = 1  # last trade is days old; flagged in the prompt and on the page
    return q


def quote_set(pairs):
    with ThreadPoolExecutor(max_workers=6) as ex:
        return list(ex.map(lambda p: get_quote(*p), pairs))


# ---------------------------------------------------------------- headlines
def clean(s, n=None):
    s = html.unescape(re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", s or ""))
    s = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
    return s[:n] if n else s


def parse_rss(xml, src):
    out = []
    for m in re.finditer(r"<item\b[\s\S]*?</item>", xml, re.I):
        it = m.group(0)

        def g(tag):
            x = re.search(rf"<{tag}\b[^>]*>([\s\S]*?)</{tag}>", it, re.I)
            return clean(x.group(1)) if x else ""
        h, u = g("title"), g("link") or g("guid")
        t = 0
        ds = g("pubDate") or g("dc:date")
        if ds:
            try:
                t = email.utils.parsedate_to_datetime(ds).timestamp()
            except (TypeError, ValueError):
                try:
                    t = dt.datetime.fromisoformat(ds.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    t = 0
        if h and re.match(r"^https?://", u):
            out.append({"h": h[:220], "u": u, "t": int(t), "s": src, "d": g("description")[:240]})
        if len(out) >= 40:
            break
    return out


def get_headlines(since_ts):
    def one(f):
        src, u = f
        try:
            return parse_rss(get(u, {"Accept": "application/rss+xml,application/xml,text/xml,*/*"}, 12), src)
        except Exception as e:  # noqa: BLE001
            log(f"  feed {src} failed: {type(e).__name__}")
            return []
    with ThreadPoolExecutor(max_workers=6) as ex:
        lists = list(ex.map(one, FEEDS))
    # Also use the dashboard's own headline collection (WSJ, Bloomberg, Reuters, FT, AP, Fed...)
    try:
        j = get_json(NEWS_JSON, timeout=12)
        extra = []
        for it in j.get("items", []):
            if it.get("s") == "AP":  # AP's feed comes via a broad Google News search and drifts off-topic
                continue
            ts = it.get("ts")
            t = int(dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()) if ts else 0
            extra.append({"h": it["t"][:220], "u": it["u"], "t": t, "s": it["s"], "d": (it.get("d") or "")[:240]})
        lists.append(extra)
    except Exception as e:  # noqa: BLE001
        log(f"  dashboard news.json failed: {type(e).__name__}")
    seen, out = set(), []
    for n in sorted((x for l in lists for x in l), key=lambda x: -x["t"]):
        if n["t"] and n["t"] < since_ts:
            continue
        k = " ".join(re.sub(r"[^a-z0-9 ]", "", n["h"].lower()).split()[:8])
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out[:40]


# ---------------------------------------------------------------- earnings
def money(s):
    try:
        v = float(re.sub(r"[$,()]", "", str(s or "")))
        return -v if "(" in str(s) else v
    except ValueError:
        return None


def verdict(a, e, kind):
    if a is None or e is None:
        return None
    if kind == "eps":
        if abs(a - e) < 0.005:
            return "In line"
    elif e and abs(a - e) / abs(e) <= 0.005:
        return "In line"
    return "Beat" if a > e else "Miss"


def earnings_for(wants, allow_untimed_day=None):
    """wants: list of (date, hour) with hour in bmo/amc/dmh."""
    key = os.environ.get("FINNHUB_KEY", "").strip()
    days = sorted({d for d, _ in wants})
    lst, src = [], ""
    if key:
        try:
            j = get_json(f"https://finnhub.io/api/v1/calendar/earnings?from={days[0]}&to={days[-1]}&token={urllib.parse.quote(key)}")
            for e in j.get("earningsCalendar") or []:
                hr = e.get("hour") or ""
                ok = any(e.get("date") == str(d) and hr == h for d, h in wants) or (allow_untimed_day and e.get("date") == str(allow_untimed_day) and not hr)
                if not ok:
                    continue
                lst.append({"sym": e.get("symbol"), "day": e.get("date"), "when": hr,
                            "epsA": e.get("epsActual"), "epsE": e.get("epsEstimate"),
                            "revA": e.get("revenueActual"), "revE": e.get("revenueEstimate"),
                            "size": float(e.get("revenueEstimate") or 0)})
            src = "Finnhub"
        except Exception as e:  # noqa: BLE001
            log(f"  Finnhub earnings failed: {type(e).__name__}: {str(e)[:80]}")
            lst = []
    if not lst:
        for d in days:
            try:
                j = get_json(f"https://api.nasdaq.com/api/calendar/earnings?date={d}",
                             {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
                for r in ((j.get("data") or {}).get("rows") or []):
                    when = "bmo" if r.get("time") == "time-pre-market" else "amc" if r.get("time") == "time-after-hours" else ""
                    if not any(wd == d and (wh == when or (not when and allow_untimed_day == d)) for wd, wh in wants):
                        continue
                    lst.append({"sym": r.get("symbol"), "name": r.get("name"), "day": str(d), "when": when,
                                "epsA": None, "epsE": money(r.get("epsForecast")), "revA": None, "revE": None,
                                "size": money(r.get("marketCap")) or 0, "cap": True})
                src = "Nasdaq"
            except Exception as e:  # noqa: BLE001
                log(f"  Nasdaq earnings {d} failed: {type(e).__name__}: {str(e)[:80]}")
    lst = [e for e in lst if e.get("sym") and re.match(r"^[A-Z.\-]{1,7}$", e["sym"])]
    # drop tiny companies (revenue estimate under $300M, or market cap under $2B on the Nasdaq fallback)
    lst = [e for e in lst if not e["size"] or e["size"] >= (2e9 if e.get("cap") else 3e8)]
    return src, lst


def get_earnings(day, kind):
    prev = prev_trading_day(day)
    if kind == "am":
        wants, untimed = [(day, "bmo"), (prev, "amc")], None
    else:
        wants, untimed = [(day, "amc"), (day, "dmh"), (day, "bmo")], day
    src, lst = earnings_for(wants, untimed)

    def rank(e):
        if kind == "am":
            return 0 if e["when"] == "bmo" else 1
        return 0 if e["when"] == "amc" else 2 if e["when"] == "bmo" else 1
    lst.sort(key=lambda e: (rank(e), -e["size"]))
    lst = lst[:12]
    top = lst[:8]
    with ThreadPoolExecutor(max_workers=6) as ex:
        moves = list(ex.map(lambda e: _safe(lambda: yahoo_quote(e["sym"], True)), top))
    for e, q in zip(top, moves):
        if not q:
            continue
        e.setdefault("name", q["n"])
        if not e.get("name"):
            e["name"] = q["n"]
        base = q["p"]
        if q.get("x") and base:
            e["mv"] = r2((q["x"] - base) / base * 100)
            e["mvWhen"] = "pre-market" if kind == "am" else "after hours"
        elif kind == "pm" and e["day"] == str(day) and e["when"] == "bmo" and q.get("pc"):
            e["mv"] = r2((q["p"] - q["pc"]) / q["pc"] * 100)
            e["mvWhen"] = "today"
    for e in lst:
        e["epsV"] = verdict(e.get("epsA"), e.get("epsE"), "eps")
        e["revV"] = verdict(e.get("revA"), e.get("revE"), "rev")
        e.pop("cap", None)
    return {"src": src, "list": lst}


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- coming up
def econ_events(days):
    out = []
    for name in ("thisweek", "nextweek"):
        try:
            j = get_json(f"https://nfs.faireconomy.media/ff_calendar_{name}.json", timeout=12)
        except Exception as e:  # noqa: BLE001
            log(f"  econ calendar {name} failed: {type(e).__name__}")
            continue
        for ev in j:
            if ev.get("country") != "USD" or ev.get("impact") not in ("High", "Medium"):
                continue
            try:
                when = dt.datetime.fromisoformat(ev["date"]).astimezone(ET)
            except (KeyError, ValueError):
                continue
            if when.date() not in days:
                continue
            out.append({"title": ev.get("title"), "ts": int(when.timestamp()), "impact": ev.get("impact"),
                        "forecast": ev.get("forecast") or None, "previous": ev.get("previous") or None})
    uniq = {(e["title"], e["ts"]): e for e in out}
    return sorted(uniq.values(), key=lambda e: e["ts"])[:14]


def upcoming(day, kind):
    nxt = day if kind == "am" else next_trading_day(day)
    up = {"day": str(nxt), "econ": econ_events({nxt}), "earn": []}
    if kind == "pm":
        src, lst = earnings_for([(nxt, "bmo"), (nxt, "amc")])
        lst.sort(key=lambda e: -e["size"])
        up["earn"] = [{"sym": e["sym"], "when": e["when"], "name": e.get("name")} for e in lst[:10]]
    return up


# ---------------------------------------------------------------- writing
def pct(q):
    return None if not q or q.get("err") or not q.get("pc") else (q["p"] - q["pc"]) / q["pc"] * 100


def fmt_q(q):
    if not q or q.get("err"):
        return f"{q['n'] if q else '?'}: unavailable"
    if q.get("y") and q.get("pc") is not None:
        bp = round((q["p"] - q["pc"]) * 100)
        s = f"{q['n']}: {q['p']:.2f}% ({'+' if bp >= 0 else ''}{bp} bp vs prior close {q['pc']:.2f}%)"
    else:
        c = pct(q)
        s = f"{q['n']}: {q['p']}" + (f" ({'+' if c >= 0 else ''}{c:.2f}% vs prior close {q['pc']})" if c is not None else "")
    if q.get("src"):
        s += f" [{q['src']}, {dt.datetime.fromtimestamp(q['t'], ET).strftime('%a %b %-d')}]"
    elif q.get("t"):
        s += f" [as of {dt.datetime.fromtimestamp(q['t'], ET).strftime('%a %-I:%M %p')} ET]"
    if q.get("old"):
        s += " (STALE: last update several days ago; do not describe as today's move)"
    return s


def brief_prompt(b):
    D, kind = b["data"], b["kind"]
    when = ("pre-market (morning) brief, written before the US stock market opens" if kind == "am"
            else "after-the-close (evening) brief, written after the US stock market closed")
    L = [f"Date: {b['day']} (US Eastern). This is the {when}."]
    groups = ([("US stock futures (overnight trading)", "fut"), ("US indexes (last session)", "us"), ("Overseas markets", "global"),
               ("Rates, dollar, commodities, crypto", "macro")] if kind == "am"
              else [("US indexes", "us"), ("Sectors (ETFs)", "sectors"), ("Rates, dollar, commodities, crypto", "macro")])
    for label, k in groups:
        a = D["q"].get(k) or []
        if a:
            L.append(f"\n{label}:\n" + "\n".join(fmt_q(q) for q in a))
    E = (D.get("earn") or {}).get("list") or []
    if E:
        def fe(e):
            mv = f"; stock {'+' if e['mv'] >= 0 else ''}{e['mv']}% {e.get('mvWhen', '')}" if e.get("mv") is not None else ""
            return (f"{e['sym']}{' (' + e['name'] + ')' if e.get('name') else ''} {e['day']} {e['when']}: "
                    f"EPS {e['epsA'] if e['epsA'] is not None else 'n/a'} vs est {e['epsE'] if e['epsE'] is not None else 'n/a'}"
                    f"{' (' + e['epsV'] + ')' if e.get('epsV') else ''}; revenue {e['revA'] if e['revA'] is not None else 'n/a'} vs est "
                    f"{e['revE'] if e['revE'] is not None else 'n/a'}{' (' + e['revV'] + ')' if e.get('revV') else ''}{mv}")
        L.append(f"\nEarnings reports ({'this morning = bmo, last night = amc' if kind == 'am' else 'today; amc = after the close'}):\n"
                 + "\n".join(fe(e) for e in E))
    up = D.get("up") or {}
    if up.get("econ"):
        L.append(f"\nScheduled US economic releases ({up['day']}):\n" + "\n".join(
            f"{dt.datetime.fromtimestamp(e['ts'], ET).strftime('%-I:%M %p')} ET {e['title']} ({e['impact']} impact)"
            + (f", forecast {e['forecast']}" if e.get("forecast") else "") + (f", previous {e['previous']}" if e.get("previous") else "")
            for e in up["econ"]))
    if up.get("earn"):
        L.append(f"\nEarnings scheduled {up['day']}: " + ", ".join(f"{e['sym']} ({e['when'] or 'time n/a'})" for e in up["earn"]))
    N = D.get("news") or []
    if N:
        L.append("\nHeadlines (newest first, numbered):\n" + "\n".join(
            f"[{i + 1}] {dt.datetime.fromtimestamp(n['t'] or time.time(), ET).strftime('%-I:%M %p')} {n['s']}: {n['h']}"
            + (f" — {n['d'][:150]}" if n.get("d") and n["d"] != n["h"] else "") for i, n in enumerate(N)))
    return "\n".join(L)


SYS = """You are a markets editor writing a short, plain-English market brief for one wealth advisor reading it on a phone.
Rules:
- Use ONLY the prices, earnings, scheduled events and headlines provided. Never invent numbers, companies, events or quotes. If something isn't in the data, leave it out.
- Percent moves you mention must match the data given. Round to one decimal. Yield moves are in basis points (bp).
- Explain WHY markets moved when the headlines support it; say "no single clear driver" if they don't.
- Neutral tone. No investment advice, no predictions, no hype.
- Cite headlines by their [number] in "src". Every driver must cite at least one headline that directly supports it.
- Do not speculate. Never mention Fed rate hikes, cuts or decisions unless a headline says so.
Reply with JSON only, no other text, in exactly this shape:
{"title": "one specific headline under 90 characters naming what moved and why, e.g. 'Stocks slip as Nvidia CEO exit weighs on tech; oil jumps'",
 "summary": "2-3 sentences: the big picture",
 "drivers": [{"h": "3-6 word lead", "t": "1-2 sentences", "src": [1,2]}],
 "earnings": [{"sym": "TICKER", "t": "one sentence"}],
 "watch": [{"t": "short item to watch next", "src": [3]}]}
Give 3-6 drivers. Add an earnings note ONLY when a headline adds context beyond the numbers (cite it in the sentence's wording); the numbers are already shown in a table, so do not just restate beat/miss.
Give 0-4 watch items. Each must be either a scheduled economic release or earnings report listed above (use "src": []) or something a numbered headline names as upcoming (cite it)."""


def parse_ai_json(txt):
    if isinstance(txt, dict):
        return txt
    s = str(txt or "")
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except ValueError:
        return None


def clean_ai(o, n_news, known=()):
    """known: lower-case names of scheduled releases and tickers a watch item may refer to without a citation."""
    if not isinstance(o, dict):
        return None

    def st(v, n):
        if not isinstance(v, str):
            return ""
        # citations are shown as tags on the page, so drop "as seen in headline [4]" asides and stray [n]
        v = re.sub(r",?\s*\(?\s*(as|which was|this was)?\s*(seen|mentioned|reported|noted|shown|cited)\s+in\s+(the\s+)?(headlines?|earnings table|data|table)[^.;)]*\)?", "", v, flags=re.I)
        v = re.sub(r"\s*\[\d+(?:\s*,\s*\d+)*\]", "", v)
        return re.sub(r"\s+([.,;])", r"\1", v).strip()[:n]
    out = {
        "title": st(o.get("title"), 140), "summary": st(o.get("summary"), 900),
        "drivers": [d for d in ({"h": st(d.get("h"), 80), "t": st(d.get("t"), 500),
                                 "src": [int(n) for n in (d.get("src") or []) if str(n).isdigit() and 1 <= int(n) <= n_news][:4]}
                                for d in (o.get("drivers") or [])[:7] if isinstance(d, dict)) if d["t"]],
        "earnings": [e for e in ({"sym": st(e.get("sym"), 10).upper(), "t": st(e.get("t"), 300)}
                                 for e in (o.get("earnings") or [])[:12] if isinstance(e, dict)) if e["sym"] and e["t"]],
        "watch": [],
    }
    out["drivers"] = [d for d in out["drivers"] if d["src"]]  # a driver must be backed by a headline
    # earnings notes that only restate the table add nothing
    raw = {str(x.get("sym", "")).upper(): str(x.get("t", "")) for x in (o.get("earnings") or []) if isinstance(x, dict)}
    out["earnings"] = [e for e in out["earnings"] if not re.search(r"earnings table|reported in the (data|table)", raw.get(e["sym"], ""), re.I)]
    for w in (o.get("watch") or [])[:6]:
        if isinstance(w, str):
            w = {"t": w, "src": []}
        if not isinstance(w, dict) or not st(w.get("t"), 200):
            continue
        txt = st(w.get("t"), 200)
        src = [int(n) for n in (w.get("src") or []) if str(n).isdigit() and 1 <= int(n) <= n_news]
        if src or (known and any(k in txt.lower() for k in known)):
            out["watch"].append(txt)
    out["watch"] = out["watch"][:4]
    return out if out["title"] or out["summary"] else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_POST = urllib.request.build_opener(_NoRedirect)


def post_json(url, body, headers, timeout=90):
    """POST JSON, re-sending the POST (not a GET) if the server redirects."""
    data = json.dumps(body).encode()
    for _ in range(4):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "market-dash-brief/1.0", **headers})
        try:
            with _POST.open(req, timeout=timeout) as r:
                raw, status = r.read().decode("utf-8", "replace"), r.status
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise
        try:
            return json.loads(raw)
        except ValueError:
            raise ValueError(f"non-JSON reply (HTTP {status}) from {url}: {raw[:200]!r}")
    raise ValueError("too many redirects")


def write_with_ai(b):
    prompt, n_news = brief_prompt(b), len(b["data"].get("news") or [])
    up = b["data"].get("up") or {}
    known = tuple({(e.get("title") or "").lower() for e in up.get("econ") or [] if e.get("title")}
                  | {(e.get("sym") or "").lower() for e in up.get("earn") or [] if e.get("sym")})
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        model = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-sonnet-5-5"
        j = post_json("https://api.anthropic.com/v1/messages",
                      {"model": model, "max_tokens": 1600, "system": SYS, "messages": [{"role": "user", "content": prompt}]},
                      {"x-api-key": key, "anthropic-version": "2023-06-01"})
        txt = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
        o = clean_ai(parse_ai_json(txt), n_news, known)
        if not o:
            raise ValueError("Claude reply could not be read")
        return {**o, "by": "Claude (Anthropic)"}
    acct, cft = os.environ.get("CF_ACCOUNT_ID", "").strip(), os.environ.get("CF_API_TOKEN", "").strip()
    if acct and cft:  # Cloudflare Workers AI: free daily allowance (10,000 neurons/day)
        model = os.environ.get("CF_AI_MODEL", "").strip() or "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
        j = post_json(f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{model}",
                      {"messages": [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}],
                       "max_tokens": 3000, "temperature": 0.3}, {"Authorization": f"Bearer {cft}"})
        if not j.get("success", True):
            raise ValueError("Workers AI error: " + json.dumps(j.get("errors"))[:200])
        res = j.get("result") or {}
        finish = ""
        if isinstance(res, dict) and res.get("choices"):  # OpenAI-style reply shape
            ch = res["choices"][0] or {}
            finish = ch.get("finish_reason") or ""
            reply = (ch.get("message") or {}).get("content") or ""
        else:
            reply = res.get("response", res) if isinstance(res, dict) else res
        o = clean_ai(parse_ai_json(reply), n_news, known)
        if not o:
            tail = reply if isinstance(reply, str) else json.dumps(reply)
            raise ValueError(f"Workers AI reply could not be read (finish={finish or '?'}, {len(tail)} chars): "
                             f"{tail[:120]!r} ... {tail[-160:]!r}")
        return {**o, "by": "Llama 3.3 (Cloudflare Workers AI)" if "llama-3.3" in model else f"{model} (Cloudflare Workers AI)"}
    return None


def fallback_text(b):
    q = b["data"]["q"]

    def pick(k, s):
        return next((x for x in q.get(k) or [] if x["s"] == s), None)
    lead = [pick("fut", "ES=F"), pick("fut", "NQ=F")] if b["kind"] == "am" else [pick("us", "^GSPC"), pick("us", "^IXIC"), pick("us", "^DJI")]
    bits = [f"{x['n'].replace(' futures', '')} {'+' if pct(x) >= 0 else ''}{pct(x):.1f}%" for x in lead if x and pct(x) is not None]
    title = (("Futures: " if b["kind"] == "am" else "") + ", ".join(bits)) if bits else ("Morning market brief" if b["kind"] == "am" else "Market close brief")
    return {"title": title, "summary": "", "drivers": [], "earnings": [], "watch": [], "by": ""}


# ---------------------------------------------------------------- build / save
def build(day, kind):
    t0 = time.time()
    b = {"id": f"{day}-{kind}", "day": str(day), "kind": kind, "data": {"q": {}}}
    D = b["data"]
    log(f"Building {b['id']}")
    sets = ["fut", "us", "global", "macro"] if kind == "am" else ["us", "sectors", "macro"]
    pairs = [p for k in sets for p in MKT[k]]
    res = quote_set(pairs)
    i = 0
    for k in sets:
        D["q"][k] = res[i:i + len(MKT[k])]
        i += len(MKT[k])
    ok = sum(1 for q in res if not q.get("err"))
    log(f"  quotes: {ok}/{len(res)} ok")
    if kind == "am":
        since = et_dt(prev_trading_day(day), 16 * 60)
    else:
        since = et_dt(day, 6 * 60)
    since_ts = min(since.timestamp(), time.time() - 6 * 3600)
    D["news"] = get_headlines(since_ts)
    log(f"  headlines: {len(D['news'])}")
    D["earn"] = get_earnings(day, kind)
    log(f"  earnings: {len(D['earn']['list'])} from {D['earn']['src'] or 'none'}")
    D["up"] = upcoming(day, kind)
    log(f"  coming up: {len(D['up']['econ'])} events, {len(D['up']['earn'])} earnings")
    try:
        try:
            t = write_with_ai(b)
        except Exception as e:  # noqa: BLE001  -- one retry: AI replies are occasionally malformed
            log(f"  AI write-up attempt 1 failed ({type(e).__name__}); retrying")
            time.sleep(3)
            t = write_with_ai(b)
    except Exception as e:  # noqa: BLE001
        body = ""
        if hasattr(e, "read"):
            try:
                body = e.read().decode()[:300]
            except Exception:  # noqa: BLE001
                pass
        D["aiErr"] = f"{type(e).__name__}: {str(e)[:150]} {body}".strip()
        log(f"  AI write-up failed: {D['aiErr']}")
        t = None
    D["text"] = t or fallback_text(b)
    log(f"  write-up: {D['text'].get('by') or 'none (data-only brief)'}")
    b["created"] = int(time.time())
    b["secs"] = round(time.time() - t0, 1)
    b["quotesOk"] = ok
    b["log"] = LOG[-40:]
    return b


def load_index():
    try:
        with open(os.path.join(OUT_DIR, "index.json")) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"briefs": []}


def save(b):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"{b['id']}.json"), "w") as f:
        json.dump(b, f, separators=(",", ":"))
    idx = load_index()
    items = [x for x in idx.get("briefs", []) if x["id"] != b["id"]]
    T = b["data"]["text"]
    items.append({"id": b["id"], "day": b["day"], "kind": b["kind"], "title": T.get("title", ""),
                  "summary": (T.get("summary") or "")[:220], "created": b["created"]})
    cutoff = str(et_now().date() - dt.timedelta(days=KEEP_DAYS))
    keep = sorted((x for x in items if x["day"] >= cutoff), key=lambda x: (x["day"], x["kind"] == "pm"), reverse=True)
    for x in items:
        if x not in keep:
            try:
                os.remove(os.path.join(OUT_DIR, f"{x['id']}.json"))
            except FileNotFoundError:
                pass
    with open(os.path.join(OUT_DIR, "index.json"), "w") as f:
        json.dump({"updated": int(time.time()), "briefs": keep}, f, separators=(",", ":"))


def notify(b):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    server = os.environ.get("NTFY_SERVER", "").strip() or "https://ntfy.sh"
    site = os.environ.get("SITE_URL", "").strip() or "https://joshlange99-blip.github.io/Market-Dash/"
    T = b["data"]["text"]
    head = ("☀️ Morning market brief" if b["kind"] == "am" else "🌆 Market close brief")
    body = (T.get("title") or "Your market brief is ready.") + ("\n" + T["summary"] if T.get("summary") else "")
    # HTTP headers must be Latin-1, so the emoji title is sent RFC 2047-encoded (ntfy decodes it).
    title = "=?UTF-8?B?" + base64.b64encode(head.encode()).decode() + "?="
    req = urllib.request.Request(f"{server.rstrip('/')}/{urllib.parse.quote(topic)}", data=body[:1000].encode(), method="POST",
                                 headers={"Title": title, "Click": site + "#briefs", "Tags": "chart_with_upwards_trend"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
        log("  notification sent")
    except Exception as e:  # noqa: BLE001
        log(f"  notification failed: {type(e).__name__}: {str(e)[:100]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="scheduled", choices=["scheduled", "latest", "am", "pm"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    now = et_now()
    if a.mode == "scheduled":
        due = due_brief(now)
        if not due:
            log(f"No brief due at {now:%a %Y-%m-%d %H:%M} ET.")
            return 0
    elif a.mode == "latest":
        due = latest_brief(now)
    else:
        d = now.date() if trading_day(now.date()) else prev_trading_day(now.date())
        due = (d, a.mode)
    bid = f"{due[0]}-{due[1]}"
    if not a.force and any(x["id"] == bid for x in load_index().get("briefs", [])):
        log(f"{bid} already published.")
        return 0
    b = build(*due)
    if b["quotesOk"] == 0 and not b["data"]["news"]:
        log("No market data or headlines could be fetched; not publishing.")
        return 1
    save(b)
    log(f"Published {bid} in {b['secs']}s: {b['data']['text'].get('title')}")
    notify(b)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write("### Market brief\n```\n" + "\n".join(LOG) + "\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
