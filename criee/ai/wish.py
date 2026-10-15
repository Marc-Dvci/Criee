"""From a person's words to a typed, signable wish.

The model proposes a draft in a fixed schema. Code converts the draft into `WishTerms`, applies
bounds, and renders the read-back sentence from the typed terms. The sentence a person approves is
generated from exactly what they sign, never from model text.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from ..domain import (Attr, BOOL_ATTRS, Category, ClearingMode, Constraint, GeoPoint, Op, Weekday, WeeklyWindow,
                      WishTerms, money)
from .provider import AI, AIUnavailable

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
CATEGORY_WORDS = {
    Category.BREAD: "bread", Category.PASTRY: "pastries", Category.PRODUCE: "fruit and vegetables",
    Category.DAIRY: "dairy", Category.PREPARED_MEAL: "prepared meals", Category.FLOWERS: "flowers",
    Category.GROCERY_BOX: "grocery boxes", Category.SERVICE_SLOT: "appointment slots",
}
ATTR_WORDS = {
    Attr.ORGANIC: ("organic", "not organic"), Attr.VEGETARIAN: ("vegetarian", "not vegetarian"),
    Attr.VEGAN: ("vegan", "not vegan"), Attr.GLUTEN_FREE: ("gluten-free", "with gluten"),
    Attr.CONTAINS_NUTS: ("containing nuts", "nut-free"), Attr.CONTAINS_DAIRY: ("containing dairy", "dairy-free"),
    Attr.BAKED_TODAY: ("baked today", "not baked today"), Attr.LOCAL: ("locally produced", "not local"),
}
INT_WORDS = {Attr.WEIGHT_G: ("weighing", "g"), Attr.BEST_BEFORE_HOURS: ("good for", "h"),
             Attr.SERVINGS: ("serving", ""), Attr.DURATION_MIN: ("lasting", "min")}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "item_label": {"type": "string", "description": "2-5 words naming what the person wants, e.g. 'sourdough loaf'"},
        "category": {"type": "string", "enum": [c.value for c in Category]},
        "constraints": {"type": "array", "maxItems": 8, "items": {
            "type": "object",
            "properties": {"attr": {"type": "string", "enum": [a.value for a in Attr]},
                           "op": {"type": "string", "enum": [o.value for o in Op]},
                           "value_bool": {"type": "boolean", "nullable": True},
                           "value_int": {"type": "integer", "nullable": True}},
            "required": ["attr", "op"]}},
        "limit_eur": {"type": "number", "nullable": True, "description": "highest price per unit, euros"},
        "units": {"type": "integer", "minimum": 1, "maximum": 20},
        "max_matches_per_week": {"type": "integer", "minimum": 1, "maximum": 50, "nullable": True},
        "budget_eur_per_week": {"type": "number", "nullable": True},
        "radius_m": {"type": "integer", "nullable": True},
        "days": {"type": "array", "items": {"type": "string", "enum": DAY_NAMES}},
        "from_hhmm": {"type": "string", "nullable": True, "description": "earliest pickup time HH:MM"},
        "until_hhmm": {"type": "string", "nullable": True, "description": "latest pickup time HH:MM"},
        "modes": {"type": "array", "items": {"type": "string", "enum": [m.value for m in ClearingMode]}},
        "valid_days": {"type": "integer", "minimum": 1, "maximum": 180, "nullable": True},
        "questions": {"type": "array", "maxItems": 2, "items": {"type": "string"},
                      "description": "only when the price limit or the item is missing"},
    },
    "required": ["item_label", "category", "constraints", "units", "days", "modes", "questions"],
    "propertyOrdering": ["item_label", "category", "constraints", "limit_eur", "units", "max_matches_per_week",
                         "budget_eur_per_week", "radius_m", "days", "from_hhmm", "until_hhmm", "modes", "valid_days",
                         "questions"],
}

SYSTEM = """You turn a person's request into a standing purchase order for Criée, a market where local
sellers clear food and services that would otherwise go to waste. Fill the schema literally from what
the person said. Rules:
- category: the closest enum value. Croissants, pains au chocolat, cakes and tarts are pastry. A box,
  basket, crate or parcel of mixed fruit and vegetables is grocery_box, not produce.
