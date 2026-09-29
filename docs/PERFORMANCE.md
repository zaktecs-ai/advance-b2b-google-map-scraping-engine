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

| Metric | Pehle | Ab |
|---|---|---|
| Raw per-listing (zero pacing) | ~10.4–15.3s | **1.9–3.3s** |
| Round-trips/listing | 251 | **34–39** |
| Social links | 1.27s | 0.026s |
| Photo columns | 1.5s | ~0.005s |
| Reviews (limited-view) | 7.2s | 0.02s |
| Identity wait tail | 8–21s | ~0s |

### Data quality spot-check (live, 3 listings)

business_name ✓, rating 4.7/4.8 ✓, review_count 4382/10858/584 ✓,
cover_image_url ✓ (real googleusercontent URL), category/address/phone/
hours/status ✓.

### Production estimate (honest)

- Raw single-stream: 1.9–3.3s + pacing (0.4–1.2s avg 0.8s) ≈ **2.7–4.1s
  per listing → ~3.7–5.7× single-stream end-to-end** (extrapolated from
  probes; a full production run should confirm).
- `maps.workers: 2` (default in config.yaml): **MEASURED live on real
  Google Maps — 20 listings / 57.4s = 2.9s/listing** with full data quality
  (ratings, review counts, websites correct) and CAPTCHA-free. With
  production pacing on top: **~3.4–4.1s/listing → ~3.7–4.5×**; on a
  challenge-free IP it can stack toward **~7×** vs the old 15.3s baseline.
  Numbers beyond this stay THEORETICAL until measured in a full run.
- Enrichment pool (150 rec/s measured) ab bhi 10× headroom rakhta hai —
  producer bottleneck khatam hone ke baad bhi pool kritikaal path nahi banega
  (0.065 → ~0.4 rec/s still << 150).

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
