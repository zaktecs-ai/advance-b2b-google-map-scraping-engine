"""Review extraction: RPC-first (listugcposts) + panel-feed fallback.

For each business, capture the latest N review texts. The RPC path fetches the
Google Maps reviews endpoint using the browser session (cookies) and returns
structured data; a live-panel scroll fallback reads the rendered review feed
on the already-open detail panel when RPC is unavailable.

Extraction is toggleable via ``reviews.enabled``.
"""
from __future__ import annotations

import json
import logging
import random
import re
import string
import time

log = logging.getLogger(__name__)

_RPC_PREFIX = ")]}'"
_PB_PLACE_TEMPLATE = "!6m4!4m1!1e1!4m1!1e3!2m2!1i{page_size}!2s{token}!5m2!1s{rid}!7e81"


def _generate_request_id(length: int = 20) -> str:
    return "".join(random.choices(string.digits + string.ascii_letters, k=length))


def build_review_rpc_url(place_id: str, page_size: int = 20, token: str = "") -> str:
    """Build the ``google.com/maps/rpc/listugcposts`` URL for a place_id."""
    rid = _generate_request_id()
    pb = (
        f"!1m6!1s{place_id}"
        f"{_PB_PLACE_TEMPLATE.format(page_size=page_size, token=token, rid=rid)}"
        "!8m9!2b1!3b1!5b1!7b1!12m4!1b1!2b1!4m1!1e1!11m0!13m1!1e1"
    )
    return f"https://www.google.com/maps/rpc/listugcposts?authuser=0&hl=en&pb={pb}"


def parse_review_rpc_response(text: str) -> tuple[list, str]:
    """Parse a raw listugcposts response into (review_texts, next_page_token)."""
    data = text
    if data.startswith(_RPC_PREFIX):
        data = data[len(_RPC_PREFIX):]
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return [], ""
    reviews: list = []
    next_token = ""
    try:
        if isinstance(parsed, list) and len(parsed) > 1:
            entries = parsed[1]
            if isinstance(entries, list):
                for entry in entries:
                    text = _extract_review_text(entry)
                    if text:
                        reviews.append(text)
            if len(parsed) > 2:
                tok = parsed[2]
                if isinstance(tok, str):
                    next_token = tok
                elif isinstance(tok, list) and tok:
                    next_token = str(tok[0])
    except Exception as e:  # noqa: BLE001
        log.debug("review RPC parse error: %s", e)
    return reviews, next_token


def _extract_review_text(entry) -> str:
    """Walk a review entry (list or nested lists) to find the review body."""
    chunks: list = []
    if isinstance(entry, list):
        for item in entry:
            found = _extract_review_text(item)
            if found:
                chunks.append(found)
    elif isinstance(entry, dict):
        for key in ("text", "comment", "review", "snippet"):
            if isinstance(entry.get(key), str) and len(entry[key]) > 3:
                chunks.append(entry[key])
    elif isinstance(entry, str) and len(entry) > 3:
        if any(c.isalpha() for c in entry) and len(entry.split()) >= 2:
            chunks.append(entry)
    if chunks:
        return max(chunks, key=len)
    return ""


def parse_review_texts_dom(html: str) -> list:
    """Fallback: pull review snippets from rendered review-feed HTML."""
    if not html:
        return []
    soup_text = _strip_tags(html)
    out: list = []
    seen: set = set()
    for m in re.finditer(r"[A-Za-z0-9][^.!?]{20,500}[.!?]", soup_text):
        s = m.group(0).strip()
        if s and s not in seen and len(s) >= 10:
            seen.add(s)
            out.append(s)
    return out


def _strip_tags(html: str) -> str:
    txt = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<style.*?</style>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return " ".join(txt.split())


# -- Live-panel fallback ----------------------------------------------------

# Review body text renders inside rolling cards (jftiEf) whose text lives in a
# span. The "Reviews" tab button has an aria-label like "381 reviews".
_REVIEW_TAB_SELECTORS = [
    'button[aria-label*="reviews"]',
    'button[jsaction*="review"]',
    'button:has-text("Reviews")',
    'div[role="tab"]:has-text("Reviews")',
]
_REVIEW_TEXT_SELECTORS = [
    'div[class*="jftiEf"] span[class*="wiI7pd"]',
    'span[class*="wiI7pd"]',
]

