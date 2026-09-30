"""Per-record quality gate.

Checks a record for completeness, contradictions, and encoding issues. Returns
a list of issue strings; an empty list means the record passes.
"""
from __future__ import annotations

import re

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Consent/interstitial markers (duplicated locally — importing the collector
# here would couple the quality gate to Playwright-capable modules; the gate
# must stay importable everywhere, including HTTP-only test contexts).
_CONSENT_TITLES = (
    "before you continue to google",
    "before you continue",
    "consent required",
)
_CONSENT_MARKER_PHRASES = (
    "consent.google", "before you continue", "accept all",
)


def _is_consent_wall_text(text: str) -> bool:
    """True when the text is the Google consent wall (never a business)."""
    if not text:
        return False
    low = text.strip().lower()
    if low in _CONSENT_TITLES:
        return True
    return any(m in low for m in _CONSENT_MARKER_PHRASES)


def quality_issues(record: dict) -> list[str]:
    issues: list[str] = []
    name = (record.get("business_name") or "").strip().upper()
    if not name or name in ("N/A", ""):
        issues.append("missing_name")

    # Battle-hardening (consent incident 2026-09-29): the consent wall's own
    # title ("Before you continue to Google") was extracted as a business
    # name and SAVED as a lead. Any consent/interstitial text in the name
    # (or a consent host as the Maps URL) fails the gate — the record is
    # never committed.
    if _is_consent_wall_text(record.get("business_name") or ""):
        issues.append("consent_wall_contamination")
    gmaps_url = (record.get("google_maps_url") or "").lower()
    if gmaps_url.startswith("https://consent.google.") or \
            "//consent.google." in gmaps_url:
        issues.append("consent_wall_url")

    rating = record.get("rating")
    if rating not in (None, "N/A", ""):
        try:
            r = float(rating)
            if not (0.0 <= r <= 5.0):
                issues.append(f"rating_out_of_range:{rating}")
        except (TypeError, ValueError):
            issues.append("rating_non_numeric")

    # Encoding: reject control chars in any string value.
    for k, v in record.items():
        if isinstance(v, str) and _CONTROL_RE.search(v):
            issues.append(f"control_chars_in:{k}")

    # Contradiction: dead website but a valid email (rare, flag only).
    return issues


def passes_quality(record: dict) -> bool:
    return len(quality_issues(record)) == 0
