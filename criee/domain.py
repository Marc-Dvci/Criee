"""Typed domain objects.

Two layers live here. *Terms* types (WishTerms, LotTerms, ...) are what the clearing engine reads:
they contain enums, integers, coordinates, timestamps and identifiers only. *Listing* types carry
the human-facing text (product names, the seller's notes) and never enter the engine.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

ID_PATTERN = r"^[a-z]{1,8}_[0-9a-z]{6,32}$"
Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
Cents = Annotated[int, Field(ge=0, le=10_000_000)]


def new_id(prefix: str) -> str:
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    return f"{prefix}_" + "".join(secrets.choice(alphabet) for _ in range(12))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ----------------------------------------------------------------------------- vocabulary

class Category(str, Enum):
    BREAD = "bread"
    PASTRY = "pastry"
    PRODUCE = "produce"
    DAIRY = "dairy"
    PREPARED_MEAL = "prepared_meal"
    FLOWERS = "flowers"
    GROCERY_BOX = "grocery_box"
    SERVICE_SLOT = "service_slot"


class ClearingMode(str, Enum):
    CALL = "call"            # uniform-price call auction at the deadline
    CLOCK = "clock"          # descending clock until sold out or the deadline
    THRESHOLD = "threshold"  # fixed price, happens only if Q units are committed by the deadline


class Attr(str, Enum):
    """Attributes a fact can state and a wish can constrain. Each has one value type."""
    ORGANIC = "organic"
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    GLUTEN_FREE = "gluten_free"
    CONTAINS_NUTS = "contains_nuts"
    CONTAINS_DAIRY = "contains_dairy"
    BAKED_TODAY = "baked_today"
    LOCAL = "local"
    WEIGHT_G = "weight_g"
    BEST_BEFORE_HOURS = "best_before_hours"
    SERVINGS = "servings"
    DURATION_MIN = "duration_min"


BOOL_ATTRS = {Attr.ORGANIC, Attr.VEGETARIAN, Attr.VEGAN, Attr.GLUTEN_FREE, Attr.CONTAINS_NUTS,
              Attr.CONTAINS_DAIRY, Attr.BAKED_TODAY, Attr.LOCAL}
INT_ATTRS = {Attr.WEIGHT_G, Attr.BEST_BEFORE_HOURS, Attr.SERVINGS, Attr.DURATION_MIN}


class Op(str, Enum):
    IS = "is"    # boolean equality
    GTE = "gte"
    LTE = "lte"


class Weekday(int, Enum):
    MON = 0
    TUE = 1
    WED = 2
    THU = 3
    FRI = 4
    SAT = 5
    SUN = 6


# ----------------------------------------------------------------------------- engine inputs

class Constraint(Frozen):
    attr: Attr
    op: Op
    value: Union[bool, int]

    @model_validator(mode="after")
    def _typed(self) -> "Constraint":
        if self.attr in BOOL_ATTRS:
            if self.op is not Op.IS or not isinstance(self.value, bool):
                raise ValueError(f"{self.attr.value} takes op 'is' with a boolean")
        else:
            if self.op is Op.IS or isinstance(self.value, bool) or not isinstance(self.value, int):
                raise ValueError(f"{self.attr.value} takes op 'gte' or 'lte' with an integer")
        return self


class Fact(Frozen):
    """A statement about an item. `value` is None when the fact is unknown.

    A known value needs evidence: the id of the image crop it was read from, or the seller's
    explicit confirmation. A fact with no evidence is stored as unknown.
    """
    attr: Attr
    value: Optional[Union[bool, int]] = None
    evidence: Optional[Id] = None

    @model_validator(mode="before")
    @classmethod
    def _no_evidence_no_value(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("evidence") is None:
            data = {**data, "value": None}
        return data

    @model_validator(mode="after")
    def _typed(self) -> "Fact":
        if self.value is not None:
            if self.attr in BOOL_ATTRS and not isinstance(self.value, bool):
                raise ValueError(f"{self.attr.value} is boolean")
            if self.attr in INT_ATTRS and (isinstance(self.value, bool) or not isinstance(self.value, int)):
                raise ValueError(f"{self.attr.value} is an integer")
        return self


class GeoPoint(Frozen):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class WeeklyWindow(Frozen):
    """A recurring local-time window: on these weekdays, from start_min to end_min after midnight."""
    days: tuple[Weekday, ...] = Field(min_length=1, max_length=7)
    start_min: int = Field(ge=0, le=1440)
    end_min: int = Field(ge=0, le=1440)

    @model_validator(mode="after")
    def _order(self) -> "WeeklyWindow":
        if self.end_min <= self.start_min:
            raise ValueError("window must end after it starts")
        return self


class WishTerms(Frozen):
    """The intent mandate the buyer signs. Everything the engine needs, nothing else."""
    category: Category
    constraints: tuple[Constraint, ...] = Field(default=(), max_length=12)
    limit_cents: Cents = Field(gt=0)
    units: int = Field(ge=1, le=20)
    max_matches_per_week: int = Field(ge=1, le=50)
    budget_cents_per_week: Cents = Field(gt=0)
    pickup: GeoPoint
    radius_m: int = Field(ge=100, le=50_000)
    windows: tuple[WeeklyWindow, ...] = Field(min_length=1, max_length=14)
    modes: tuple[ClearingMode, ...] = Field(min_length=1, max_length=3)
    expires_at: datetime
    timezone: Literal["Europe/Paris"] = "Europe/Paris"

    @field_validator("modes")
    @classmethod
    def _unique_modes(cls, v: tuple[ClearingMode, ...]) -> tuple[ClearingMode, ...]:
        return tuple(sorted(set(v), key=lambda m: m.value))

    @model_validator(mode="after")
    def _budget(self) -> "WishTerms":
        if self.budget_cents_per_week < self.limit_cents * self.units:
            raise ValueError("weekly budget must cover at least one match")
        return self


class LotTerms(Frozen):
    """What the seller offers, as the engine sees it."""
    category: Category
    facts: tuple[Fact, ...] = Field(default=(), max_length=24)
    quantity: int = Field(ge=1, le=500)
    mode: ClearingMode
    start_cents: Cents = Field(gt=0)
    floor_cents: Cents = Field(gt=0)
    opens_at: datetime
    closes_at: datetime
    pickup: GeoPoint
    pickup_from: datetime
    pickup_until: datetime
    threshold_qty: Optional[int] = Field(default=None, ge=2, le=500)
    clock_step_s: int = Field(default=60, ge=5, le=3600)

    @model_validator(mode="after")
    def _shape(self) -> "LotTerms":
        if self.floor_cents > self.start_cents:
            raise ValueError("floor must not exceed the start price")
        if self.closes_at <= self.opens_at:
            raise ValueError("lot must close after it opens")
        if self.pickup_until <= self.pickup_from:
            raise ValueError("pickup window must end after it starts")
        if self.mode is ClearingMode.THRESHOLD:
            if self.threshold_qty is None or self.threshold_qty > self.quantity:
                raise ValueError("a threshold lot needs threshold_qty <= quantity")
            if self.floor_cents != self.start_cents:
                raise ValueError("a threshold lot has one fixed price")
        return self

    def fact(self, attr: Attr) -> Optional[Union[bool, int]]:
        for f in self.facts:
            if f.attr is attr:
                return f.value
        return None


# ----------------------------------------------------------------------------- engine outputs

class Fill(Frozen):
    """One match decided by the engine."""
    match_id: Id
    wish_id: Id
    lot_id: Id
    units: int = Field(ge=1)
    unit_cents: Cents
    rule: ClearingMode
    round: int = Field(ge=0)   # clock tick index, 0 for a call auction, commit order for threshold


# ----------------------------------------------------------------------------- listings (never in the engine)

class Crop(Frozen):
    """An image region a fact was read from. Box is [ymin, xmin, ymax, xmax] on a 0-1000 grid."""
    id: Id
    image_id: Id
    box: tuple[int, int, int, int]
    label: Annotated[str, StringConstraints(max_length=120)]


class LotListing(BaseModel):
    """Presentation data attached to a lot: shown to people, ignored by the engine."""
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    name_fr: Optional[Annotated[str, StringConstraints(max_length=80)]] = None
    unit_label: Annotated[str, StringConstraints(max_length=30)] = "item"
    crops: list[Crop] = Field(default_factory=list)
    image_id: Optional[Id] = None


SHORT_THRESHOLD_WINDOW_S = 3 * 24 * 3600  # PayPal's honor period: holds can be taken at commitment


def is_short_threshold(terms: LotTerms) -> bool:
    return (terms.closes_at - terms.opens_at).total_seconds() <= SHORT_THRESHOLD_WINDOW_S


def money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def parse_money(value: str) -> int:
    m = re.fullmatch(r"\s*(\d{1,6})(?:[.,](\d{1,2}))?\s*", value)
    if not m:
        raise ValueError(f"not an amount: {value!r}")
    frac = (m.group(2) or "0").ljust(2, "0")
    return int(m.group(1)) * 100 + int(frac)