- item_label: the item only, without the quantity ("croissants", not "two croissants").
- constraints: only properties the person explicitly asked for. "For one", "a dinner", "a loaf" are not
  constraints; never add servings, weight or freshness unless a number or condition was stated. Use op "is" with value_bool for yes/no properties,
  op "gte"/"lte" with value_int for numbers (weight_g in grams, best_before_hours, servings, duration_min).
- limit_eur: the highest price per unit they accept. Null if they gave none, and then ask one question.
- units: items per purchase (default 1). max_matches_per_week: how often per week (default 3).
- budget_eur_per_week: only if stated.
- days: the weekdays they can collect (default all seven). from_hhmm/until_hhmm: pickup hours if stated.
- modes: all three unless they exclude one ("only fixed price" -> threshold; "auction"/"best price" -> call, clock).
- valid_days: how long the order stands (default 30).
- radius_m: from "within X km" or "near me" (1500) or "in my neighbourhood" (1000); else null.
The person's words may be in French or English. Never invent a price."""


@dataclass
class Draft:
    terms: Optional[WishTerms]
    label: str
    readback: str
    questions: list[str] = field(default_factory=list)
    source: str = "rules"
    raw: Optional[dict] = None


def _hhmm(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:hH.](\d{2}))?\s*", s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return h * 60 + mi if 0 <= h <= 24 and 0 <= mi < 60 else None


def to_terms(d: dict, pickup: GeoPoint, now: datetime) -> tuple[Optional[WishTerms], list[str]]:
    questions = [q for q in (d.get("questions") or []) if isinstance(q, str)][:2]
    limit = d.get("limit_eur")
    if limit is None or not isinstance(limit, (int, float)) or limit <= 0:
        return None, questions or ["What is the most you would pay for one?"]
    limit_cents = int(round(min(float(limit), 500.0) * 100))
    units = max(1, min(20, int(d.get("units") or 1)))
    per_week = max(1, min(50, int(d.get("max_matches_per_week") or 3)))
    budget = d.get("budget_eur_per_week")
    budget_cents = int(round(float(budget) * 100)) if isinstance(budget, (int, float)) and budget > 0 else 0
    budget_cents = max(budget_cents, limit_cents * units)
    if not d.get("budget_eur_per_week"):
        budget_cents = limit_cents * units * per_week
    cons = []
    for c in d.get("constraints") or []:
        try:
            attr = Attr(c["attr"])
            if attr in BOOL_ATTRS:
                v = c.get("value_bool")
                cons.append(Constraint(attr=attr, op=Op.IS, value=True if v is None else bool(v)))
            elif c.get("value_int") is not None and c.get("op") in ("gte", "lte"):
                if c["op"] == "gte" and int(c["value_int"]) <= 1:
                    continue
                cons.append(Constraint(attr=attr, op=Op(c["op"]), value=int(c["value_int"])))
        except (KeyError, ValueError):
            continue
    seen, uniq = set(), []
    for c in cons:
        if (c.attr, c.op) not in seen:
            seen.add((c.attr, c.op))
            uniq.append(c)
    days = tuple(Weekday(DAY_NAMES.index(x)) for x in DAY_NAMES if x in (d.get("days") or DAY_NAMES))
    start, end = _hhmm(d.get("from_hhmm")), _hhmm(d.get("until_hhmm"))
    start = 0 if start is None else start
    end = 1440 if end is None or end <= start else end
    modes = tuple(ClearingMode(m) for m in (d.get("modes") or [m.value for m in ClearingMode])
                  if m in {x.value for x in ClearingMode}) or tuple(ClearingMode)
    radius = int(d.get("radius_m") or 2000)
    valid = int(d.get("valid_days") or 30)
    terms = WishTerms(category=Category(d["category"]), constraints=tuple(uniq), limit_cents=limit_cents,
                      units=units, max_matches_per_week=per_week, budget_cents_per_week=budget_cents,
                      pickup=pickup, radius_m=max(100, min(50_000, radius)),
                      windows=(WeeklyWindow(days=days or tuple(Weekday), start_min=start, end_min=end),),
                      modes=modes, expires_at=now + timedelta(days=max(1, min(180, valid))))
    return terms, []


