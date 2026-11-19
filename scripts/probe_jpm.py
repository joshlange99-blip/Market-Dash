"""One-off probe: where does J.P. Morgan's Weekly Market Recap text live? Writes structure only (no article text)."""
import re
from curl_cffi import requests as r
S = r.Session(impersonate="chrome")
out = []
for page in ["https://am.jpmorgan.com/us/en/asset-management/adv/insights/market-insights/market-updates/weekly-market-recap/",
             "https://am.jpmorgan.com/us/en/asset-management/per/insights/market-insights/market-updates/weekly-market-recap/"]:
    x = S.get(page, timeout=30); h = x.text
    out.append(f"== {page}\nstatus {x.status_code} len {len(h)}")
    for pat in ["week in review", "week ahead", "View and download PDF", "Week of", "application/json", "__NEXT_DATA__", ".model.json", "data-src", "/content/dam/"]:
        out.append(f"  {pat!r}: {len(re.findall(re.escape(pat), h, re.I))}")
    for u in sorted(set(re.findall(r'["\'(]([^"\'()\s]+?\.(?:pdf|json))["\')]', h)))[:40]:
        out.append(f"  url: {u}")
    for u in sorted(set(re.findall(r'["\'](/[^"\'\s]*weekly-market-recap[^"\'\s]*)["\']', h)))[:30]:
        out.append(f"  wmr: {u}")
    i = h.lower().find("week of")
    if i >= 0:
        out.append("  tags near 'Week of': " + " ".join(re.findall(r"<(\w+)[^>]*class=\"([^\"]{0,60})\"", h[max(0, i-1500):i+200]))[:600])
open("probe.txt", "w").write("\n".join(out))
