# CHANGES.md

Remediation of the senior-architect audit (AUDIT-REMEDIATION-PLAN.md). One entry
per F-ID: root cause, files touched, tests added, verification evidence.

Suite grew 158 → 191 passing tests.

---

## THROUGHPUT-01 — Continuous producer/consumer enrichment pipeline
- **Problem:** the pipeline created a `ThreadPoolExecutor` PER BATCH and waited
  for every batch to drain: Maps discovery idled the workers while assembling
  the next batch, and each batch tail under-utilized the pool. On a 2–3k
  business run this meant hours of pipeline idle time.
- **Fix (architecture, not knob-turning):**
  - `scraper/pipeline.py`: long-lived worker pool created ONCE per run
    (`_start_pool`/`_worker_loop`/`_committer_loop`/`_shutdown_pool`).
    Discovery (producer) streams prepared records into a bounded
    `queue.Queue` (`concurrency.max_in_flight`, default workers x 4) so
    memory stays flat; workers consume continuously; a single committer
    thread serializes every shared-state mutation (dedup rollback, social
    ownership registry, CSV append, checkpoint) exactly as the batch design
    did. Workers/committer are exception-proof — one bad record can never
    shrink the pool or deadlock the bounded queue.
  - `scraper/websites/rate_limiter.py` (new): `DomainGate` (per-domain
    concurrency slots, default 1 request at a time per domain), 
    `DomainCooldowns` (park a domain after 429/503 Retry-After instead of
    hammering), `parse_retry_after` (seconds + HTTP-date, clamped).
  - `scraper/websites/fetcher.py`: transient-failure retry (429/503,
    timeouts, connection errors) with exponential backoff + jitter; Retry-
    After parks the domain (worker never blocks minutes inline); DNS/TLS/4xx
    are permanent (no retry storms); explicit `httpx.Limits` connection-pool
    reuse; testable `transport` injection.
  - `scraper/websites/browser_pool.py` (new): `concurrency.playwright_workers`
    (2–4) persistent Chromium browser workers with lazy start and browser
    reuse for JS-required sites — no more throwaway Chromium per site.
    `renderer.py` is now a facade over the pool.
  - `scraper/websites/enricher.py`: emails/social extracted INCREMENTALLY per
    page with early stop once the required signals are found; sitemap.xml only
    fetched when the homepage link crawl was weak.
  - `scraper/websites/tech_detect.py`: `Wappalyzer.latest()` cached process-
    wide (it re-parsed a ~1MB technologies.json per call).
  - `scraper/pipeline.py`: runtime stats (`records_per_second`,
    `avg_enrich_seconds`, `max_enrich_seconds`, `queue_depth_max`,
    `worker_utilization`) surfaced via `Pipeline.runtime_stats()` and written
    to `summary.json` under `throughput`.
- **Config:** `concurrency.website_workers` 16→20 (cap 1–32, 16–25
  recommended window); new `per_domain_concurrency` (1), `max_in_flight` (0 =
  auto), `respect_retry_after` (true); `website.retry_backoff_base_seconds` /
  `website.retry_backoff_cap_seconds`; `playwright_workers` now actually
  drives the browser pool (default 2).
- **Tests:** `tests/test_rate_limiter.py` (12), `tests/test_worker_pool.py`
  (9), `tests/test_benchmark.py` (2); `tests/test_concurrency.py` rewritten
  for the pool lifecycle. Suite: 241 → 270 passing.
- **Benchmark:** `scraper/benchmark.py` — local mock-site fleet (one port per
  site = one rate-limit domain) through the REAL pipeline. Old code (git HEAD,
  best setting 16 workers) vs new, 60 sites, 300 ms simulated request latency:
  old 19.8 rec/s; new 27.7 rec/s @16 workers (+40%), 29.6 rec/s @20 workers
  (+50%). At 50 ms latency: old 38.7 rec/s; new 65.4 rec/s @16 workers
  (+69%). Early stop additionally cuts real-world requests per site from 4
  to ~2 (homepage + contact) whenever the contact page yields emails+social,
  and Wappalyzer caching removes a ~1MB JSON parse per site. Durability,
  dedup, checkpoint/resume, CSV/XLSX output and schema are byte-for-byte
  unchanged.

---

## F01 — Facebook column constant (panel-wide anchor scrape)
- **Root cause:** `_extract_social_links` harvested anchors from the ENTIRE page
  (including the results feed behind/around the panel), so one business's
  `facebook.com/championplumbers` leaked into every row.
- **Files:** `scraper/maps/collector.py` (new `filter_panel_hrefs`, scoped panel
  selectors).
- **Tests:** `tests/test_panel_guard.py::test_filter_panel_hrefs_drops_maps_nav_links`.
- **Evidence:** test green; `filter_panel_hrefs` drops `maps/place|dir|search`.

## F02 — One-row-shift contamination
- **Root cause:** extraction proceeded on blind sleeps with no proof the panel
  switched to the clicked card, so name/phone/rating belonged to the PREVIOUS
  business.
- **Files:** `scraper/maps/collector.py` (`_click_to_open` URL-switch wait,
  `_open_and_extract` panel-identity guard + coherence sentinel, new
  `_names_compatible`).
- **Tests:** `tests/test_panel_guard.py::test_names_compatible_guard`.
- **Evidence:** green.

## F03 — kgmid never extracted
- **Root cause:** production URLs carry `!16s%2Fg%2F…` (percent-encoded); the
  regex only handled plain `/g/…`.
- **Files:** `scraper/maps/parsing.py` (`unquote` once + updated kgmid regex).
- **Tests:** `test_kgmid_extracted_from_encoded_url`, `test_kgmid_extracted_from_plain_url`.
- **Evidence:** green.

