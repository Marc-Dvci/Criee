"""The running service: ledger, market, settlement book, clocks and the live event stream."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .canonical import digest, iso
from .config import Config
from .domain import LotListing, LotTerms, WishTerms, new_id, utcnow
from .engine import Market, run_until
from .ledger import Event, Ledger, Signer

log = logging.getLogger("criee")


@dataclass
class Settle:
    order_id: Optional[str] = None
    authorization_id: Optional[str] = None
    capture_id: Optional[str] = None
    captured_cents: int = 0
    refunded_cents: int = 0
    payout_id: Optional[str] = None
    tracked: bool = False
    auth_ms: Optional[float] = None


@dataclass
class Payout:
    payout_id: str
    seller_id: str
    match_ids: list[str]
    gross: int
    fee: int
    debit: int
    net: int
    batch_id: Optional[str] = None
    status: str = "PENDING"
    at: Optional[datetime] = None


@dataclass
class Dispute:
    dispute_id: str
    match_id: Optional[str]
    reason: str
    status: str
    at: datetime
    evidence_sent: bool = False
    outcome: Optional[str] = None
    cents: int = 0


@dataclass
class Book:
    """Settlement facts folded from the ledger (what PayPal has done for each match)."""
    settle: dict[str, Settle] = field(default_factory=dict)
    payouts: dict[str, Payout] = field(default_factory=dict)
    seller_debit: dict[str, int] = field(default_factory=dict)
    disputes: dict[str, Dispute] = field(default_factory=dict)
    by_capture: dict[str, str] = field(default_factory=dict)
    bursts: list[dict] = field(default_factory=list)
    webhooks_seen: set[str] = field(default_factory=set)

    def s(self, mid: str) -> Settle:
        return self.settle.setdefault(mid, Settle())

    def apply(self, ev: Event) -> None:
        b, t = ev.body, ev.type
        if t == "settlement.authorized":
            s = self.s(b["match_id"])
            s.order_id, s.authorization_id, s.auth_ms = b.get("order_id"), b.get("authorization_id"), b.get("ms")
        elif t == "settlement.captured":
            s = self.s(b["match_id"])
            s.capture_id, s.captured_cents = b.get("capture_id"), b.get("cents", 0)
            if s.capture_id:
                self.by_capture[s.capture_id] = b["match_id"]
        elif t == "settlement.tracked":
            self.s(b["match_id"]).tracked = True
        elif t == "settlement.refunded":
            s = self.s(b["match_id"])
            s.refunded_cents += b["cents"]
            if s.payout_id and b.get("seller_id"):
                self.seller_debit[b["seller_id"]] = self.seller_debit.get(b["seller_id"], 0) + b["cents"]
        elif t == "payout.sent":
            p = Payout(b["payout_id"], b["seller_id"], b["match_ids"], b["gross"], b["fee"], b["debit"], b["net"],
                       b.get("batch_id"), "PENDING", ev.at)
            self.payouts[p.payout_id] = p
            for mid in p.match_ids:
                self.s(mid).payout_id = p.payout_id
            if p.debit:
                self.seller_debit[p.seller_id] = max(0, self.seller_debit.get(p.seller_id, 0) - p.debit)
        elif t == "payout.settled":
            p = self.payouts.get(b["payout_id"])
            if p:
                p.status = b["status"]
        elif t == "dispute.opened":
            self.disputes[b["dispute_id"]] = Dispute(b["dispute_id"], b.get("match_id"), b.get("reason", ""),
                                                     b.get("status", "OPEN"), ev.at, cents=b.get("cents", 0))
        elif t == "dispute.evidence_sent":
            d = self.disputes.get(b["dispute_id"])
            if d:
                d.evidence_sent, d.status = True, "UNDER_REVIEW"
        elif t == "dispute.resolved":
            d = self.disputes.get(b["dispute_id"])
            if d:
                d.status, d.outcome = "RESOLVED", b.get("outcome")
        elif t == "burst.measured":
            self.bursts.append({**b, "at": iso(ev.at)})
        elif t == "webhook.received":
            self.webhooks_seen.add(b["event_id"])


@dataclass
class Account:
    id: str
    handle: str
    roles: set[str]
    simulated: bool = False
    vaulted: bool = False
    shop: Optional[dict] = None


class Core:
    def __init__(self, cfg: Config, paypal: Any, ai: Any = None, ledger: Optional[Ledger] = None):
        self.cfg = cfg
        self.ledger = ledger or Ledger(cfg.db_path, Signer.load_or_create(cfg.data_dir / "ledger.key"))
        self.paypal = paypal
        self.ai = ai
        self.market = Market(self.seed_for)
        self.book = Book()
        self.accounts: dict[str, Account] = {}
        self.listings: dict[str, LotListing] = {}
        self.wish_meta: dict[str, dict] = {}
        self.inflight: set[tuple[str, str]] = set()
        self._subs: set[asyncio.Queue] = set()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        for ev in self.ledger.events():
            self._fold(ev)

    # ------------------------------------------------------------------ events

    def _fold(self, ev: Event) -> None:
        self.market.apply(ev.seq, ev.type, ev.body, ev.at)
        self.book.apply(ev)
        b = ev.body
        if ev.type == "account.created":
            self.accounts[b["account_id"]] = Account(b["account_id"], b["handle"], set(b["roles"]),
                                                     b.get("simulated", False))
        elif ev.type == "account.role_added":
            self.accounts[b["account_id"]].roles.add(b["role"])
        elif ev.type == "buyer.vaulted":
            self.accounts[b["account_id"]].vaulted = True
        elif ev.type == "seller.shop":
            self.accounts[b["account_id"]].shop = b["shop"]
        elif ev.type == "wish.signed":
            self.wish_meta[b["wish_id"]] = {"label": b.get("label") or "item", "readback": b.get("readback"),
                                            "place": b.get("place"), "terms_hash": b.get("terms_hash"),
                                            "signature": b.get("signature"), "seq": ev.seq}
        elif ev.type == "lot.listed":
            self.listings[b["lot_id"]] = LotListing.model_validate(b["listing"]) if b.get("listing") else \
                LotListing(name="Lot")

    def emit(self, type_: str, body: dict[str, Any], at: Optional[datetime] = None) -> Event:
        ev = self.ledger.append(type_, body, at or utcnow())
        self._fold(ev)
        self._broadcast(ev)
        self._wake.set()
        return ev

    def _append_engine(self, evs: list) -> list[Event]:
        out = self.ledger.append_many(evs)
        for ev in out:
            self.book.apply(ev)
            self._broadcast(ev)
        self._wake.set()
        return out

    def tick(self, now: Optional[datetime] = None) -> int:
        return run_until(self.market, now or utcnow(), self._append_engine)

    def _broadcast(self, ev: Event) -> None:
        msg = ev.public()
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self._subs.discard(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    # ------------------------------------------------------------------ seeds and secrets

    def seed_for(self, lot_id: str) -> str:
        s = self.ledger.kv_get(f"seed:{lot_id}")
        if s is None:
            s = secrets.token_hex(16)
            self.ledger.kv_set(f"seed:{lot_id}", s)
        return s

    def vault_for(self, account_id: str) -> Optional[str]:
        acc = self.accounts.get(account_id)
        if acc and acc.simulated:
            return self.ledger.kv_get("vault:demo") or self.cfg.demo_vault_id
        return self.ledger.kv_get(f"vault:{account_id}")

    def payout_email_for(self, seller_id: str) -> Optional[str]:
        return self.ledger.kv_get(f"payout:{seller_id}") or self.cfg.seller_payout_email

    # ------------------------------------------------------------------ commands

    def create_account(self, handle: str, roles: list[str], simulated: bool = False) -> Account:
        aid = new_id("acct")
        self.emit("account.created", {"account_id": aid, "handle": handle[:40], "roles": sorted(roles),
                                      "simulated": simulated})
        return self.accounts[aid]

    def sign_wish(self, buyer_id: str, terms: WishTerms, signature: dict, simulated: bool = False,
                  at: Optional[datetime] = None, readback: Optional[str] = None, label: str = "item",
                  place: Optional[str] = None) -> str:
        wid = new_id("w")
        self.emit("wish.signed", {"wish_id": wid, "buyer_id": buyer_id, "terms": terms.model_dump(mode="json"),
                                  "terms_hash": digest(terms), "signature": signature, "readback": readback,
                                  "label": label[:40], "place": (place or "")[:60], "simulated": simulated}, at)
        self.tick()
        return wid

    def list_lot(self, seller_id: str, terms: LotTerms, listing: LotListing, simulated: bool = False,
                 at: Optional[datetime] = None) -> str:
        lid = new_id("lot")
        seed = self.seed_for(lid)
        self.emit("lot.listed", {"lot_id": lid, "seller_id": seller_id, "terms": terms.model_dump(mode="json"),
                                 "listing": listing.model_dump(mode="json"),
                                 "seed_commit": hashlib.sha256(seed.encode()).hexdigest(),
                                 "simulated": simulated}, at)
        self.tick()
        return lid

    # ------------------------------------------------------------------ background loops

    async def start(self) -> None:
        from .settlement.worker import SettlementWorker
        self.worker = SettlementWorker(self)
        self._tasks = [asyncio.create_task(self._clock_loop(), name="clock"),
                       asyncio.create_task(self.worker.run(), name="settlement")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _clock_loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("clock tick failed")
            await asyncio.sleep(1.0)

    async def wait_for_change(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def describe_match(self, mid: str) -> str:
        m = self.market.matches[mid]
        listing = self.listings.get(m.fill.lot_id)
        name = listing.name if listing else "lot"
        return f"Criée: {m.fill.units} x {name}"
