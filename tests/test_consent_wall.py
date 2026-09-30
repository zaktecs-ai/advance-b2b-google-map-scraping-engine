"""Consent-wall battle-hardening regression tests (incident 2026-09-29).

Covers the production incident where Google served the consent wall on the
detail panel mid-run:
- wall detection (URL + title text)
- wall NEVER extracted/saved as a business (ConsentWallError path)
- handle_consent_wall dismisses (Reject-all preferred) + verifies
- sticky consent cookies captured once -> injected into every new context
- quality gate rejects consent-contaminated records
- worker recovery: dismiss -> retry once -> recycle on persistence
"""
from __future__ import annotations

from scraper.maps import collector as C
from scraper.maps.collector import (
    ConsentWallError,
    handle_consent_wall,
    is_consent_wall_text,
    is_consent_wall_url,
)
from scraper.validation.quality import passes_quality, quality_issues


# -- pure detection helpers ----------------------------------------------------

def test_is_consent_wall_text_titles():
    assert is_consent_wall_text("Before you continue to Google")
    assert is_consent_wall_text("  before you continue  ")
    assert is_consent_wall_text("Consent Required")
    # Real business names never match
    assert not is_consent_wall_text("Nick's Plumbing & Air Conditioning")
    assert not is_consent_wall_text("Before Sunset Caf\u00e9")  # partial word
    assert not is_consent_wall_text("")
    assert not is_consent_wall_text(None)


def test_is_consent_wall_url():
    assert is_consent_wall_url(
        "https://consent.google.com/sg?continue=https://maps")
    assert is_consent_wall_url("https://CONSENT.Google.xx/x")
    assert not is_consent_wall_url(
        "https://www.google.com/maps/place/X/data=!1s0x1")
    assert not is_consent_wall_url("")
    assert not is_consent_wall_url(None)


# -- wall NEVER becomes data (ConsentWallError + quality gate) ------------------

def test_consent_wall_error_raises_not_yields():
    err = ConsentWallError("https://maps/place/x")
    assert "consent wall" in str(err).lower()


def test_quality_gate_rejects_consent_wall_record():
    # The exact incident record: wall title as business name.
    rec = {
        "business_name": "Before you continue to Google",
        "google_maps_url": "https://www.google.com/maps/place/X",
        "rating": "N/A",
    }
    issues = quality_issues(rec)
    assert "consent_wall_contamination" in issues
    assert not passes_quality(rec)


def test_quality_gate_rejects_consent_url_record():
    rec = {
        "business_name": "Some Business",
        "google_maps_url": "https://consent.google.com/sg?continue=x",
        "rating": "N/A",
    }
    assert "consent_wall_url" in quality_issues(rec)
    assert not passes_quality(rec)


def test_quality_gate_accepts_normal_record():
    rec = {
        "business_name": "Nick's Plumbing & Air",
        "google_maps_url": "https://www.google.com/maps/place/N",
        "rating": "4.7",
    }
    assert passes_quality(rec)


# -- handle_consent_wall dismissal ----------------------------------------------

class _FakeBtn:
    def __init__(self, visible=True):
        self._visible = visible
        self.clicked = 0

    def count(self):
        return 1

    def is_visible(self):
        return self._visible

    def click(self, timeout=None):
        self.clicked += 1
        return True


class _FakeLocator:
    def __init__(self, buttons):
        self._buttons = buttons

    @property
    def first(self):
        return self._buttons[0] if self._buttons else _FakeBtn(visible=False)


class _FakeConsentPage:
    """A page showing the consent wall with Reject-all + Accept-all buttons."""

    def __init__(self):
        buttons = [
            ("button:has-text(\"Reject all\")", _FakeBtn()),
            ("button[aria-label*=\"Reject all\"]", _FakeBtn()),
            ("button:has-text(\"Accept all\")", _FakeBtn()),
        ]
        self._selectors = dict(buttons)
        self.clicked_selector = None
        self.url = "https://consent.google.com/sg?continue=https://maps"
        self._content = (
            "<html><h1>Before you continue to Google</h1>"
            "We use cookies and data ... Accept all ... Reject all"
            "</html>"
        )

    def content(self):
        return self._content

    def locator(self, sel):
        if sel not in self._selectors:
            return _FakeLocator([])
        btn = self._selectors[sel]
        if btn.clicked == 0 and self.clicked_selector is None:
            self.clicked_selector = sel
        return _FakeLocator([btn])

    def wait_for_url(self, pattern, timeout=None):
        self.url = "https://www.google.com/maps/place/X"
        return True


def test_handle_consent_wall_dismisses_reject_all_first():
    page = _FakeConsentPage()
    assert handle_consent_wall(page) is True
    # Reject-all was the button clicked (preferred over Accept-all).
    assert page.clicked_selector is not None
    assert "Reject all" in page.clicked_selector


def test_handle_consent_wall_no_wall_returns_false():
    class _Clean:
        def content(self):
            return "<html><h1>maps</h1></html>"

        @property
        def url(self):
            return "https://www.google.com/maps"

    assert handle_consent_wall(_Clean()) is False


def test_consent_button_selectors_include_reject_all():
    src = open(C.__file__, encoding="utf-8").read()
    assert 'button:has-text("Reject all")' in src
    assert 'button[aria-label*="Reject all"]' in src


# -- sticky consent cookies (BrowserManager) -----------------------------------

