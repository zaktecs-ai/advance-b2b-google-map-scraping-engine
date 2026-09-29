"""Pipeline orchestrator: collect -> dedup -> filter -> enrich -> verify -> score -> export.

The pipeline owns resumability: it seeds the identity resolver from committed
records, runs pre-enrichment filters, enriches websites, then post-enrichment
filters, and appends surviving records to the atomic CSV before advancing the
checkpoint. Rejected records roll back their dedup entries so a legitimate
re-discovery is preserved.

Throughput architecture (continuous producer/consumer):

    Maps discovery (producer thread)  ->  bounded work queue
        ->  long-lived HTTP worker pool (created ONCE per run)
            ->  serial committer thread  ->  CSV / checkpoint

Discovery streams records into enrichment as they are found; enrichment never
waits for a batch to assemble and discovery never waits for enrichment to
drain. A single committer thread keeps every shared-state mutation (dedup
rollback, social ownership, CSV append, checkpoint) serialized exactly as the
old batch design did. Domain-aware rate limiting (per-domain slots, Retry-
After, backoff with jitter) keeps the aggregate throughput polite to each
individual website.
"""
from __future__ import annotations

import csv
import logging
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .analysis.engine import analyze
from .analysis.llm_hooks import LLMHookGenerator
from .checkpoint.store import CheckpointStore
from .config import AppConfig
from .dedup.dedup import IdentityResolver
from .email.verification import MXChecker, SMTPVerifier
from .export.csv_writer import AtomicCSVWriter
from .export.summary import write_summary
from .export.xlsx_writer import write_xlsx
from .filters.engine import evaluate, split_filters
from .maps.collector import MapsCollector
from .maps.geo import geojson_polygons, point_in_any_polygon
from .maps.reviews import filter_reviews
from .maps.transform import normalize_listing
from .models import OUTPUT_COLUMNS
from .signals.detector import SignalDetector
from .signals.social import detect_social
from .utils.normalize import normalize_text
from .utils.resources import ResourceMonitor, host_details
from .validation.quality import passes_quality
from .websites.enricher import Enricher
from .websites.rate_limiter import DomainCooldowns, DomainGate

log = logging.getLogger(__name__)


class _RunIdle(RuntimeError):
    """Raised to stop a run gracefully when no new leads are committed."""

_SOCIAL_COLS = ["facebook", "instagram", "linkedin", "youtube", "twitter_x",
                "tiktok", "pinterest", "github", "snapchat"]
# Columns sourced from the tech detector (string values like cms=WordPress).
_TECH_COLS = ["cms", "analytics", "tag_manager", "ssl"]
# Columns sourced from the signal detector (YES/NO toggle values).
_BOOL_TECH_COLS = ["meta_pixel", "ga4", "gtm", "advertising",
                   "booking_system", "chat_widget"]
_SIGNAL_COLS = ["signal_pricing", "signal_financing", "signal_licensed_insured",
                "signal_established", "signal_portfolio", "signal_mobile_service",
                "signal_membership"]


def _make_record_id(raw: dict) -> str:
    strong = raw.get("kgmid") or raw.get("place_id")
    if strong and str(strong).upper() not in ("N/A", ""):
        # G11: normalize the strong id so every record_id contains exactly
        # ONE colon (the uuid suffix separator). A place_id like
        # `0x864…:0x841…` previously produced a 3-colon id, breaking any
        # consumer that splits record_id on ':'.
        return f"{str(strong).replace(':', '-')}:{uuid.uuid4().hex[:8]}"
    return uuid.uuid4().hex


class SocialOwnershipRegistry:
    """G13: one social profile belongs to exactly one business.

    Production evidence: All American Plumbing (allamerican-plumbing.com)
    exported the SAME facebook/instagram handles as Houston Plumbing Expert —
    a different business row. Whatever the leak path (panel fallback scope,
    agency-built websites linking another client's socials), a social URL
    claimed by two distinct record identities in one run is contaminated.
    The first record to commit keeps it; later claimants are blanked to N/A.
    """

    def __init__(self) -> None:
        self._owner: dict[str, str] = {}

    def claim(self, url: str, record_id: str) -> bool:
        """Claim ``url`` for ``record_id``. False when already owned by another."""
        if not url or url == "N/A":
            return True
        owner = self._owner.get(url)
        if owner is None:
            self._owner[url] = record_id
            return True
        return owner == record_id


