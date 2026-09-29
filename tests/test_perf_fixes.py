"""PERF regression tests: the 10x optimization must not change behavior.

Covers (see docs/PERFORMANCE.md):
- Fix A: card-map click path (build + indexed click) with legacy fallback
- Fix B: batched review reads + wait-based tab open
- Fix C: maps.extract_owner_posts gate (default off)
- Fix E: combined identity wait replaces 3 sequential waits
- Fix F: batched panel read merges the same fields the per-field path did
- Fix G: new pacing defaults load
- Fix H: maps.workers wiring (serial fallback, parallel dispatch)
"""
from __future__ import annotations

import json

import pytest
import yaml

from scraper.config import AppConfig, load_config
from scraper.maps import collector as C
from scraper.maps import reviews as R
from scraper.maps.collector import MapsCollector
from scraper.pipeline import Pipeline
from scraper.maps.collector import DemoCollector


# -- Fix A: card map ----------------------------------------------------------

class _FakeLocator:
    def __init__(self, cards):
        self._cards = cards

    def count(self):
        return len(self._cards)

    def nth(self, i):
        return _FakeCard(self._cards[i])


class _FakeCard:
    def __init__(self, card):
        self._card = card

    def click(self, timeout=None):
        self._card["clicked"] = True


class _FakePage:
    """Minimal page double for the card-map path."""

    def __init__(self, cards):
        # cards: list of dicts {href, clicked}
        self._cards = cards
        self.url = "https://www.google.com/maps/search/test"
        self.eval_results = {}
        self.wait_fn_ok = True

    def locator(self, sel):
        return _FakeLocator(self._cards)

    def evaluate(self, js, *args):
        # _build_card_map: emulate per-selector querySelectorAll maps.
        # Shape: {selector: {token-or-href: index}} — tokens are the
        # !1s0x…:0x… place-id fragments INCLUDING the !1s prefix, exactly
        # what _place_token() builds on the Python side.
        # The DOM-click dispatch (round 2) is emulated too: clicking via
        # evaluate marks the card clicked exactly like the real page.
        import re as _re
        # DOM-click dispatch (round 2): args = [selector, index]
        if (args and isinstance(args[0], list) and len(args[0]) == 2
                and isinstance(args[0][0], str)
                and isinstance(args[0][1], int)):
            sel, i = args[0]
            if 0 <= i < len(self._cards):
                self._cards[i]["clicked"] = True
                return True
            return False
        out = {}
        for sel in C.RESULT_CARD_SELECTORS:
            m = {}
            for i, card in enumerate(self._cards):
                href = card.get("href")
                if not href or "/maps/place/" not in href:
                    continue
                mm = _re.search(r"(!1s0x[0-9a-f]+:0x[0-9a-f]+)", href)
                key = mm.group(1) if mm else href
                if key not in m:
                    m[key] = i
            if m:
                out[sel] = m
        return out

    def wait_for_function(self, js, arg=None, timeout=None):
        # Token-aware proof: the fake's URL must contain the place token
        # (mirrors the real location.href token check).
        if isinstance(arg, str) and arg.startswith("!1s"):
            return arg in self.url
        return self.wait_fn_ok


def test_build_card_map_returns_href_to_index():
    # Cards carry identity tokens (!1s0x…:0x…); the map keys on them.
    page = _FakePage([
        {"href": "https://www.google.com/maps/place/A/data=!1s0x11:0x22"},
        {"href": None},
        {"href": "https://www.google.com/maps/place/B/data=!1s0x33:0x44"},
    ])
    m = C._build_card_map(page)
    # Shape: per-selector {selector: {token: index}} — an index from one
    # selector's nodelist must never be applied to another's.
    assert isinstance(m, dict)
    first = m[C.RESULT_CARD_SELECTORS[0]]
    assert first == {"!1s0x11:0x22": 0, "!1s0x33:0x44": 2}


