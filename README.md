# Advance B2B GMS — Google Maps Business Lead Scraper

**Advance B2B GMS** is a command-line program that collects business leads
from Google Maps. It searches Google Maps for your queries (for example,
*"plumbers in Houston, TX"*), opens every business listing, saves the
details (name, address, phone, website, rating, reviews, hours, photos),
then visits each business's own website to find contact emails, social
media links, and website technology details. Everything is saved into
**CSV** and **Excel** files that you can open in any spreadsheet program.

You run it on a Linux server (a VPS). No programming knowledge is needed
for everyday use — see the [Quick Start](QUICKSTART.md) page, which uses
just **three commands**.

---

## At a Glance (Summary)

| What you get | Details |
| --- | --- |
| **Leads from Google Maps** | Business name, category, full address, phone (local + international format), website, Google Maps URL, rating, review count, business hours, open/closed status, claimed status, plus code, cover photo, and more — **72 columns** total |
| **Website enrichment** | For every business website: contact **emails**, **social media** links (Facebook, Instagram, LinkedIn, X, YouTube…), **technology stack** (WordPress, Shopify, etc.), SSL check, website live/dead status |
| **Reviews & analysis** | Latest review texts, review sentiment (positive/negative), keywords, top review (optional, per config) |
| **Duplicate removal** | The same business found twice (same query or different queries) is automatically merged — one row per business |
| **Crash safety** | Every saved lead is written to disk safely (fsync). If the server crashes or the power goes out, run the same command again and it **resumes exactly where it stopped** — no leads are lost or duplicated |
| **Output files** | `leads.csv` (Excel-compatible), `leads.xlsx` (Excel), `summary.json` (run statistics), `run.log` (full log) |
| **Speed** | Live-measured on real Google Maps: **~3–4× faster** than the previous version (see [docs/PERFORMANCE.md](docs/PERFORMANCE.md) for measured numbers) |

---

## How It Works (Simple Overview)

The engine works like a small factory with three stations:

```
 Station 1: Google Maps            Station 2: Websites           Station 3: Saving
 ┌───────────────────────────┐    ┌────────────────────────┐   ┌──────────────────┐
 │ Search your query          │    │ Visit each business's  │   │ Check quality     │
 │ Scroll through results    │ →  │ website (20 workers in  │ → │ Remove duplicates │
 │ Open every listing        │    │ parallel): find emails,│   │ Save to CSV/Excel │
 │ Save Maps details          │    │ social links, tech     │   │ + checkpoint DB   │
 └───────────────────────────┘    └────────────────────────┘   └──────────────────┘
```

1. **Google Maps discovery** — the engine searches Google Maps for each of
   your queries, scrolls through the full results list, and opens every
   business listing to read its details. Two browser windows can work in
   parallel (`maps.workers`) to make this faster.
2. **Website enrichment** — the moment a listing is found, its website goes
   into a work queue. Twenty background workers (by default) fetch the
   websites in parallel, crawl up to a few pages each, and extract emails,
   social links, and the technology stack. Slow or broken websites do not
   slow down the others.
3. **Commit** — one by one, each lead passes the quality check and
   duplicate filter, then is saved to the CSV file and the safety database
   (SQLite checkpoint). This is done one at a time on purpose, so files
   never get corrupted.

This is a **continuous** pipeline: Maps discovery never waits for website
scanning to finish, and website scanning never waits for the next search.

---

## Quick Start

> Full beginner walkthrough with just three commands: **[QUICKSTART.md](QUICKSTART.md)**

Short version, on your server:

```bash
git clone https://github.com/zaktecs-ai/advance-b2b-google-map-scraping-engine.git
cd advance-b2b-google-map-scraping-engine
bash server.sh setup     # installs Python packages + Chromium browser
./server.sh config       # opens settings — put your search queries here
./server.sh run          # starts the scraper (keeps running after logout)
```

Or with plain Python:

```bash
pip install -r requirements.lock
python -m playwright install chromium
python main.py            # live scrape using config.yaml
python main.py --demo     # offline test run (no Google, sample data)
```