# Google "limited view" (rolled out Feb 2026, still triggers on logged-out
# sessions / flagged traffic): the place panel is served WITHOUT the reviews
# section entirely — no tab, no dialog, no error. Detecting it up front lets
# the extractor fast-fail in milliseconds instead of burning every selector
# timeout + scroll pass against a section that does not exist.
_LIMITED_VIEW_MARKERS = [
    "sign in for reviews",
    "you're seeing limited information",
    "you are seeing limited information",
    "seeing limited information",
]


def _panel_has_reviews_section(page) -> bool:
    """True when the open panel exposes ANY reviews affordance.

    One evaluate() checks every signal at once: review-tab buttons (scoped
    to the panel, excluding the feed cards and the legal-disclosure link),
    review-text spans, and the limited-view banner. Returns False quickly
    when Google served a limited panel — the caller skips the whole review
    extraction instead of timing out against nothing.
    """
    try:
        out = page.evaluate(
            """() => {
                const panel = document.querySelector('div[role="main"]')
                             || document.body;
                if (!panel) return {has: false, limited: false};
                const txt = (panel.innerText || '').toLowerCase();
                for (const m of %s) {
                    if (txt.includes(m)) return {has: false, limited: true};
                }
                // Review tab buttons (not the feed's, not the legal link)
                const btns = panel.querySelectorAll(
                    'button[aria-label*="review" i], button[jsaction*="review"]');
                for (const b of btns) {
                    const a = (b.getAttribute('aria-label') || '').toLowerCase();
                    if (a.includes('legal') || a.includes('write a review'))
                        continue;
                    if (b.closest('div[role="feed"]')) continue;
                    return {has: true, limited: false};
                }
                if (panel.querySelector('span[class*="wiI7pd"],'
                                        + ' div[class*="jftiEf"]')) {
                    return {has: true, limited: false};
                }
                // Rating row present but no reviews affordance anywhere:
                // on the current layout this is a limited panel.
                const stars = panel.querySelectorAll('div.F7nice').length;
                return {has: false, limited: stars > 0};
            }""" % _js_array(_LIMITED_VIEW_MARKERS))
        if isinstance(out, dict):
            return bool(out.get("has")), bool(out.get("limited"))
        return False, False
    except Exception:  # noqa: BLE001 — treat as "unknown, try the old path"
        return True, False


def _js_array(items) -> str:
    import json as _json
    return _json.dumps(list(items))

_REVIEW_NOISE_RE = re.compile(
    r"\b\d+\s+reviews?\b|\b\d+\s+photos?\b"
    r"|\b\d+\s+(?:months?|weeks?|days?|years?|hours?)\s+ago\b"
    r"|\b(?:a|an)\s+(?:month|week|day|year|hour)\s+ago\b"
    r"|\bLocal Guide\b|\bEdited\b|\bMore\b|\bLike\b(?:\s+\d+)?\b|\bShare\b"
    r"|\s·\s|\s•\s",
    re.I,
)


def clean_review_text(text: str) -> str:
    """Strip Maps review-chrome (counts, "X ago", Local Guide, Like/Share)."""
    t = _REVIEW_NOISE_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", t).strip()


def open_reviews_tab(page) -> bool:
    """Click the Reviews tab so the review feed becomes the scroll target.

    PERF: the fixed 1.5s post-click sleep is replaced by a bounded wait for
    the review feed to actually appear (wait_for_selector), i.e.
    condition-based instead of blind. The tab button itself gets a short
    visibility wait first — on a freshly-clicked card panel the button can
    still be hydrating, which is why the old code often failed to open the
    tab at all and then burned scroll passes on a feed that never existed.
    """
    for sel in _REVIEW_TAB_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                try:
                    loc.wait_for(state="visible", timeout=2500)
                except Exception:  # noqa: BLE001 — still try the click
                    pass
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=4000)
                try:
                    page.wait_for_selector(
                        ",".join(_REVIEW_TEXT_SELECTORS), timeout=4000)
                except Exception:  # noqa: BLE001 — some panels are slow
                    time.sleep(0.8)
                return True
        except Exception:
            continue
    return False


def review_dialog_open(page) -> bool:
    """True when a reviews dialog overlay is currently covering the panel.

    The dialog intercepts card clicks (the next listing's click lands on the
    backdrop and the URL never switches — the 8s timeout path). The caller
    closes it before clicking the next result card.
    """
    try:
        return page.locator(
            'div[role="dialog"], div[class*="review-dialog"]').count() > 0
    except Exception:  # noqa: BLE001
        return False