## F04 — cid grabs a wrong hex token
- **Root cause:** standalone `0x…:0x…` regex matched the first pair anywhere,
  including ad-panel `!5s` tokens.
- **Files:** `scraper/maps/parsing.py` (cid derived only from `!1s`).
- **Tests:** `test_cid_matches_place_id_and_ignores_ad_tokens`.
- **Evidence:** green; `cid == place_id` on the Aberle ad-token URL.

## F05 — lat/lng polluted with viewport center
- **Root cause:** `/maps/@…` (search camera) was treated as the place pin.
- **Files:** `scraper/maps/parsing.py` (deleted `/maps/@` branch; only `!3d/!4d`).
- **Tests:** `test_viewport_center_never_becomes_place_coords`,
  `test_true_pin_coords_extracted_over_viewport`; updated existing
  `test_parse_maps_url_coords` per R3.
- **Evidence:** green.

## F06 — Duplicate businesses committed twice
- **Root cause:** weak-phone rule skipped when a place_id was present; no
  same-website guard at all.
- **Files:** `scraper/dedup/dedup.py` (same-domain+name / same-phone+name guards
  independent of strong ids), `scraper/checkpoint/store.py` (`name_key` column +
  migration + seeds), `scraper/pipeline.py` (pass seeds).
- **Tests:** `test_same_domain_same_name_duplicate_even_with_distinct_place_ids`,
  `test_chain_locations_same_domain_diff_names_not_merged`,
  `test_same_phone_same_name_duplicate_with_distinct_place_ids`.
- **Evidence:** green.

## F07 — Decision-maker fabrication
- **Root cause:** CTA verbs glue to names; titles split; duplicated tokens;
  non-person words captured.
- **Files:** `scraper/signals/detector.py` (multi-word `Vice President` title,
  CTA-prefix reject, `_NAME_NOT_PERSON_WORDS`, duplicated-token reject,
  title-ending reject).
- **Tests:** `test_no_decision_maker_from_production_false_positives`,
  `test_vice_president_title_not_split`, `test_real_decision_makers_still_detected`.
- **Evidence:** green; all 5 production FP rows → `("", "")`.

## F08 — Review chrome → junk keywords
- **Root cause:** whole-review-card harvest pulled reviewer name + "4 reviews ·
  1 photo… months ago Like Share" into top_review/keywords.
- **Files:** `scraper/maps/reviews.py` (body-span selectors + `clean_review_text` +
  `len>=25`), `scraper/analysis/engine.py` (`_UI_NOISE` guard).
- **Tests:** `test_clean_review_text_strips_ui_chrome`,
  `test_keywords_never_contain_ui_noise`.
- **Evidence:** green.

## F09 — Nonexistent `asyncio.CoroutineNotAllowedError`
- **Root cause:** that attribute does not exist in any Python version.
- **Files:** `scraper/browser/browser_manager.py`.
- **Tests:** existing `test_browser_manager_importable` (in test_smoke); import
  succeeds.
- **Evidence:** `import scraper.browser.browser_manager` exits 0.

## F10 — `(?-i:…)` needs Python 3.11 while `requires-python >=3.9`
- **Root cause:** scoped inline regex flags are 3.11+; the pin was 3.9.
- **Files:** `pyproject.toml` (`requires-python = ">=3.11"`), `README.md`,
  `scraper/signals/detector.py` (comment).
- **Tests:** detector imports on 3.11.
- **Evidence:** suite green on Python 3.11.16.

## F11 — `FetchResult.headers: dict = None`
- **Root cause:** mutable-class-type default.
- **Files:** `scraper/websites/fetcher.py` (`field(default_factory=dict)`).
- **Tests:** `test_fetch_result_default_headers`.
- **Evidence:** green.

## F12 — Dead `ConnectTimeout` branch in `_classify`
- **Root cause:** unreachable (already caught by the top TIMEOUT branch).
- **Files:** `scraper/websites/fetcher.py`.
- **Tests:** `test_connect_timeout_classifies_as_timeout`.
- **Evidence:** green.

## F13 — `ProgressConsole.query_total` method/attr collision
- **Root cause:** `def query_total` wrote `self.query_total`, shadowing the
  method; attribute uninitialized.
- **Files:** `scraper/utils/progress.py` (`current_query_total` + `set_query_total`),
  `scraper/main.py` (`on_query_total=progress.set_query_total`).
- **Tests:** `tests/test_progress.py::test_footer_before_query_started_does_not_crash`.
- **Evidence:** green.

## F14 — Collector context/page leak
- **Root cause:** ctx/page created outside try/finally.
- **Files:** `scraper/maps/collector.py` (`collect` wraps creation in try/finally;
  extracted `_collect_on_page`).
- **Tests:** N/A (headless); verification = full suite green.

## F15 — Dead branch in `_extract_phone_international`
- **Root cause:** `re.sub(r"\D",…)` can never start with `+`.
- **Files:** `scraper/maps/collector.py` (new pure `digits_to_intl`).
- **Tests:** `test_digits_to_intl`.
- **Evidence:** green.

## F16 — CSV row counting breaks on multi-line fields
- **Root cause:** physical-line count ≠ row count for quoted embedded newlines.
- **Files:** `scraper/export/csv_writer.py` (`csv.reader` count).
- **Tests:** `test_row_count_handles_multiline_quoted_field`.
- **Evidence:** green.

## F17 — `at`/`dot` corrupts prose into fake emails
- **Root cause:** bare `\s+at\s+` / `\s+dot\s+` decoded natural language.
- **Files:** `scraper/email/extract.py` (bracket-only decode).
- **Tests:** `test_prose_at_dot_not_decoded`.
- **Evidence:** green.

