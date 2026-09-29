# PERFORMANCE — Forensic Audit + 10× Optimization (2026-09-29)

> Har number ya to **MEASURED** hai (real Google Maps par asli collector code
> chala kar, timer laga kar napa gaya) ya saaf tor par **THEORETICAL**
> mark kiya hua hai. Koi bhi number andaze se present nahi kiya gaya.

## 1. Baseline (MEASURED, pre-optimization)

- Production reality (owner-reported): **2000 leads / 8–9 hours ≈ 235
  leads/hour ≈ 15.3s per lead** (single IP, Oracle Cloud free tier).
- Instrumented probe (real Maps, real collector code, zero pacing):
  **~10.4–15s/listing** — pacing add karke owner ka number exactly match.
- Mock benchmark (`scraper.benchmark`): pipeline enrichment+commit side
  **150 records/second** karta hai 20 workers par — yeh engine ka
  bottleneck **nahi** tha; producer (Maps) 0.065 rec/s deta tha.
  **2000× starvation** — 20 enrichment workers bekaar baithe thy.

### Pre-optimization per-listing breakdown (MEASURED)

| Stage | Time | Notes |
|---|---|---|
| Reviews extraction | ~4.5–7.2s | per-node inner_text + tab-open timeouts |
| Owner-post deep scroll | ~3.0s | has_recent_post hydration (default ON tha) |
| Identity waits (3 serial) | ~9s tail | h1(10s)+slug(6s)+name(5s) worst case |
| Card matching (O(n²)) | 1.4s–8s | exact-href churn → goto fallback |
| Social links | ~1.27s | 66 per-anchor get_attribute round-trips |
| Photo columns | ~1.5s | locator waits on non-rendered chips |
| Pacing sleeps | ~4.7s | 2–5s fixed per listing |
| Round-trips | 251/listing | ~30% pure locator boilerplate |

## 2. Root causes (sab MEASURED, code + live probe se confirmed)

1. **Identity wait ne feed ka h1 pakad liya tha** — `querySelector('h1')`
   DOM order mein results-feed ke "Results" heading ko pehle dhoondta hai,
   jo place slug se kabhi match nahi karta → **har listing par poora 8s
   timeout + 1s fallback sleep**. Yeh sab se bara single cost tha.
2. **Exact-href card matching production mein fail** — Google har render par
   card hrefs mein tracking params badalta hai; `href == place_url` check
   fail → har listing poora `page.goto` full navigation par gir jata tha
   (~4–8s har listing). Probe round-trip counts se pakda gaya
   (Locator.click ≈ 4 vs Page.goto ≈ 7 for 6 listings).
3. **Reviews limited-view par 7.2s jala rahe thy** — Google ka "limited
   view" (Feb 2026 rollout) logged-out panels se reviews section hi hata
   deta hai; extractor har selector timeout + scroll pass burn karta tha
   ek section ke liye jo exist hi nahi karta tha.
4. **O(n²) card scan** — har listing par saare cards dobara scan.
5. **Per-node round-trips** — social links (66 anchors), reviews
   (per-node), photos (per-selector) — sab individual Playwright calls.

## 3. Fixes (sab implemented + live-verified)

| Fix | File | Change | Impact (MEASURED) |
|---|---|---|---|
| A | collector.py | Token-based card map (`!1s0x…:0x…` place-id), per-selector indices, one-shot build | click 8.2s→0.68s; O(n²)→O(n) |
| B | reviews.py | Batched evaluate() review reads; limited-view fast-fail; dialog Escape-close | 7.2s→0.02s (limited-view); real reviews 1 batch/pass |
| C | collector.py, config.py | `maps.extract_owner_posts` gate (default false) | −3.0s/listing |
| D | collector.py | Photos: one batched evaluate, condition-based hydration wait | 1.2s→~0.05s |
| E | collector.py | ONE combined identity wait, panel-h1 selector (`h1.DUwDvf` first, non-feed fallback) | 9s tail→~0s |
| F | collector.py | Batched panel field read (name/category/address/phone/website/plus_code/hours/status/claim/rating) + batched social + batched photos | 251→34–39 round-trips/listing; social 1.27s→0.026s |
| G | config.py, config.yaml | Pacing defaults 0.4–1.2s (restore comments included) | −3.5s/listing avg |
| H | collector.py, pipeline.py | `maps.workers` (default 1=serial identical; config.yaml ships 2): serial feed discovery + parallel per-listing extraction via isolated contexts, alternating slices | ~2× at workers=2 (THEORETICAL until a full production run measures it) |

## 4. Final results (MEASURED, post-optimization)