def close_review_dialog(page) -> None:
    """Best-effort close of an open reviews dialog (Escape, like a human)."""
    try:
        if review_dialog_open(page):
            page.keyboard.press("Escape")
            try:
                page.wait_for_selector(
                    'div[role="dialog"]', state="hidden", timeout=2000)
            except Exception:  # noqa: BLE001 — best effort
                pass
    except Exception:  # noqa: BLE001
        pass


def _read_review_texts_batched(page, max_reviews: int) -> list:
    """ONE evaluate() round-trip: read every visible review body text.

    PERF (Fix B): replaces the per-node inner_text() loop (~n round-trips per
    pass). The SAME selectors and the SAME min-length screen are applied in
    JS; results still flow through clean_review_text in Python so the output
    contract is identical.
    """
    import json as _json
    js = """(args) => {
        const [selsJson, maxReviews] = args;
        const sels = JSON.parse(selsJson);
        const out = [];
        for (const sel of sels) {
            try {
                const nodes = document.querySelectorAll(sel);
                for (const el of nodes) {
                    const t = (el.textContent || '').trim();
                    if (t && t.length >= 25 && out.length < maxReviews) {
                        out.push(t);
                    }
                }
                if (out.length) break;
            } catch (e) { /* invalid selector - try next */ }
        }
        return out;
    }"""
    try:
        raw = page.evaluate(js, [_json.dumps(_REVIEW_TEXT_SELECTORS),
                                 max_reviews])
        return [clean_review_text(t) for t in (raw or [])
                if t and len(t) >= 25]
    except Exception:  # noqa: BLE001 — fall back to per-node reads
        return []


def extract_reviews_from_panel(page, max_reviews: int = 5,
                               open_tab: bool = True) -> list:
    """Open the reviews feed and pull up to ``max_reviews`` review texts.

    ``open_tab`` first clicks the Reviews tab (so the feed becomes scrollable),
    then repeatedly scrolls and harvests unique review bodies.

    PERF (Fix B): each scroll pass now reads ALL visible reviews in ONE
    evaluate() instead of per-node round-trips. A failed tab open now means
    the feed never existed — return [] immediately instead of burning
    scroll passes against nothing. Google's "limited view" panels (rolled
    out Feb 2026, still served to logged-out sessions) expose NO reviews
    affordance at all — detected up front in one evaluate(), the whole
    extraction is skipped in milliseconds.
    The reviews dialog is closed before returning so it cannot intercept
    the next result-card click.
    """
    texts: list = []
    seen: set = set()
    if open_tab:
        try:
            has_section, limited = _panel_has_reviews_section(page)
        except Exception:
            has_section, limited = True, False
        if not has_section:
            if limited:
                log.debug("limited-view panel: reviews section absent — "
                          "skipping review extraction")
            return []
        try:
            opened_ok = open_reviews_tab(page)
        except Exception:
            opened_ok = False
        if not opened_ok:
            return []

    # First read before scrolling: the first batch often already fills the
    # quota for small feeds (zero extra scroll passes needed).
    for t in _read_review_texts_batched(page, max_reviews):
        if t not in seen:
            seen.add(t)
            texts.append(t)
    if len(texts) >= max_reviews:
        close_review_dialog(page)
        return texts[:max_reviews]

    # Several scrolling passes; the feed lives inside the detail panel.
    scroll_attempts = max(3, max_reviews)
    for _ in range(scroll_attempts):
        # Element-scoped scroll (F29): drive the review feed's own scroll
        # rather than a global mouse wheel + blind sleep.
        try:
            feed = page.locator(
                "div[role='main'] .m6QErb, div[class*='review-dialog-list']").first
            if feed.count() > 0:
                feed.evaluate("el => el.scrollBy(0, 1500)")
            else:
                page.mouse.wheel(0, 1800)
        except Exception:
            page.mouse.wheel(0, 1800)
        page.wait_for_timeout(400)
        for t in _read_review_texts_batched(page, max_reviews):
            if t not in seen:
                seen.add(t)
                texts.append(t)
                if len(texts) >= max_reviews:
                    close_review_dialog(page)
                    return texts[:max_reviews]
    close_review_dialog(page)
    return texts[:max_reviews]


def filter_reviews(reviews: list, min_len: int = 0, max_len: int = 1000) -> list:
    """Drop out-of-length and duplicate reviews."""
    out: list = []
    seen: set = set()
    for r in reviews:
        r = (r or "").strip()
        if not r or r in seen:
            continue
        seen.add(r)
        if min_len and len(r) < min_len:
            continue
        if max_len:
            r = r[:max_len]
        out.append(r)
    return out