def _days_phrase(days: tuple[Weekday, ...]) -> str:
    idx = sorted(d.value for d in days)
    if idx == list(range(7)):
        return "any day"
    if idx == [0, 1, 2, 3, 4]:
        return "Monday to Friday"
    if idx == [5, 6]:
        return "at weekends"
    if len(idx) > 2 and idx == list(range(idx[0], idx[-1] + 1)):
        return f"{DAY_LONG[idx[0]]} to {DAY_LONG[idx[-1]]}"
    names = [DAY_LONG[i] + "s" for i in idx]
    return "on " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0])


def _clock(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}" if m < 1440 else "midnight"


def readback(t: WishTerms, label: str, place: str) -> str:
    """The sentence a person approves before signing. Rendered only from typed terms."""
    qual = []
    for c in t.constraints:
        if c.attr in ATTR_WORDS:
            qual.append(ATTR_WORDS[c.attr][0 if c.value else 1])
        else:
            word, unit = INT_WORDS[c.attr]
            amount = f"{c.value / 1000:g} kg" if c.attr is Attr.WEIGHT_G and c.value >= 1000 else f"{c.value}{unit}"
            qual.append(f"{word} {'at least' if c.op is Op.GTE else 'at most'} {amount}".rstrip())
    low = label.lower()
    qual = [q for q in qual if q.lower() not in low]
    cat_word = CATEGORY_WORDS[t.category]
    detail = "; ".join(([] if cat_word.lower() in low or low in cat_word.lower() else [cat_word]) + qual)
    detail = f" ({detail})" if detail else ""
    what = f"{t.units} {label}" if t.units > 1 else f"one {label}"
    w = t.windows[0]
    hours = "" if (w.start_min, w.end_min) == (0, 1440) else f" between {_clock(w.start_min)} and {_clock(w.end_min)}"
    modes = {m.value for m in t.modes}
    how = "through any clearing" if modes == {m.value for m in ClearingMode} else         "only through " + " or ".join({"call": "end-of-day auctions", "clock": "falling-price clocks",
                                       "threshold": "group orders"}[m] for m in sorted(modes))
    km = f"{t.radius_m / 1000:.1f}".rstrip("0").rstrip(".")
    n = t.max_matches_per_week
    return (f"Buy {what}{detail} for at most €{money(t.limit_cents)} each, from sellers within {km} km of "
            f"{place}, collected {_days_phrase(w.days)}{hours}. No more than {n} purchase{'s' if n > 1 else ''} "
            f"or €{money(t.budget_cents_per_week)} a week, {how}, until {t.expires_at.day} "
            f"{t.expires_at.strftime('%B %Y')}. Paid from my saved PayPal when a match clears.")


# ----------------------------------------------------------------------------- rules path

RULES_CATEGORY = [
    (Category.GROCERY_BOX, r"box|panier|colis|basket|crate|parcel"),
    (Category.PASTRY, r"croissant|pain au chocolat|chocolatine|viennoiserie|pastr|tarte|tart|cake|gâteau|gateau|kouglof|kougelhopf|brioche|éclair|eclair|patisserie|pâtisserie"),
    (Category.BREAD, r"bread|pain|baguette|levain|sourdough|loaf|loaves|miche|bretzel|pretzel"),
    (Category.PRODUCE, r"vegetable|légume|legume|fruit|apple|pomme|tomato|salad|salade|veg\b|produce"),
    (Category.DAIRY, r"cheese|fromage|yogh?urt|yaourt|milk|lait|dairy|munster"),
    (Category.PREPARED_MEAL, r"meal|repas|dinner|dîner|lunch|déjeuner|plat|choucroute|tarte flambée|flammekueche|sushi"),
    (Category.FLOWERS, r"flower|fleur|bouquet"),
    (Category.SERVICE_SLOT, r"haircut|coupe|coiffeur|hairdresser|appointment|slot|massage|rendez"),
]
RULES_ATTR = [
    (Attr.GLUTEN_FREE, r"gluten[- ]?free|sans gluten"), (Attr.ORGANIC, r"organic|\bbio\b"),
    (Attr.VEGAN, r"\bvegan|végétalien"), (Attr.VEGETARIAN, r"vegetarian|végétarien|veggie"),
    (Attr.LOCAL, r"\blocal\b|du coin"), (Attr.BAKED_TODAY, r"baked today|du jour|fresh today"),
]