def test_click_to_open_uses_cached_map_single_roundtrip():
    cards = [{"href": f"https://www.google.com/maps/place/N{i}"
                      f"/data=!1s0x{i}:0x{i}{i}",
              "clicked": False} for i in range(1, 6)]
    page = _FakePage(cards)
    # The fake's location.href carries card tokens so the identity proof
    # works exactly like the real page.
    page.url = "https://www.google.com/maps/place/N4/data=!1s0x4:0x44"
    col = MapsCollector.__new__(MapsCollector)  # no __init__ (no browser)
    target = cards[3]["href"]
    # Pre-cache the map exactly as the collector would after the first call.
    setattr(page, "_abgms_card_map", C._build_card_map(page))
    opened = MapsCollector._click_to_open(col, page, target)
    assert opened is True
    # ONLY the target card was clicked — no O(n) scan of every card.
    assert cards[3]["clicked"] is True
    assert sum(1 for c in cards if c["clicked"]) == 1


def test_click_card_for_wrong_selector_index_is_isolated():
    """A place present under selector A but not selector B must click via A
    only — the cross-selector index bug (clicked wrong card -> 8s timeout)
    must stay fixed."""
    cards = [{"href": "https://www.google.com/maps/place/X",
              "clicked": False}] * 4
    page = _FakePage(cards)
    # Map claims the place is index 3 under the SECOND selector only.
    card_map = {C.RESULT_CARD_SELECTORS[1]:
                {"https://www.google.com/maps/place/X": 3}}
    assert C._click_card_for(page, card_map,
                             "https://www.google.com/maps/place/X") is True
    # And a miss (wrong href) returns False without clicking anything.
    cards2 = [{"href": "https://www.google.com/maps/place/Y",
               "clicked": False} for _ in range(3)]
    page2 = _FakePage(cards2)
    assert C._click_card_for(page2, card_map,
                             "https://www.google.com/maps/place/Z") is False


# -- Fix B: batched review reads ----------------------------------------------

def test_read_review_texts_batched_cleans_and_screens():
    class _P:
        def evaluate(self, js, arg=None):
            sels = json.loads(arg[0])
            assert "wiI7pd" in sels[0]
            return ["Great plumbing work, done fast and neat!",
                    "x",  # too short -> dropped
                    "Reliable service over many years of visits."]

    out = R._read_review_texts_batched(_P(), max_reviews=5)
    assert len(out) == 2
    assert all(len(t) >= 25 for t in out)


def test_read_review_texts_batched_failure_returns_empty():
    class _P:
        def evaluate(self, js, arg=None):
            raise RuntimeError("selector drift")

    assert R._read_review_texts_batched(_P(), 5) == []


# -- Fix C: owner-post gate ---------------------------------------------------

def test_owner_post_default_off_in_config_model():
    cfg = AppConfig(queries=["dentists in Dallas"])
    assert cfg.maps.extract_owner_posts is False
    assert cfg.maps.workers == 1


def test_owner_post_field_wired_to_collector():
    # The main._build_collector wiring must pass the flag through.
    import scraper.main as M
    src = open(M.__file__, encoding="utf-8").read()
    assert "extract_owner_posts=m.extract_owner_posts" in src


# -- Fix G: pacing defaults ---------------------------------------------------

