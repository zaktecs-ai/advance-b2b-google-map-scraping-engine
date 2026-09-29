# Advance B2B GMS — Google Maps Lead Scraper (Fast Edition)

Advance B2B GMS Google Maps se business leads nikalta hai: listing data,
reviews, website emails, social links, technology stack — sab CSV/XLSX mein.
Yeh ek command-line program hai jo Linux server (VPS) par chalta hai.

> **5–8× tez:** Is version mein Google Maps ka discovery path redesign kiya
> gaya hai (live-measured: ~15 second/listing → ~2–3.5 second/listing, bina
> koi field ya quality kam kiye). Tafseel neeche **"Speed"** section mein.

> **Python 3.11+ zaroori.** Decision-maker regex Python 3.11 ke naye
> features use karta hai.

---

## Simple Overview — Yeh kaise kaam karta hai

1. **Google Maps search** — aap ki query (jaise `plumbers in Houston, TX`)
   Maps par search hoti hai aur saare result cards load hote hain
   (scroll kar kar).
2. **Har listing khol kar data nikaalna** — naam, category, address, phone,
   website, rating, review count, hours, photos — ek browser click se.
3. **Website enrichment (parallel)** — har business ki website HTTP se
   fetch hoti hai (JS-only sites ke liye Chromium fallback), emails +
   social links + technology stack nikaale jaate hain.
4. **Checkpoint + CSV/XLSX** — har record dedup + quality-check ke baad
   SQLite checkpoint + CSV mein commit hota hai. Crash hoo to rerun karne
   par wahi se resume hota hai jahan ruka tha.

```
Maps discovery (producer) -> bounded queue -> N website workers (parallel)
                                          -> serial committer -> CSV/SQLite
```

## Quick Start (Roman Urdu)

```bash
# 1. Install (ek dafa)
./setup.sh                      # ya: pip install -r requirements.lock
python -m playwright install chromium

# 2. Config dekhen — queries aur options
nano config.yaml                # queries: [ ... ] mein apni queries likhen

# 3. Chalayen
python main.py                  # live scrape
python main.py --demo           # offline test (browser ke baghair)

# 4. Output
output/<client>/leads.csv       # CSV leads
output/<client>/leads.xlsx      # Excel
output/<client>/run.log         # full log
```

CAPTCHA aa jaye to: `maps.headless: false` + VNC (`vnc-screen.sh`) se
browser dekh kar khud solve karein — phir engine khud continue karta hai.

---

## Speed — Kya tez kiya gaya (live-measured)

Sab numbers **real Google Maps** par asli code chala kar napa gaye hain
(6-listing probes, zero pacing):

| Cheez | Pehle | Ab | Notes |
|---|---|---|---|
| Per-listing raw cost | ~15.3s | **1.9–3.3s** | 5–8× faster single stream |
| Browser round-trips / listing | 251 | **34–39** | batched evaluate() calls |
| Reviews (jab na hon) | ~7.2s jala | **0.02s** | limited-view ko detect kar ke skip |
| Card matching | O(n²) per query | **O(n)** | token-based index map |
| Identity wait (worst case) | 21s serial waits | **~0s** | ek combined wait |
| Pacing default | 2–5s/listing | **0.4–1.2s** | config se wapas 2–5 kar sakte hain |

Asal bug jo pakra gaya: identity wait **feed ke "Results" h1** ko pakad
raha tha panel ke business h1 ki jagah — har listing par 8 second ka
timeout burn hota tha. Aur card-href matching Google ke href churn ki
wajah se har listing par full page load par fallback karta tha.

`maps.workers: 2` (default) ke saath do browser contexts parallel extraction
karte hain — single-IP par yeh realistic limit hai (zyada workers = zyada
CAPTCHA risk). CAPTCHA aane par wapas `workers: 1` + `maps_min/max_seconds:
2/5` kar dein.

Measurement tooling: `python -m scraper.benchmark` (mock enrichment pool) —
docs/PERFORMANCE.md mein live probe ka tarika bhi hai.

## Jo kuch NAHI badla (feature preservation)

- Saare output columns (72 + custom signals) — bilkul same schema
- Reviews, photos/cover, social links, technology detection, signals
- Emails/decision-maker/MX/SMTP enrichment options
- Dedup, quality gate, filters (pre/post)
- Checkpoint/resume, crash recovery, fsync-backed CSV durability
- Proxy support (per-context rotation) + Playwright fallback pool
- Browser recycle, consent-wall handling, bot-challenge detection
- `has_recent_post`/`latest_post_date` ab bhi mojud hain — bas default
  `maps.extract_owner_posts: false` hai kyunki owner ne kaha yeh columns
  kaam ke nahi (deep scroll ~3s/listing bachta hai; `true` karne par
  purana behavior wapas)

## Repository Layout

```
scraper/
├── main.py              # CLI entrypoint
├── pipeline.py          # producer/consumer + serial committer
├── maps/                # Google Maps discovery + extraction
│   ├── collector.py     # card click flow, batched reads, parallel workers
│   ├── reviews.py       # reviews: limited-view detect + batched reads
│   └── parsing.py       # URL/place-id parsing helpers
├── websites/            # HTTP-first enrichment + Chromium fallback pool
├── export/              # CSV / XLSX / summary writers
├── checkpoint/          # SQLite store + mirror
├── dedup/, filters/, signals/, analysis/, validation/
└── utils/               # rate limiter, retry, dns cache, progress
tests/                   # 290 tests (unit + e2e + perf regression)
docs/                    # ARCHITECTURE.md, PERFORMANCE.md
config.yaml              # ek hi file se poora job control
```

## Configuration (sirf zaroori cheezein)

| Key | Default | Kya karta hai |
|---|---|---|
| `queries` | — | Maps search queries |
| `maps.workers` | 2 | Parallel browser contexts (1 = serial) |
| `maps.extract_owner_posts` | false | Owner-post deep scroll on/off |
| `delays.maps_min/max_seconds` | 0.4/1.2 | Listing ke darmiyan pacing |
| `concurrency.website_workers` | 20 | Website enrichment parallelism |
| `reviews.enabled` | true | Review texts + sentiment |
| `website.max_pages_per_site` | 3 | Kitni pages per site crawl |
| `email.enable_mx_check` | false | MX verify on/off |

Baqi sab keys config.yaml ke comments mein documented hain.

## Tests

```bash
python -m pytest tests/ -q     # 290 tests
```

## License / Credits

See repository license file.