def parse_rules(text: str) -> dict:
    s = text.lower()
    cat = next((c for c, rx in RULES_CATEGORY if re.search(rx, s)), Category.BREAD)
    m = re.search(r"(?:€\s*(\d+(?:[.,]\d{1,2})?))|(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur|euros?)", s)
    limit = float((m.group(1) or m.group(2)).replace(",", ".")) if m else None
    cons = [{"attr": a.value, "op": "is", "value_bool": True} for a, rx in RULES_ATTR if re.search(rx, s)]
    if re.search(r"nut[- ]?free|sans (?:noix|fruits à coque)", s):
        cons.append({"attr": "contains_nuts", "op": "is", "value_bool": False})
    units_m = re.search(r"\b(\d{1,2})\s*(?:x\s*)?(?:croissants|loaves|baguettes|items|pieces|pastries|boxes)", s)
    words = {"once": 1, "twice": 2, "three times": 3, "four times": 4, "every day": 7, "daily": 7}
    per_week = next((v for k, v in words.items() if k in s), None)
    pw = re.search(r"(\d+)\s*(?:times|fois)\s*(?:a|per|par)\s*(?:week|semaine)", s)
    if pw:
        per_week = int(pw.group(1))
    if re.search(r"weekday|en semaine|monday to friday|lundi au vendredi", s):
        days = DAY_NAMES[:5]
    elif re.search(r"weekend|week-end", s):
        days = DAY_NAMES[5:]
    else:
        days = [d for d, rx in zip(DAY_NAMES, [r"monday|lundi", r"tuesday|mardi", r"wednesday|mercredi",
                                                 r"thursday|jeudi", r"friday|vendredi", r"saturday|samedi",
                                                 r"sunday|dimanche"]) if re.search(rx, s)] or DAY_NAMES
    after = re.search(r"(?:after|from|après|à partir de)\s*(\d{1,2})(?:[:h.](\d{2}))?\s*(pm)?", s)
    before = re.search(r"(?:before|until|by|avant|jusqu'à)\s*(\d{1,2})(?:[:h.](\d{2}))?\s*(pm)?", s)
    def _t(m):
        h = int(m.group(1)) + (12 if m.group(3) and int(m.group(1)) < 12 else 0)
        return f"{h:02d}:{m.group(2) or '00'}"
    km = re.search(r"(\d+(?:[.,]\d)?)\s*km", s)
    label_m = re.search(r"(?:a|an|some|\d+)\s+([a-zà-ÿ\- ]{3,30}?)(?:\s+(?:under|for|at|below|max|if|when|near|after|before|,|$))", s)
    return {
        "item_label": (label_m.group(1).strip() if label_m else CATEGORY_WORDS[cat]),
        "category": cat.value, "constraints": cons, "limit_eur": limit,
        "units": int(units_m.group(1)) if units_m else 1, "max_matches_per_week": per_week,
        "budget_eur_per_week": None, "radius_m": int(float(km.group(1).replace(",", ".")) * 1000) if km else None,
        "days": days,
        "from_hhmm": _t(after) if after else None,
        "until_hhmm": _t(before) if before else None,
        "modes": [m.value for m in ClearingMode], "valid_days": 30,
        "questions": [] if limit else ["What is the most you would pay for one?"],
    }


async def compile_wish(ai: Optional[AI], text: str, pickup: GeoPoint, place: str, now: datetime,
                       visitor: Optional[str] = None) -> Draft:
    text = text.strip()[:600]
    raw, source = None, "rules"
    if ai is not None:
        try:
            raw = await ai.json("wish", SYSTEM, [f"Person's words: {text}"], SCHEMA, visitor=visitor, thinking="LOW")
            source = ai.last_source or "live"
        except AIUnavailable:
            raw = None
    if raw is None:
        raw = parse_rules(text)
    label = re.sub(r"[^\w\s\-'’àâäéèêëîïôöùûüç]", "", str(raw.get("item_label") or "item"))[:40].strip() or "item"
    label = re.sub(r"^(?:\d+|one|two|three|four|five|six|a|an|un|une|deux|trois|quatre)\s+", "", label, flags=re.I) or label
    try:
        terms, questions = to_terms(raw, pickup, now)
    except (ValueError, KeyError) as e:
        return Draft(None, label, "", [f"I could not read that order ({type(e).__name__}). Could you rephrase it?"],
                     source, raw)
    if terms is None:
        return Draft(None, label, "", questions, source, raw)
    return Draft(terms, label, readback(terms, label, place), [], source, raw)
