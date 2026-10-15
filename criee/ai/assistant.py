"""The seller's assistant: PayPal Agent Toolkit tools plus Criée tools, through Gemini function calling.

Toolkit tools act on PayPal directly (disputes, invoices). Criée tools are scoped to the signed-in
seller's own sales. Refunds are prepared by the assistant and executed only when the seller taps
Confirm, through the same endpoint the console uses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from ..canonical import iso
from ..domain import money, utcnow
from ..engine import MatchStatus
from .provider import AIUnavailable

log = logging.getLogger("criee.assistant")

TOOLKIT_ALLOWED = ["list_disputes", "get_dispute", "create_invoice", "send_invoice", "generate_invoice_qr_code",
                   "list_invoices", "get_invoice"]

SYSTEM = """You are the counter assistant of a small food seller using Criée, a marketplace that sells
their end-of-day stock to buyers' standing wishes and settles through PayPal. Today is {today}. The shop is
{shop}. Currency {cur}. Answer in the seller's language, in one to three short sentences.
Use tools for every fact about sales, payouts, disputes and invoices; never guess amounts or ids.
For a refund, call prepare_refund: it does not move money, the seller confirms on screen.
For an invoice: create_invoice (currency {cur}, invoicer business name = the shop), then send_invoice, then
generate_invoice_qr_code if they want a code to show. Amounts are decimal strings like "12.50"."""

CRIEE_TOOLS = [
    {"name": "today_sales", "description": "This seller's sales today with status, price, pickup state and sale id.",
     "parametersJsonSchema": {"type": "object", "properties": {}}},
    {"name": "open_lots", "description": "This seller's lots still clearing, with live price and units left.",
     "parametersJsonSchema": {"type": "object", "properties": {}}},
    {"name": "payouts", "description": "Recent PayPal payouts to this seller: gross, Criée fee, net, status.",
     "parametersJsonSchema": {"type": "object", "properties": {}}},
    {"name": "prepare_refund",
     "description": "Prepare a refund for one sale. The seller confirms it on screen; nothing is refunded until then.",
     "parametersJsonSchema": {"type": "object", "properties": {
         "sale": {"type": "string", "description": "sale id (m_...) or the buyer's 6-digit pickup code"},
         "amount": {"type": "string", "description": "amount in euros, e.g. '2.00'; omit for the full price"},
         "reason": {"type": "string"}}, "required": ["sale"]}},
]


def _toolkit(core):
    if not core.cfg.paypal_live:
        return None, []
    try:
        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Context
        from paypal_agent_toolkit.shared.tools import tools as specs
    except ImportError:
        return None, []
    api = getattr(core, "_toolkit_api", None)
    if api is None:
        api = PayPalAPI(client_id=core.cfg.paypal_client_id, secret=core.cfg.paypal_secret,
                        context=Context(sandbox=True))
        core._toolkit_api = api
    decls = []
    for spec in specs:
        if spec["method"] not in TOOLKIT_ALLOWED:
            continue
        schema = spec["args_schema"].model_json_schema()
        for k in ("title", "description"):
            schema.pop(k, None)
        decls.append({"name": spec["method"], "description": spec["description"][:900],
                      "parametersJsonSchema": schema})
    return api, decls


def _seller_matches(core, acc) -> list:
    return [m for m in core.market.matches.values() if core.market.lots[m.fill.lot_id].seller_id == acc.id]


def _find_sale(core, acc, ref: str):
    ref = ref.strip()
    for m in _seller_matches(core, acc):
        if m.fill.match_id == ref or (m.pickup_code and m.pickup_code == ref):
            return m
    return None


def run_criee_tool(core, acc, name: str, args: dict) -> dict:
    now = utcnow()
    if name == "today_sales":
        day = now - timedelta(hours=18)
        rows = []
        for m in _seller_matches(core, acc):
            if m.at < day:
                continue
            listing = core.listings.get(m.fill.lot_id)
            s = core.book.settle.get(m.fill.match_id)
            rows.append({"sale": m.fill.match_id, "item": listing.name if listing else "", "units": m.fill.units,
                         "price": money(m.fill.unit_cents * m.fill.units), "status": m.status.value,
                         "collected": m.scanned, "refunded": money(s.refunded_cents) if s and s.refunded_cents else "0.00",
                         "at": iso(m.at)})
        return {"sales": rows[-40:], "count": len(rows),
                "captured_total": money(sum(m.fill.unit_cents * m.fill.units for m in _seller_matches(core, acc)
                                            if m.status is MatchStatus.CAPTURED and m.at >= day))}
    if name == "open_lots":
        from ..api.views import lot_view
        return {"lots": [{k: v for k, v in lot_view(core, l.id, now).items()
                          if k in ("id", "name", "mode", "price", "remaining", "sold", "status", "closes_at", "waiting")}
                         for l in core.market.lots.values() if l.seller_id == acc.id and not l.closed]}
    if name == "payouts":
        return {"payouts": [{"id": p.payout_id, "gross": money(p.gross), "fee": money(p.fee), "net": money(p.net),
                             "status": p.status, "sales": len(p.match_ids), "paypal_batch": p.batch_id}
                            for p in core.book.payouts.values() if p.seller_id == acc.id][-10:]}
    if name == "prepare_refund":
        m = _find_sale(core, acc, str(args.get("sale", "")))
        if m is None:
            return {"error": "no sale with that id or pickup code"}
        s = core.book.settle.get(m.fill.match_id)
        if not s or not s.capture_id:
            return {"error": "this sale has not been paid yet; a hold is released automatically if not collected"}
        left = s.captured_cents - s.refunded_cents
        amt = args.get("amount")
        try:
            cents = int(round(float(str(amt).replace(",", ".")) * 100)) if amt else left
        except ValueError:
            return {"error": "amount not understood"}
        if cents <= 0 or cents > left:
            return {"error": f"refundable amount is {money(left)}"}
        listing = core.listings.get(m.fill.lot_id)
        return {"pending_action": {"kind": "refund", "match_id": m.fill.match_id, "cents": cents,
                                   "reason": str(args.get("reason") or "Refund")[:120],
                                   "summary": f"Refund €{money(cents)} on {m.fill.units} × "
                                              f"{listing.name if listing else 'item'}"}}
    return {"error": f"unknown tool {name}"}


async def _run_tool(core, acc, api, name: str, args: dict) -> dict:
    if name in TOOLKIT_ALLOWED and api is not None:
        if name == "list_disputes":
            args = {k: v for k, v in args.items() if k in ("disputed_transaction_id", "dispute_state", "page_size")}
        try:
            out = await asyncio.to_thread(api.run, name, args)
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except ValueError:
                    out = {"text": out[:2000]}
            return {"result": out, "via": "paypal_agent_toolkit"}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {str(e)[:300]}", "via": "paypal_agent_toolkit"}
    return run_criee_tool(core, acc, name, args)


def _fallback(core, acc, text: str) -> dict:
    s = text.lower()
    if re.search(r"refund|rembours", s):
        code = re.search(r"\b(\d{6})\b", s)
        amt = re.search(r"(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur)", s)
        if code:
            out = run_criee_tool(core, acc, "prepare_refund", {"sale": code.group(1), "amount": amt.group(1) if amt else None})
            if "pending_action" in out:
                return {"reply": out["pending_action"]["summary"] + ". Confirm below.", "pending": out["pending_action"],
                        "tools": [{"name": "prepare_refund", "via": "criee"}]}
            return {"reply": out.get("error", "I could not prepare that refund."), "tools": []}
    if re.search(r"payout|paid|virement|versement", s):
        p = run_criee_tool(core, acc, "payouts", {})["payouts"]
        if p:
            last = p[-1]
            return {"reply": f"Last payout: €{last['net']} net (fee €{last['fee']}), {last['status'].lower()}.",
                    "tools": [{"name": "payouts", "via": "criee"}]}
        return {"reply": "No payout yet.", "tools": []}
    sales = run_criee_tool(core, acc, "today_sales", {})
    return {"reply": f"{sales['count']} sales today, €{sales['captured_total']} collected.",
            "tools": [{"name": "today_sales", "via": "criee"}]}


async def run_assistant(core, acc, messages: list[dict], visitor: Optional[str] = None) -> dict:
    texts = [m for m in messages if isinstance(m, dict) and isinstance(m.get("text"), str)][-8:]
    if not texts or texts[-1].get("role") != "user":
        return {"reply": "Ask me about today's sales, a refund, a payout, a dispute or an invoice.", "tools": []}
    api, toolkit_decls = _toolkit(core)
    shop = (acc.shop or {}).get("name") or acc.handle
    system = SYSTEM.format(today=utcnow().strftime("%A %d %B %Y"), shop=shop, cur=core.cfg.currency)
    contents = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["text"][:1000]}]}
                for m in texts]
    used: list[dict] = []
    pending = None
    if core.ai is None:
        return _fallback(core, acc, texts[-1]["text"])
    try:
        for _ in range(5):
            content = await core.ai.turn("assistant", system, contents, CRIEE_TOOLS + toolkit_decls, visitor=visitor)
            parts = content.get("parts", [])
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                reply = " ".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                return {"reply": reply or "Done.", "tools": used, "pending": pending}
            contents.append({"role": "model", "parts": parts})
            responses = []
            for c in calls:
                out = await _run_tool(core, acc, api, c["name"], c.get("args") or {})
                used.append({"name": c["name"], "via": out.get("via", "criee"), "ok": "error" not in out})
                if "pending_action" in out:
                    pending = out["pending_action"]
                responses.append({"functionResponse": {"name": c["name"], "response": out}})
            contents.append({"role": "user", "parts": responses})
        return {"reply": "I stopped after several steps; please ask again more specifically.", "tools": used,
                "pending": pending}
    except AIUnavailable:
        return _fallback(core, acc, texts[-1]["text"])
