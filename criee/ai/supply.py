"""From a filmed shelf and the seller's voice to draft lots.

The model reads the frames and the spoken note and proposes items with facts. A fact read from a
label is kept only with the box it was read from, which becomes a stored image crop; a fact the
seller said is kept only when the quoted words appear in the transcript. Everything else is stored
as unknown, and the seller confirms each lot before it goes live.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageOps

from ..domain import Attr, BOOL_ATTRS, Category, ClearingMode, Crop, Fact, new_id
from .provider import AI, AIUnavailable, Media

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string", "description": "verbatim transcript of the audio, empty if none"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "short English product name"},
                "name_fr": {"type": "string", "description": "French name as on the label or as said"},
                "category": {"type": "string", "enum": [c.value for c in Category]},
                "quantity": {"type": "integer", "minimum": 1, "maximum": 200},
                "unit_label": {"type": "string", "description": "loaf, piece, box, slice, bunch, slot..."},
                "price_eur": {"type": "number", "nullable": True},
                "price_source": {"type": "string", "enum": ["label", "spoken", "none"]},
                "image_index": {"type": "integer", "minimum": 0},
                "box_2d": {"type": "array",
                           "items": {"type": "integer", "minimum": 0, "maximum": 1000}},
                "facts": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "attr": {"type": "string", "enum": [a.value for a in Attr]},
                        "value_bool": {"type": "boolean", "nullable": True},
                        "value_int": {"type": "integer", "nullable": True},
                        "source": {"type": "string", "enum": ["label", "spoken"]},
                        "image_index": {"type": "integer", "nullable": True},
                        "box_2d": {"type": "array", "nullable": True,
                                   "items": {"type": "integer", "minimum": 0, "maximum": 1000}},
                        "quote": {"type": "string", "description": "the exact words read on the label or heard"}},
                    "required": ["attr", "source", "quote"]}}},
            "required": ["name", "name_fr", "category", "quantity", "unit_label", "price_source", "image_index",
                         "box_2d", "facts"]}},
        "floor_eur": {"type": "number", "nullable": True, "description": "lowest price the seller said they accept"},
        "close_hhmm": {"type": "string", "nullable": True, "description": "time the seller wants to be sold out by"},
        "mode_hint": {"type": "string", "enum": ["call", "clock", "threshold", "none"]},
    },
    "required": ["transcript", "items", "mode_hint"],
}

SYSTEM = """You help a small food seller in Strasbourg list what is left on the shelf before closing.
You receive photos of the shelf (indexed from 0) and possibly a voice note in French or English.
List each distinct product once, with how many units you can count and the box_2d of the group of units
in the image where it is clearest. box_2d is [ymin, xmin, ymax, xmax] normalised to 0-1000.
Facts: only record a property when it is printed on a visible label or sign (source "label": give the
image_index and the box_2d of the printed words and quote them), or when the seller says it (source
"spoken": quote their words exactly as in the transcript). Never infer a fact from appearance: a loaf
that looks wholemeal is not organic. weight_g in grams, best_before_hours in hours.
Prices: from a price sign (label) or the voice note (spoken). Spoken quantities override counted ones.
floor_eur: only if the seller says the lowest price they will accept. mode_hint: "threshold" if they talk
about a minimum number of orders, "call" for a single end-of-day sale, "clock" for a falling price."""


@dataclass
class DraftLot:
    name: str
    name_fr: Optional[str]
    category: Category
    quantity: int
    unit_label: str
    price_cents: Optional[int]
    price_source: str
    image_id: Optional[str]
    box: Optional[tuple[int, int, int, int]]
    facts: list[Fact] = field(default_factory=list)
    crops: list[Crop] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)   # facts the model proposed without evidence


@dataclass
class SupplyRead:
    lots: list[DraftLot]
    transcript: str
    floor_cents: Optional[int]
    close_hhmm: Optional[str]
    mode_hint: Optional[ClearingMode]
    source: str
    image_ids: list[str]


def normalise_image(data: bytes, max_side: int = 1280) -> bytes:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def cut_crop(jpeg: bytes, box: tuple[int, int, int, int], pad: int = 12) -> bytes:
    img = Image.open(io.BytesIO(jpeg))
    w, h = img.size
    y0, x0, y1, x1 = box
    l, t = max(0, x0 * w // 1000 - pad), max(0, y0 * h // 1000 - pad)
    r, b = min(w, x1 * w // 1000 + pad), min(h, y1 * h // 1000 + pad)
    if r - l < 8 or b - t < 8:
        raise ValueError("empty crop")
    out = io.BytesIO()
    img.crop((l, t, r, b)).save(out, "JPEG", quality=88)
    return out.getvalue()


def _valid_box(b: Any) -> Optional[tuple[int, int, int, int]]:
    if isinstance(b, list) and len(b) == 4 and all(isinstance(v, int) for v in b):
        y0, x0, y1, x1 = (max(0, min(1000, v)) for v in b)
        if y1 > y0 and x1 > x0:
            return (y0, x0, y1, x1)
    return None


async def read_shelf(ai: Optional[AI], images: list[bytes], audio: Optional[Media], store: Path,
                     visitor: Optional[str] = None) -> SupplyRead:
    """`images` are raw uploads; they are normalised to JPEG and stored under `store`."""
    store.mkdir(parents=True, exist_ok=True)
    jpegs = [normalise_image(b) for b in images[:4]]
    image_ids = []
    for j in jpegs:
        iid = "img_" + hashlib.sha256(j).hexdigest()[:16]
        (store / f"{iid}.jpg").write_bytes(j)
        image_ids.append(iid)
    raw, source = None, "manual"
    if ai is not None and jpegs:
        parts: list = [Media("image/jpeg", j) for j in jpegs]
        if audio is not None:
            parts.append(audio)
        parts.append("List the products on this shelf." + (" The voice note is the seller speaking." if audio else ""))
        try:
            raw = await ai.json("supply", SYSTEM, parts, SCHEMA, visitor=visitor, thinking="LOW")
            source = ai.last_source or "live"
        except AIUnavailable:
            raw = None
    if raw is None:
        return SupplyRead([DraftLot("", None, Category.BREAD, 1, "item", None, "none",
                                    image_ids[0] if image_ids else None, None)], "", None, None, None, source, image_ids)
    transcript = str(raw.get("transcript") or "")
    lots = []
    for it in (raw.get("items") or [])[:12]:
        try:
            idx = int(it.get("image_index") or 0)
            image_id = image_ids[idx] if 0 <= idx < len(image_ids) else (image_ids[0] if image_ids else None)
            lot = DraftLot(name=str(it["name"])[:80], name_fr=str(it.get("name_fr") or "")[:80] or None,
                           category=Category(it["category"]), quantity=max(1, min(200, int(it["quantity"]))),
                           unit_label=str(it.get("unit_label") or "item")[:30],
                           price_cents=int(round(float(it["price_eur"]) * 100)) if it.get("price_eur") else None,
                           price_source=it.get("price_source") or "none", image_id=image_id,
                           box=_valid_box(it.get("box_2d")))
        except (KeyError, ValueError, TypeError):
            continue
        seen: set[Attr] = set()
        for f in (it.get("facts") or [])[:8]:
            try:
                attr = Attr(f["attr"])
            except (KeyError, ValueError):
                continue
            if attr in seen:
                continue
            value = f.get("value_bool") if attr in BOOL_ATTRS else f.get("value_int")
            if attr in BOOL_ATTRS and value is None:
                value = True
            quote = str(f.get("quote") or "")[:120]
            evidence = None
            if f.get("source") == "label":
                box = _valid_box(f.get("box_2d"))
                fi = f.get("image_index")
                fi = int(fi) if isinstance(fi, int) and 0 <= fi < len(jpegs) else idx
                if box is not None and 0 <= fi < len(jpegs):
                    try:
                        crop_bytes = cut_crop(jpegs[fi], box)
                        cid = "crop_" + hashlib.sha256(crop_bytes).hexdigest()[:16]
                        (store / f"{cid}.jpg").write_bytes(crop_bytes)
                        lot.crops.append(Crop(id=cid, image_id=image_ids[fi], box=box, label=quote or attr.value))
                        evidence = cid
                    except (ValueError, OSError):
                        evidence = None
            elif f.get("source") == "spoken" and quote and quote.lower().strip(" .") in transcript.lower():
                evidence = "say_" + hashlib.sha256(quote.encode()).hexdigest()[:16]
            if evidence is None or value is None:
                lot.dropped.append(f"{attr.value}: {quote}" if quote else attr.value)
                continue
            try:
                lot.facts.append(Fact(attr=attr, value=value, evidence=evidence))
                seen.add(attr)
            except ValueError:
                lot.dropped.append(attr.value)
        lots.append(lot)
    mode = raw.get("mode_hint")
    floor = raw.get("floor_eur")
    return SupplyRead(lots, transcript, int(round(float(floor) * 100)) if isinstance(floor, (int, float)) and floor > 0 else None,
                      raw.get("close_hhmm"), ClearingMode(mode) if mode in ("call", "clock", "threshold") else None,
                      source, image_ids)