## F18 — Social path rejection uses substring matching
- **Root cause:** `/tr` killed `/travel`; `/pages/` killed real pages.
- **Files:** `scraper/signals/social.py` (first-segment reject; multi-segment
  instagram; `/pages/` accepted).
- **Tests:** `test_segment_rejection_not_substring`.
- **Evidence:** green.

## F19 — Signal keywords raw substring match
- **Root cause:** `licensed` matched `unlicensed`.
- **Files:** `scraper/signals/detector.py` (`_kw_in_blob` word-boundary).
- **Tests:** `test_keyword_word_boundaries`.
- **Evidence:** green.

## F20 — Tech detection scans body prose + legacy `detect_tech`
- **Root cause:** fallback scanned raw HTML text; `detect_tech` duplicated and
  contradicted `SignalDetector`.
- **Files:** `scraper/websites/tech_detect.py` (markup-only `_fallback_detect`,
  removed prose patterns, deleted `detect_tech`).
- **Tests:** `test_fallback_detect_ignores_body_prose`,
  `test_fallback_detect_finds_markup_artifact`.
- **Evidence:** green; `grep detect_tech` → nothing.

## F21 — Phone extensions destroyed
- **Root cause:** extension regex stripped and discarded the extension.
- **Files:** `scraper/utils/normalize.py` (`_EXT_RE` capture + `x<ext>` append).
- **Tests:** updated `test_normalize_phone_strips_extension_and_trailing_noise`.
- **Evidence:** green; `+1 555-123-4567 ext 890` → `+15551234567 x890`.

## F22 — Address decomposition drops valid cities
- **Root cause:** comma-only split + blanket digit rejection.
- **Files:** `scraper/maps/parsing.py` (wider separators; postal-shaped rejection
  only).
- **Tests:** `test_pipe_and_newline_separated_addresses`.
- **Evidence:** green.

## F23 — SMTP verification hardening
- **Root cause:** MAIL FROM response discarded; plain starttls; placeholder
  sender allowed.
- **Files:** `scraper/email/verification.py` (check mail_from, `ssl` context),
  `scraper/config.py` (SMTPConfig validator).
- **Tests:** `test_smtp_from_email_must_be_real_when_enabled`.
- **Evidence:** green.

## F24 — Crawler nondeterminism + dead `early_stop_reached`
- **Root cause:** bare set→list sort unstable; `early_stop_reached` never wired.
- **Files:** `scraper/websites/crawler.py` (order-preserving dedup),
  `scraper/websites/enricher.py` (early-stop in extra-pages loop).
- **Tests:** existing suite.
- **Evidence:** green.

## F25 — Dead/unwired config knobs
- **Root cause:** several knobs validated but never read.
- **Files:** `scraper/main.py` (`logging.level` wired), `scraper/pipeline.py`
  (`max_email_length`, `reviews.min_len/max_len`, enrichment toggles wired),
  `scraper/config.py` (`exclude_selectors` = F33), `scraper/websites/enricher.py`.
- **Tests:** `test_config_template_has_no_dead_sections`.
- **Evidence:** green; every `config.yaml` top-level key maps to an `AppConfig`
  field.

## F26 — One failing record aborts the batch
- **Root cause:** `ex.map` re-raised first worker exception.
- **Files:** `scraper/pipeline.py` (`_safe_enrich`).
- **Tests:** `test_one_failing_enrich_does_not_abort_batch`.
- **Evidence:** green.

## F27 — Proxy pool never applied to httpx
- **Root cause:** `ProxyManager` only fed Playwright contexts.
- **Files:** `scraper/main.py` (http/https wired, proxy_manager passed),
  `scraper/pipeline.py` (threads `httpx_proxy()`, failure feedback),
  `scraper/websites/enricher.py` (`proxy_manager`).
- **Tests:** `test_pipeline_threads_proxy_to_enricher`.
- **Evidence:** green.

## F28 — Pipeline resources leak on `run()` raise
- **Root cause:** `finally` closed only the browser.
- **Files:** `scraper/pipeline.py` (idempotent `close()`), `scraper/main.py`.
- **Tests:** existing suite.
- **Evidence:** green.

## F29 — Remaining blind sleeps in collector
- **Root cause:** `time.sleep` used for data sync instead of selector waits.
- **Files:** `scraper/maps/collector.py` (feed/h1 waits), `scraper/maps/reviews.py`
  (element-scoped scroll + `wait_for_timeout`).
- **Tests:** existing suite.
- **Evidence:** `grep time.sleep` shows only pacing/cooldown/fallback sleeps.

## F30 — `_write_mirror` rewrites the entire table
- **Root cause:** full select+serialize+rewrite every N records.
- **Files:** `scraper/checkpoint/store.py` (NDJSON `_append_mirror`; deleted
  `_write_mirror`/`_maybe_mirror`/`MIRROR_EVERY`).
- **Tests:** `test_mirror_is_ndjson_incremental` (replaces the old throttle test
  per R3).
- **Evidence:** green; 1200 commits → 2400 lines.

## F31 — `_finalize` OOM (whole dataset in RAM + non-streaming XLSX)
- **Root cause:** `committed_rows()` materialized everything; `write_xlsx` built
  a full Workbook.
- **Files:** `scraper/checkpoint/store.py` (`iter_committed_rows`),
  `scraper/export/xlsx_writer.py` (write-only), `scraper/pipeline.py`.
- **Tests:** `test_iter_committed_rows_streams`.
- **Evidence:** green; demo run writes a loadable `leads.xlsx`.

## F32 — Dedup loads all history into Python sets
- **Root cause:** unbounded cold-start preload.
- **Files:** `scraper/checkpoint/store.py` (bounded preload + `identity_exists` /
  `domain_name_seen` / `phone_name_seen`).