class Pipeline:
    def __init__(self, config: AppConfig, collector: MapsCollector | None = None,
                 browser_manager=None, progress=None, proxy_manager=None):
        self.cfg = config
        self.collector = collector
        self._bm = browser_manager
        self._proxy_manager = proxy_manager
        from .utils.progress import NullProgress
        self._progress = progress or NullProgress()

        # -- Config-driven custom signals (signals.custom) ------------------
        # Each enabled spec becomes a YES/NO column appended to the export
        # schema. The user chooses the column name; removal from config.yaml
        # removes the column — no decorative keys.
        self._custom_signal_specs = {
            name: dict(spec)
            for name, spec in (config.signals.custom or {}).items()
            if isinstance(spec, dict) and spec.get("enabled", True)
        }
        self.custom_signal_columns = [
            str(spec.get("column") or f"signal_{name}").strip().lower()
            for name, spec in self._custom_signal_specs.items()
        ]
        _collide = sorted(set(self.custom_signal_columns) & set(OUTPUT_COLUMNS))
        if _collide:
            raise ValueError(
                f"signals.custom column(s) collide with the built-in schema: "
                f"{_collide} — choose different names")
        self.output_columns = tuple(OUTPUT_COLUMNS) + tuple(
            self.custom_signal_columns)

        # -- job.output_filename: optional override of the CSV base name ----
        csv_name = (f"{config.job.output_filename}"
                    if config.job.output_filename else "leads.csv")

        # -- analysis.lexicon_hint: optional custom sentiment lexicon -------
        if config.analysis.lexicon_hint:
            from .analysis.engine import extend_lexicon
            extend_lexicon(config.analysis.lexicon_hint)

        out_dir = Path(config.job.output_dir) / config.job.client_name
        out_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = out_dir
        self.checkpoint = CheckpointStore(out_dir / "checkpoint.sqlite")
        # Schema migration: an existing leads.csv written by an older schema
        # is rebuilt from the checkpoint's committed raw records (missing new
        # columns become N/A, removed columns are dropped) BEFORE the writer
        # opens the file, so the append writer always sees a matching header.
        self._migrate_csv_schema(out_dir / csv_name, list(self.output_columns))
        self.csv = AtomicCSVWriter(out_dir / csv_name, list(self.output_columns))
        self._reconcile_csv_with_checkpoint()
        # G13: serial-pass guard against cross-business social contamination.
        self._social_registry = SocialOwnershipRegistry()
        seeds = self.checkpoint.seed_sets()
        self.resolver = IdentityResolver(
            seen_identities=seeds["identities"],
            seen_domains=seeds["domains"],
            seen_phones=seeds["phones"],
            seen_domain_city=seeds["domain_city"],
            domain_first_name=seeds.get("domain_first_name"),
            phone_first_name=seeds.get("phone_first_name"),
            default_country=config.job.default_country,
        )

        # Email verification (native, off by default). email.enable_mx_check
        # is the per-email toggle; enrichment.mx_verify the pipeline toggle —
        # either one switches the MX checker on.
        self.mx_checker = MXChecker(
            enabled=config.enrichment.mx_verify or config.email.enable_mx_check)
        self.smtp_verifier = SMTPVerifier(
            enabled=config.enrichment.smtp_verify,
            timeout=config.smtp.verification_timeout_seconds,
            from_email=config.smtp.from_email,
            retries=config.smtp.retries,
            max_workers=config.smtp.workers,
            connect_timeout=config.smtp.connection_timeout_seconds,
        )

        max_pages = config.website.max_pages_per_site
        # Thread the proxy through to the httpx fetcher so enrichment traffic
        # (the bulk of requests) leaves from the configured proxy, not the
        # server's real IP (F27).
        proxy_url = (proxy_manager.httpx_proxy() if proxy_manager is not None
                     else None)
        # Domain-aware rate limiting: per-domain slots + shared cooldown
        # registry. The GLOBAL cap is the worker pool size; these gates keep
        # individual domains polite while aggregate throughput stays high.
        cc = config.concurrency
        self._gate = DomainGate(per_domain=cc.per_domain_concurrency)
        self._cooldowns = DomainCooldowns()
        self.enricher = Enricher(
            timeout=config.website.http_read_timeout_seconds,
            max_pages=max_pages,
            signal_detector=SignalDetector(self._custom_signal_specs),
            total_request_timeout=config.runtime.request_timeout,
            overall_site_timeout_seconds=config.website.overall_site_timeout_seconds,
            connect_timeout=config.website.http_connect_timeout_seconds,
            http_retries=config.website.http_retries,
            enable_sitemap=config.website.enable_sitemap,
            enable_playwright_fallback=config.website.enable_playwright_fallback,
            page_navigation_timeout_seconds=config.website.page_navigation_timeout_seconds,
            site_delay=(config.delays.site_min_seconds,
                        config.delays.site_max_seconds),
            proxies=proxy_url,
            mx_checker=self.mx_checker,
            smtp_verifier=self.smtp_verifier,
            use_wappalyzer=config.website.use_wappalyzer,
            decision_makers=config.enrichment.decision_makers,
            proxy_manager=proxy_manager,
            exclude_selectors=config.enrichment.exclude_selectors,
            max_email_length=config.email.max_email_length,
            gate=self._gate,
            cooldowns=self._cooldowns,
            backoff_base=config.website.retry_backoff_base_seconds,
            backoff_cap=config.website.retry_backoff_cap_seconds,
            respect_retry_after=cc.respect_retry_after,
            playwright_pool_size=cc.playwright_workers,
        )

        # LLM personalized hook generator (optional, auto-detects API key).
        self.llm_hook = LLMHookGenerator(
            enabled=config.ai_hook.enabled,
            provider=config.ai_hook.provider,
            model=config.ai_hook.model,
            api_key_env=config.ai_hook.api_key_env,
            timeout=config.ai_hook.timeout_seconds,
            max_calls=config.ai_hook.max_calls,
            retries=config.ai_hook.retries,
        )

        self.pre_filters, self.post_filters = split_filters(
            config.filters.model_dump() if config.filters else {},
            extra_post_fields=set(self.custom_signal_columns),
        )
        self.polygons = geojson_polygons(config.geo.polygons) if config.geo.polygons else []
        self.counters = {"collected": 0, "deduped": 0, "filtered": 0,
                         "committed": 0, "failed": 0}
        # -- Continuous producer/consumer enrichment pool --------------------
        # Created ONCE per pipeline run. Maps discovery (producer) feeds a
        # bounded work queue; ``website_workers`` long-lived threads consume
        # newly discovered websites continuously; a single committer thread
        # serializes everything that touches shared state (resolver rollback,
        # social registry, CSV append, checkpoint).
        cc = config.concurrency
        self._worker_count = max(1, cc.website_workers)
        self._max_in_flight = cc.max_in_flight or self._worker_count * 4
        self._work_q: "queue.Queue[tuple | None]" = queue.Queue(
            maxsize=self._max_in_flight)
        self._done_q: "queue.Queue[tuple | None]" = queue.Queue()
        self._worker_threads: list[threading.Thread] = []
        self._committer_thread: threading.Thread | None = None
        self._pool_started = False
        self._pool_shut = False
        # Runtime stats (throughput/latency/utilization) for summary.json and
        # the benchmark harness.
        self._stats_lock = threading.Lock()
        self._stats = {"enriched": 0, "enrich_seconds": 0.0,
                       "enrich_seconds_max": 0.0,
                       "queue_depth_max": 0, "worker_busy_seconds": 0.0}
        # -- runtime.idle_exit_seconds + summary.json state -----------------
        self._started_mono = time.monotonic()
        self._idle_exit_seconds = float(config.runtime.idle_exit_seconds or 0.0)
        self._last_commit_mono = self._started_mono
        self._summary_leads: list[dict] = []
        self._monitor: ResourceMonitor | None = None

    def _idle_exceeded(self) -> bool:
        """True when runtime.idle_exit_seconds > 0 and no lead has been
        committed for longer than that window."""
        return (self._idle_exit_seconds > 0
                and (time.monotonic() - self._last_commit_mono)
                > self._idle_exit_seconds)

    def runtime_stats(self) -> dict:
        """Throughput/latency/utilization metrics for the enrichment pool.

        - ``records_per_second``: committed rows / elapsed wall clock.
        - ``avg_enrich_seconds`` / ``max_enrich_seconds``: per-record
          enrichment latency (fetch + crawl + extract + analyze).
        - ``queue_depth_max``: peak prepared-but-not-enriched backlog.
        - ``worker_utilization``: fraction of total worker-thread time spent
          enriching (1.0 = workers were never idle).
        """
        with self._stats_lock:
            s = dict(self._stats)
        elapsed = max(1e-6, time.monotonic() - self._started_mono)
        enriched = s["enriched"] or 1
        pool_seconds = elapsed * self._worker_count
        return {
            "elapsed_seconds": round(elapsed, 2),
            "records_per_second": round(self.counters["committed"] / elapsed, 3),
            "enriched": s["enriched"],
            "avg_enrich_seconds": round(s["enrich_seconds"] / enriched, 3),
            "max_enrich_seconds": round(s["enrich_seconds_max"], 3),
            "queue_depth_max": s["queue_depth_max"],
            "in_flight_cap": self._max_in_flight,
            "worker_utilization": round(
                min(1.0, s["worker_busy_seconds"] / pool_seconds), 3),
            "worker_count": self._worker_count,
        }

    def _migrate_csv_schema(self, csv_path: Path, expected: list[str]) -> None:
        """Rebuild an old-schema leads.csv to the current column contract.

        The checkpoint's committed raw records are the source of truth; rows
        are re-projected onto the new schema (missing keys -> N/A, removed
        keys dropped). No-op when the file is absent or already current.
"""
        try:
            with open(csv_path, encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001 - unreadable file: leave it
            log.warning("schema migration skipped (unreadable CSV): %s", e)
            return
        if not rows or rows[0] == expected:
            return
        committed = list(self.checkpoint.iter_committed_rows())
        log.warning(
            "schema change detected (%d -> %d columns) - rebuilding %s from "
            "checkpoint (%d committed rows)",
            len(rows[0]), len(expected), csv_path.name, len(committed))
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(expected)
            for raw in committed:
                writer.writerow([str(raw.get(c, "N/A")) for c in expected])
            fh.flush()
            os.fsync(fh.fileno())

    def _reconcile_csv_with_checkpoint(self) -> None:
        """Trim CSV rows that were durably written but never committed.

        A crash between ``csv.append`` and ``checkpoint.mark_committed`` leaves
        a well-formed CSV row with no committed marker. On startup the
        checkpoint's committed count is the authority for how many CSV rows are
        valid; any uncommitted tail row is trimmed so the same business is
        cleanly re-collected (and dedup-seeded) on this run.
        """
        try:
            committed = self.checkpoint.committed_count()
        except Exception as e:  # noqa: BLE001 — a broken checkpoint must not abort startup
            log.warning("checkpoint count failed; skipping reconcile: %s", e)
            return
        if self.csv.row_count > committed:
            log.warning(
                "reconciling CSV: %d rows written but only %d committed — "
                "trimming %d uncommitted tail row(s)",
                self.csv.row_count, committed, self.csv.row_count - committed,
            )
            self.csv.truncate_to(committed)

    # -- collection ----------------------------------------------------------
    def run(self) -> dict:
        if self.cfg.summary.enabled:
            self._monitor = ResourceMonitor(
                self.cfg.summary.sample_interval_seconds)
            self._monitor.start()
        try:
            self._start_pool()
            for idx, query in enumerate(self.cfg.queries, start=1):
                if self.checkpoint.is_query_done(query):
                    self._progress.note(f"skipped (already done): {query}")
                    continue
                if self._idle_exceeded():
                    self._progress.note(
                        f"idle exit: no new leads for "
                        f"{self._idle_exit_seconds:.0f}s — stopping run")
                    break
                self.checkpoint.register_query(query)
                self._progress.query_started(idx, query)
                try:
                    self._produce_query(query)
                except _RunIdle:
                    self._progress.note(
                        f"idle exit: no new leads for "
                        f"{self._idle_exit_seconds:.0f}s — stopping run")
                    break
                except Exception as e:
                    log.error("query %r failed: %s (left un-done for retry)",
                              query, e)
                    self._progress.note(
                        f"query failed (will retry next run): {query}")
                    self.checkpoint.mark_query_failed(query)
                    continue
                self.checkpoint.mark_query_done(query)
                self._progress.query_done()
                if self._bm is not None:
                    self._bm.mark_query()
                    self._bm.recycle()

            # Drain: workers finish every queued task, the committer writes
            # every enriched record, THEN the run finishes. Discovery already
            # overlapped enrichment throughout — no per-batch waits happened.
            self._shutdown_pool()
            self._progress.finish(failed=self.counters['failed'])
            return self.counters
        finally:
            # Summary + XLSX are produced even when a query crashed mid-run.
            self._shutdown_pool(timeout=10.0)
            self._finalize()

    # -- continuous enrichment pool -------------------------------------------
    def _start_pool(self) -> None:
        """Start the long-lived enrichment workers + committer (once)."""
        if self._pool_started:
            return
        self._pool_started = True
        for i in range(self._worker_count):
            t = threading.Thread(target=self._worker_loop,
                                 name=f"enrich-worker-{i}", daemon=True)
            t.start()
            self._worker_threads.append(t)
        self._committer_thread = threading.Thread(
            target=self._committer_loop, name="committer", daemon=True)
        self._committer_thread.start()
        log.info("enrichment pool started: %d workers, in-flight cap %d, "
                 "per-domain concurrency %d", self._worker_count,
                 self._max_in_flight, self.cfg.concurrency.per_domain_concurrency)

    def _worker_loop(self) -> None:
        """Consume prepared records until a None sentinel arrives."""
        while True:
            task = self._work_q.get()
            if task is None:
                break
            rec, sig, query = task
            t0 = time.monotonic()
            try:
                self._safe_enrich(rec)
            except Exception as e:  # noqa: BLE001 — a worker must NEVER die:
                # a dead thread silently shrinks the pool and can deadlock the
                # bounded queue (producer blocks on a full queue forever).
                log.exception("worker loop error: %s", e)
                rec["website_status"] = "UNKNOWN"
                rec["website_failure_reason"] = f"worker_error:{type(e).__name__}"
                rec.setdefault("emails", "N/A")
                rec.setdefault("email_count", 0)
            duration = time.monotonic() - t0
            with self._stats_lock:
                s = self._stats
                s["enriched"] += 1
                s["enrich_seconds"] += duration
                s["enrich_seconds_max"] = max(s["enrich_seconds_max"], duration)
                s["worker_busy_seconds"] += duration
            self._done_q.put((rec, sig, query))

    def _committer_loop(self) -> None:
        """Serial commit stage: post-filters -> quality -> CSV + checkpoint.

        All shared-state mutation (resolver rollback, social registry, CSV
        writer, checkpoint) stays on this single thread, preserving the exact
        serialization guarantees the batch design had. The loop NEVER dies:
        an exception during one commit skips that record but keeps draining,
        so the pipeline can never deadlock on a full work queue.
        """
        while True:
            item = self._done_q.get()
            if item is None:
                break
            rec, sig, query = item
            try:
                self._commit_stage(rec, query, sig)
            except Exception as e:  # noqa: BLE001
                log.exception("commit failed for %s: %s",
                              rec.get("business_name"), e)
                self.counters["failed"] += 1

    def _shutdown_pool(self, timeout: float | None = None) -> None:
        """Sentinel-shutdown the pool (idempotent).

        Workers finish every queued task before exiting; the committer drains
        every enriched record before exiting. With ``timeout`` set, threads
        that don't finish in time are abandoned (daemon threads) so a crash
        path can never hang the process.
        """
        if self._pool_shut or not self._pool_started:
            self._pool_shut = True
            return
        self._pool_shut = True
        for _ in self._worker_threads:
            try:
                self._work_q.put_nowait(None)
            except queue.Full:
                # Bounded queue is full of real work; workers reach the
                # sentinel after draining (blocking put keeps ordering safe).
                self._work_q.put(None)
        deadline = time.monotonic() + (timeout if timeout else 3600.0)
        for t in self._worker_threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._committer_thread is not None:
            self._done_q.put(None)
            self._committer_thread.join(
                timeout=max(0.0, deadline - time.monotonic()))

    def _collect_records(self, query: str):
        """Dispatch to the parallel Maps fan-out when maps.workers > 1.

        The collector itself owns the worker model (contexts, slicing,
        challenge fallback); the pipeline only picks the entry point, so
        DemoCollector (no collect_parallel) and tests keep working with the
        plain serial collect().
        """
        workers = getattr(self.cfg.maps, "workers", 1) or 1
        if workers > 1 and hasattr(self.collector, "collect_parallel"):
            return self.collector.collect_parallel(query, workers=workers)
        return self.collector.collect(query)

    def _produce_query(self, query: str) -> None:
        """Producer stage: stream Maps listings into the enrichment queue.

        Discovery never waits for enrichment to finish and enrichment never
        waits for the next batch — the bounded queue provides backpressure so
        memory stays flat while both stages run continuously.
        """
        keyword = self._split_keyword(query)
        self._query_collected = 0
        for raw in self._collect_records(query):
            self.counters["collected"] += 1
            self._query_collected += 1
            rec = self._normalize_record(raw, query, keyword)
            if not rec:
                continue
            name = rec.get("business_name") or "—"
            pos = (raw or {}).get("_position") or self._query_collected
            total = (raw or {}).get("_total") or 0
            self._progress.business_collected(pos, name, total)
            prepared = self._dedup_and_prefilter(rec, query)
            if prepared is None:
                continue
            depth = self._work_q.qsize()
            with self._stats_lock:
                st = self._stats
                if depth > st["queue_depth_max"]:
                    st["queue_depth_max"] = depth
            # Blocks when the in-flight cap is reached: natural backpressure.
            self._work_q.put((prepared[0], prepared[1], query))
            if self._idle_exceeded():
                raise _RunIdle(
                    f"no new leads committed for "
                    f"{self._idle_exit_seconds:.0f}s")

    def _dedup_and_prefilter(self, rec: dict, query: str) -> tuple[dict, dict] | None:
        """Serial pass: assign id, dedup, and run pre-enrichment filters.

        Returns ``(rec, sig)`` for records that proceed to enrichment, or None
        for duplicates / pre-filter rejects (already counted + rolled back).
        """
        record_id = _make_record_id(rec)
        rec["record_id"] = record_id
        is_dup, _, sig = self.resolver.is_duplicate(rec)
        if is_dup:
            self.counters["deduped"] += 1
            self._progress.business_dup()
            return None
        # website.require_website: a hard config gate — records with no
        # website never proceed, regardless of the filters section.
        if self.cfg.website.require_website and rec.get(
                "website", "N/A") in (None, "", "N/A"):
            self.resolver.rollback(rec)
            self.counters["filtered"] += 1
            self._progress.business_filtered()
            return None
        keep, _ = evaluate(rec, self.pre_filters)
        if not keep:
            self.resolver.rollback(rec)
            self.counters["filtered"] += 1
            self._progress.business_filtered()
            return None
        return rec, sig

    def _safe_enrich(self, rec: dict) -> None:
        """Enrich one record without letting a single failure abort the batch.

        ``ex.map`` re-raises the first worker exception, which previously
        marked the whole query failed and triggered a full re-scrape (F26).
        """
        try:
            self._enrich_and_stage(rec)
        except Exception as e:  # noqa: BLE001
            log.exception("enrich failed for %s", rec.get("business_name"))
            rec["website_status"] = "UNKNOWN"
            rec["website_failure_reason"] = f"enrich_error:{type(e).__name__}"
            rec.setdefault("emails", "N/A")
            rec.setdefault("email_count", 0)

    def _enrich_and_stage(self, rec: dict) -> None:
        """Parallel-safe, record-local mutation: enrich + analyze + LLM hook.

        The shared ``Enricher`` uses an httpx.Client (thread-safe for concurrent
        requests); MX/SMTP and the LLM hook guard their own shared state, so
        concurrent calls only touch their own ``rec`` dict.
        """
        website = rec.get("website")
        # enrichment.require_website: skip the whole website stage (fetch,
        # signals, emails, tech) for records without a website instead of
        # wasting a doomed fetch.
        if self.cfg.enrichment.require_website and website in (None, "", "N/A"):
            rec["website_status"] = "SKIPPED_NO_WEBSITE"
            return
        self._enrich(rec, website)
        self._analyze(rec)
        self._apply_llm_hook(rec)

    def _commit_stage(self, rec: dict, query: str, sig: dict) -> None:
        """Serial pass: post-enrichment filters, quality gate, then commit."""
        keep2, freason2 = evaluate(rec, self.post_filters)
        if not keep2:
            self.resolver.rollback(rec)
            self.counters["filtered"] += 1
            self._progress.business_filtered()
            rec["filtered_out_reason"] = freason2
            return
        if not passes_quality(rec):
            self.resolver.rollback(rec)
            self.counters["failed"] += 1
            return
        # G13: a social URL already committed under a DIFFERENT record is
        # cross-business contamination — blank it here (the first claimant
        # keeps the profile).
        if self.cfg.enrichment.social:
            for c in _SOCIAL_COLS:
                u = rec.get(c)
                if u and u != "N/A" and not self._social_registry.claim(
                        u, rec["record_id"]):
                    log.warning(
                        "social %s already owned by another record — "
                        "blanking on %s", u, rec.get("business_name"))
                    rec[c] = "N/A"
        row = {c: rec.get(c, "N/A") for c in self.output_columns}
        idx = self.csv.append(row)
        self.checkpoint.register_record(
            record_id=rec["record_id"], identity_key=sig.get("identity_key", ""),
            sig=sig, source_query=query, raw=rec)
        self.checkpoint.mark_committed(rec["record_id"], idx)
        self.counters["committed"] += 1
        self._last_commit_mono = time.monotonic()
        self._summary_leads.append(row)
        self._progress.business_saved()

    @staticmethod
    def _split_keyword(query: str) -> str:
        """'dentists in Dallas, TX' -> 'dentists'; 'plumbers near 32,-96' -> 'plumbers'."""
        m = re.split(r"\s+(?:in|near)\s+", query, maxsplit=1, flags=re.I)
        return m[0].strip() if len(m) == 2 else query

    def _normalize_record(self, raw: dict, query: str, keyword: str) -> dict | None:
        loc = (
            re.split(r"\s+(?:in|near)\s+", query, maxsplit=1, flags=re.I)[-1]
            if re.search(r"\s+(?:in|near)\s+", query, re.I)
            else ""
        )
        rec = normalize_listing(
            raw,
            query=query,
            keyword=keyword,
            default_country=self.cfg.job.default_country,
        )
        if rec.get("source_location") in (None, "N/A", ""):
            rec["source_location"] = normalize_text(loc)

        # Polygon filter.
        if self.polygons:
            try:
                lat = float(rec.get("latitude"))
                lng = float(rec.get("longitude"))
            except (TypeError, ValueError):
                lat = lng = None
            if lat is not None and lng is not None and not point_in_any_polygon(
                lat, lng, self.polygons
            ):
                return None
        return rec

    def _enrich(self, rec: dict, website) -> None:
        if website in (None, "N/A", ""):
            rec["website_status"] = "N/A"
            rec["website_failure_reason"] = "N/A"
            return
        enr = self.enricher.enrich(website)
        rec["website_status"] = enr.website_status
        rec["website_failure_reason"] = enr.failure_reason or "N/A"
        # Feed transient failures back to the proxy so a dead endpoint drops
        # out of rotation (F27).
        if (self._proxy_manager is not None
                and enr.failure_reason in ("HTTP_BLOCKED", "TIMEOUT")):
            try:
                self._proxy_manager.report_failure(
                    self._proxy_manager.httpx_proxy())
            except Exception:
                pass
        # Honor the enrichment toggles (F25): a disabled stage leaves its
        # columns at the producer default instead of being overwritten.
        if self.cfg.enrichment.emails:
            rec["emails"] = ",".join(enr.emails) if enr.emails else "N/A"
            rec["email_count"] = len(enr.emails)
        rec["decision_maker_name"] = enr.decision_maker_name or "N/A"
        rec["decision_maker_title"] = enr.decision_maker_title or "N/A"

        # Social links (merge website-discovered with any Maps-discovered).
        if self.cfg.enrichment.social:
            merged_social = detect_social(
                [rec.get(c) for c in _SOCIAL_COLS if rec.get(c) not in (None, "N/A", "")])
            for c in _SOCIAL_COLS:
                if merged_social.get(c) and merged_social[c] != "N/A":
                    rec[c] = merged_social[c]
                elif enr.social.get(c) and enr.social[c] != "N/A":
                    rec[c] = enr.social[c]

        # Tech columns (string values from the tech detector).
        if self.cfg.enrichment.tech_detect:
            for c in _TECH_COLS:
                rec[c] = enr.tech.get(c, "N/A")
            # tag_manager (string) + gtm (YES/NO) both exist; map each correctly.
            rec["tag_manager"] = enr.tech.get("tag_manager", "N/A")
            rec["tech_stack"] = enr.tech.get("tech_stack", "N/A") or "N/A"

        # Boolean tech columns from the signal detector (YES/NO).
        for c in _BOOL_TECH_COLS:
            val = enr.signals.get(c, "NO")
            rec[c] = "yes" if val == "YES" else "NO"

        # Business signal columns (YES/NO).
        for c in _SIGNAL_COLS:
            rec[c] = enr.signals.get(c, "NO")

        # Config-driven custom signal columns (signals.custom) — YES/NO from
        # the website content, exported under the user-chosen column names.
        for c in self.custom_signal_columns:
            rec[c] = enr.signals.get(c, "NO")

        # MX / SMTP verification for the first extracted email (native).
        # `mx_enabled` / `smtp_enabled` were removed from the export (schema
        # section 4): `mx_status`=Not Checked already conveys "disabled".
        if enr.emails and (self.cfg.enrichment.mx_verify or self.cfg.enrichment.smtp_verify):
            ver = self.enricher.verify_email(enr.emails[0])
            rec["mx_status"] = ver["mx_status"]
            rec["mx_reason"] = ver["mx_reason"]
            rec["smtp_status"] = ver["smtp_status"]
            rec["smtp_reason"] = ver["smtp_reason"]

    def _analyze(self, rec: dict) -> None:
        reviews = [r for r in (rec.get("_reviews") or []) if r]
        # Honor reviews.min_len / max_len (F25).
        reviews = filter_reviews(
            reviews,
            min_len=self.cfg.reviews.min_len,
            max_len=self.cfg.reviews.max_len,
        )
        if not reviews and rec.get("top_review") not in (None, "N/A", ""):
            reviews = [rec["top_review"]]
        a = analyze(reviews, rating=rec.get("rating"),
                    review_count=rec.get("review_count"),
                    business_name=rec.get("business_name"),
                    category=rec.get("category"))
        rec["sentiment_score"] = a["sentiment_score"]
        rec["review_keywords"] = a["review_keywords"]
        rec["lead_score"] = a["lead_score"]
        rec["pitch_hook"] = a["pitch_hook"]
        if a.get("top_review"):
            rec["top_review"] = a["top_review"]

    def _apply_llm_hook(self, rec: dict) -> None:
        """Replace the pitch hook with an LLM-generated one when active.

        Falls back to the existing rule-based hook automatically.
        """
        hook = self.llm_hook.generate(rec)
        if hook:
            rec["pitch_hook"] = hook

    def _finalize(self) -> None:
        # Write the XLSX + summary while the checkpoint is still open, then
        # tear everything down through the shared idempotent close().
        try:
            write_xlsx(self.out_dir / "leads.xlsx", list(self.output_columns),
                       self.checkpoint.iter_committed_rows())
        except Exception as e:  # noqa: BLE001
            log.warning("xlsx write skipped: %s", e)
        if self.cfg.summary.enabled:
            self._write_run_summary()
        self.close()

    def _write_run_summary(self) -> None:
        """Build the per-campaign summary.json (metrics + leads)."""
        if self._monitor is not None:
            metrics = self._monitor.stop()
            self._monitor = None
        else:
            metrics = {"method": "not-sampled", "cpu_max_usage_percent": 0.0,
                       "ram_consumed_mb": 0.0, "ram_final_mb": 0.0}
        host = host_details()
        doc = {
            "campaign_id": self.cfg.job.client_name,
            "generated_at": datetime.now().astimezone().isoformat(),
            "cpu_max_usage_percent": metrics["cpu_max_usage_percent"],
            "ram_consumed_mb": metrics["ram_consumed_mb"],
            "ram_final_mb": metrics["ram_final_mb"],
            "execution_time_seconds": round(
                time.monotonic() - self._started_mono, 2),
            "counters": dict(self.counters),
            "throughput": self.runtime_stats(),
            "campaign_details": {
                "client_name": self.cfg.job.client_name,
                "queries": list(self.cfg.queries),
                "output_dir": str(self.out_dir),
                "output_files": (sorted(p.name for p in self.out_dir.iterdir())
                                 if self.out_dir.exists() else []),
                "concurrency": {
                    "website_workers": self.cfg.concurrency.website_workers,
                    "playwright_workers": self.cfg.concurrency.playwright_workers,
                },
                "maps": {"zoom": self.cfg.maps.zoom, "hl": self.cfg.maps.hl,
                          "gl": self.cfg.maps.gl,
                          "headless": self.cfg.maps.headless},
                "reviews": {"enabled": self.cfg.reviews.enabled,
                             "per_business": self.cfg.reviews.per_business},
                "filters": self.cfg.filters.model_dump(),
                "custom_signals": {
                    name: dict(spec)
                    for name, spec in self._custom_signal_specs.items()
                },
            },
            "servers": [{
                "name": host.get("name", "localhost"),
                "details": {
                    "platform": host.get("platform"),
                    "python_version": host.get("python_version"),
                    "cpu_count_logical": host.get("cpu_count_logical"),
                    "total_ram_mb": host.get("total_ram_mb"),
                    "cpu_max_usage_percent": metrics["cpu_max_usage_percent"],
                    "ram_peak_mb": metrics["ram_consumed_mb"],
                    "metrics_method": metrics["method"],
                },
            }],
            "leads": self._summary_leads,
        }
        write_summary(self.out_dir / "summary.json", doc)

    def close(self) -> None:
        """Idempotent teardown: CSV → enricher → checkpoint → collector.

        Called from ``_finalize`` AND from ``main``'s ``finally`` so a run that
        raises still releases every resource (F28).
        """
        if self.csv is not None:
            try:
                self.csv.close()
            except Exception as e:
                log.debug("csv close: %s", e)
        if getattr(self, "enricher", None) is not None:
            try:
                self.enricher.close()
            except Exception as e:
                log.debug("enricher close: %s", e)
        if getattr(self, "checkpoint", None) is not None:
            try:
                self.checkpoint.close()
            except Exception as e:
                log.debug("checkpoint close: %s", e)
        if self.collector is not None:
            try:
                self.collector.close()
            except Exception:
                pass