Output lands in `output/<client_name>/`:

```
output/campaign/
├── leads.csv        # all leads (open in Excel / Google Sheets)
├── leads.xlsx       # Excel version
├── summary.json     # run statistics (counts, CPU/RAM used, timing)
└── run.log          # full detailed log
```

**If Google shows a CAPTCHA** (rare): set `maps.headless: false` in the
config and connect with VNC (`vnc-screen.sh`) to see the browser and solve
it by hand. The engine then continues on its own.

---

## What Gets Collected (Output Columns)

Every lead is one row with **72 columns**. The most important ones:

| Group | Columns |
| --- | --- |
| **Identity** | business name, category, Google Maps URL, place ID, record ID, source query |
| **Location** | full address, city, state, postal code, country, plus code, latitude/longitude |
| **Contact** | phone, international phone, website, website status |
| **Maps data** | rating, review count, business hours, open/closed status, claimed status, cover photo URL, latest photo upload, photos by owner |
| **From website** | emails (up to N), email count, social links (Facebook, Instagram, LinkedIn, X/Twitter, YouTube, TikTok, Pinterest), technology stack, SSL |
| **Analysis** | review sentiment, review keywords, top review, review summary, custom "signals" you define in config |

Missing values are written as `N/A` — never silently blank.

> **Two columns are OFF by default** (owner's choice): `has_recent_post` and
> `latest_post_date` (posts written by the business owner). Turning them on
> (`maps.extract_owner_posts: true`) makes each listing ~3 seconds slower
> because the engine must scroll deep inside the listing panel.

---

## Main Settings (config.yaml)

The whole job is controlled by **one file** (`config.yaml`). Every setting
has a comment explaining it. The ones you will use most:

| Setting | Default | What it does |
| --- | --- | --- |
| `queries` | — | **Your searches.** One line per query, e.g. `- "roofers in Austin, TX"` |
| `job.client_name` | campaign | Name of the output folder |
| `job.max_total_results` | 0 | Stop after this many leads (0 = no limit) |
| `maps.workers` | 2 | How many browser windows work in parallel (1 = old single-browser mode; 2 is a good balance; higher = faster but more CAPTCHA risk on one IP) |
| `maps.extract_owner_posts` | false | The owner-post columns above (on = slower) |
| `delays.maps_min/max_seconds` | 0.4 / 1.2 | Pause between listings (politeness). **If you see CAPTCHAs often, set back to 2.0 / 5.0** |
| `reviews.enabled` | true | Collect review texts + sentiment |
| `website.max_pages_per_site` | 3 | Pages to read per website |
| `concurrency.website_workers` | 20 | Parallel website scanners (safe to leave) |
| `filters.include_all` | — | Keep only leads matching rules (e.g. must have email) — optional |

Advanced sections (proxies, MX/SMTP email verification, AI pitch-hook,
custom signals, VNC, logging) are all documented with comments inside
`config.yaml` itself.

---

## Everyday Commands (server.sh)

| You want to… | Command |
| --- | --- |
| Start / resume the scraper | `./server.sh run` |
| Test safely (no Google contact) | `./server.sh demo` |
| Check if it's running | `./server.sh status` |
| Watch live progress | `./server.sh logs` |
| Stop cleanly (safe — checkpoint kept) | `./server.sh stop` |
| Change queries / settings | `./server.sh config` |
| Update to newest code | `./server.sh update` (never touches your settings or leads) |

---

## Speed & Performance

Measured on **real Google Maps** with the real engine code (not estimates):

| Measurement | Before | After |
| --- | --- | --- |
| Time per listing (one browser) | ~15.3 s | **1.9–3.3 s** |
| Time per listing (default 2 browsers) | ~15.3 s | **2.9 s** (measured: 20 listings in 57.4 s) |
| Browser commands per listing | 251 | **34–39** |

In production terms: a 2,000-lead job that previously took **8–9 hours**
now takes roughly **2 hours** (depending on pauses, website speeds, and
Google's mood). The full audit report — every measurement, what was slow
and why, what was changed, and how to reproduce the numbers — is in
**[docs/PERFORMANCE.md](docs/PERFORMANCE.md)**.

The safety behavior is unchanged: pause settings, CAPTCHA detection, and
cooldown still work exactly as before — if Google pushes back, slow the
engine down (see the `delays` settings above).

---

## Reliability & Safety (What Never Changed)

- **Resume after crash** — every lead is saved before the program moves on.
  Rerun the same command after any crash; it picks up where it stopped.
- **No duplicates** — the same business appearing in two searches is
  detected (phone + website + place ID + name checks) and kept once.
- **Quality gate** — leads missing both a name and a phone are marked
  failed, not saved as junk rows.
- **Politeness** — per-website rate limits, Retry-After honoring, backoff
  with jitter; the engine never hammers one site.
- **Proxy support** — optional rotating proxy pool (per-browser-context
  rotation) for large runs; works with or without proxies.
- **Optional email verification** — MX lookup and SMTP check (off by
  default, slow on purpose).
- **Transparent logging** — clean progress on screen; every detail in
  `run.log`.

---

## Repository Layout (for developers)

```
scraper/
├── main.py              # CLI entrypoint (--demo, --config, --version)
├── config.py            # config loading/validation (pydantic; .env support)
├── pipeline.py          # continuous producer/consumer + serial committer
├── benchmark.py         # mock-workload throughput benchmark
├── maps/                # Google Maps discovery + listing extraction
│   ├── collector.py     #   click flow, batched reads, parallel workers
│   ├── reviews.py       #   review texts (+ limited-view fast-fail)
│   ├── parsing.py       #   place URL / ID parsing helpers
│   ├── transform.py     #   Maps record normalization
│   └── geo.py           #   grid subdivision for city coverage
├── websites/            # HTTP-first enrichment
│   ├── enricher.py      #   per-site orchestration, early stop, sitemap
│   ├── fetcher.py      #   HTTP fetch + transient retries
│   ├── browser_pool.py #   pooled Chromium for JS-only sites
│   ├── rate_limiter.py  #   per-domain gates, Retry-After cooldowns
│   └── tech_detect.py   #   technology fingerprinting
├── email/               # email extraction + optional MX/SMTP verify
├── signals/             # social classification + custom YES/NO signals
├── analysis/            # review sentiment/keywords (+ optional LLM hook)
├── filters/             # pre/post filters (include/exclude rules)
├── dedup/               # identity deduplication
├── validation/          # quality gate
├── export/              # CSV / XLSX / summary writers
├── checkpoint/          # SQLite store + NDJSON mirror
├── browser/             # browser lifecycle + proxy rotation
└── utils/               # retry, DNS cache, progress, logging, resources
tests/                   # 290 tests (unit + end-to-end + perf regression)
docs/                    # ARCHITECTURE.md, PERFORMANCE.md
server.sh               # operator controller (run/stop/logs/update)
config.yaml              # the one file that drives a job
```

**Requirements:** Python 3.11+ (see `pyproject.toml`), Playwright +
Chromium, and the packages in `requirements.lock`. Run the test suite with
`python -m pytest tests/ -q`.

**For developers:** the performance audit (what was slow, root causes, every
fix with its measured impact) lives in `docs/PERFORMANCE.md`; the design
deep-dive lives in `docs/ARCHITECTURE.md` and `CHANGES.md`.

---

## Questions

- **"It stopped / Google blocked me."** — Stop, wait a few hours, raise
  `delays.maps_min/max_seconds` to 2.0/5.0, set `maps.workers: 1`, restart.
  The checkpoint means nothing is lost.
- **"I only want businesses with websites."** — `website.require_website: true`
  or a `filters.include_all` rule.
- **"Where are my leads?"** — `output/<client_name>/leads.csv` (+ `.xlsx`).
- **"How do I add more searches?"** — `./server.sh config`, add lines under
  `queries:`, then `./server.sh run`. Already-done searches are skipped on
  resume; new ones run.