- **Tests:** `test_db_path_covers_older_history`.
- **Evidence:** green.

## F33 — Email/decision-maker stripping deletes real team data
- **Root cause:** `.author`/`blockquote`/`.quote`/`cite` blanket decompose.
- **Files:** `scraper/config.py` (`EnrichmentConfig.exclude_selectors`),
  `scraper/email/extract.py`, `scraper/websites/enricher.py`.
- **Tests:** existing suite (default excludes no longer strip `blockquote`/`.author`).
- **Evidence:** green.

## Schema change — 75 → 68 columns
- Removed: `kgmid`, `cid`, `subcategory`, `about`, `mx_enabled`, `smtp_enabled`,
  `filtered_out_reason`.
- **Files:** `scraper/models.py`, `scraper/maps/transform.py`,
  `scraper/pipeline.py`, `tests/test_maps_transform.py`, `README.md`,
  `docs/ARCHITECTURE.md`.
- **Tests:** `test_schema_has_no_unproduced_ghost_columns` (68 + removed names),
  `test_protected_columns_still_present`.
- **Evidence:** demo run CSV header = 68 columns; removed columns absent.

---

## Definition of Done (global)

| Check | Status |
|---|---|
| `python -m pytest -q` | 191 passed |
| `python -m compileall -q scraper` | clean |
| `python -m scraper.main --demo` | completes; 68-col CSV, XLSX + summary.json |
| Every F-ID in CHANGES.md | yes |
| `grep time.sleep` collector/reviews | pacing/cooldown/fallback only |
| `grep detect_tech` | nothing |
| No dead config section | `test_config_template_has_no_dead_sections` green |
| README/docs updated | 68 columns, Python 3.11+, migration note |
| Zero test deletions | yes (updated tests carry R3 comment) |
| Final report table | this section |

---

# Generation-2 remediation (AUDIT-REMEDIATION-PLAN-V2.md, G01–G13)

Remediation of the forensic Generation-2 audit of the 2000+ record production
run (`updated_leads.csv`). One entry per G-ID: root cause, files touched,
tests added, verification evidence.

Suite grew 191 → 215 passing tests. V1 suite (F01–F33) fully green throughout.

---

## G01 — `business_description` = "See photos" / rating-block text (CRITICAL)
- **Root cause:** `DESCRIPTION_SELECTORS` included the generic body-text class
  `fontBodyMedium` and the bare `div.PYvSYb`, whose first non-empty match on
  ~100% of panels is the "See photos" gallery button or the `4.9 (34)` rating
  block.
- **Files:** `scraper/maps/collector.py` (authoritative selectors only; new
  pure `clean_description()` junk guard; `div.PYvSYb` demoted to a text-quality
  fallback trusted only at >=60 chars).
- **Tests:** `tests/test_panel_guard.py::test_description_junk_rejected`,
  `::test_description_real_text_kept`.
- **Evidence:** all G1-E shapes ("See photos", `4.9 (34)`, "Open 24 hours")
  now return `N/A`; real editorial text survives.

## G02 — Identical coordinates across distinct businesses (viewport leak)
- **Root cause:** F05 validated the `!3d…!4d…` token shape but not zoom
  semantics: on 9z–12z search-viewport fallback URLs that pair is the map
  CAMERA position (G2-E: 4 businesses shared `29.836095,-95.46119`, all `10z`).
- **Files:** `scraper/maps/parsing.py` (`parse_google_maps_url` zoom guard:
  coords only at >=15z; unmarked detail URLs default to 17).
- **Tests:** `tests/test_maps_parsing.py::test_low_zoom_url_coords_rejected`,
  `::test_17z_pin_coords_kept`; `test_true_pin_coords_extracted_over_viewport`
  updated with the R3 comment (10z pins now intentionally rejected).
- **Evidence:** fixtures `LOW_ZOOM_VIEWPORT_URL` rejected; `FULL_PLACE_URL` /
  `ABERLE_AD_TOKEN_URL` (17z) still yield coords.

