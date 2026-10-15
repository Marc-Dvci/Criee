"""The settlement worker: turns settlement actions into PayPal calls and records the outcomes.

Criée is the merchant of record. A match takes a hold on the buyer's saved PayPal wallet; the
pickup scan captures it, attaches the pickup as order tracking, and pays the seller through
Payouts minus the platform fee. Holds for a threshold lot are taken in one concurrent burst.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from ..domain import ClearingMode, money, utcnow
from ..engine import MatchStatus
from .paypal import PayPalResult, auth_of
from .policy import Action, settlement_actions

if TYPE_CHECKING:
    from ..core import Core

log = logging.getLogger("criee.settlement")


class SettlementWorker:
    def __init__(self, core: "Core"):
        self.core = core

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except Exception:
                log.exception("settlement step failed")
            await self.core.wait_for_change(timeout=2.0)

    async def step(self) -> int:
        core = self.core
        acts = [a for a in settlement_actions(core.market, utcnow()) if (a.kind, a.match_id) not in core.inflight]
        payouts = self.payout_groups()
        await self.answer_disputes()
        if not acts and not payouts:
            await self.poll_payouts()
            return 0
        for a in acts:
            core.inflight.add((a.kind, a.match_id))
        bursts: dict[str, list[Action]] = defaultdict(list)
        single: list[Action] = []
        for a in acts:
            lot = core.market.lots[core.market.matches[a.match_id].fill.lot_id]
            if a.kind == "authorize" and lot.terms.mode is ClearingMode.THRESHOLD and lot.reached:
                bursts[lot.id].append(a)
            else:
                single.append(a)
        jobs = [self._burst(lot_id, group) for lot_id, group in bursts.items()]
        jobs += [self._do(a) for a in single]
        jobs += [self._payout(seller, mids) for seller, mids in payouts]
        await asyncio.gather(*jobs, return_exceptions=False)
        return len(acts) + len(payouts)

    async def _do(self, a: Action) -> float:
        try:
            if a.kind == "authorize":
                return await self.authorize(a.match_id)
            if a.kind == "capture":
                await self.capture(a.match_id)
            elif a.kind == "void":
                await self.void(a.match_id, a.paypal)
        except Exception as e:
            log.exception("%s %s failed", a.kind, a.match_id)
            if a.kind == "authorize":
                self.core.emit("settlement.auth_failed", {"match_id": a.match_id, "reason": type(e).__name__})
        finally:
            self.core.inflight.discard((a.kind, a.match_id))
        return 0.0

    async def _burst(self, lot_id: str, group: list[Action]) -> None:
        t0 = time.perf_counter()
        times = await asyncio.gather(*[self._do(a) for a in group])
        wall = (time.perf_counter() - t0) * 1000
        done = [t for t in times if t]
        if len(group) >= 2:
            self.core.emit("burst.measured", {
                "lot_id": lot_id, "holds": len(group), "authorized": len(done), "wall_ms": round(wall),
                "p50_ms": round(statistics.median(done)) if done else None,
                "max_ms": round(max(done)) if done else None})

    # ------------------------------------------------------------------ single operations

    async def authorize(self, mid: str) -> float:
        core = self.core
        m = core.market.matches[mid]
        if m.status is not MatchStatus.PENDING:
            return 0.0
        wish = core.market.wishes[m.fill.wish_id]
        vault = core.vault_for(wish.buyer_id)
        if not vault:
            core.emit("settlement.auth_failed", {"match_id": mid, "reason": "NO_SAVED_PAYPAL"})
            return 0.0
        cents = m.fill.units * m.fill.unit_cents
        res: PayPalResult = await core.paypal.authorize_vaulted(vault, cents, mid, core.describe_match(mid))
        auth = auth_of(res.body) if res.ok else {}
        if res.ok and auth.get("status") == "CREATED":
            core.emit("settlement.authorized", {"match_id": mid, "order_id": res.body.get("id"),
                                                "authorization_id": auth.get("id"), "cents": cents,
                                                "expires": auth.get("expiration_time"), "ms": round(res.ms),
                                                "debug_id": res.debug_id})
            return res.ms
        core.emit("settlement.auth_failed", {"match_id": mid, "reason": res.issue() or auth.get("status", "DENIED"),
                                             "debug_id": res.debug_id})
        return 0.0

    async def capture(self, mid: str) -> None:
        core = self.core
        m = core.market.matches[mid]
        s = core.book.s(mid)
        if m.status is not MatchStatus.HELD or not s.authorization_id:
            return
        cents = m.fill.units * m.fill.unit_cents
        res = await core.paypal.capture(s.authorization_id, cents, mid, invoice_id=mid)
        if not res.ok:
            core.emit("settlement.capture_failed", {"match_id": mid, "reason": res.issue(), "debug_id": res.debug_id})
            return
        core.emit("settlement.captured", {"match_id": mid, "capture_id": res.body.get("id"), "cents": cents,
                                          "ms": round(res.ms), "debug_id": res.debug_id})
        if m.pickup_code and s.order_id:
            tr = await core.paypal.track(s.order_id, res.body.get("id"), m.pickup_code, mid)
            if tr.ok:
                core.emit("settlement.tracked", {"match_id": mid, "carrier": "OTHER",
                                                 "tracking": f"CRIEE-{m.pickup_code}"})

    async def void(self, mid: str, paypal: bool) -> None:
        core = self.core
        s = core.book.s(mid)
        if paypal and s.authorization_id:
            res = await core.paypal.void(s.authorization_id, mid)
            if not res.ok and res.issue() not in ("AUTHORIZATION_ALREADY_VOIDED", "AUTHORIZATION_VOIDED"):
                core.emit("settlement.void_failed", {"match_id": mid, "reason": res.issue(), "debug_id": res.debug_id})
                return
        core.emit("settlement.voided", {"match_id": mid, "paypal": bool(paypal and s.authorization_id)})

    async def refund(self, mid: str, cents: int, reason: str) -> PayPalResult:
        core = self.core
        s = core.book.s(mid)
        if not s.capture_id:
            raise ValueError("nothing captured for this match")
        if cents <= 0 or cents > s.captured_cents - s.refunded_cents:
            raise ValueError("refund exceeds what was captured")
        n = len([e for e in core.ledger.events(types=["settlement.refunded"]) if e.body["match_id"] == mid])
        res = await core.paypal.refund(s.capture_id, cents, f"{mid}-r{n}", reason)
        if res.ok:
            m = core.market.matches[mid]
            core.emit("settlement.refunded", {"match_id": mid, "refund_id": res.body.get("id"), "cents": cents,
                                              "reason": reason[:120], "seller_id": core.market.lots[m.fill.lot_id].seller_id,
                                              "full": s.refunded_cents + cents >= s.captured_cents})
        return res

    async def answer_disputes(self) -> None:
        """Criée is the merchant of record: it answers each dispute with the evidence pack from the ledger."""
        from ..api.disputes import send_evidence_pack
        core = self.core
        for d in list(core.book.disputes.values()):
            key = ("evidence", d.dispute_id)
            if d.evidence_sent or not d.match_id or d.status == "RESOLVED" or key in core.inflight:
                continue
            tries = int(core.ledger.kv_get(f"evidence_tries:{d.dispute_id}") or 0)
            last = float(core.ledger.kv_get(f"evidence_last:{d.dispute_id}") or 0)
            if tries >= 6 or (tries and time.time() - last < 300 * tries):
                continue
            core.inflight.add(key)
            try:
                res = await send_evidence_pack(core, d.dispute_id)
                if not res.get("ok"):
                    core.ledger.kv_set(f"evidence_tries:{d.dispute_id}", str(tries + 1))
                    core.ledger.kv_set(f"evidence_last:{d.dispute_id}", str(time.time()))
                    log.warning("evidence for %s refused: %s", d.dispute_id, res)
            except Exception:
                log.exception("evidence for %s failed", d.dispute_id)
            finally:
                core.inflight.discard(key)

    # ------------------------------------------------------------------ payouts

    def payout_groups(self) -> list[tuple[str, list[str]]]:
        """Captured, unpaid matches grouped by seller. A threshold lot is paid once, when all its holds settled."""
        core = self.core
        groups: dict[str, list[str]] = defaultdict(list)
        for mid, s in core.book.settle.items():
            if not s.capture_id or s.payout_id or ("payout", mid) in core.inflight:
                continue
            m = core.market.matches.get(mid)
            if not m:
                continue
            lot = core.market.lots[m.fill.lot_id]
            if lot.terms.mode is ClearingMode.THRESHOLD:
                busy = any(core.market.matches[x].status in (MatchStatus.PENDING, MatchStatus.HELD)
                           for x in core.market.by_lot.get(lot.id, []))
                if busy:
                    continue
            groups[lot.seller_id].append(mid)
        out = []
        for seller, mids in groups.items():
            for mid in mids:
                core.inflight.add(("payout", mid))
            out.append((seller, sorted(mids)))
        return out

    async def _payout(self, seller_id: str, mids: list[str]) -> None:
        core = self.core
        try:
            gross = sum(core.book.s(m).captured_cents - core.book.s(m).refunded_cents for m in mids)
            fee = round(gross * core.cfg.fee_bps / 10_000)
            debit = min(core.book.seller_debit.get(seller_id, 0), max(0, gross - fee))
            net = gross - fee - debit
            email = core.payout_email_for(seller_id)
            pid = "po_" + mids[0].split("_")[1] + f"{len(mids):02d}"
            batch = None
            if net > 0 and email:
                res = await core.paypal.payout(email, net, pid, f"Criée sales: {len(mids)} item(s), fee {money(fee)}")
                if not res.ok:
                    log.warning("payout failed: %s", res.issue())
                    return
                batch = res.body.get("batch_header", {}).get("payout_batch_id")
            core.emit("payout.sent", {"payout_id": pid, "seller_id": seller_id, "match_ids": mids, "gross": gross,
                                      "fee": fee, "debit": debit, "net": net, "batch_id": batch})
        finally:
            for m in mids:
                core.inflight.discard(("payout", m))

    async def poll_payouts(self) -> None:
        core = self.core
        for p in list(core.book.payouts.values()):
            if p.status in ("PENDING", "PROCESSING") and p.batch_id and ("poll", p.payout_id) not in core.inflight:
                if p.at and (utcnow() - p.at).total_seconds() < 5:
                    continue
                core.inflight.add(("poll", p.payout_id))
                try:
                    res = await core.paypal.get_payout(p.batch_id)
                    status = res.body.get("batch_header", {}).get("batch_status")
                    if res.ok and status and status not in ("PENDING", "PROCESSING"):
                        core.emit("payout.settled", {"payout_id": p.payout_id, "batch_id": p.batch_id,
                                                     "status": status})
                finally:
                    core.inflight.discard(("poll", p.payout_id))
            elif p.status in ("PENDING",) and not p.batch_id:
                core.emit("payout.settled", {"payout_id": p.payout_id, "batch_id": None, "status": "NOTHING_DUE"})
