"""Pull market headlines from public RSS/Atom feeds and write news.json.

Runs on GitHub Actions (never in the browser). Keeps the last 48 hours,
de-duplicates by headline, and records which sources worked.
"""
import datetime as dt, email.utils, html, json, re, sys, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# (source label, topic, url)
FEEDS = [
    ("CNBC", "Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC", "Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("CNBC", "Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
    ("MarketWatch", "Markets", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MarketWatch", "Markets", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ("WSJ", "Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    ("WSJ", "Economy", "https://feeds.content.dowjones.io/public/rss/socialeconomyfeed"),
    ("Bloomberg", "Markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("Bloomberg", "Economy", "https://feeds.bloomberg.com/economics/news.rss"),
    ("Yahoo Finance", "Markets", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EDJI,%5EIXIC&region=US&lang=en-US"),
    ("Financial Times", "Markets", "https://www.ft.com/markets?format=rss"),
    ("Reuters", "Markets", "https://news.google.com/rss/search?q=site:reuters.com+markets+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Reuters", "Economy", "https://news.google.com/rss/search?q=site:reuters.com+economy+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("Federal Reserve", "Fed & Data", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Federal Reserve", "Fed & Data", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ("BEA", "Fed & Data", "https://apps.bea.gov/rss/rss.xml"),
    ("AP", "Economy", "https://news.google.com/rss/search?q=site:apnews.com+business+when:1d&hl=en-US&gl=US&ceid=US:en"),
]

KEEP_HOURS = 48
MAX_ITEMS = 600


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def clean(text, limit=None):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", html.unescape(text))
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return text


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(el, *names):
    for c in el:
        if local(c.tag) in names and (c.text or "").strip():
            return c.text
    return ""


def parse(raw, source, topic):
    root = ET.fromstring(raw)
    items = []
    for el in root.iter():
        kind = local(el.tag)
        if kind not in ("item", "entry"):
            continue
        title = clean(child_text(el, "title"))
        link = ""
        for c in el:
            if local(c.tag) == "link":
                link = (c.get("href") or c.text or "").strip()
                if link:
                    break
        if not link:
            link = child_text(el, "guid", "id").strip()
        when = parse_date(child_text(el, "pubDate", "published", "updated", "date"))
        summary = clean(child_text(el, "description", "summary", "content"), 240)
        src = source
        if "news.google.com" in link:
            # Google News titles end with " - Publisher"; keep only Reuters stories.
            m = re.search(r"\s+-\s+([^-]+)$", title)
            if m:
                want = "associated press" if source == "AP" else source.lower()
                if want not in m.group(1).lower() and not (source == "AP" and m.group(1).strip().lower() == "ap news"):
                    continue
                title = title[: m.start()].strip()
            summary = ""
        if summary and summary.lower().startswith(title.lower()[:40]):
            summary = ""
        if re.match(r"^(NEWSLETTER|Podcast|VIDEO|WATCH|LIVE BLOG)\b", title, re.I):
            continue
        if not title or not link.startswith("http"):
            continue
        items.append({"t": title, "u": link, "s": src, "c": topic, "d": summary,
                      "ts": when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None})
    return items


def one(feed):
    source, topic, url = feed
    try:
        return feed, parse(fetch(url), source, topic), None
    except Exception as e:  # noqa: BLE001
        return feed, [], f"{type(e).__name__}: {e}"[:160]


def main():
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=KEEP_HOURS)
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(one, FEEDS))
    seen, items, status = set(), [], []
    for (source, topic, url), got, err in results:
        kept = 0
        for it in got:
            ts = parse_date(it["ts"]) if it["ts"] else None
            if ts and ts < cutoff:
                continue
            if ts and ts > now + dt.timedelta(minutes=10):
                it["ts"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            key = re.sub(r"[^a-z0-9]", "", it["t"].lower())[:90]
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
            kept += 1
        status.append({"source": source, "topic": topic, "url": url, "items": kept, "error": err})
        print(f"{'OK ' if not err else 'ERR'} {source:16} {topic:11} {kept:3}  {err or ''}")
    items.sort(key=lambda x: x["ts"] or "", reverse=True)
    items = items[:MAX_ITEMS]
    if not items:
        print("No headlines fetched; not writing.", file=sys.stderr)
        sys.exit(1)
    out = {"updated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "count": len(items), "feeds": status, "items": items}
    with open("news.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {len(items)} headlines")


if __name__ == "__main__":
    main()
