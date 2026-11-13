# Market Pulse

A personal market dashboard: live quotes (TradingView widgets), Treasury yields, a U.S. economic calendar, a multi-source headline feed, a release playbook, and twice-daily market briefs.

`index.html` is a single static page served by GitHub Pages. Everything else is produced by scheduled GitHub Actions and saved as JSON that the page reads.

| Job | Workflow | Output |
|---|---|---|
| Treasury yields (official daily curve) | `yields.yml` → `scripts/update_yields.py` | `yields.json` |
| Headlines (CNBC, WSJ, MarketWatch, Bloomberg, FT, Reuters, AP, Fed, BEA) | `news.yml` → `scripts/update_news.py` | `news.json` on the `data` branch |
| Market briefs (before the open / after the close) | `brief.yml` → `scripts/market_brief.py` | `briefs/index.json` + `briefs/<date>-am|pm.json` (last ~2 weeks) |

## Market briefs

* **Morning (~8:15–8:30 AM ET):** headlines since the prior close, S&P/Nasdaq/Dow/Russell futures and indexes, VIX, 10-yr and 2-yr yields, dollar, oil, gold, Bitcoin, Japan/Hong Kong/Europe/UK, this morning's and last night's earnings (EPS and revenue vs. estimates, Beat/Miss/In line, pre-market move), today's scheduled data.
* **After the close (~4:50–5:05 PM ET; ~1:50 PM on early-close days):** indexes and all 11 sector ETFs, what drove the session, after-hours earnings, and what's coming next (economic releases and earnings).
* NYSE holidays are skipped. A brief is never published twice, so extra triggers are harmless.
* Tables come straight from the data. The AI only writes the title, summary, drivers (citing headlines by number), earnings notes and watch list, and may use only the supplied data. If the AI step fails, the brief still publishes with the data.

### Secrets (Settings → Secrets and variables → Actions)

| Secret | Needed? | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | Recommended | Claude writes the summary (default model `claude-sonnet-5-5`, about 2 cents a brief; override with a repository *variable* `CLAUDE_MODEL`). |
| `CF_ACCOUNT_ID` + `CF_API_TOKEN` | Free alternative | Cloudflare Workers AI writes the summary within its free daily allowance (token needs the "Workers AI" permission). Used only when no Claude key is set. |
| `FINNHUB_KEY` | Recommended | Earnings with *actual* EPS/revenue vs. estimates (free key). Without it, Nasdaq's public calendar gives estimates only. |
| `NTFY_TOPIC` | Optional | Phone notification through the free ntfy app when each brief is published. |

### On-time runs

GitHub's built-in scheduler can start runs late or skip them. All three workflows also listen for a `tick` sent from outside, so a free external timer (e.g. cron-job.org) can run them on time:

```
POST https://api.github.com/repos/joshlange99-blip/Market-Dash/dispatches
Authorization: Bearer <fine-grained token: this repo only, Contents: read & write>
Accept: application/vnd.github+json
{"event_type": "tick"}
```

Every 15 minutes is enough. The GitHub schedules stay on as a backup.

### Run by hand

Actions → **Market brief** → Run workflow → choose `latest`, `am` or `pm` (tick *force* to rebuild).