## G03 — Decision-maker role-word / heading false positives
- **Root cause:** F07 rejected CTA prefixes and duplicated tokens but not
  (a) role/department words INSIDE the captured name, (b) heading shapes
  ("Why Choose X", "Hotels Near"), (c) neighborhood names ("Lakewood Highland
  Park Kelli").
- **Files:** `scraper/signals/detector.py` (`_NAME_ROLE_WORDS`,
  `_NAME_HEADING_PREFIXES`, `_NAME_PLACE_WORDS` checked against non-final
  tokens only, so surname "Park" survives).
- **Tests:** `tests/test_signals.py::test_no_decision_maker_from_g2_production_false_positives`
  (all 9 G3-E rows), `::test_real_decision_makers_still_pass_g2` (V1 positives
  stay green — no over-rejection).
- **Evidence:** green.

## G04 — `review_keywords` verb/tokenization junk
- **Root cause:** frequency ranking after stopword removal surfaces review
  VERBS (`fixed`, `gave`, `installed`) and contraction stems (`couldn`,
  `shutt`), producing "praising installed" hooks.
- **Files:** `scraper/analysis/engine.py` (`_JUNK_TOKENS` blocklist in
  `review_keywords`; `pitch_hook` falls back to the category when the top
  keyword is junk). R11 narrowing: `service`/`services` kept legitimate —
  blocking them emptied `review_keywords` on the V1 roundtrip surface.
- **Tests:** `tests/test_analysis.py::test_keywords_exclude_verb_junk_g2`,
  `::test_pitch_hook_never_says_praising_junk`,
  `::test_keywords_keep_brand_and_topic_tokens`.
- **Evidence:** green; brand/topic tokens ("halo", "plumbing") preserved.

## G05 — Instagram POST URL exported as profile (F18 regression)
- **Root cause:** the F18 regex checked the negative lookahead only on the
  FIRST segment, so `/handle/p/<postid>` passed (G5-E: a POST under the
  brand's old name).
- **Files:** `scraper/signals/social.py` (sub-path's first segment must not be
  `p`/`reel`/`tv`).
- **Tests:** `tests/test_signals.py::test_instagram_post_url_rejected_g2`;
  V1 `/natgeo/travel/` case stays accepted.
- **Evidence:** green.

## G06 — Hotel `category` / `business_status` N/A
- **Root cause:** category selectors targeted only `<button>` variants; status
  inference ignored the "Open 24 hours" evidence in the hours text.
- **Files:** `scraper/maps/collector.py` (`CATEGORY_SELECTORS` + div/span
  variants — never fabricated from the name; new pure `_status_from_hours()`
  wired only when the status chip missed).
- **Tests:** `tests/test_panel_guard.py::test_status_from_hours_open24`,
  `::test_status_from_hours_normal_hours_is_honest_none`.
- **Evidence:** green; posted ranges stay honest `N/A`.

## G07 — Malformed/agency email contacts
- **Root cause:** (a) panel CTA text "message hpd@…" glued into
  `messagehpd@hpdentist.com`; (b) the site developer's free-mail address
  (`websolid2020@gmail.com`) harvested as the business contact.
- **Files:** `scraper/utils/normalize.py` (new rejection reasons
  `cta_glued_local_part` and `agency_freemail_local_part` — the latter only
  for personal-provider domains on a known different website).
- **Tests:** `tests/test_email_extract.py::test_clean_rejects_cta_glued_local_part`,
  `::test_clean_rejects_agency_freemail_local_part`,
  `::test_clean_keeps_legit_freemail_and_domain_emails`.
- **Evidence:** green; legitimate free-mail ("david2020@gmail.com") and
  business-domain addresses unaffected.

## G08 — Per-query "of N" summary never rendered
- **Root cause:** half-rename — `query_started` wrote `self.query_total`,
  `set_query_total`/`business_collected` wrote `current_query_total`,
  `query_done` read the never-updated one.
- **Files:** `scraper/utils/progress.py` (`query_started` resets
  `current_query_total`; `query_done` reads it).
- **Tests:** `tests/test_progress.py::test_query_done_reports_of_n`,
  `::test_query_total_resets_between_queries`.
- **Evidence:** summary renders "collected 1 of 7"; totals reset per query.

## G09 — Dead duplicate return in `_extract_scripts`
- **Root cause:** unreachable duplicated `re.findall` block after the return.
- **Files:** `scraper/websites/enricher.py`.
- **Tests:** `python -m compileall` + full suite green.
- **Evidence:** dead code removed; no behavior change.

## G10 — Docs hygiene
- **Root cause:** README had a duplicated/corrupted ending block; stale
  "Python 3.9+" comment in `requirements.txt` (floor is 3.11); no interpreter
  version guard.
- **Files:** `README.md` (deduped ending), `requirements.txt`,
  `scraper/__init__.py` (fail-fast guard with actionable message — the code
  relies on 3.11+ scoped-flag regex syntax).
- **Tests:** full suite green on 3.14 (guard is trivially version-gated).
- **Evidence:** `tail README.md` clean; comment matches `pyproject.toml`.

## G11 — Inconsistent `record_id` formats
- **Root cause:** `{place_id}:{uuid8}` where place_id itself contains a colon
  produced 3-colon ids (`0x…:0x…:4cfd2e8c`) alongside 1-colon kgmid ids,
  breaking `:`-splitting consumers.
- **Files:** `scraper/pipeline.py` (`_make_record_id` normalizes the strong id,
  so every id carries exactly ONE colon).
- **Tests:** `tests/test_pipeline.py::test_record_id_has_exactly_one_colon`,
  `::test_record_id_fallback_is_bare_uuid`.
- **Evidence:** green.

## G12 — URL hygiene: tracking params in `google_maps_url`, `plus_code` space
- **Root cause:** the raw `page.url` (with `authuser`, `entry`, `g_ep`,
  `rclk`, …) was exported verbatim; plus-code text carried uncollapsed
  whitespace.
- **Files:** `scraper/maps/parsing.py` (new pure `clean_maps_url()` — tracking
  params and `utm_*` stripped, locale `hl`/`gl` kept); `scraper/maps/collector.py`
  (wired into `_open_and_extract`; new `_clean_plus_code()`).
- **Tests:** `tests/test_maps_parsing.py::test_clean_maps_url_strips_tracking_params`,
  `::test_clean_maps_url_passthrough_and_empty`;
  `tests/test_panel_guard.py::test_plus_code_whitespace_normalized`.
- **Evidence:** green.

## G13 — Cross-business social contamination (VERIFY → defensive fix)
- **Root cause:** production row All American Plumbing carried the SAME
  facebook/instagram handles as Houston Plumbing Expert (a different row).
  The engine's panel scoping (F01/F02) already narrows the leak path, but an
  agency-built website or a panel fallback can still carry another business's
  profile — and nothing stopped one social URL from being committed under two
  record identities in one run.
- **Files:** `scraper/pipeline.py` (new serial-pass
  `SocialOwnershipRegistry`: the first committed record keeps a social URL;
  later claimants are blanked to `N/A` with a warning).
- **Tests:** `tests/test_pipeline.py::test_social_ownership_registry_blocks_cross_business_reuse`.
- **Evidence:** green. Honest limitation: without a live replay we cannot
  prove which surface leaked; this guard guarantees the exported symptom
  (one profile on two businesses) cannot recur.

## Generation-2 bonus fix — UnicodeEncodeError on non-UTF-8 stdout
- **Root cause:** the progress reporter's Unicode glyphs crash under legacy
  codecs (cp1252) whenever stdout is a pipe — `server.sh demo` aborted
  mid-header on Windows.
- **Files:** `scraper/utils/progress.py` (`_make_stdout_encoding_safe()`:
  `sys.stdout.reconfigure(errors="replace")`).
- **Tests:** `tests/test_progress.py::test_print_survives_non_utf8_stdout`;
  `server.sh` help/demo flow now exits cleanly under a cp1252 pipe.
- **Evidence:** `tests/test_server_controller.py` green where a usable POSIX
  bash exists (skipped with a clear reason on WSL-stub-only Windows hosts).

---

# Config tuning verification (Control file / config.yaml, workers + filters)

Operator-requested tuning: `concurrency.website_workers` 8 → 16,
`concurrency.playwright_workers` 2 → 4, plus confirmation that every knob in
`config.yaml` / `config.local.yaml` (the server.sh control-panel file) really
drives behavior. Verification-only generation: no engine code changed.

## Wiring audit (every knob traced to its runtime reader)

- `concurrency.website_workers` — READ at `scraper/pipeline.py`
  (`workers = max(1, self.cfg.concurrency.website_workers)`); sizes the
  enrichment `ThreadPoolExecutor` and the drain batch (`workers * 4`).
  Valid range 1–16 (pydantic), so 16 is the supported maximum.
- `concurrency.playwright_workers` — validated and capped (1–4) but NOT read
  by any runtime module: the Maps collector runs one sequential browser by
  design (anti-bot pacing). Changing it has NO runtime effect today; the
  config.yaml comment now says so honestly, and
  `tests/test_concurrency.py::test_playwright_workers_is_reserved_no_runtime_reader`
  guards that documentation (it fails if a future change wires it up).
- `filters:` (`include_all` / `include_any` / `exclude_all` / `exclude_any`) —
  READ at `scraper/pipeline.py` via `split_filters()` (Maps-field conditions run
  pre-enrichment, enrichment-field conditions run post-enrichment) and
  `evaluate()` in both pipeline passes. Operators: `= != > < >= <= in notin
  contains`, plus `negate`.
- All other sections were already covered by the F25 dead-section guard
  (`test_config_template_has_no_dead_sections`).

## Tests added (`tests/test_concurrency.py`)

- `test_template_concurrency_values_load` — the shipped config.yaml loads with
  website_workers=16 / playwright_workers=4.
- `test_website_workers_above_cap_fails_fast` /
  `test_website_workers_below_one_fails_fast` — 17 or 0 abort at startup with
  a clear ConfigError (fail-fast, no mid-run misbehavior).
- `test_website_workers_sizes_enrichment_thread_pool` — end-to-end demo run
  with a spied `ThreadPoolExecutor`: the pool is created with EXACTLY the
  configured count (16).
- `test_website_workers_one_runs_serially` — workers=1 takes the serial path
  (no pool built).
- `test_exclude_any_filter_drops_matching_records` /
  `test_include_all_filter_keeps_only_matching_records` — end-to-end proof
  that filter entries in config.yaml keep/drop exported rows (3 demo rows →
  2 committed, 1 filtered; CSV contents verified).
- `test_playwright_workers_cap_is_four`,
  `test_playwright_workers_is_reserved_no_runtime_reader`.

## Deployment procedure (server)

1. Back up the control file: `cp config.local.yaml config.local.yaml.bak-$(date +%F)`.
2. Apply repo update: `./server.sh update` (pulls this commit; NOTE — it does
   NOT overwrite your `config.local.yaml`).
3. Either re-copy the tuned values into the control file
   (`cp config.yaml config.local.yaml` — preserves nothing local, review first)
   or edit just the `concurrency:` block via `./server.sh config`.
4. Smoke: `./server.sh demo` (offline, no Google contact) then
   `./server.sh status` / `./server.sh logs`.

## Rollback

- Repo template: `git revert <commit>` or
  `cp config.yaml.bak-2026-09-03 config.yaml` (backup kept outside VCS), commit, push.
- Server: `cp config.local.yaml.bak-$(date +%F) config.local.yaml && ./server.sh stop && ./server.sh run`.

## Risk assessment

- website_workers=16 raises parallel HTTP load on target websites and on the
  VPS (sockets/RAM). It is I/O-bound work, validated ≤16, and each fetch has
  its own timeouts — accepted risk; drop to 8 if the VPS shows pressure.
- playwright_workers change is inert (reserved) — zero runtime risk.

---

# Config-driven custom signals + summary.json + full wiring audit

Generation implementing: (1) user-defined website signals from config.yaml
(column name + keywords + any/all combination), (2) per-campaign
`summary.json` with CPU/RAM/time/server/lead details, (3) an audit + wiring of
every remaining decorative config key. No hard-coded defaults: every key in
config.yaml now has a runtime reader, enforced by a test.

## 1. Custom signals (`signals.custom` in config.yaml)

- `scraper/config.py`: new `SignalsConfig` — per-signal spec `column` (the
  export column name), `match: any|all` (OR/AND combination of multiple
  keyword filters), `keywords`/`regex`, `enabled`. Validates column shape
  (`[a-z0-9_]`, ≤64 chars), duplicate columns, empty keyword sets, unknown
  match modes — all fail fast as ConfigError.
- `scraper/signals/detector.py`: `SignalDetector.run` honors `column` (output
  key) and `match: all` (new `_kw_signal_all`, AND semantics; `any` = legacy
  OR). Built-ins unchanged.
- `scraper/filters/engine.py`: `split_filters(extra_post_fields=...)` —
  custom signal columns are treated as enrichment fields so `filters:`
  conditions on them run in the post-enrichment pass.
- `scraper/pipeline.py`: schema becomes dynamic — 68 base columns + one column
  per enabled custom signal (CSV, XLSX and summary all use it). Collision with
  built-in columns is rejected at startup. Removal from config.yaml removes
  the column.
- Tests (`tests/test_custom_signals.py`, 12): validation failures, any/all
  detection, disabled spec, end-to-end CSV export, add/remove column parity,
  filter-on-custom-column end-to-end.

## 2. summary.json (per campaign)

- New `scraper/utils/resources.py`: `ResourceMonitor` (psutil when available,
  stdlib fallback) samples process+children CPU % and RSS on a daemon thread;
  `host_details()` reports platform/python/CPU count/total RAM.
- New `SummaryConfig` (`summary.enabled`, `summary.sample_interval_seconds`);
  written to `output/<client_name>/summary.json` in a `finally` block, so a
  mid-run crash still produces the summary.
- Structure: `campaign_id`, `generated_at`, `cpu_max_usage_percent`,
  `ram_consumed_mb` (peak, process+children), `ram_final_mb`,
  `execution_time_seconds`, `counters`, `campaign_details` (queries, output
  files, concurrency, maps, reviews, filters, custom signals), `servers`
  (per-host environment + metric details), `leads` (every committed row with
  all columns incl. custom signals).
- Dependency: `psutil>=5.9,<8` added (requirements.txt + pyproject).
- Tests (`tests/test_summary.py`, 3): required fields, disabled = no file,
  custom signals echoed in campaign_details.

## 3. Wiring audit — zero decorative variables

New `tests/test_config_wiring.py::test_every_config_key_is_operational` parses
every leaf key of config.yaml and requires a reader in scraper source outside
config.py. This audit exposed 19 previously decorative keys, now wired:

| Key | Wiring |
|---|---|
| `website.require_website` | hard pre-enrichment gate: drop records with no website (`pipeline._dedup_and_prefilter`) |
| `enrichment.require_website` | skip the whole website stage for no-website records (`_enrich_and_stage`) |
| `website.enable_playwright_fallback` | new `scraper/websites/renderer.py`: thread-local Playwright render for JS_REQUIRED sites |
| `website.page_navigation_timeout_seconds` | renderer goto timeout |
| `website.enable_sitemap` | sitemap.xml discovery merged into the priority crawl (`enricher.enrich` + `crawler.crawl_sitemap_aware`) |
| `website.http_connect_timeout_seconds` | httpx connect phase cap (`Fetcher`) |
| `website.http_retries` | retry attempts with linear backoff on transient errors (`Fetcher.fetch`) |
| `runtime.request_timeout` | per-request phase cap (`Fetcher.total_timeout`) |
| `runtime.idle_exit_seconds` | run-level watchdog: graceful stop when no leads committed for N seconds (`Pipeline._idle_exceeded`, `_RunIdle`) |
| `runtime.pacing` | multiplier on the between-query Maps delay (`main._build_collector`) |
| `maps.max_scrolls` | scroll-round cap in `_scroll_results` (0 = built-in bound 12) |
| `maps.scroll_pause_seconds` | settle wait when the feed height stalls (lazy-loaded cards) |
| `job.output_filename` | optional CSV filename override (`Pipeline.__init__`) |
| `email.enable_mx_check` | switches the MX checker on (`Pipeline`, OR with `enrichment.mx_verify`) |
| `analysis.lexicon_hint` | `analysis.engine.extend_lexicon`: JSON/YAML custom sentiment lexicon (single-token entries) |
| `smtp.connection_timeout_seconds` | TCP connect cap for port-25 verification (`SMTPVerifier`) |
| `delays.site_min_seconds` / `site_max_seconds` | same-site crawl pacing (`Enricher` page loop) |
| `website.overall_site_timeout_seconds` | whole-site crawl deadline (`Enricher.enrich`) |

`job.output_filename` note: empty keeps the historical `leads.csv` name —
existing consumers unaffected (backward compatible).

## Tests

Suite grew 224 → 242 passing (18 new: signals/summary/wiring). Full suite,
compileall, and the F25 dead-section guard all green.

---

# Photos/owner-activity columns (5 new) + business_description ELIMINATED (68 → 72 base)

Operator-requested changes: (1) `business_description` completely eliminated
from the engine (production showed only "See photos" junk on every row), and
(2) five new Maps detail-panel columns added right after `business_hours`.

## Removed — `business_description`

- Dropped from `models.OUTPUT_COLUMNS` (68 → 72 base columns), from the
  collector (`DESCRIPTION_SELECTORS`, `clean_description`, junk regex — all
  removed), from `_TEXT_COLUMNS` (transform), and from `DemoCollector`.
- Tests for the removed feature were replaced by an elimination lock
  (`test_business_description_removed_from_schema`) per the owner decision.

## Added — 5 columns (after `business_hours`)

| Column | Source (detail panel) | Value |
|---|---|---|
| `cover_image_url` | hero header `button.aoRNLd img[src]` (+ hero-jsaction / `div.ZKCDEc` / carousel-All fallbacks) | Google CDN image URL |
| `latest_image_upload` | photos carousel card `button[aria-label^="Latest"]` → aria-label "Latest · 11 days ago" | relative time ("11 days ago") |
| `by_owner_photos` | carousel card `button[aria-label="By owner"]` presence | YES / NO |
| `has_recent_post` | `h2` "From the owner" section presence (deep panel) | YES / NO |
| `latest_post_date` | owner-post timestamp (`.S3NLN .lqMB`) | relative time ("3 days ago") |

Extraction lives in `scraper/maps/collector.py`:
`_settle_panel()` (cursor moved INTO `div[role=main]`, bounded wheel scroll —
lazy sections hydrate on scroll), `_scroll_photos_into_view()`,
`_deep_scroll_panel()` (scrolls all tall containers to bottom for the
virtualized owner-post section), and `_read_photo_columns()` (pure-read
helper, retried once after a deep-scroll round when everything misses —
Google hydrates the photos section inconsistently across runs; live-verified).
Pure helper `parse_latest_upload_label()` splits the carousel aria-label.

## Schema migration (backward compatible)

`Pipeline._migrate_csv_schema()`: an existing leads.csv written by an older
schema is detected (header mismatch) and rebuilt from the checkpoint's
committed `raw_json` records — new columns padded `N/A`, removed columns
(`business_description`) dropped. Zero data loss; no manual action needed.

## Live verification (real Google Maps run, headless Chromium)

Query "SEO Expert in Pakistan - Digital Marketing Services", 2 results:
- `cover_image_url` — real googleusercontent URLs on both rows ✓
- `latest_image_upload` — "11 days ago" on the listing whose carousel has a
  Latest card (matches the operator's inspected listing exactly); honest N/A
  on the listing without one ✓
- `by_owner_photos` — YES where the owner-photos category exists ✓
- `summary.json` — 2 leads, metrics + campaign details present ✓
- `business_description` — absent from header ✓

## Known limitation

`has_recent_post`/`latest_post_date` depend on the "From the owner" section
being rendered; Google serves that section inconsistently to anonymous
headless sessions (it virtualizes deep below the fold and never entered the
DOM in our live runs, even though it appears in the operator's logged-in
session). The column honestly reports NO/N/A rather than fabricating data.
Tests (`tests/test_photo_columns.py`, 4): pure parsing, schema position/count,
e2e CSV export order + values, old-CSV migration rebuild.

Suite: 242 → 245 passing.

---

## PERF-2026-09 — Google Maps discovery 5–8× (forensic, live-measured)

Baseline (MEASURED on real Google Maps with the real collector, plus owner
production numbers: 2000 leads / 8–9h ≈ 15.3s/lead): the serial Maps browser
was the critical path — enrichment pool (150 rec/s measured on the mock
benchmark) sat ~2000× starved. Suite grew 272 → 290 passing tests.

Root causes found (each live-verified before fixing):
- **P01 — identity wait grabbed the feed's h1.** `querySelector('h1')`
  matched the results-feed "Results" heading (DOM order) — never the panel
  h1 — so the combined wait burned its full 8s timeout + 1s fallback sleep
  on EVERY listing. Fix: panel-first selector chain
  (`h1.DUwDvf / fontHeadline` then non-feed h1 fallback).
- **P02 — exact-href card matching failed in production.** Google churns
  card hrefs (tracking params) every render; the `href == place_url` check
  failed → every listing fell back to a full `page.goto` navigation
  (~4–8s). Fix: `!1s0x…:0x…` place-id token matching for both the card map
  and the panel-switch proof (clicks now ~0.4s, verified live).
- **P03 — reviews burned 7.2s against panels with no reviews section.**
  Google's "limited view" (Feb 2026 rollout, still active for logged-out
  sessions) serves panels with the reviews section entirely absent. Fix:
  one evaluate() detects the affordance (tab buttons scoped to the panel,
  review spans, limited-view banner) → skip in ~0.02s instead of timing out.
  Where reviews exist, extraction is unchanged (batched reads).
- **P04 — O(n²) card scan.** Every listing re-scanned every result card via
  per-card get_attribute round-trips. Fix: one evaluate() builds a
  per-selector token→index map once per query (indices are per-selector —
  a cross-selector index click was caught and fixed during bring-up).
- **P05 — per-node round-trips.** Social links: 66 anchors → 1.27s
  (measured) → one batched evaluate 0.026s (49×). Photo columns: per-selector
  locator waits (1.5s, chips often not rendered) → one batched evaluate
  (~0.005s). Panel fields (name/category/address/phone/website/plus_code/
  hours/status/claim/rating): ~20 locator reads → 1–2 evaluate() calls with
  per-field fallback preserved.
- **P06 — fixed sleeps.** settle_panel 4×400ms → one in-page evaluate,
  condition-based (returns when panel scroll height stabilizes). Photos
  600ms fixed → condition-based. Reviews tab-open 1.5s → wait_for_selector.
- **P07 — owner-post deep scroll (~3s/listing) served two columns the
  owner confirmed are unused.** Fix: `maps.extract_owner_posts` gate
  (default false; `true` restores the exact old behavior + columns).
- **P08 — pacing defaults.** `delays.maps_*` 2.0–5.0 → 0.4–1.2 (comment in
  config.yaml explains how to restore conservative values; anti-bot
  cooldown/challenge handling unchanged).

New capability:
- **P09 — `maps.workers` (default 1 = identical serial behavior; ships as
  2 in config.yaml).** Feed discovery stays serial (one context scrolls +
  builds the card list); per-listing extraction fans out across N isolated
  browser contexts working alternating slices of the same card list. Dedup
  identity is worker-independent (pipeline identity keys unchanged). A
  single-IP VPS should stay at 2 (higher = more CAPTCHA risk).

Verification: 290/290 tests pass; live probes on real Google Maps
(plumbers Houston + dentists Dallas): raw per-listing 10.4–15.3s →
1.9–3.3s across runs; round-trips/listing 251 → 34–39; data spot-check
(name/rating/review_count/cover image/category/address/phone) all correct.
Full breakdown + reproduction steps: docs/PERFORMANCE.md.
