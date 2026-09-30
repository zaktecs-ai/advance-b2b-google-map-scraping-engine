"""CLI entrypoint: run a scrape (headless or headed via VNC), or demo mode.

Usage:
    python -m scraper.main                    # live scrape (config.yaml)
    python -m scraper.main --demo             # offline sample records
    python -m scraper.main --config other.yaml

The engine is driven entirely by ``config.yaml`` + ``.env`` (a pure CLI /
background execution model). The ``maps.headless: false`` + ``vnc.display``
settings route the visible browser to a TightVNC display for manual CAPTCHA
solving — exactly as on a VNC-enabled VPS.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .maps.collector import DemoCollector, MapsCollector
from .pipeline import Pipeline
from .utils.logging_utils import setup_logging


def _build_collector(config, demo: bool, browser_manager, progress=None):
    if demo:
        return DemoCollector()
    m = config.maps
    return MapsCollector(
        browser_manager,
        max_results_per_query=m.max_results_per_query or config.job.max_results_per_query,
        max_total_results=m.max_total_results or config.job.max_total_results,
        include_permanently_closed=m.include_permanently_closed,
        scroll_delay=(m.scroll_delay_min_ms, m.scroll_delay_max_ms),
        cooldown_seconds=config.delays.cooldown_seconds,
        hl=m.hl, gl=m.gl,
        # runtime.pacing scales the between-query Maps delay (1.0 = as
        # configured; >1 slower/cautious, <1 faster/riskier).
        maps_delay=(config.delays.maps_min_seconds * config.runtime.pacing,
                    config.delays.maps_max_seconds * config.runtime.pacing),
        reviews_per_business=config.reviews.per_business,
        collect_reviews=config.reviews.enabled,
        on_query_total=(progress.set_query_total if progress is not None else None),
        max_scrolls=m.max_scrolls,
        scroll_pause_seconds=m.scroll_pause_seconds,
        extract_owner_posts=m.extract_owner_posts,
        consent_retries=m.consent_retries,
    )


def _check_chromium() -> str | None:
    """Preflight: fail fast with ONE clear message when the Playwright
    Chromium binary is missing, instead of failing every query one by one
    (the Maps collector raises per query otherwise). Returns None when OK.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ("Playwright is not installed. Run ./setup.sh or "
                "`pip install playwright && playwright install chromium`.")
    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
        if exe and not Path(exe).exists():
            return ("Chromium browser binary is missing. Run "
                    "`python -m playwright install chromium` (or ./setup.sh).")
    except Exception as e:  # noqa: BLE001 — driver/env problems surface here
        return f"Playwright driver problem: {e}"
    return None


def run(config_path: str, demo: bool) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"[config error] {e}", file=sys.stderr)
        return 1

    # Log file lives next to the outputs; full debug detail goes there, while
    # the console stays clean (progress only).
    out_dir = Path(config.job.output_dir) / config.job.client_name
    setup_logging(config.logging.level, log_file=str(out_dir / "run.log"))

    # Clean console progress reporter (structured, with business names).
    from .utils.progress import ProgressConsole
    progress = ProgressConsole(total_queries=len(config.queries),
                               client_name=config.job.client_name,
                               quiet=config.logging.quiet if hasattr(config, "logging") else False)

    browser_manager = None
    collector = None
    proxy_manager = None
    if not demo:
        # Preflight: one clear startup error beats 20 per-query failures.
        chromium_problem = _check_chromium()
        if chromium_problem:
            print(f"[setup error] {chromium_problem}", file=sys.stderr)
            return 1
        # Build the Playwright-backed collector with a shared BrowserManager.
        # BrowserManager is imported lazily so HTTP-only tests don't need it.
        from .browser import BrowserManager, ProxyManager
        proxy_cfg = ProxyManager().config
        proxy_cfg.enabled = config.proxy.enabled
        proxy_cfg.http = config.proxy.http
        proxy_cfg.https = config.proxy.https
        proxy_cfg.pool = list(config.proxy.pool or config.proxy.urls)
        proxy_cfg.rotation = config.proxy.rotation
        proxy_manager = ProxyManager(proxy_cfg)

        m = config.maps
        browser_manager = BrowserManager(
            restart_after_queries=m.browser_restart_after_queries,
            headless=m.headless,
            proxy_manager=proxy_manager,
            nav_timeout_ms=m.page_navigation_timeout_ms,
            display=config.vnc.display if not m.headless else None,
        )
        collector = _build_collector(config, demo, browser_manager, progress)

    if collector is None:
        collector = DemoCollector()

    pipeline = Pipeline(config, collector=collector, browser_manager=browser_manager,
                        progress=progress, proxy_manager=proxy_manager)
    try:
        pipeline.run()
    except KeyboardInterrupt:
        print("\ninterrupted — checkpoint state is durable; rerun to resume.",
              file=sys.stderr)
        return 130
    finally:
        # Release pipeline resources (csv/enricher/checkpoint/collector) even on
        # an exception, then the browser manager (F28).
        try:
            pipeline.close()
        except Exception:
            pass
        if browser_manager is not None:
            try:
                browser_manager.close()
            except Exception:
                pass

    print(f"\nOutput: {pipeline.out_dir}/leads.xlsx  (full log: {pipeline.out_dir}/run.log)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="abgms", description="Advance B2B GMS")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--demo", action="store_true",
                        help="Offline demo mode (sample records, no browser)")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__
        print(__version__)
        return 0

    return run(args.config, args.demo)


if __name__ == "__main__":
    raise SystemExit(main())
