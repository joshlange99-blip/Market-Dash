"""Fetch the official U.S. Treasury daily par yield curve and write yields.json.

Primary source: home.treasury.gov daily par yield curve CSV (no key).
Fallback: FRED constant-maturity series (no key).
Only rewrites yields.json when the data actually changes.
"""
import csv, io, json, re, sys, datetime as dt, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (compatible; market-dash/1.0; +https://github.com)"}
TENORS = ["1M", "2M", "3M", "4M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]
KEEP_DAYS = 400


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8-sig")


def tenor_from_header(h):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(mo|month|yr|year)", h.strip().lower())
    if not m:
        return None
    n, unit = m.group(1), m.group(2)
    if "." in n:
        return None
    return f"{n}{'M' if unit.startswith('mo') else 'Y'}"


def from_treasury():
    rows = {}
    year = dt.date.today().year
    for y in (year - 1, year):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value={y}&page&_format=csv")
        text = get(url)
        for rec in csv.DictReader(io.StringIO(text)):
            d = rec.get("Date") or rec.get("date")
            if not d:
                continue
            date = dt.datetime.strptime(d.strip(), "%m/%d/%Y").date().isoformat()
            vals = {}
            for k, v in rec.items():
                t = tenor_from_header(k or "")
                if t in TENORS and v not in (None, "", "N/A"):
                    try:
                        vals[t] = round(float(v), 3)
                    except ValueError:
                        pass
            if vals:
                rows[date] = vals
    return rows, "U.S. Department of the Treasury — Daily Par Yield Curve Rates"


def from_fred():
    ids = {"1M": "DGS1MO", "3M": "DGS3MO", "6M": "DGS6MO", "1Y": "DGS1", "2Y": "DGS2", "3Y": "DGS3",
           "5Y": "DGS5", "7Y": "DGS7", "10Y": "DGS10", "20Y": "DGS20", "30Y": "DGS30"}
    start = (dt.date.today() - dt.timedelta(days=KEEP_DAYS + 10)).isoformat()
    rows = {}
    for t, sid in ids.items():
        text = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}")
        for rec in csv.reader(io.StringIO(text)):
            if len(rec) < 2 or not re.match(r"\d{4}-\d{2}-\d{2}", rec[0]):
                continue
            try:
                rows.setdefault(rec[0], {})[t] = round(float(rec[1]), 3)
            except ValueError:
                pass
    return rows, "Federal Reserve Bank of St. Louis (FRED), Treasury constant maturity yields"


def main():
    rows, source = {}, ""
    try:
        rows, source = from_treasury()
    except Exception as e:  # noqa: BLE001
        print("Treasury fetch failed:", e, file=sys.stderr)
    if len(rows) < 5:
        try:
            rows, source = from_fred()
        except Exception as e:  # noqa: BLE001
            print("FRED fetch failed:", e, file=sys.stderr)
    rows = {d: v for d, v in rows.items() if "2Y" in v and "10Y" in v}
    if len(rows) < 5:
        print("No usable yield data; leaving yields.json unchanged.", file=sys.stderr)
        sys.exit(1)
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    series = [{"date": d, **rows[d]} for d in sorted(rows) if d >= cutoff]
    out = {"source": source, "latest_date": series[-1]["date"], "tenors": TENORS, "series": series}
    try:
        with open("yields.json") as f:
            old = json.load(f)
        if old.get("series") == out["series"]:
            print("No change in data.")
            return
    except FileNotFoundError:
        pass
    out["updated_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    with open("yields.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {len(series)} days; latest {out['latest_date']} from {source}")


if __name__ == "__main__":
    main()
