"""Smart priority website crawler.

Discovers relevant internal pages (homepage -> contact/about/services) with a
priority queue, and early-stops once emails/social/booking/contact are found.
Optional sitemap.xml-aware discovery of relevant pages.
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4 import FeatureNotFound

log = logging.getLogger(__name__)

_PRIORITY_HINTS = ["contact", "about", "services", "team", "staff", "our-story",
                   "about-us", "contact-us", "get-in-touch", "pricing", "booking"]
_GOAL_HINTS = ["mailto:", "contact", "book", "appointment", "facebook.com",
               "instagram.com", "linkedin.com"]


def extract_links(html: str, base_url: str) -> list:
    """Return absolute internal + external links from an HTML page."""
    soup = BeautifulSoup(html, "lxml")
    links: list = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "tel:", "mailto:")):
            continue
        abs_url = urljoin(base_url, href)
        if abs_url.startswith(("http://", "https://")):
            links.append(abs_url)
    return links


def _priority(url: str) -> int:
    low = url.lower()
    for i, hint in enumerate(_PRIORITY_HINTS):
        if hint in low:
            return i
    return len(_PRIORITY_HINTS)


def _is_same_domain(url: str, base: str) -> bool:
    try:
        return urlparse(url).netloc == urlparse(base).netloc
    except ValueError:
        return False


def crawl_priority(html: str, base_url: str, max_pages: int = 10) -> list:
    """Return a prioritized list of internal pages to visit (homepage first,
    then contact/about/services by hint order)."""
    links = extract_links(html, base_url)
    internal = [l for l in links if _is_same_domain(l, base_url)]
    # Order-preserving dedup before a stable sort; a bare set->list had
    # non-deterministic order across runs (F24).
    internal = list(dict.fromkeys(internal))
    internal = sorted(internal, key=_priority)
    return internal[:max_pages]


def crawl_sitemap_aware(sitemap_xml: str, base_url: str, keyword: str = "",
                        max_pages: int = 15) -> list:
    """Extract relevant URLs from a sitemap.xml, filtered by keyword/hints."""
    # Sitemaps are XML: parse with the XML parser when available (silences
    # XMLParsedAsHTMLWarning and is more reliable for namespaced sitemaps);
    # fall back to the HTML parser only when the XML mode is unavailable.
    try:
        soup = BeautifulSoup(sitemap_xml, "lxml-xml")
    except FeatureNotFound:  # pragma: no cover — lxml ships in requirements
        soup = BeautifulSoup(sitemap_xml, "lxml")
    urls = [loc.get_text().strip() for loc in soup.find_all("loc")]
    urls = [u for u in urls if u.startswith(("http://", "https://"))]
    if keyword:
        kw = keyword.lower()
        relevant = [u for u in urls if kw in u.lower()
                    or any(h in u.lower() for h in _PRIORITY_HINTS)]
        urls = relevant or urls
    return urls[:max_pages]


def early_stop_reached(page_text: str) -> bool:
    """True when the page already yields the goal signals (stop crawling)."""
    low = page_text.lower()
    return any(h in low for h in _GOAL_HINTS)
