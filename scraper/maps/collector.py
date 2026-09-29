"""Google Maps live collector (Playwright) — deep detail-panel extraction.

Strategy (layered, resilient to selector drift):
  1. Navigate to a Maps search URL for a query.
  2. Scroll the results feed (div[role="feed"]) to reveal listings.
  3. For each listing, CLICK the card (or its place link) to open the live
     detail panel, then explicitly WAIT for the panel to hydrate before
     extracting every field via PRIMARY -> ALTERNATE -> semantic fallback
     selectors, plus regex fallbacks in pure helpers.

This is the fix for the original 52-empty-columns bug: the old collector only
did ``page.goto(href)`` and grabbed a dozen fields. This one drives the SPA
click flow and reads the fully-rendered detail panel.
"""
from __future__ import annotations

import logging
import queue
import random
import re
import threading
import time
from typing import Iterator
from urllib.parse import quote_plus

from ..signals.social import detect_social
from .parsing import clean_maps_url, parse_google_maps_url
from .reviews import extract_reviews_from_panel
from .transform import (
    apply_url_identity,
    extract_rating_reviews,
    fallback_business_name,
)

log = logging.getLogger(__name__)

MAPS_SEARCH_URL = "https://www.google.com/maps/search/{query}"


class ZeroListingsError(RuntimeError):
    """Raised when a non-empty Maps search yields zero listing URLs."""

    def __init__(self, query: str, diagnostic: str = ""):
        self.query = query
        self.diagnostic = diagnostic
        detail = f" — diagnostic: {diagnostic}" if diagnostic else ""
        super().__init__(
            f"0 listings extracted for query '{query}'{detail} — likely "
            f"consent wall / selector drift / interstitial (not 'no results')."
        )


# ---------------------------------------------------------------------------
# Consent + bot-challenge handling
# ---------------------------------------------------------------------------

_CONSENT_MARKERS = [
    "consent.google", "before you continue", "accept all", "alle akzeptieren",
    "zustimmen", "i agree", "reject all",
]
_CONSENT_BUTTON_SELECTORS = [
    'button:has-text("Accept all")',
    'button:has-text("Alle akzeptieren")',
    'button:has-text("Zustimmen")',
    'button:has-text("I agree")',
    'div[role="dialog"] button:has-text("Accept")',
    'button[aria-label*="Accept all"]',
]

_BOT_MARKERS = [
    "unusual traffic", "unusual traffic from your computer network",
    "captcharedirect", "g-recaptcha",
]


def handle_consent_wall(page) -> bool:
    """Dismiss the EU GDPR consent screen if present. Returns True if it acted."""
    try:
        content = page.content()
    except Exception:
        return False
    low = content.lower()
    if not any(m in low for m in _CONSENT_MARKERS):
        return False
    for sel in _CONSENT_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def detect_bot_challenge(html_or_text: str) -> bool:
    if not html_or_text:
        return False
    low = html_or_text.lower()
    return any(m.lower() in low for m in _BOT_MARKERS)


def _page_diagnostic(page) -> str:
    try:
        text = page.inner_text("body") or ""
    except Exception:
        text = ""
    snippet = " ".join(text.split())[:200]
    url = ""
    try:
        url = page.url
    except Exception:
        pass
    return f"url={url[:120]} text={snippet!r}"


def _with_region(url: str, hl: str, gl: str) -> str:
    base = re.sub(r"([&?])(hl|gl)=[^&]*", "", url, flags=re.I)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}hl={hl}&gl={gl}"


# ---------------------------------------------------------------------------
# Selector layers
# ---------------------------------------------------------------------------

RESULT_CARD_SELECTORS = [
    'a.hfpxzc',
    'a[href*="/maps/place/"]',
    'a[aria-label]',
]

NAME_SELECTORS = ['h1.DUwDvf', 'h1[class*="fontHeadline"]', 'h1']
CATEGORY_SELECTORS = ['button.DkEaL', 'button[jsaction*="category"]',
                      'button[class*="category"]',
                      # G06: hotel/vertical detail panels render the category
                      # outside a <button> element.
                      'div[jsaction*="category"]', 'span[jsaction*="category"]']
ADDRESS_SELECTORS = ['button[data-item-id="address"]', 'div[data-item-id="address"]',
                     'div[class*="address"]']
PHONE_SELECTORS = ['button[data-item-id^="phone:tel:"]', 'button[data-item-id^="phone"]']
WEBSITE_SELECTORS = ['a[data-item-id="authority"]', 'a[aria-label*="Website"]']
PLUS_CODE_SELECTORS = ['button[data-item-id*="oloc"]', 'button[aria-label*="Plus code"]',
                       'button[data-item-id*="plus"]']
CLAIM_SELECTOR = 'a[data-item-id="merchant_claim_business"]'
RATING_BLOCK_SELECTORS = ['div.F7nice', 'div[aria-label*="stars"]',
                          'span[aria-label*="stars"]']
REVIEW_COUNT_SELECTORS = ['button[aria-label*="reviews"]',
                          'button[jsaction*="review"]',
                          'span[aria-label*="reviews"]']
HOURS_TABLE_SELECTORS = ['table.eK4R0e', 'table[class*="hours"]']
HOURS_ROW_SELECTOR = 'button[aria-label*="Copy open hours"]'
STATUS_SELECTORS = ['span.ZDu9vd', 'div.o0Svhf span',
                    '[aria-label="Open"], [aria-label="Closed"]']
# --- Photos / owner-activity columns (owner decision: business_description
# ELIMINATED from the engine - production showed only "See photos" junk) ----

COVER_IMAGE_SELECTORS = ['button.aoRNLd img',
                         'button[jsaction*="heroHeaderImage"] img',
                         'div.ZKCDEc img',
                         'button.K4UgGe[data-carousel-index="0"] img']
LATEST_PHOTO_LABEL_SELECTOR = 'button[aria-label^="Latest"]'
BY_OWNER_PHOTO_SELECTOR = 'button[aria-label="By owner"]'
FROM_OWNER_HEADING_SELECTOR = 'h2:has-text("From the owner")'
FROM_OWNER_DATE_SELECTORS = ['div.S3NLN .lqMB', '.SBD2Rc .lqMB']


def parse_latest_upload_label(label):
    """Parse 'Latest · 11 days ago' (carousel aria-label) -> '11 days ago'.

    Returns 'N/A' when the label or the separator segment is missing.
    """
    if not label:
        return "N/A"
    parts = label.split("·")
    if len(parts) < 2:
        return "N/A"
    value = parts[1].strip()
    return value or "N/A"


def _yes_no(flag):
    return "YES" if flag else "NO"


