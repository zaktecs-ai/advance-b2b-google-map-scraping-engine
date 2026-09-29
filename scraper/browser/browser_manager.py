"""Playwright browser lifecycle management.

Responsibilities:
  * Launch a browser lazily and reuse it across many pages/contexts (avoiding a
    fresh process per task).
  * Recycle the browser after a configurable number of queries, or when memory
    pressure is detected, keeping long VPS jobs healthy.
  * Route a VISIBLE browser to a specific X display (TightVNC) BEFORE Playwright
    starts, exporting both DISPLAY and XAUTHORITY into the inherited
    environment — so an operator can watch and manually solve a CAPTCHA.
  * Enforce per-operation timeouts and safe teardown so a hung page or crashed
    browser never freezes or leaks the whole job.

The manager is used by both the Maps collector and the Playwright website
fallback. It is not imported at module top anywhere, so the engine can run in
HTTP-only / test contexts without Playwright installed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

log = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self, restart_after_queries: int = 0, headless: bool = True,
                 proxy: dict | None = None, nav_timeout_ms: int = 30_000,
                 display: str | None = None, locale: str = "en-US",
                 user_agent: str | None = None, proxy_manager=None):
        self._restart_after_queries = restart_after_queries
        self._headless = headless
        self._proxy = proxy
        # A ProxyManager triggers per-context proxy resolution (A2): instead of
        # freezing one proxy at launch, every new context re-resolves so the
        # round-robin/random pool actually rotates across queries.
        self._proxy_manager = proxy_manager
        self._nav_timeout_ms = nav_timeout_ms
        self._display = display
        self._locale = locale
        self._user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
        self._pw = None
        self._browser = None
        self._queries_processed = 0
        self._lock = threading.Lock()
        # The proxy most recently handed to a context, for failure feedback (A3).
        self._active_proxy: str | None = None

    # ------------------------------------------------------------------
    def _ensure_browser(self):
        # Fast path: already launched (safe — a set reference).
        if self._browser is not None:
            return self._browser
        # Slow path guarded by the lock so two threads racing on first use
        # cannot both launch a Chromium process (double-launch / leak).
        with self._lock:
            if self._browser is not None:
                return self._browser
            # Route the visible browser to the VNC X display BEFORE starting
            # Playwright, and set XAUTHORITY too. Both are inherited via the
            # environment (NOT a `env=` launch kwarg, which would replace the
            # whole environment and drop XAUTHORITY — causing "Client is not
            # authorized to connect").
            if not self._headless and self._display:
                os.environ["DISPLAY"] = self._display
                xauth = os.path.join(os.path.expanduser("~"), ".Xauthority")
                if os.path.exists(xauth):
                    os.environ["XAUTHORITY"] = xauth
                log.info("visible browser -> display %s (XAUTHORITY=%s)",
                         self._display, os.environ.get("XAUTHORITY", ""))

            try:
                from playwright.sync_api import sync_playwright
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "Playwright is not installed. Run ./setup.sh or "
                    "`pip install playwright && playwright install chromium`."
                ) from e

            self._pw = sync_playwright().start()
            launch_kwargs = {"headless": self._headless}
            if self._proxy:
                launch_kwargs["proxy"] = self._proxy
            try:
                self._browser = self._pw.chromium.launch(
                    args=["--disable-blink-features=AutomationControlled"],
                    **launch_kwargs,
                )
            except Exception as e:  # pragma: no cover — env dependent
                msg = str(e).lower()
                if ("executable doesn't exist" in msg) or ("playwright install" in msg):
                    self._pw.stop()
                    self._pw = None
                    raise RuntimeError(
                        "Chromium binary is missing. Run "
                        "`python -m playwright install chromium` (or ./setup.sh)."
                    ) from e
                raise
            log.info("browser launched (headless=%s)", self._headless)
            return self._browser

    def new_context(self, proxy: dict | None = None,
                    geolocation: dict | None = None,
                    locale: str | None = None):
        browser = self._ensure_browser()
        # Per-context proxy resolution: prefer an explicit arg, then a fresh
        # rotation from the ProxyManager (A2), falling back to the frozen
        # launch-time proxy only when neither is present.
        ctx_proxy = proxy
        if ctx_proxy is None and self._proxy_manager is not None:
            ctx_proxy = self._proxy_manager.playwright_proxy()
        if ctx_proxy is None:
            ctx_proxy = self._proxy
        if ctx_proxy:
            self._active_proxy = ctx_proxy.get("server") if isinstance(ctx_proxy, dict) else str(ctx_proxy)
        kwargs = {
            "viewport": {"width": 1366, "height": 900},
            "locale": locale or self._locale,
            "user_agent": self._user_agent,
        }
        if ctx_proxy:
            kwargs["proxy"] = ctx_proxy
        if geolocation:
            kwargs["geolocation"] = geolocation
            kwargs["permissions"] = ["geolocation"]
        return browser.new_context(**kwargs)

    def report_proxy_failure(self) -> None:
        """Report the active proxy as failed so it drops out of rotation (A3)."""
        if self._proxy_manager is not None and self._active_proxy:
            self._proxy_manager.report_failure(self._active_proxy)

    @property
    def nav_timeout_ms(self) -> int:
        return self._nav_timeout_ms

    def mark_query(self) -> None:
        self._queries_processed += 1

    def should_restart(self) -> bool:
        if self._restart_after_queries and \
                self._queries_processed >= self._restart_after_queries:
            return True
        return False

    def recycle(self, force: bool = False) -> bool:
        """Close the browser; it is re-launched lazily on next use."""
        with self._lock:
            if not force and not self.should_restart():
                return False
            self._close()
            self._queries_processed = 0
            return True

    def _close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as e:  # pragma: no cover
                log.debug("browser close error: %s", e)
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception as e:  # pragma: no cover
                log.debug("playwright stop error: %s", e)
            self._pw = None
        # Drain Playwright's asyncio loop so its pending callbacks don't raise
        # "Exception in callback SyncBase._sync" after the greenlet is gone.
        self._drain_playwright_loop()

    def _drain_playwright_loop(self) -> None:
        try:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                # `get_event_loop()` raises RuntimeError when there is no
                # current event loop. The previously-referenced
                # `asyncio.CoroutineNotAllowedError` attribute does not exist
                # in any Python version and would itself raise AttributeError
                # during teardown (F09).
                loop = None
            if loop is not None and not loop.is_running() and not loop.is_closed():
                loop.run_until_complete(asyncio.sleep(0))
        except Exception:  # pragma: no cover — teardown hygiene, never fatal
            pass

    def close(self) -> None:
        self._close()

    @property
    def browser(self):
        return self._ensure_browser()

    # ------------------------------------------------------------------
    # Per-thread browsers (parallel Maps workers)
    # ------------------------------------------------------------------
    def thread_browser(self):
        """A PRIVATE Playwright connection + Chromium for the CALLING thread.

        Playwright's sync API is greenlet-bound: a browser created on one
        thread cannot be driven from another (``greenlet.error: Cannot
        switch to a different thread``). The parallel Maps workers therefore
        each need their own connection — the same one-connection-per-thread
        pattern ``websites/browser_pool.py`` uses. Use as a context manager::

            with bm.thread_browser() as tb:
                ctx = tb.new_context()
                page = ctx.new_page()

        Launch settings (headless, proxy, anti-automation flag, VNC display
        env) mirror ``_ensure_browser`` so behavior matches the main browser.
        """
        return _ThreadBrowser(self)


class _ThreadBrowser:
    """One private Playwright + Chromium owned by a single worker thread.

    All launch/context settings are delegated to the parent BrowserManager so
    there is exactly ONE source of truth for proxy rotation, user agent, and
    viewport (no drift between the main and worker browsers).
    """

    def __init__(self, bm: "BrowserManager"):
        self._bm = bm
        self._pw = None
        self._browser = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        bm = self._bm
        # Mirror _ensure_browser's display routing for visible browsers.
        if not bm._headless and bm._display:
            os.environ["DISPLAY"] = bm._display
            xauth = os.path.join(os.path.expanduser("~"), ".Xauthority")
            if os.path.exists(xauth):
                os.environ["XAUTHORITY"] = xauth
        self._pw = sync_playwright().start()
        launch_kwargs = {"headless": bm._headless}
        if bm._proxy:
            launch_kwargs["proxy"] = bm._proxy
        self._browser = self._pw.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"],
            **launch_kwargs)
        log.info("thread browser launched (headless=%s)", bm._headless)
        return self

    def new_context(self, proxy: dict | None = None,
                    geolocation: dict | None = None,
                    locale: str | None = None):
        """Same per-context resolution as BrowserManager.new_context."""
        bm = self._bm
        ctx_proxy = proxy
        if ctx_proxy is None and bm._proxy_manager is not None:
            ctx_proxy = bm._proxy_manager.playwright_proxy()
        if ctx_proxy is None:
            ctx_proxy = bm._proxy
        kwargs = {
            "viewport": {"width": 1366, "height": 900},
            "locale": locale or bm._locale,
            "user_agent": bm._user_agent,
        }
        if ctx_proxy:
            kwargs["proxy"] = ctx_proxy
        if geolocation:
            kwargs["geolocation"] = geolocation
            kwargs["permissions"] = ["geolocation"]
        return self._browser.new_context(**kwargs)

    @property
    def nav_timeout_ms(self) -> int:
        return self._bm.nav_timeout_ms

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as e:  # pragma: no cover
                log.debug("thread browser close error: %s", e)
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception as e:  # pragma: no cover
                log.debug("thread playwright stop error: %s", e)
            self._pw = None

    def __exit__(self, *exc):
        self.close()
        return False