def test_capture_and_inject_consent_cookies():
    from scraper.browser.browser_manager import BrowserManager

    bm = BrowserManager(restart_after_queries=0, headless=True)

    class _Ctx:
        def __init__(self):
            self.injected = []

        def cookies(self):
            return [
                {"name": "SOCS", "value": "yes", "domain": ".google.com",
                 "path": "/", "expires": 1790000000, "httpOnly": False,
                 "secure": True, "sameSite": "Lax"},
                {"name": "CONSENT", "value": "PENDING+987",
                 "domain": ".google.com", "path": "/",
                 "expires": 1790000000, "httpOnly": False,
                 "secure": True, "sameSite": "Lax"},
                {"name": "SIDCC", "value": "unrelated-session",
                 "domain": ".google.com", "path": "/",
                 "expires": 1790000000, "secure": True},
            ]

        def add_cookies(self, cookies):
            self.injected.extend(cookies)

    ctx = _Ctx()
    bm.capture_consent_cookies(ctx)
    # Only consent-relevant cookies kept; SIDCC (session) dropped.
    with bm._lock:
        names = sorted(c["name"] for c in bm._consent_cookies)
    assert names == ["CONSENT", "SOCS"]

    # Injection into a NEW context gets exactly those cookies.
    ctx2 = _Ctx()
    bm.inject_consent_cookies(ctx2)
    injected_names = sorted(c["name"] for c in ctx2.injected)
    assert injected_names == ["CONSENT", "SOCS"]


def test_capture_consent_cookies_none_ctx_is_noop():
    from scraper.browser.browser_manager import BrowserManager
    bm = BrowserManager(restart_after_queries=0, headless=True)
    bm.capture_consent_cookies(None)  # must not raise
    assert bm._consent_cookies == []


# -- worker recovery path (collector) ------------------------------------------

def test_extract_with_consent_recovery_retry_then_success():
    col = C.MapsCollector.__new__(C.MapsCollector)  # no __init__
    col._consent_retries = 2  # dismiss+retry budget (config: maps.consent_retries)
    calls = {"n": 0}
    captured = []

    def _open(page, place_url, position=0, total=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConsentWallError(place_url)
        return {"business_name": "Real Business", "rating": "4.8",
                "_position": position, "_total": total}

    col._open_and_extract = _open

    class _Page:
        url = "https://www.google.com/maps/place/X"

    class _BM:
        def capture_consent_cookies(self, ctx):
            captured.append("captured")

    col._bm = _BM()

    orig = C.handle_consent_wall
    C.handle_consent_wall = lambda page: True
    try:
        out = C.MapsCollector._extract_with_consent_recovery(
            col, _Page(), "https://maps/place/X", 1, 5)
    finally:
        C.handle_consent_wall = orig
    assert out is not None
    assert out["business_name"] == "Real Business"
    assert calls["n"] == 2
    assert captured == ["captured"]


def test_extract_with_consent_recovery_persistent_wall_returns_none():
    col = C.MapsCollector.__new__(C.MapsCollector)
    col._consent_retries = 2  # both rounds hit the wall -> None -> recycle

    def _open(page, place_url, position=0, total=0):
        raise ConsentWallError(place_url)

    col._open_and_extract = _open

    class _BM:
        def capture_consent_cookies(self, ctx):
            pass

    col._bm = _BM()

    class _Page:
        url = "https://www.google.com/maps/place/X"

    orig = C.handle_consent_wall
    C.handle_consent_wall = lambda page: True
    try:
        out = C.MapsCollector._extract_with_consent_recovery(
            col, _Page(), "https://maps/place/X", 1, 5)
    finally:
        C.handle_consent_wall = orig
    # Persistent wall: None -> caller recycles the browser, NEVER saves junk.
    assert out is None


def test_open_and_extract_checks_consent_url_before_reading():
    # The readiness-wait guard: consent URL raises ConsentWallError.
    src = open(C.__file__, encoding="utf-8").read()
    assert "is_consent_wall_url(page.url" in src
    assert "raise ConsentWallError(place_url)" in src


# -- the exact terminal incident shape -----------------------------------------

def test_incident_junk_name_is_detected_everywhere():
    # The exact string saved 53 times in the owner's production run.
    junk = "Before you continue to Google"
    assert is_consent_wall_text(junk)
    assert "consent_wall_contamination" in quality_issues(
        {"business_name": junk, "google_maps_url": "https://maps/x",
         "rating": "N/A"})
    assert is_consent_wall_url(
        "https://consent.google.com/sg?continue=https%3A%2F%2Fwww.google.com"
        "%2Fmaps%2Fplace%2FSouthwest%2BHeating%2B26%2BAir%2BConditioning")


# -- coherence-retry (goto) path: wall title must never repair the name -------

def test_coherence_retry_goto_detects_wall_title():
    # The incident path: name mismatch -> goto -> the goto landed on the
    # consent wall -> the wall title was read as the "repaired" name.
    # Verify the guard exists in the source: the retry checks the wall
    # after the goto, before accepting a name.
    src = open(C.__file__, encoding="utf-8").read()
    assert "retry_name = _first_text(page, NAME_SELECTORS) or \"\"" in src
    assert "if is_consent_wall_text(retry_name):" in src
    # And the goto path dismisses the wall first.
    assert "consent wall dismissed after coherence-retry" in src


def test_worker_recycles_browser_on_persistent_wall():
    # Verify the parallel worker has the recycle branch (source contract).
    src = open(C.__file__, encoding="utf-8").read()
    assert "recycling worker browser" in src
    assert "_extract_with_consent_recovery(" in src


def test_serial_loop_uses_consent_recovery():
    src = open(C.__file__, encoding="utf-8").read()
    # The serial per-listing loop calls the recovery wrapper too.
    assert src.count("_extract_with_consent_recovery(") >= 3  # worker + serial + method def call sites