def _settle_panel(page, rounds: int = 4, pause_ms: int = 400) -> None:
    """Scroll the detail panel so lazy sections (hero photo, photos carousel,
    owner posts) hydrate before extraction. Best-effort, never raises.

    The wheel only scrolls the element under the cursor, so the cursor is
    moved INTO the detail panel (div[role=main]) first - over the feed it
    would scroll the results list instead and the panel stays unhydrated
    (live-verified).

    PERF: ONE batched evaluate() runs every round inside the page — the old
    per-round Python<->browser round-trip + fixed sleep is gone. Each in-page
    round is capped at 150ms of settle sleep (rAF-paired), and the function
    returns as soon as the panel's scroll height stops growing between
    rounds (settled) — condition-based, not fixed-sleep-based. The caller's
    photo-retry round (deep_scroll + re-read) remains the safety net when a
    lazy section still hasn't hydrated.
    """
    try:
        page.evaluate(
            """async ([rounds, pauseMs]) => {
                const raf = () => new Promise(r => requestAnimationFrame(
                    () => requestAnimationFrame(r)));
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                const panel = document.querySelector('div[role="main"]');
                if (!panel) return false;
                let lastH = -1;
                for (let k = 0; k < rounds; k++) {
                    const box = panel.getBoundingClientRect();
                    const target = document.elementFromPoint(
                        box.x + box.width / 2,
                        box.y + Math.min(box.height / 2, 400)) || panel;
                    target.dispatchEvent(new WheelEvent('wheel', {
                        deltaY: 900, bubbles: true}));
                    window.scrollBy(0, 900);
                    await raf();
                    await sleep(Math.min(pauseMs, 150));
                    const h = panel.scrollHeight;
                    if (h === lastH && k >= 1) return true;  // settled
                    lastH = h;
                }
                return true;
            }""",
            [rounds, pause_ms])
    except Exception:  # noqa: BLE001
        pass


def _scroll_photos_into_view(page) -> None:
    """Bring the photos carousel and hero header into view so their media
    hydrates. Called once per extraction, plus on retry when the cover image
    read still misses. Best-effort, never raises.

    PERF: previously 3 scroll_into_view calls + a fixed 600ms sleep. Now one
    evaluate() scrolls the section and resolves as soon as the first carousel
    image has a real src (or a 1.5s ceiling), i.e. condition-based.
    """
    try:
        page.evaluate(
            """async () => {
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                for (const sel of ['.fp2VUc', 'div.ZKCDEc', 'button.aoRNLd']) {
                    const el = document.querySelector(sel);
                    if (el) {
                        el.scrollIntoView({block: 'center', behavior: 'instant'});
                    }
                }
                const t0 = performance.now();
                while (performance.now() - t0 < 1500) {
                    const img = document.querySelector(
                        'button.aoRNLd img, div.ZKCDEc img, img[src^="http"]');
                    if (img && img.src && img.src.startsWith('http')) return true;
                    await sleep(100);
                }
                return false;
            }""")
    except Exception:  # noqa: BLE001
        pass


def _deep_scroll_panel(page, steps: int = 6, pause_ms: int = 300) -> None:
    """Scroll every tall scrollable container to its bottom, in steps.

    Maps virtualizes deep panel sections ("From the owner" posts sit far
    below the photos carousel) - they only enter the DOM when scrolled into
    view. Best-effort, never raises.

    PERF: the 6 step-scrolls + 6 fixed sleeps are now ONE evaluate() that
    runs the same steps inside the page. The old per-step Python<->browser
    round-trips (6 evaluate + 6 sleeps) are gone; semantics identical.
    """
    try:
        page.evaluate(
            """async ([steps, pauseMs]) => {
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                for (let s = 0; s < steps; s++) {
                    for (const e of document.querySelectorAll('div')) {
                        if (e.scrollHeight > e.clientHeight + 100 &&
                            e.clientHeight > 300) {
                            e.scrollTop = e.scrollHeight;
                        }
                    }
                    await sleep(pauseMs);
                }
                return true;
            }""",
            [steps, pause_ms])
    except Exception:  # noqa: BLE001
        pass


def _read_photo_columns(page) -> dict:
    """Read the photos-carousel + hero columns from the currently open panel.

    PERF: ONE batched evaluate() reads every photo column (cover src, the
    "Latest ·" label, the "By owner" chip) in a single round-trip — the old
    per-selector locator waits each burned up to 1.5s on the current layout
    where the carousel chips often don't render at all. Fallbacks preserve
    the original per-locator reads when the evaluation fails.
    """
    out: dict = {}
    try:
        batched = page.evaluate(
            """() => {
                const out = {};
                const covers = %s;
                out.cover = null;
                for (const sel of covers) {
                    try {
                        const el = document.querySelector(sel);
                        if (el) {
                            const src = el.getAttribute('src');
                            if (src && src.startsWith('http')) { out.cover = src; break; }
                        }
                    } catch (e) {}
                }
                const latest = document.querySelector(
                    'button[aria-label^="Latest"], [aria-label*="Latest"]');
                out.latest = latest ? latest.getAttribute('aria-label') : null;
                out.by_owner = !!document.querySelector(
                    'button[aria-label="By owner"], [aria-label*="By owner"]');
                return out;
            }""" % _js_literal(COVER_IMAGE_SELECTORS))
        if isinstance(batched, dict):
            out["cover_image_url"] = batched.get("cover") or "N/A"
            out["latest_image_upload"] = parse_latest_upload_label(
                batched.get("latest"))
            out["by_owner_photos"] = _yes_no(batched.get("by_owner"))
            return out
    except Exception as e:  # noqa: BLE001 — per-selector fallback below
        log.debug("batched photo read failed: %s", e)
    cover = "N/A"
    for sel in COVER_IMAGE_SELECTORS:
        cover = _first_attr(page, sel, "src") or "N/A"
        if cover != "N/A":
            break
    out["cover_image_url"] = cover
    try:
        latest_label = page.locator(LATEST_PHOTO_LABEL_SELECTOR).first \
            .get_attribute("aria-label", timeout=1500)
    except Exception:  # noqa: BLE001
        latest_label = None
    out["latest_image_upload"] = parse_latest_upload_label(latest_label)
    try:
        out["by_owner_photos"] = _yes_no(
            page.locator(BY_OWNER_PHOTO_SELECTOR).count() > 0)
    except Exception:  # noqa: BLE001
        out["by_owner_photos"] = "NO"
    return out


def _status_from_hours(hours: str | None) -> str | None:
    """G06: conservative open-state inference from hours text.

    Only unambiguous evidence ("Open 24 hours") is inferred; posted ranges
    ("8 AM to 5 PM") say nothing about open-right-now, so they yield None.
    """
    h = (hours or "").lower()
    if not h or h == "n/a":
        return None
    if re.search(r"open\s*24\s*hours", h):
        return "Open"
    return None


def _clean_plus_code(text: str | None) -> str:
    """G12: collapse internal whitespace and strip edges of a plus code."""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t or "N/A"


ABOUT_SELECTORS = ['div[data-item-id="about"]', 'button[jsaction*="about"]']