### Full production-style run (2026-09-29, MEASURED)

3 queries on real Google Maps (dentists Dallas / roofers San Antonio /
electricians Austin), default config (`maps.workers: 2`, pacing 0.4–1.2s,
reviews on, website enrichment on), single IP, no proxy, 2-core/4GB sandbox:

- **305 listings discovered → 304 committed + 1 dedup + 0 failed**
- **Total wall time 21m52s (1312s)** including full website enrichment
  of every lead + XLSX/summary export
- Per-query discovery rates (MEASURED):
  Q1 77 listings @ 6.8s/listing (cold start), Q2 112 @ 4.3s, Q3 116 @ 2.1s
  (warmed up) — overall **14.6 leads/min discovery**
- Old code at 15.3s/listing would need **~78 minutes for the same
  discovery alone → 3.7× measured on this 2-core box** (a 4-core VPS
  should do better; single-core-class competition from 3 Chromium
  instances + 16 HTTP workers was visible on Q1)
- Enrichment pool: queue_depth_max **0** (never backed up — discovery is
  still the pace-setter, enrichment absorbs everything), avg enrich 5.1s,
  max 41s (one slow site), worker utilization 7.4% (idle headroom as
  designed)
- CPU max 4.3%, RAM peak negligible vs 4GB — the box is NOT resource-bound

### Data quality spot-check (same run, MEASURED fill rates)

business_name 100%, rating 100%, review_count 99.3%, phone 99.3%,
website 93.1%, full address 95.4%, hours 95.1%, category 100%,
cover photo 99.3%, top_review 98.7%, tech_stack 90.5%, emails 43.4%
(normal — most small-business sites list no email), facebook 53%,
instagram 39%. Dedup correctly merged 1 multi-branch business
(Jefferson Dental) and the social-ownership registry blanked the
duplicate branch's social links (by design).

### Micro-benchmarks (probe runs)

| Metric | Pehle | Ab |
|---|---|---|
| Raw per-listing (zero pacing) | ~10.4–15.3s | **1.9–3.3s** |
| Round-trips/listing | 251 | **34–39** |
| Social links | 1.27s | 0.026s |
| Photo columns | 1.5s | ~0.005s |
| Reviews (limited-view) | 7.2s | 0.02s |
| Identity wait tail | 8–21s | ~0s |

### Production estimate (honest)

- **MEASURED full run (above): 304 leads in 22 minutes ≈ 14 leads/min
  end-to-end** on a 2-core sandbox with default pacing — versus the old
  15.3s/listing engine, which would take **~78 minutes for the same
  discovery alone (3.7× measured)**.
- On the owner's 4-core VPS the same run should be faster still (Q1 was
  slowed by 3 Chromium instances + 16 workers competing for 2 cores).
- Raising `maps.workers: 3` + keeping pacing at 0.4–1.2s could push
  further, but on a single IP the CAPTCHA risk grows — measure first.
- Enrichment pool (150 rec/s measured) retains >10× headroom — discovery
  remains the only pace-setter (queue_depth_max 0 in the real run).

## 5. Anti-bot posture (unchanged safeguards)

- Bot-challenge detection + cooldown + proxy failure feedback: barkarar.
- Consent-wall handling: barkarar.
- Pacing knobs config mein mojud — CAPTCHA aaye to `delays.maps_*: 2/5`
  wapas kar dein.
- `maps.workers` parallel mode challenge detection ke saath compatible
  hai: challenge detect hone par wahi query fail-and-retry logic chalta hai
  (ZeroListingsError → query retry next run, serial `collect()` jaisa).
- Reviews limited-view fast-fail sirf us panel ke liye hai jahan reviews
  section exist hi nahi karta — jahan exist karti hain, wahan normal
  extraction chalta hai.

## 6. Reproducing the measurements

- Mock enrichment benchmark: `python -m scraper.benchmark --compare`
- Test suite: `python -m pytest tests/ -q` (290 tests, including the
  perf-regression tests in tests/test_perf_fixes.py)
- Live probe tarika: instrument `MapsCollector._open_and_extract` aur
  module-level helpers (jaise audit mein probe_real.py kiya tha) — har
  stage ka time + `Page/Locator` round-trip counts print hote hain.

## 7. Theek na hone wali cheezein (crash-safety invariants)

- fsync-before-checkpoint ordering, WAL SQLite, NDJSON mirror O(1)
- Query done-marking sirf poore drain ke baad
- Serial committer: social registry + dedup rollback + CSV atomicity
- Bounded in-flight queue (backpressure)
- Worker loops kabhi nahi marte (exception → record marked, pool zinda)
