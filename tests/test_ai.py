from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from PIL import Image

from criee.ai.advisor import Demand, numbers_ok, template
from criee.ai.provider import AI, AIUnavailable, Budget
from criee.ai.supply import read_shelf
from criee.ai.wish import compile_wish, parse_rules, readback, to_terms
from criee.domain import Attr, Category, GeoPoint
from test_settlement import make_config

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
HERE = GeoPoint(lat=48.58, lon=7.75)


def test_budget_refuses_after_cutoff_and_over_caps(tmp_path):
    kv: dict = {}
    cfg = replace(make_config(tmp_path), ai_provider="gemini", gemini_api_key="k", ai_daily_cap=2,
                  ai_per_visitor_hourly=1, ai_until=date.today() - timedelta(days=1))
    assert Budget(cfg, kv.get, kv.__setitem__).live_allowed(None) == "after_cutoff"
    cfg = replace(cfg, ai_until=date.today() + timedelta(days=1))
    b = Budget(cfg, kv.get, kv.__setitem__)
    assert b.live_allowed("v1") is None
    b.spend("v1")
    assert b.live_allowed("v1") == "visitor_cap"
    b.spend("v2")
    assert b.live_allowed("v3") == "daily_cap"
    assert Budget(replace(cfg, ai_provider="recorded"), kv.get, kv.__setitem__).live_allowed(None) == "no_live_model"


def test_without_a_model_the_wish_compiler_uses_rules(tmp_path):
    ai = AI(make_config(tmp_path), {}.get, {}.__setitem__)
    d = asyncio.run(compile_wish(ai, "Une baguette bio, 1,30 € max, en semaine après 18h", HERE, "Krutenau", NOW))
    assert d.source == "rules" and d.terms is not None
    assert d.terms.category is Category.BREAD and d.terms.limit_cents == 130
    assert {c.attr for c in d.terms.constraints} == {Attr.ORGANIC}
    assert d.terms.windows[0].start_min == 18 * 60
    assert "€1.30" in d.readback


def test_missing_price_asks_instead_of_guessing():
    terms, questions = to_terms(parse_rules("a sourdough loaf on Fridays"), HERE, NOW)
    assert terms is None and questions


def test_trivial_constraints_are_dropped():
    raw = parse_rules("a vegetarian dinner up to 6 euros")
    raw["constraints"].append({"attr": "servings", "op": "gte", "value_int": 1})
    terms, _ = to_terms(raw, HERE, NOW)
    assert [c.attr for c in terms.constraints] == [Attr.VEGETARIAN]


def test_rules_put_boxes_in_grocery_boxes():
    assert parse_rules("a vegetable box up to 28 euros")["category"] == "grocery_box"


def test_readback_is_rendered_from_terms_only():
    terms, _ = to_terms(parse_rules("2 croissants under 1.50 euros on saturday"), HERE, NOW)
    text = readback(terms, "croissants", "Neudorf")
    assert text.startswith("Buy 2 croissants") and "€1.50" in text and "Saturdays" in text


def _demand() -> Demand:
    return Demand(Category.BREAD, wishes=12, buyers=9, curve=[(450, 3), (400, 7)], quantity=10, best_price=400,
                  clear_at_best=7, revenue_uniform=2800, revenue_clock=3000, clock_start=450, clock_floor=380,
                  tomorrow_wishes=8, hidden=False)


def test_advice_checker_rejects_invented_numbers():
    d = _demand()
    assert numbers_ok("12 wishes from 9 buyers: at €4.00 you sell 7 for €28.00.", d)
    assert not numbers_ok("12 wishes: at €3.50 you would sell 9.", d)
    assert numbers_ok(template(d, "bread"), d)


def test_shelf_facts_without_evidence_are_unknown(tmp_path, monkeypatch):
    img = io.BytesIO()
    Image.new("RGB", (400, 300), (200, 180, 150)).save(img, "JPEG")

    class FakeAI:
        last_source = "live"

        async def json(self, *a, **k):
            return {"transcript": "Tout est bio aujourd'hui", "mode_hint": "clock", "items": [{
                "name": "Rye loaf", "name_fr": "Pain de seigle", "category": "bread", "quantity": 4, "unit_label": "loaf",
                "price_eur": 5.5, "price_source": "label", "image_index": 0, "box_2d": [100, 100, 600, 700],
                "facts": [
                    {"attr": "gluten_free", "value_bool": True, "source": "label", "image_index": 0,
                     "box_2d": [100, 100, 300, 500], "quote": "SANS GLUTEN"},
                    {"attr": "vegan", "value_bool": True, "source": "label", "quote": "looks vegan"},
                    {"attr": "organic", "value_bool": True, "source": "spoken", "quote": "Tout est bio"},
                    {"attr": "local", "value_bool": True, "source": "spoken", "quote": "farm down the road"},
                ]}]}

    r = asyncio.run(read_shelf(FakeAI(), [img.getvalue()], None, tmp_path))
    [lot] = r.lots
    known = {f.attr: f.evidence for f in lot.facts}
    assert known[Attr.GLUTEN_FREE].startswith("crop_") and (tmp_path / f"{known[Attr.GLUTEN_FREE]}.jpg").exists()
    assert known[Attr.ORGANIC].startswith("say_")
    assert Attr.VEGAN not in known and Attr.LOCAL not in known
    assert len(lot.dropped) == 2


def test_model_outage_falls_back(tmp_path):
    class Down:
        last_source = None

        async def json(self, *a, **k):
            raise AIUnavailable("model unreachable")

    d = asyncio.run(compile_wish(Down(), "a baguette under 2 euros", HERE, "Gare", NOW))
    assert d.source == "rules" and d.terms is not None