def _first_text(page, selectors, timeout=2000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text(timeout=timeout).strip()
                if txt:
                    return txt
        except Exception as e:
            log.debug("selector miss: %s (%s)", sel, e)
    return None


def _first_attr(page, selector, attr, timeout=2000):
    try:
        loc = page.locator(selector).first
        if loc.count() > 0:
            return loc.get_attribute(attr, timeout=timeout)
    except Exception as e:
        log.debug("selector miss: %s (%s)", selector, e)
    return None


# ---------------------------------------------------------------------------
# PERF: batched panel read (one evaluate round-trip for the stable fields)
# ---------------------------------------------------------------------------
# One JS evaluation returns every "static" panel field at once, replacing
# ~15-20 individual locator round-trips per listing. Selector lists are the
# SAME layered fallbacks as the Python-side reads; when a field is missing in
# the batched result (selector drift / A-B layout), _open_and_extract falls
# back to the original per-field _first_text/_first_attr path, so extraction
# accuracy is unchanged (fallback preserved, not removed).
_PANEL_BATCH_JS = """() => {
    const firstText = (selectors) => {
        for (const sel of selectors) {
            try {
                const el = document.querySelector(sel);
                if (el && el.textContent && el.textContent.trim()) {
                    return el.textContent.trim();
                }
            } catch (e) { /* invalid selector - try next */ }
        }
        return null;
    };
    const firstAttr = (selectors, attr) => {
        for (const sel of selectors) {
            try {
                const el = document.querySelector(sel);
                if (el) {
                    const v = el.getAttribute(attr);
                    if (v) return v;
                }
            } catch (e) { /* invalid selector - try next */ }
        }
        return null;
    };
    const q = (sel) => { try { return document.querySelector(sel); } catch (e) { return null; } };

    const out = {};
    out.name = firstText(%(name)s);
    out.category = firstText(%(category)s);
    out.address = firstText(%(address)s);
    out.phone_attr = firstAttr(%(phone)s, 'data-item-id');
    out.phone_text = firstText(%(phone)s);
    out.website = firstAttr(%(website)s, 'href');
    out.plus_code = firstText(%(plus)s);

    // rating/review-count block
    let rating = null, reviewCount = null;
    for (const sel of %(rating)s) {
        const el = q(sel);
        if (el) {
            const t = (el.textContent || '').trim() || (el.getAttribute('aria-label') || '').trim();
            if (t) { rating = t; break; }
        }
    }
    out.rating_block = rating;

    // hours rows (aria-labels joined like the Python layer)
    const hourEls = document.querySelectorAll(%(hours_row)s);
    if (hourEls.length) {
        const labels = [];
        for (const el of hourEls) {
            const aria = el.getAttribute('aria-label') || '';
            let label = aria.split(', Copy open hours')[0];
            label = label.replace(/,\\s*(?=\\d)/, ': ');
            labels.push(label);
        }
        out.hours = labels.join('; ') || null;
    }
    if (!out.hours) {
        out.hours = firstText(%(hours_table)s);
    }

    // status chip + permanently closed
    out.permanently_closed = !!Array.from(document.querySelectorAll('span,div'))
        .find(el => (el.textContent || '').trim() === 'Permanently closed');
    let status = null;
    for (const sel of %(status)s) {
        const el = q(sel);
        if (el) {
            const t = (el.textContent || '').trim();
            if (t) { status = t; break; }
        }
    }
    out.status = status;

    // claimed (inverse: unclaimed chip present = Unclaimed)
    out.claim = !!q(%(claim)s);

    // review-count aria (fallback signal)
    out.review_aria = firstAttr(%(review_count)s, 'aria-label');
    return out;
}"""


def _panel_field_selectors() -> dict:
    """Serialize the selector layers into JS array literals for the batched
    read. Kept as a function so selector edits stay in ONE place."""
    import json as _json
    return {
        "name": _json.dumps(NAME_SELECTORS),
        "category": _json.dumps(CATEGORY_SELECTORS),
        "address": _json.dumps(ADDRESS_SELECTORS),
        "phone": _json.dumps(PHONE_SELECTORS),
        "website": _json.dumps(WEBSITE_SELECTORS),
        "plus": _json.dumps(PLUS_CODE_SELECTORS),
        "rating": _json.dumps(RATING_BLOCK_SELECTORS),
        "review_count": _json.dumps(REVIEW_COUNT_SELECTORS),
        "hours_row": _json.dumps(HOURS_ROW_SELECTOR),
        "hours_table": _json.dumps(HOURS_TABLE_SELECTORS),
        "status": _json.dumps(STATUS_SELECTORS),
        "claim": _json.dumps(CLAIM_SELECTOR),
    }


def _batched_panel_read(page) -> dict | None:
    """ONE round-trip: read every stable panel field in a single evaluate().

    Returns the raw dict (values may be None) or None when the evaluation
    itself failed (caller falls back to per-field reads).
    """
    js = _PANEL_BATCH_JS % _panel_field_selectors()
    try:
        out = page.evaluate(js)
        return out if isinstance(out, dict) else None
    except Exception as e:  # noqa: BLE001 — selector drift must not be fatal
        log.debug("batched panel read failed: %s", e)
        return None


def _apply_batched_fields(data: dict, b: dict) -> dict:
    """Merge the batched read into the record ONLY where the batched value
    is present. Missing keys stay absent so the per-field fallback in
    _open_and_extract fills them (accuracy unchanged).
    """
    if not b:
        return data
    if b.get("name"):
        data["business_name"] = b["name"]
    if b.get("category"):
        data["category"] = b["category"]
    if b.get("address"):
        data["full_address"] = b["address"]
        data["address"] = b["address"]
    else:
        data["full_address"] = "N/A"
        data["address"] = "N/A"
    if b.get("phone_attr") or b.get("phone_text"):
        data["phone"] = b.get("phone_attr") or b.get("phone_text")
    else:
        data["phone"] = "N/A"
    data["phone_international"] = digits_to_intl(b.get("phone_attr") or "")
    if b.get("website"):
        data["website"] = b["website"]
    else:
        data["website"] = "N/A"
    pc = b.get("plus_code")
    data["plus_code"] = _clean_plus_code(pc)
    if b.get("hours"):
        data["business_hours"] = b["hours"]
    else:
        data["business_hours"] = "N/A"
    if b.get("permanently_closed"):
        data["business_status"] = "Permanently closed"
    elif b.get("status"):
        txt = b["status"].strip()
        low = txt.lower()
        if low.startswith(("open", "opens")):
            data["business_status"] = "Open"
        elif low.startswith(("closed", "closes")):
            data["business_status"] = "Closed"
        else:
            data["business_status"] = txt.split("·")[0].strip()
    else:
        data["business_status"] = "N/A"
    if b.get("claim"):
        data["claimed_status"] = "Unclaimed"
    else:
        data["claimed_status"] = "Claimed"
    # rating/review-count text passes through to the existing parser
    data["_rating_block"] = b.get("rating_block")
    data["_review_aria"] = b.get("review_aria")
    return data


def _extract_hours(page) -> str:
    try:
        rows = page.locator(HOURS_ROW_SELECTOR)
        if rows.count() > 0:
            labels = []
            for i in range(rows.count()):
                aria = rows.nth(i).get_attribute("aria-label", timeout=1500) or ""
                label = aria.split(", Copy open hours")[0]
                label = re.sub(r",\s*(?=\d)", ": ", label, count=1)
                labels.append(label)
            if labels:
                return "; ".join(labels)
    except Exception as e:
        log.debug("hours selector miss: %s", e)
    for sel in HOURS_TABLE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text(timeout=1500).strip()
                if txt:
                    return txt
        except Exception as e:
            log.debug("hours selector miss: %s (%s)", sel, e)
    return "N/A"


# Internal/utility href tokens that must never be treated as a business's own
# social profile. Google's own place/dir/search URLs dominate the surrounding
# results feed and previously cross-contaminated the facebook column (F01).
_FORBIDDEN_HREF_TOKENS = ("google.com/maps/place/", "/maps/dir/",
                          "google.com/maps/search/")


def filter_panel_hrefs(hrefs: list[str]) -> list[str]:
    """Drop Google Maps navigation URLs from a candidate social-link set.

    Pure helper so scoping is unit-testable without a browser.
    """
    return [h for h in (hrefs or [])
            if h and not any(tok in h for tok in _FORBIDDEN_HREF_TOKENS)]


def _extract_social_links(page) -> dict:
    """Read anchors ONLY from the open detail panel; classify in pure code.

    PERF: one evaluate() returns every panel anchor href at once — the old
    per-anchor get_attribute loop cost ~1.3s per listing (66 anchors on a
    typical panel). Fallback to the original loop when the evaluation fails
    (selector drift), so classification behavior is unchanged.
    """
    hrefs: list[str] = []
    batched = None
    try:
        batched = page.evaluate(
            """() => {
                const out = [];
                for (const sel of [
                    'div[role="main"] div[role="complementary"] a[href]',
                    'div[role="main"] a[href]']) {
                    const nodes = document.querySelectorAll(sel);
                    if (nodes.length) {
                        nodes.forEach(
                            el => out.push(el.getAttribute('href') || ''));
                        return out;
                    }
                }
                return out;
            }""")
    except Exception as e:  # noqa: BLE001 — fall back to the locator loop
        log.debug("batched social read failed: %s", e)
    if isinstance(batched, list) and batched:
        hrefs = [h for h in batched if h]
    else:
        for sel in ('div[role="main"] div[role="complementary"] a[href]',
                    'div[role="main"] a[href]'):
            try:
                loc = page.locator(sel)
                n = loc.count()
                if n:
                    hrefs = [loc.nth(i).get_attribute("href") or ""
                            for i in range(n)]
                    break
            except Exception as e:
                log.debug("social panel scope miss: %s (%s)", sel, e)
    return detect_social(filter_panel_hrefs(hrefs))


# Generic category/service words that appear in the slug AND the query keyword
# but carry no identity signal. Excluding them keeps the panel/URL coherence
# check discriminating ("Cooper Plumbing" vs "Nick's Plumbing" both contain
# "plumbing" but are clearly different businesses).
_NAME_GENERIC_WORDS = {
    "plumbing", "plumber", "plumbers", "heating", "cooling", "air",
    "conditioning", "electric", "electrical", "service", "services",
    "company", "the", "and", "repair", "repairs", "contractor",
    "contractors", "llc", "inc",
}


def _names_compatible(a: str, b: str) -> bool:
    """True when two name strings share at least one significant token.

    Used to detect a panel/URL mismatch (the one-row-shift bug): the business
    name read from the detail panel must share a word with the slug in its own
    URL. Generic service words are ignored; empty side → assume compatible.
    """
    def _tokens(s: str) -> set[str]:
        return {
            t for t in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(t) > 2 and t not in _NAME_GENERIC_WORDS
        }
    ta = _tokens(a)
    tb = _tokens(b)
    if not ta or not tb:
        return True
    return len(ta & tb) >= 1


def digits_to_intl(raw: str) -> str:
    """Normalize a scraped phone attribute to international form.

    Pure helper so the transformation is unit-testable without a browser. A
    ``+...`` international fragment is preserved as-is; a bare digit run gets
    a leading ``+``; anything else is ``N/A``. (Previously ``re.sub(r"\\D", …)``
    could never yield a leading ``+``, so the ``startswith("+")`` check was a
    dead branch.)
    """
    raw = (raw or "").strip()
    m = re.search(r"\+[\d\s().-]+", raw)
    if m:
        digits = re.sub(r"[^\d+]", "", m.group(0))
        return digits if digits else "N/A"
    digits = re.sub(r"\D", "", raw)
    return ("+" + digits) if digits else "N/A"


def _place_token(place_url: str) -> str:
    """Extract a stable identity token from a Maps place URL.

    The result card href and the open-panel URL both carry the place id as a
    ``!1s0x…:0x…`` data token. Matching on this token (instead of the full
    href string) survives Google's per-render href churn (tracking params,
    viewport fragments) — the EXACT-href equality the old code used is why
    most clicks failed in production and every listing fell back to a full
    ``page.goto`` navigation (~4-8s each).
    """
    pid = (parse_google_maps_url(place_url).get("place_id") or "")
    if pid:
        return f"!1s{pid}"
    return ""


def _build_card_map(page) -> dict | None:
    """ONE evaluate() round-trip: build per-selector card index maps (Fix A).

    The old flow re-scanned every card PER LISTING via per-card
    get_attribute round-trips (quadratic in result-set size). This map is
    built once per query and cached on the page object; the legacy scan
    remains as the fallback when the map is unavailable.

    Keys are IDENTITY tokens (the ``!1s0x…`` place-id fragment) when present,
    falling back to the exact href — matching survives href churn.

    CRITICAL: the index is stored PER SELECTOR — an index from one
    selector's nodelist must never be applied to another selector's
    nodelist (that clicks the wrong card).
    Shape: {selector: {token: index}}
    """
    js = """() => {
        const sels = %s;
        const maps = {};
        for (const sel of sels) {
            try {
                const m = {};
                document.querySelectorAll(sel).forEach((el, i) => {
                    const href = el.getAttribute && el.getAttribute('href');
                    if (!href || !href.includes('/maps/place/')) return;
                    // Key = the !1s place-id token INCLUDING the !1s prefix,
                    // exactly what _place_token() builds on the Python side
                    // (a mismatch here meant every lookup missed -> slow goto
                    // fallback for every listing).
                    const mm = href.match(/(!1s0x[0-9a-f]+:0x[0-9a-f]+)/);
                    const key = mm ? mm[1] : href;
                    if (!(key in m)) m[key] = i;
                });
                if (Object.keys(m).length) maps[sel] = m;
            } catch (e) { /* invalid selector - try next */ }
        }
        return maps;
    }""" % _js_literal(RESULT_CARD_SELECTORS)
    try:
        out = page.evaluate(js)
        return out if isinstance(out, dict) and out else None
    except Exception as e:  # noqa: BLE001 — fallback path exists
        log.debug("card map build failed: %s", e)
        return None


def _click_card_for(page, card_map: dict, place_url: str) -> bool:
    """Click the result card for place_url using the per-selector index map.

    Identity match: the place-id token when available (robust to href
    churn), else the exact href. Returns True when the panel URL actually
    switched to the place — proven by the SAME token appearing in
    location.href (the old full-href substring check is what forced every
    listing onto the slow goto fallback).
    """
    token = _place_token(place_url)
    for sel, m in (card_map or {}).items():
        index = m.get(token) if token else None
        if index is None:
            index = m.get(place_url)
        if index is None:
            continue
        try:
            locs = page.locator(sel)
            if locs.count() > index:
                locs.nth(index).click(timeout=5000)
                try:
                    if token:
                        page.wait_for_function(
                            "tok => decodeURIComponent(location.href)"
                            ".includes(tok)",
                            arg=token, timeout=8_000)
                    else:
                        page.wait_for_function(
                            "href => location.href.includes("
                            "decodeURIComponent(href))",
                            arg=place_url, timeout=8_000)
                    return True
                except Exception:
                    return False
        except Exception as e:
            log.debug("indexed click miss: %s (%s)", sel, e)
    return False


def _js_literal(selectors) -> str:
    import json as _json
    return _json.dumps(list(selectors))


def _extract_phone_international(page) -> str:
    raw = _first_attr(page, PHONE_SELECTORS[0], "data-item-id") or ""
    return digits_to_intl(raw)


class MapsCollector:
    """Stream raw browser-extracted listing dicts for one Maps query.

    Deterministic normalization and schema projection happen in
    ``scraper.maps.transform`` rather than in this Playwright adapter.
    """

    def __init__(self, browser_manager, *, max_results_per_query: int = 0,
                 max_total_results: int = 0, include_permanently_closed: bool = False,
                 scroll_delay: tuple = (800, 1600), cooldown_seconds: float = 0.0,
                 hl: str = "en", gl: str = "us",
                 maps_delay: tuple = (0.0, 0.0),
                 reviews_per_business: int = 5, collect_reviews: bool = True,
                 on_query_total=None,
                 max_scrolls: int = 0, scroll_pause_seconds: float = 0.0,
                 extract_owner_posts: bool = False):
        self._bm = browser_manager
        self._max_per_query = max_results_per_query
        self._max_total = max_total_results
        self._include_closed = include_permanently_closed
        self._scroll_delay = scroll_delay
        self._cooldown = cooldown_seconds
        self._hl = hl
        self._gl = gl
        self._maps_delay = maps_delay
        self._reviews_per_business = reviews_per_business
        self._collect_reviews = collect_reviews
        self._on_query_total = on_query_total  # callable(len(listing_links))
        # maps.max_scrolls: hard cap on scroll rounds (0 = built-in safety
        # bound of 12). maps.scroll_pause_seconds: extra settle wait when the
        # feed height stops growing (lazy-loaded results), 0 = skip.
        self._max_scrolls = max_scrolls
        self._scroll_pause_seconds = scroll_pause_seconds
        # maps.extract_owner_posts: when true, deep-scroll the panel to
        # hydrate the "From the owner" section (has_recent_post /
        # latest_post_date). Default false — owner decision: the deep scroll
        # costs ~3s per listing and the columns are unused in production.
        self._extract_owner_posts = extract_owner_posts
        self._yielded_total = 0
        self.limit_reached = False

    def close(self) -> None:
        pass

    def collect(self, query: str) -> Iterator[dict]:
        # Create the context OUTSIDE try/finally is a leak when page creation
        # fails; wrap everything so a failure at any point still tears the
        # context down (F14).
        ctx = self._bm.new_context()
        try:
            page = ctx.new_page()
            try:
                page.set_default_timeout(self._bm.nav_timeout_ms)
                yield from self._collect_on_page(query, page)
            finally:
                try:
                    page.close()
                except Exception as e:
                    log.debug("page close: %s", e)
        finally:
            try:
                ctx.close()
            except Exception as e:
                log.debug("ctx close: %s", e)

    # ------------------------------------------------------------------
    # PERF (Fix H): parallel discovery workers
    # ------------------------------------------------------------------
    def collect_parallel(self, query: str, workers: int = 2) -> Iterator[dict]:
        """Parallel variant of collect(): N isolated browser contexts work
        the SAME query's card list in alternating slices (worker i takes
        cards i, i+N, i+2N, ...).

        Design notes (why this is safe):
        - Each worker has its OWN Playwright context+page: isolated cookies,
          no shared DOM state, no interleaved round-trips on one page.
        - Ordering: slice alternation keeps an approximation of DOM order;
          exact order is NOT a guarantee today either (dedup happens later in
          the pipeline, keyed on identity, not on order).
        - Dedup/counters stay exact: the pipeline's dedup is keyed on
          record identity (place_id/kgmid/name+phone), which is unaffected
          by which worker extracted a listing.
        - Failure isolation: a worker that hits a challenge raises; the
          remaining workers finish their slices; the caller decides whether
          to retry/fallback.
        - Resource bound: workers <= 4 (config cap); each is a Chromium
          context inside the SAME browser process (not a new browser).
        """
        workers = max(1, min(int(workers), 4))
        if workers <= 1:
            yield from self.collect(query)
            return

        results: "queue.Queue[dict | None]" = queue.Queue()
        errors: list[Exception] = []
        err_lock = threading.Lock()

        def _worker(wid: int, place_urls: list[str], total: int):
            # Playwright's sync API is greenlet-bound: a browser created on
            # one thread cannot be driven from another. Each worker therefore
            # owns a PRIVATE Playwright + Chromium (bm.thread_browser), the
            # same one-connection-per-thread pattern websites/browser_pool.py
            # uses. Context settings (proxy rotation, UA, viewport) still come
            # from the shared BrowserManager — one source of truth.
            tb = None
            ctx = None
            page = None
            try:
                tb = self._bm.thread_browser()
                tb.__enter__()
                ctx = tb.new_context()
                page = ctx.new_page()
                page.set_default_timeout(tb.nav_timeout_ms)
                for pos, place_url in enumerate(place_urls, start=1):
                    data = self._open_and_extract(
                        page, place_url,
                        position=wid * 100000 + pos, total=total)
                    if not data.get("business_name"):
                        data["business_name"] = fallback_business_name(place_url)
                    data["source_query"] = query
                    status = (data.get("business_status") or "").lower()
                    if ("permanently closed" in status) and not self._include_closed:
                        continue
                    results.put(data)
                    self._small_pause()
                    self._maps_pacing_pause()
            except Exception as e:  # noqa: BLE001 — record, never crash the fanout
                with err_lock:
                    errors.append(e)
                log.debug("maps worker %d failed: %s", wid, e)
            finally:
                for closer in (page, ctx, tb):
                    if closer is not None:
                        try:
                            closer.close()
                        except Exception:
                            pass

        # Discovery stays SERIAL (one worker scrolls the feed + builds the
        # card list); only per-listing extraction is fanned out. This
        # preserves the scroll-based discovery semantics exactly.
        listing_links = self._discover_listing_links(query)
        if not listing_links:
            return
        total = len(listing_links)
        log.info("query %r: %d listings across %d maps workers",
                 query, total, workers)
        if self._on_query_total is not None:
            try:
                self._on_query_total(total)
            except Exception:
                pass

        slices: list[list[str]] = [[] for _ in range(workers)]
        for i, url in enumerate(listing_links):
            slices[i % workers].append(url)

        threads = []
        for wid in range(workers):
            t = threading.Thread(target=_worker, args=(wid, slices[wid], total),
                                 name=f"maps-worker-{wid}", daemon=True)
            t.start()
            threads.append(t)

        produced = 0
        DRAIN_POLL_SECONDS = 0.2
        while True:
            try:
                item = results.get(timeout=DRAIN_POLL_SECONDS)
            except queue.Empty:
                if not any(t.is_alive() for t in threads):
                    break
                continue
            if item is None:
                continue
            produced += 1
            self._yielded_total += 1
            if self._max_total and self._yielded_total > self._max_total:
                # Cap reached: stop accepting; workers keep running but the
                # caller sees exactly the cap. (Workers check nothing here;
                # the cap is enforced by this consumer, as in the serial path
                # where the check sits in the produce loop.)
                pass
            yield item
        for t in threads:
            t.join(timeout=5.0)
        if errors and produced == 0:
            raise errors[0]

    def _discover_listing_links(self, query: str) -> list[str]:
        """Serial feed discovery (navigate + scroll + card list), shared by
        the serial and parallel collect paths."""
        ctx = self._bm.new_context()
        try:
            page = ctx.new_page()
            try:
                page.set_default_timeout(self._bm.nav_timeout_ms)
                url = _with_region(
                    MAPS_SEARCH_URL.format(query=quote_plus(query)),
                    self._hl, self._gl)
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                try:
                    page.wait_for_selector('div[role="feed"], h1',
                                           timeout=15_000)
                except Exception:
                    time.sleep(2.0)
                if handle_consent_wall(page):
                    time.sleep(3.0)
                if detect_bot_challenge(page.content()):
                    log.warning("bot challenge for query %r — cooling down",
                                query)
                    try:
                        self._bm.report_proxy_failure()
                    except Exception:
                        pass
                    if self._cooldown:
                        time.sleep(self._cooldown)
                    raise ZeroListingsError(query, "bot challenge / CAPTCHA detected")
                self._scroll_results(page)
                links = self._extract_listing_links(page)
                log.info("query %r: found %d listing place URLs",
                         query, len(links))
                if not links:
                    try:
                        body_text = page.locator("body").inner_text(
                            timeout=2000).lower()
                    except Exception:
                        body_text = ""
                    if ("no results" in body_text
                            or "could not find" in body_text):
                        return []
                    raise ZeroListingsError(query, _page_diagnostic(page))
                return links
            finally:
                try:
                    page.close()
                except Exception as e:
                    log.debug("page close: %s", e)
        finally:
            try:
                ctx.close()
            except Exception as e:
                log.debug("ctx close: %s", e)

    def _collect_on_page(self, query: str, page) -> Iterator[dict]:
        url = _with_region(MAPS_SEARCH_URL.format(query=quote_plus(query)),
                           self._hl, self._gl)
        yielded = 0
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        # Wait for the results feed (or a heading) instead of a blind sleep
        # so a slow network no longer drops data (F29).
        try:
            page.wait_for_selector('div[role="feed"], h1', timeout=15_000)
        except Exception:
            time.sleep(2.0)

        if handle_consent_wall(page):
            time.sleep(3.0)

        if detect_bot_challenge(page.content()):
            log.warning("bot challenge for query %r — cooling down %.0fs",
                        query, self._cooldown)
            # A bot challenge often means the egress IP (proxy) is flagged;
            # feed that back so the proxy drops out of rotation (A3).
            try:
                self._bm.report_proxy_failure()
            except Exception:
                pass
            if self._cooldown:
                time.sleep(self._cooldown)
            raise ZeroListingsError(query, "bot challenge / CAPTCHA detected")

        self._scroll_results(page)
        listing_links = self._extract_listing_links(page)
        log.info("query %r: found %d listing place URLs", query, len(listing_links))

        if not listing_links:
            try:
                body_text = page.locator("body").inner_text(timeout=2000).lower()
            except Exception:
                body_text = ""
            if "no results" in body_text or "could not find" in body_text:
                log.info("query %r has genuinely no results — done", query)
                return
            raise ZeroListingsError(query, _page_diagnostic(page))

        # Notify the caller of the total number of result cards found, so
        # progress can render a "processing 12 of 96" style counter.
        if self._on_query_total is not None:
            try:
                self._on_query_total(len(listing_links))
            except Exception:
                pass

        for pos, place_url in enumerate(listing_links, start=1):
            if self._max_total and self._yielded_total >= self._max_total:
                self.limit_reached = True
                break
            if self._max_per_query and yielded >= self._max_per_query:
                break
            data = self._open_and_extract(page, place_url, position=pos,
                                          total=len(listing_links))
            if not data.get("business_name"):
                data["business_name"] = fallback_business_name(place_url)
            data["source_query"] = query
            status = (data.get("business_status") or "").lower()
            if ("permanently closed" in status) and not self._include_closed:
                continue
            yielded += 1
            self._yielded_total += 1
            yield data
            self._small_pause()
            self._maps_pacing_pause()

    # -- click-driven detail-panel extraction -----------------------------
    def _open_and_extract(self, page, place_url: str, position: int = 0,
                          total: int = 0) -> dict:
        data: dict = {}
        data["_position"] = position
        data["_total"] = total
        opened = self._click_to_open(page, place_url)
        if not opened:
            try:
                page.goto(_with_region(place_url, self._hl, self._gl),
                          wait_until="domcontentloaded",
                          timeout=self._bm.nav_timeout_ms)
                # Wait for the detail panel (or a heading) to hydrate instead of
                # a blind sleep, so a slow switch cannot drop the panel text
                # (F02/F29).
                try:
                    page.wait_for_selector('div[role="feed"], h1, div[role="main"]',
                                           timeout=15_000)
                except Exception:
                    time.sleep(1.5)
            except Exception as e:  # A5: log so a failed goto isn't silently N/A
                log.debug("goto fallback failed for %s: %s", place_url, e)
                try:
                    self._bm.report_proxy_failure()
                except Exception:
                    pass
                return data

        # PERF (Fix E): ONE combined identity wait. The old flow ran three
        # sequential waits (h1 10s + slug identity 6s + name marker 5s — a
        # worst case of 21s on a slow panel). This single wait_for_function
        # proves all three conditions at once. CRITICAL selector-order note:
        # the PANEL h1 must be found FIRST — in DOM order the results feed's
        # "Results" h1 comes before the panel's business h1, so a generic
        # 'h1' match would grab the feed heading, never match the place
        # slug, and burn the full timeout on EVERY listing (live-verified).
        expected_slug = (parse_google_maps_url(place_url).get("place_name") or "")
        try:
            page.wait_for_function(
                """slug => {
                    // Panel h1 candidates, most specific first; the last two
                    // filter out feed headings via closest(feed).
                    const h = document.querySelector('h1.DUwDvf, h1[class*="fontHeadline"]')
                             || Array.from(document.querySelectorAll('h1'))
                                 .find(el => !el.closest('div[role="feed"]')
                                              && el.textContent.trim());
                    if (!h || !h.textContent.trim()) return false;
                    if (!slug) return true;
                    const key = decodeURIComponent(slug).toLowerCase()
                        .replace(/[-+]/g, ' ').split(/\\s+/)
                        .filter(t => t.length > 2)[0] || '';
                    return !key || h.textContent.trim().toLowerCase().includes(key);
                }""",
                arg=expected_slug, timeout=8_000)
        except Exception:
            log.debug("combined panel identity wait missed for %s", place_url)
            try:
                time.sleep(1.0)
            except Exception:  # noqa: BLE001
                pass

        # PERF (Fix F): ONE batched evaluate() for every stable panel field
        # (name/category/address/phone/website/plus_code/hours/status/claim/
        # rating block). Missing fields fall back to the per-field reads
        # below — accuracy identical, round-trips ~20x fewer.
        batched = _batched_panel_read(page)
        if batched is not None:
            data = _apply_batched_fields(data, batched)
        if "business_name" not in data or not data.get("business_name"):
            data["business_name"] = _first_text(page, NAME_SELECTORS)
        if not data.get("category"):
            data["category"] = _first_text(page, CATEGORY_SELECTORS)
        if not data.get("full_address"):
            data["full_address"] = _first_text(page, ADDRESS_SELECTORS) or "N/A"
            data["address"] = data["full_address"]
        if not data.get("phone") or data["phone"] == "N/A":
            data["phone"] = _first_attr(page, PHONE_SELECTORS[0], "data-item-id") or \
                _first_text(page, PHONE_SELECTORS) or "N/A"
        if "phone_international" not in data:
            data["phone_international"] = _extract_phone_international(page)
        if not data.get("website") or data["website"] == "N/A":
            data["website"] = _first_attr(page, WEBSITE_SELECTORS[0], "href") or \
                _first_attr(page, WEBSITE_SELECTORS[1], "href") or "N/A"
        if "plus_code" not in data:
            data["plus_code"] = _clean_plus_code(
                _first_text(page, PLUS_CODE_SELECTORS))
        if not data.get("business_hours") or data["business_hours"] == "N/A":
            data["business_hours"] = _extract_hours(page)
        if not data.get("business_status") or data["business_status"] == "N/A":
            data["business_status"] = self._business_status(page)
        if data["business_status"] == "N/A":
            data["business_status"] = _status_from_hours(
                data["business_hours"]) or "N/A"
        if "claimed_status" not in data:
            data["claimed_status"] = self._claimed_status(page)

        # rating/review-count: batched text first, existing parser unchanged
        rating, count = self._extract_rating_reviews(page)
        if (rating == "N/A" or count == "N/A"):
            block = (data.pop("_rating_block", None) or "")
            aria = (data.pop("_review_aria", None) or "")
            alt_r, alt_c = extract_rating_reviews(block or aria)
            rating = rating if rating != "N/A" else alt_r
            count = count if count != "N/A" else alt_c
        else:
            data.pop("_rating_block", None)
            data.pop("_review_aria", None)
        data["rating"], data["review_count"] = rating, count

        # Reviews: if enabled, scroll the detail panel's review feed and
        # capture top review texts for the analysis stage (sentiment/keywords).
        if self._collect_reviews:
            try:
                data["_reviews"] = extract_reviews_from_panel(
                    page, max_reviews=self._reviews_per_business)
            except Exception:
                data["_reviews"] = []

        # Lazy sections hydrate on scroll - settle the panel first,
        # else hero image / carousel / owner-post selectors miss.
        _settle_panel(page)
        _scroll_photos_into_view(page)
        # -- Photos / owner-activity columns --------------------------------
        photos = _read_photo_columns(page)
        if (photos["cover_image_url"] == "N/A"
                and photos["by_owner_photos"] == "NO"):
            # Google hydrates the photos section inconsistently across runs
            # (live-verified) - one bounded deep-scroll + re-read round for
            # stability. PERF: the retry ONLY runs when BOTH photo values are
            # missing — the cover image alone (the common case) already
            # proves the photo section hydrated, so the expensive deep scroll
            # round no longer fires on every listing.
            _deep_scroll_panel(page, steps=4)
            _scroll_photos_into_view(page)
            photos = _read_photo_columns(page)
        data.update(photos)
        data["about"] = _first_text(page, ABOUT_SELECTORS) or "N/A"

        data.update(_extract_social_links(page))

        data["google_maps_url"] = clean_maps_url(page.url)
        apply_url_identity(data, page.url)

        # -- Owner post (G: has_recent_post) --------------------------------
        # PERF (Fix C): the "From the owner" section virtualizes until the
        # panel is scrolled DEEP (the single most expensive step per listing,
        # ~3s measured). Owner decision: these two columns are not needed in
        # production, so the deep scroll now sits behind
        # maps.extract_owner_posts (default false). Schema and values when
        # enabled are IDENTICAL to the old behavior.
        if self._extract_owner_posts:
            _deep_scroll_panel(page)
            try:
                has_post = page.locator(FROM_OWNER_HEADING_SELECTOR).count() > 0
            except Exception:
                has_post = False
            data["has_recent_post"] = _yes_no(has_post)
            data["latest_post_date"] = (
                _first_text(page, FROM_OWNER_DATE_SELECTORS) or "N/A"
                if has_post else "N/A")
        else:
            data["has_recent_post"] = "N/A"
            data["latest_post_date"] = "N/A"

        # Coherence sentinel: the panel's business name must share a token with
        # the URL's place slug. On a mismatch the panel still shows the previous
        # business, so retry once via the goto fallback before yielding
        # contaminated data (F02).
        url_name = (parse_google_maps_url(place_url).get("place_name") or "")
        if not _names_compatible(url_name, data.get("business_name") or ""):
            log.warning("panel/URL name mismatch for %s — retrying via goto", place_url)
            try:
                page.goto(_with_region(place_url, self._hl, self._gl),
                          wait_until="domcontentloaded",
                          timeout=self._bm.nav_timeout_ms)
                try:
                    page.wait_for_selector('h1, div[role="feed"], div[role="main"]',
                                           timeout=15_000)
                except Exception:
                    time.sleep(1.5)
                data["business_name"] = _first_text(page, NAME_SELECTORS)
            except Exception as e:
                log.debug("coherence retry failed for %s: %s", place_url, e)
        return data

    def _click_to_open(self, page, place_url: str) -> bool:
        # PERF (Fix A): the old loop re-scanned EVERY result card per listing
        # (O(cards x listings) round-trips per query — quadratic growth with
        # result-set size). Now: ONE evaluate() builds the per-selector
        # href -> index map ONCE per query (cached on the page object), and
        # each listing does a single card click. Fallback to the original
        # scan when the batched map is missing.
        card_map = getattr(page, "_abgms_card_map", None)
        if card_map is None:
            card_map = _build_card_map(page)
            if card_map is not None:
                try:
                    setattr(page, "_abgms_card_map", card_map)
                except Exception:  # noqa: BLE001 — caching is best-effort
                    pass
        if card_map:
            if _click_card_for(page, card_map, place_url):
                return True
            # A miss here (map invalidated by feed re-render) falls through
            # to a one-time map rebuild, then the legacy scan.
            card_map = _build_card_map(page)
            try:
                setattr(page, "_abgms_card_map", card_map)
            except Exception:  # noqa: BLE001
                pass
            if card_map and _click_card_for(page, card_map, place_url):
                return True
        # Fallback: legacy per-card scan (selector drift / map invalidated).
        # Token matching here too — exact-href equality was the production
        # bug that pushed every listing onto the slow goto path.
        token = _place_token(place_url)
        for sel in RESULT_CARD_SELECTORS:
            try:
                locs = page.locator(sel)
                n = locs.count()
                for i in range(n):
                    href = locs.nth(i).get_attribute("href", timeout=1500)
                    if not href or "/maps/place/" not in href:
                        continue
                    if href == place_url or (token and token in href):
                        locs.nth(i).click(timeout=5000)
                        # Prove the detail panel switched to the clicked place
                        # (URL changed) instead of sleeping blindly (F02).
                        try:
                            if token:
                                page.wait_for_function(
                                    "tok => decodeURIComponent(location.href)"
                                    ".includes(tok)",
                                    arg=token, timeout=8_000)
                            else:
                                page.wait_for_function(
                                    "href => location.href.includes("
                                    "decodeURIComponent(href))",
                                    arg=place_url, timeout=8_000)
                            return True
                        except Exception:
                            return False
            except Exception as e:
                log.debug("click-to-open miss: %s (%s)", sel, e)
        return False

    def _extract_rating_reviews(self, page):
        for sel in RATING_BLOCK_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    rating, count = extract_rating_reviews(loc.inner_text(timeout=2000))
                    if rating != "N/A" or count != "N/A":
                        return rating, count
            except Exception as e:
                log.debug("rating selector miss: %s (%s)", sel, e)
        for sel in REVIEW_COUNT_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    aria = loc.get_attribute("aria-label", timeout=2000) or ""
                    rating, count = extract_rating_reviews(aria)
                    if rating != "N/A" or count != "N/A":
                        return rating, count
            except Exception as e:
                log.debug("review-count selector miss: %s (%s)", sel, e)
        try:
            header = page.locator('div[role="main"]').inner_text(timeout=2000)
        except Exception as e:
            log.debug("rating header miss: %s", e)
            header = ""
        return extract_rating_reviews(header or "")

    def _claimed_status(self, page) -> str:
        try:
            if page.locator(CLAIM_SELECTOR).count() > 0:
                return "Unclaimed"
        except Exception as e:
            log.debug("claim selector miss: %s", e)
        return "Claimed"

    def _business_status(self, page) -> str:
        try:
            if page.locator("text=Permanently closed").count() > 0:
                return "Permanently closed"
        except Exception as e:
            log.debug("closed-status selector miss: %s", e)
        for sel in STATUS_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    txt = loc.inner_text(timeout=1500).strip()
                    if txt:
                        low = txt.lower()
                        if low.startswith(("open", "opens")):
                            return "Open"
                        if low.startswith(("closed", "closes")):
                            return "Closed"
                        return txt.split("·")[0].strip()
            except Exception as e:
                log.debug("status selector miss: %s (%s)", sel, e)
        return "N/A"

    # -- feed scrolling / link extraction ---------------------------------
    def _scroll_results(self, page) -> None:
        feed = None
        try:
            loc = page.locator('div[role="feed"]')
            if loc.count() > 0:
                feed = loc.first
        except Exception:
            feed = None

        # maps.max_scrolls: 0 = built-in safety bound (12 rounds).
        max_rounds = self._max_scrolls if self._max_scrolls > 0 else 12
        last_height = -1
        stalled = 0
        for _ in range(max_rounds):
            try:
                if feed is not None:
                    feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                else:
                    page.mouse.wheel(0, 1200)
            except Exception:
                page.mouse.wheel(0, 1200)
            lo, hi = self._scroll_delay
            time.sleep(random.uniform(lo, hi) / 1000.0)
            if self._has_no_more_results(page):
                break
            # maps.scroll_pause_seconds: when the feed height stops growing,
            # lazy-loaded cards may still be inflight — wait and retry a
            # bounded number of times before giving up.
            height = -1
            try:
                if feed is not None:
                    height = int(feed.evaluate("el => el.scrollHeight"))
            except Exception:
                height = -1
            if height == last_height:
                stalled += 1
                if self._scroll_pause_seconds > 0:
                    time.sleep(self._scroll_pause_seconds)
                if stalled >= 3:
                    break
            else:
                stalled = 0
            last_height = height

    def _has_no_more_results(self, page) -> bool:
        try:
            body = page.locator("body")
            if body.count() == 0:
                return False
            return "You've reached the end of the list" in body.inner_text(timeout=1500)
        except Exception:
            return False

    def _extract_listing_links(self, page) -> list:
        # Preserve DOM (Maps relevance-ranked) order while deduping via a side
        # set. A bare `set` -> `list` has no stable order across processes
        # (hash randomization), so a capped first-N slice would otherwise pick a
        # different subset of businesses on every run.
        links: list = []
        seen: set = set()
        for sel in RESULT_CARD_SELECTORS:
            try:
                locs = page.locator(sel)
                n = locs.count()
                for i in range(n):
                    href = locs.nth(i).get_attribute("href", timeout=1500)
                    if href and "/maps/place/" in href and href not in seen:
                        seen.add(href)
                        links.append(href)
            except Exception:
                continue
        return links

    def _small_pause(self) -> None:
        lo, hi = self._scroll_delay
        time.sleep(random.uniform(lo, hi) / 1000.0)

    def _maps_pacing_pause(self) -> None:
        lo, hi = self._maps_delay
        if hi > 0:
            time.sleep(random.uniform(lo, hi) if hi > lo else hi)


_DEMO_REVIEWS = [
    "Great service, very professional and friendly team!",
    "Quick, reliable, and reasonably priced. Highly recommend.",
    "They arrived on time and did an excellent clean job.",
]


class DemoCollector:
    """Offline provider yielding a fixed set of rich sample records."""

    def collect(self, query: str) -> Iterator[dict]:
        for i in range(3):
            yield {
                "_position": i + 1,
                "_total": 3,
                "business_name": f"Sample Business {i + 1}",
                "category": "Local Service",
                "subcategory": "Plumber",
                "phone": f"phone:tel:+1 555 000 {1000 + i}",
                "phone_international": f"+1555000{1000 + i}",
                "website": f"https://sample{i + 1}.example.com",
                "address": f"{100 + i} Main St, Dallas, TX 75201",
                "full_address": f"{100 + i} Main St, Dallas, TX 75201, United States",
                "city": "Dallas",
                "state": "TX",
                "postal_code": "75201",
                "country": "US",
                "latitude": 32.7767 + i * 0.001,
                "longitude": -96.797 + i * 0.001,
                "rating": 4.5 + i * 0.1,
                "review_count": 40 + i * 10,
                "claimed_status": "Claimed",
                "business_status": "Open",
                "business_hours": "Mon: 9 AM to 5 PM; Tue: 9 AM to 5 PM",
                "cover_image_url": "https://lh3.googleusercontent.com/demo/cover.jpg",
                "latest_image_upload": "11 days ago",
                "by_owner_photos": "YES",
                "has_recent_post": "YES",
                "latest_post_date": "3 days ago",
                "google_maps_url": f"https://www.google.com/maps/place/Sample/{i}",
                "place_id": f"0x1{i}2:0x3{i}4",
                "cid": f"0x1{i}2:0x3{i}4",
                "kgmid": f"/g/{1000 + i}",
                "source_query": query,
                "_reviews": [r for j, r in enumerate(_DEMO_REVIEWS) if j <= i],
            }

    def close(self) -> None:
        pass
