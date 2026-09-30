"""Email extraction and static cleaning.

Extraction sources (priority order, per page):
  1. mailto: links
  2. JSON-LD / microdata structured data
  3. visible HTML text (testimonials/comments/excluded sections removed)
  4. rendered DOM text (supplied by the Playwright path)

Inline ``<script>`` bodies (GA4/GTM config, Sentry DSNs) and testimonial text are
NOT mined: they are the dominant source of tracking-vendor and third-party-PII
false positives. Cleaning decodes obfuscation (``[at]``, ``[dot]``, ``&#64;``)
and rejects disposable/placeholder/asset/vendor junk, with domain-relationship
filtering.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..signals.social import _parse_html_or_xml
from ..utils.normalize import is_usable_email, normalize_email

_EMAIL_TOKEN_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,61}[A-Za-z0-9])?\.[A-Za-z]{2,63}",
)


def _decode_obfuscated(text: str) -> str:
    """Decode entity/bracket obfuscation; NEVER bare prose ``at``/``dot``.

    Bare-word decoding turned ordinary sentences ("Order now at shop dot com")
    into fake emails (F17). Only explicit obfuscation markers ([at], (at), &#64;,
    etc.) decode.
    """
    t = text
    t = t.replace("&#64;", "@").replace("&commat;", "@").replace("@&#8203;", "@")
    t = t.replace("&#46;", ".").replace("&#x2E;", ".").replace("&period;", ".")
    t = re.sub(r"\s*(?:\[|\()\s*at\s*(?:\]|\))\s*", "@", t, flags=re.I)
    t = re.sub(r"\s*(?:\[|\()\s*dot\s*(?:\]|\))\s*", ".", t, flags=re.I)
    return t


def extract_emails_from_text(text: str) -> list[str]:
    """Extract unique normalized emails from arbitrary text."""
    if not text:
        return []
    decoded = _decode_obfuscated(text)
    found: list[str] = []
    seen: set[str] = set()
    for m in _EMAIL_TOKEN_RE.finditer(decoded):
        candidate = normalize_email(m.group(0))
        if candidate and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    return found


_DEFAULT_EXCLUDE = [".testimonial", ".testimonials", ".review", ".reviews",
                    ".review-body", ".comment", ".comments",
                    ".wp-block-comment", "figcaption"]


def extract_emails(html: str | None, rendered_text: str = "", url: str = "",
                   exclude_selectors: list | None = None) -> list[str]:
    """Extract raw emails from an HTML page (and optional rendered DOM text)."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(emails) -> None:
        for e in emails:
            ne = normalize_email(e)
            if ne and ne not in seen:
                seen.add(ne)
                candidates.append(ne)

    if html:
        # Sitemap XML payloads reach this parser too — parse them with the
        # XML parser so BeautifulSoup does not warn (and namespaced XML
        # parses reliably); HTML pages keep the HTML parser.
        soup = _parse_html_or_xml(html)
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if href.lower().startswith("mailto:"):
                add([href[7:].split("?")[0]])
        for script in soup.find_all("script", type="application/ld+json"):
            add(extract_emails_from_text(script.get_text()))
        # Remove testimonial/review/figure-caption sections before reading
        # visible text so a reviewer's personal address is never harvested as
        # the business contact. `.author`/`blockquote`/`.quote`/`cite` are
        # intentionally NOT stripped (real team bios too often) — see F33.
        selectors = exclude_selectors if exclude_selectors is not None else _DEFAULT_EXCLUDE
        for tag in soup.select(",".join(selectors)):
            tag.decompose()
        add(extract_emails_from_text(soup.get_text(" ")))

    if rendered_text:
        add(extract_emails_from_text(rendered_text))

    return candidates


def clean_emails(candidates: list[str], max_length: int = 120,
                 website_url: str | None = None) -> list[str]:
    """Apply static cleaning; returns only usable emails, ordered."""
    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        e = normalize_email(c)
        if not e or e in seen:
            continue
        seen.add(e)
        if is_usable_email(e, max_length, website_url):
            out.append(e)
    return out