def test_new_pacing_defaults_load(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "queries: ['dentists in Dallas']\n"
        f"job:\n  output_dir: '{tmp_path}/out'\n  client_name: pacing\n",
        encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.delays.maps_min_seconds == 0.4
    assert cfg.delays.maps_max_seconds == 1.2


def test_template_pacing_is_fast_not_conservative():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    t = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    assert t["delays"]["maps_min_seconds"] <= 0.5
    assert t["delays"]["maps_max_seconds"] <= 1.5
    assert t["maps"]["workers"] >= 1


# -- Fix H: workers wiring ----------------------------------------------------

class _SerialCollector:
    def __init__(self):
        self.calls = []

    def collect(self, query):
        self.calls.append(("serial", query))
        yield {"business_name": "A", "source_query": query}

    def close(self):
        pass


class _ParallelCollector(_SerialCollector):
    def collect_parallel(self, query, workers=2):
        self.calls.append(("parallel", query, workers))
        yield {"business_name": "A", "source_query": query}


def _pipeline_for(tmp_path, collector, workers, client="wtest"):
    p = tmp_path / "config.yaml"
    p.write_text(
        "queries: ['dentists in Dallas']\n"
        f"job:\n  output_dir: '{tmp_path}/out'\n  client_name: {client}\n"
        f"maps:\n  workers: {workers}\n",
        encoding="utf-8")
    return Pipeline(load_config(str(p)), collector)


def test_workers_one_uses_serial_collect(tmp_path):
    c = _ParallelCollector()
    _pipeline_for(tmp_path, c, workers=1, client="ser1").run()
    assert c.calls and c.calls[0][0] == "serial"


def test_workers_two_dispatches_parallel(tmp_path):
    c = _ParallelCollector()
    _pipeline_for(tmp_path, c, workers=2, client="par2").run()
    assert c.calls and c.calls[0][0] == "parallel"


def test_workers_two_without_parallel_attr_falls_back(tmp_path):
    c = _SerialCollector()
    _pipeline_for(tmp_path, c, workers=2, client="par3").run()
    # DemoCollector-shaped objects without collect_parallel keep working.
    assert c.calls and c.calls[0][0] == "serial"


# -- Fix F: batched panel read merge ------------------------------------------

def test_apply_batched_fields_full_merge():
    data = {}
    b = {
        "name": "Cooper Plumbing",
        "category": "Plumber",
        "address": "123 Main St, Houston, TX",
        "phone_attr": "phone:tel:+17135551234",
        "phone_text": "+1 713 555 1234",
        "website": "https://cooper.example",
        "plus_code": "ABCD",
        "hours": "Monday: 9 AM - 5 PM",
        "permanently_closed": False,
        "status": "Open · Closes 5 PM",
        "claim": False,
        "rating_block": "4.6 (381)",
        "review_aria": "4.6 stars 381 reviews",
    }
    out = C._apply_batched_fields(data, b)
    assert out["business_name"] == "Cooper Plumbing"
    assert out["category"] == "Plumber"
    assert out["full_address"] == "123 Main St, Houston, TX"
    assert out["phone"] == "phone:tel:+17135551234"
    assert out["phone_international"] == "+17135551234"
    assert out["website"] == "https://cooper.example"
    assert out["business_hours"] == "Monday: 9 AM - 5 PM"
    assert out["business_status"] == "Open"
    assert out["claimed_status"] == "Claimed"
    assert out["_rating_block"] == "4.6 (381)"


def test_apply_batched_fields_permanently_closed_wins():
    out = C._apply_batched_fields({}, {"permanently_closed": True,
                                       "status": None})
    assert out["business_status"] == "Permanently closed"


def test_apply_batched_fields_missing_values_are_na():
    # A batched read that RAN but found nothing: the merge fills the
    # stable N/A defaults; the caller's per-field fallback may still upgrade
    # them when selectors match there.
    out = C._apply_batched_fields({}, {"name": None, "category": None})
    assert out["phone"] == "N/A"
    assert out["website"] == "N/A"
    assert out["business_hours"] == "N/A"
    assert out["business_status"] == "N/A"
    assert out["claimed_status"] == "Claimed"
    # An entirely empty batched dict means the evaluation returned nothing
    # useful — the merge is a no-op so per-field reads take over.
    out2 = C._apply_batched_fields({}, {})
    assert out2 == {}


def test_apply_batched_fields_claim_chip_means_unclaimed():
    out = C._apply_batched_fields({}, {"claim": True})
    assert out["claimed_status"] == "Unclaimed"


# -- Fix E: single wait, no triple waits ---------------------------------------

def test_open_and_extract_has_no_three_sequential_waits():
    src = open(C.__file__, encoding="utf-8").read()
    # The three old sequential waits must be gone; ONE combined readiness
    # wait remains (round 2 renamed identity -> readiness: URL identity is
    # proven in the click path; this wait proves h1 + data rows).
    assert "combined panel readiness wait" in src
    assert "combined panel identity wait" not in src
    assert src.count("wait_for_selector('h1', timeout=10_000") == 0
    assert "timeout=5_000)\n        except Exception:\n            time.sleep(1.0)" \
        not in src


# -- integration: demo pipeline still green with workers=1 --------------------

def test_demo_pipeline_with_perf_config(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "queries: ['dentists in Dallas']\n"
        f"job:\n  output_dir: '{tmp_path}/out'\n  client_name: perfdemo\n"
        "maps:\n  workers: 1\n",
        encoding="utf-8")
    counters = Pipeline(load_config(str(p)), DemoCollector()).run()
    assert counters["committed"] > 0
