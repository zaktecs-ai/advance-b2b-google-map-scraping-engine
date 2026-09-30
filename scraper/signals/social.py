"""Social profile detection — per-platform, domain-anchored patterns.

A URL is matched to exactly one platform by its host, so a Facebook URL can
never land in the Instagram column.

A URL is only treated as a *profile* when both its host AND its path look like
a genuine handle. Share dialogs, intent/tweet actions, tracking redirects,
pixels, and embed URLs are rejected so they never populate an export column.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def _parse_html_or_xml(text: str):
    """Parse a fetched document with the right BeautifulSoup parser.

    Websites' ``sitemap.xml`` payloads reach the email/social extractors;
    parsing XML with the HTML parser makes BeautifulSoup emit
    ``XMLParsedAsHTMLWarning`` on every such page (and namespaced XML can
    mis-parse). Detection: a document whose first tag is ``<?xml`` or
    ``<urlset``/``<sitemapindex`` uses the XML parser; everything else keeps
    the HTML parser. Both parsers ship with lxml (requirements.lock).
    """
    from bs4 import BeautifulSoup
    head = (text or "").lstrip()[:200].lower()
    if head.startswith("<?xml") or "<urlset" in head or "<sitemapindex" in head:
        try:
            return BeautifulSoup(text, "lxml-xml")
        except Exception:  # noqa: BLE001 — fall back to the HTML parser
            pass
    return BeautifulSoup(text, "lxml")

_PLATFORM_HOSTS = {
    "facebook": r"(^|\.)facebook\.com$",
    "instagram": r"(^|\.)instagram\.com$",
    "linkedin": r"(^|\.)linkedin\.com$",
    "youtube": r"(^|\.)youtube\.com$|(^|\.)youtu\.be$",
    "twitter_x": r"(^|\.)twitter\.com$|(^|\.)x\.com$",
    "tiktok": r"(^|\.)tiktok\.com$",
    "pinterest": r"(^|\.)pinterest\.com$",
    "github": r"(^|\.)github\.com$",
    "snapchat": r"(^|\.)snapchat\.com$",
}

# Redirect/tracking wrapper hosts that are never a real profile handle.
_REDIRECT_HOSTS = {
    "l.facebook.com", "lm.facebook.com", "lnkd.in", "t.co", "bit.ly",
}

# Path substrings that always mark an action/redirect/embed/pixel URL, not a
# profile. Present on any of a platform's official domains, regardless of which
# platform the final host match belongs to.
# NOTE: rejection is by FIRST PATH SEGMENT, not raw substring (F18), so
# ``/travel`` is no longer killed by the shared ``/tr`` prefix and real
# ``/pages/<name>`` Facebook pages are accepted.
_REJECT_SEGMENTS = {
    "sharer", "share", "sharing", "intent", "plugins", "dialog", "l.php",
    "tr", "watch", "embed", "login", "signup", "accounts", "redirect",
}

# Per-platform profile-path shapes. A real handle is a short segment of
# username-legal characters; a bare root or a well-known non-handle path is not.
_PROFILE_PATH = {
    "facebook": re.compile(r"^/(?!sharer|plugins|tr|groups|watch|events|marketplace|photo|videos|story|reel)(?:pages/)?[A-Za-z0-9.\-]{1,}/?$"),
    # The FIRST segment is the handle; a trailing sub-path (e.g. /natgeo/travel/)
    # is still the handle's profile (F18). G05: the sub-path's first segment
    # must NOT be a post/reel/tv marker — production exported
    # `instagram.com/<handle>/p/BrlrQ52Hdi3` (a POST) as a profile.
    "instagram": re.compile(
        r"^/(?!p/|reel/|tv/|stories/|explore/|accounts/)[A-Za-z0-9._]{1,}"
        r"(?:/(?!(?:p|reel|tv)(?:/|$))[A-Za-z0-9._/-]*)?$"),
    "linkedin": re.compile(r"^/(?:company|school|in)/[A-Za-z0-9%_.\-]+/?$"),
    "youtube": re.compile(r"^/(?:@|c/|channel/|user/)[A-Za-z0-9_.\-]+/?$"),
    "twitter_x": re.compile(r"^/(?!intent|share|home|search|explore|messages|settings|i/)[A-Za-z0-9_]{1,15}/?$"),
    "tiktok": re.compile(r"^/@[A-Za-z0-9_.\-]+/?$"),
    "pinterest": re.compile(r"^/[A-Za-z0-9_.\-]{1,}/?$"),
    "github": re.compile(r"^/[A-Za-z0-9_.\-]{1,39}/?$"),
    "snapchat": re.compile(r"^/add/[A-Za-z0-9_.\-]+/?$"),
}


def platform_for_url(url: str) -> str | None:
    """Return the platform key for a URL, or None if not a social profile.

    Classification requires BOTH a social host AND a genuine-looking profile
    path. Share/redirect/embed/tracking URLs are rejected so the first URL seen
    per platform (often a junk widget) can no longer beat the real link.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if host in _REDIRECT_HOSTS:
        return None
    path = parts.path or "/"
    low_path = path.lower()
    segments = [s for s in low_path.split("/") if s]
    if segments and segments[0] in _REJECT_SEGMENTS:
        return None
    for platform, pattern in _PLATFORM_HOSTS.items():
        if re.search(pattern, host):
            prof_re = _PROFILE_PATH.get(platform)
            if prof_re is None:
                return None
            return platform if prof_re.match(path) else None
    return None


def detect_social(urls: list[str]) -> dict[str, str]:
    """Classify a list of URLs into a per-platform profile dict."""
    result = {k: "N/A" for k in _PLATFORM_HOSTS}
    for u in urls:
        p = platform_for_url(u)
        if p and result[p] == "N/A":
            result[p] = u
    return result


# ---------------------------------------------------------------------------
# Reverse: collect social URLs found inside an HTML page.
# ---------------------------------------------------------------------------

def social_urls_from_html(html: str, base_url: str = "") -> list[str]:
    """Return social-profile URLs discovered in an HTML page's anchors."""
    from bs4 import BeautifulSoup
    if not html:
        return []
    from urllib.parse import urljoin
    # Sitemap XML payloads reach this parser too — parse them with the XML
    # parser so BeautifulSoup does not warn; HTML pages keep the HTML parser.
    soup = _parse_html_or_xml(html)
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_url = urljoin(base_url, href) if base_url else href
        if platform_for_url(abs_url):
            out.append(abs_url)
    return out
