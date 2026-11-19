"""One-off probe: structure of J.P. Morgan's Weekly Market Recap data file (key paths and sizes only)."""
import re, json
from urllib.parse import urljoin
from curl_cffi import requests as r
S = r.Session(impersonate="chrome")
page = "https://am.jpmorgan.com/us/en/asset-management/adv/insights/market-insights/market-updates/weekly-market-recap/"
h = S.get(page, timeout=30).text
m = re.search(r'["\'](/content/[^"\']*jpm_am_wmr\.model\.json[^"\']*)["\']', h)
out = [f"model url found: {bool(m)}"]
if m:
    x = S.get(urljoin(page, m.group(1)), timeout=30)
    out.append(f"status {x.status_code} len {len(x.text)} type {x.headers.get('content-type')}")
    try:
        j = x.json()
        def walk(o, p=""):
            if isinstance(o, dict):
                for k, v in o.items(): walk(v, f"{p}.{k}")
            elif isinstance(o, list):
                out.append(f"{p}[] list len {len(o)}")
                for i, v in enumerate(o[:3]): walk(v, f"{p}[{i}]")
            elif isinstance(o, str):
                flag = " PDF" if ".pdf" in o.lower() else ""
                short = o if (len(o) < 70 and not re.search(r"[a-z]{4,} [a-z]{4,} [a-z]{4,} [a-z]{4,}", o)) else ""
                out.append(f"{p}: str {len(o)}{flag} {short!r}" if short or flag else f"{p}: str {len(o)}")
            else:
                out.append(f"{p}: {type(o).__name__} {o!r}"[:120])
        walk(j)
    except Exception as e:
        out.append(f"not json: {e} :: {x.text[:200]!r}")
open("probe.txt", "w").write("\n".join(out[:400]))
