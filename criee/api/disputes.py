"""Answering "my agent bought this" from the ledger."""
from __future__ import annotations

from datetime import datetime

from ..canonical import iso
from ..core import Core
from ..domain import money
from ..evidence import EvidencePack, FactLine, LedgerLine, build_evidence_pdf, evidence_entries

RELEVANT = {"wish.signed", "lot.listed", "clock.tick", "call.cleared", "threshold.reached", "match.made",
            "settlement.authorized", "pickup.scanned", "settlement.captured", "settlement.tracked"}


def build_pack(core: Core, match_id: str, dispute_id: str, reason: str) -> EvidencePack:
    mk = core.market
    m = mk.matches[match_id]
    lot = mk.lots[m.fill.lot_id]
    wish = mk.wishes[m.fill.wish_id]
    meta = core.wish_meta.get(wish.id, {})
    listing = core.listings.get(lot.id)
    s = core.book.s(match_id)
    sig = meta.get("signature") or {}
    lines = []
    scanned_at = None
    for ev in core.ledger.events():
        b = ev.body
        ref = (b.get("wish_id") == wish.id and ev.type == "wish.signed") or \
              (b.get("lot_id") == lot.id and ev.type in ("lot.listed", "call.cleared", "threshold.reached")) or \
              (ev.type == "clock.tick" and b.get("lot_id") == lot.id and b.get("round") == m.fill.round) or \
              ((b.get("fill") or {}).get("match_id") == match_id) or (b.get("match_id") == match_id)
        if ref and ev.type in RELEVANT:
            lines.append(LedgerLine(ev.seq, iso(ev.at), ev.type, ev.hash, ev.prev))
            if ev.type == "pickup.scanned":
                scanned_at = iso(ev.at)
    facts = []
    for f in lot.terms.facts:
        if f.value is None:
            continue
        src = f"label crop {f.evidence}" if (f.evidence or "").startswith("crop_") else \
            "seller's voice note" if (f.evidence or "").startswith("say_") else "seller confirmed"
        facts.append(FactLine(f.attr.value, str(f.value).lower(), src))
    crops: list[tuple[str, bytes]] = []
    for c in (listing.crops if listing else [])[:4]:
        p = core.cfg.data_dir / "images" / f"{c.id}.jpg"
        if p.exists():
            crops.append((c.id, p.read_bytes()))
    seller = core.accounts.get(lot.seller_id)
    shop = (seller.shop or {}).get("name") if seller else None
    cur = core.cfg.currency
    total = m.fill.units * m.fill.unit_cents
    return EvidencePack(
        dispute_id=dispute_id, dispute_reason=reason, disputed_amount=f"{money(total)} {cur}",
        match_id=match_id, units=m.fill.units, unit_price=f"{money(m.fill.unit_cents)} {cur}",
        total=f"{money(total)} {cur}", rule=m.fill.rule.value, round=m.fill.round, matched_at=iso(m.at),
        wish_id=wish.id, readback=meta.get("readback") or "", terms_hash=meta.get("terms_hash") or "",
        signed_at=iso(wish.signed_at), signature_kind=sig.get("kind", ""), credential_id=sig.get("credential_id", ""),
        lot_id=lot.id, item_name=listing.name if listing else "", shop_name=shop or "",
        facts=tuple(facts), pickup_code=m.pickup_code or "", scanned_at=scanned_at,
        tracking_number=f"CRIEE-{m.pickup_code}" if s.tracked else None,
        order_id=s.order_id or "", authorization_id=s.authorization_id or "", capture_id=s.capture_id or "",
        ledger=tuple(lines), ledger_head=core.ledger.head[1], ledger_verified=bool(core.ledger.verify()["ok"]),
        public_key_pem=core.ledger.signer.public_pem(), crop_images=tuple(crops))


def combined_entry(entries: list[dict]) -> list[dict]:
    """PayPal wants an item id per entry when several are sent; one entry carries everything."""
    first = dict(entries[0])
    notes = " ".join(e.get("notes", "") for e in entries if e.get("notes"))
    first["notes"] = notes if len(notes) <= 2000 else notes[:1997] + "..."
    return [first]


async def send_evidence_pack(core: Core, dispute_id: str) -> dict:
    d = core.book.disputes[dispute_id]
    pack = build_pack(core, d.match_id, dispute_id, d.reason)
    pdf = build_evidence_pdf(pack)
    entries = combined_entry(evidence_entries(pack))
    res = await core.paypal.provide_evidence(dispute_id, entries, [(f"criee-evidence-{d.match_id}.pdf", pdf)])
    if not res.ok and hasattr(core.paypal, "sandbox_require_evidence"):
        await core.paypal.sandbox_require_evidence(dispute_id)
        res = await core.paypal.provide_evidence(dispute_id, entries, [(f"criee-evidence-{d.match_id}.pdf", pdf)])
    if not res.ok:
        return {"ok": False, "issue": res.issue(), "debug_id": res.debug_id}
    core.emit("dispute.evidence_sent", {"dispute_id": dispute_id, "match_id": d.match_id, "pages_bytes": len(pdf),
                                        "evidence_types": [e["evidence_type"] for e in entries],
                                        "ledger_lines": len(pack.ledger)})
    return {"ok": True, "bytes": len(pdf), "entries": len(entries)}
