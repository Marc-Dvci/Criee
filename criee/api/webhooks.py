"""PayPal webhooks: verified, de-duplicated by event id, and folded into the ledger."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..core import Core

log = logging.getLogger("criee.webhooks")
router = APIRouter()

EVENTS = ["CHECKOUT.ORDER.COMPLETED", "PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.AUTHORIZATION.VOIDED",
          "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.REFUNDED",
          "PAYMENT.CAPTURE.REVERSED", "VAULT.PAYMENT-TOKEN.CREATED", "VAULT.PAYMENT-TOKEN.DELETED",
          "PAYMENT.PAYOUTSBATCH.SUCCESS", "PAYMENT.PAYOUTSBATCH.DENIED", "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
          "PAYMENT.PAYOUTS-ITEM.FAILED", "CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED",
          "CUSTOMER.DISPUTE.RESOLVED"]


def dispute_capture_id(resource: dict) -> str | None:
    for t in resource.get("disputed_transactions") or []:
        if t.get("seller_transaction_id"):
            return t["seller_transaction_id"]
    return None


def apply_dispute(core: Core, resource: dict, event_type: str) -> None:
    did = resource.get("dispute_id")
    if not did:
        return
    known = core.book.disputes.get(did)
    if known is None and event_type != "CUSTOMER.DISPUTE.RESOLVED":
        cap = dispute_capture_id(resource)
        mid = core.book.by_capture.get(cap) if cap else None
        amount = resource.get("dispute_amount") or {}
        cents = int(round(float(amount.get("value", "0")) * 100))
        core.emit("dispute.opened", {"dispute_id": did, "match_id": mid, "capture_id": cap,
                                     "reason": resource.get("reason", ""), "status": resource.get("status", ""),
                                     "stage": resource.get("dispute_life_cycle_stage", ""), "cents": cents})
    status = resource.get("status")
    if status == "RESOLVED" or event_type == "CUSTOMER.DISPUTE.RESOLVED":
        if known is None or known.status != "RESOLVED":
            outcome = (resource.get("dispute_outcome") or {}).get("outcome_code", "")
            core.emit("dispute.resolved", {"dispute_id": did, "outcome": outcome})


@router.post("/api/paypal/webhook")
async def webhook(request: Request) -> dict:
    core: Core = request.app.state.core
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "not json")
    wh_id = core.cfg.webhook_id or core.ledger.kv_get("webhook_id")
    if not wh_id or not await core.paypal.verify_webhook(dict(request.headers), event, wh_id):
        raise HTTPException(400, "signature not verified")
    eid = event.get("id")
    if not eid or eid in core.book.webhooks_seen:
        return {"ok": True, "duplicate": True}
    resource: dict[str, Any] = event.get("resource") or {}
    core.emit("webhook.received", {"event_id": eid, "event_type": event.get("event_type"),
                                   "resource_id": resource.get("id") or resource.get("dispute_id")})
    et = event.get("event_type", "")
    if et.startswith("CUSTOMER.DISPUTE."):
        apply_dispute(core, resource, et)
    elif et in ("PAYMENT.PAYOUTSBATCH.SUCCESS", "PAYMENT.PAYOUTSBATCH.DENIED"):
        batch = (resource.get("batch_header") or {}).get("payout_batch_id")
        for p in core.book.payouts.values():
            if p.batch_id == batch and p.status == "PENDING":
                core.emit("payout.settled", {"payout_id": p.payout_id, "batch_id": batch,
                                             "status": "SUCCESS" if et.endswith("SUCCESS") else "DENIED"})
    return {"ok": True}


async def register_webhook(core: Core) -> None:
    try:
        wid = await core.paypal.ensure_webhook(f"{core.cfg.public_url}/api/paypal/webhook", EVENTS)
        core.ledger.kv_set("webhook_id", wid)
        log.info("PayPal webhook %s registered", wid)
    except Exception:
        log.exception("webhook registration failed")


async def poll_disputes(core: Core, every_s: float = 45.0) -> None:
    """Covers deployments PayPal cannot reach (a laptop). Webhooks remain the primary path."""
    while True:
        try:
            res = await core.paypal.list_disputes()
            for item in res.body.get("items", []) if res.ok else []:
                did = item.get("dispute_id")
                known = core.book.disputes.get(did)
                if known is None or (known.status != "RESOLVED" and item.get("status") == "RESOLVED"):
                    full = await core.paypal.get_dispute(did)
                    if full.ok:
                        apply_dispute(core, full.body, "POLL")
        except Exception:
            log.exception("dispute poll failed")
        await asyncio.sleep(every_s)
