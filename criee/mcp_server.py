"""MCP server: any agent can hold standing wishes for its person.

Agents authenticate with a token the person creates in the buyer app. An agent can draft a wish,
but the wish only exists once the person opens the signing link and approves it with their own
passkey: `create_wish` returns that link, never a signed wish.
"""
from __future__ import annotations

import contextvars
import hashlib
from typing import Any, Optional

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer

from .ai.wish import compile_wish
from .canonical import digest, iso
from .domain import GeoPoint, new_id, utcnow

current_account: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("criee_mcp_account", default=None)

PLACES = {"krutenau": (48.5795, 7.7555), "petite france": (48.5810, 7.7400), "neudorf": (48.5700, 7.7600),
          "esplanade": (48.5790, 7.7680), "gare": (48.5850, 7.7350), "contades": (48.5900, 7.7550),
          "bourse": (48.5760, 7.7470), "orangerie": (48.5900, 7.7700), "cronenbourg": (48.5950, 7.7200),
          "koenigshoffen": (48.5800, 7.7150), "centre": (48.5834, 7.7458), "place kléber": (48.5834, 7.7458),
          "place kleber": (48.5834, 7.7458)}


class _AuthMiddleware:
    def __init__(self, app, core):
        self.app, self.core = app, core

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        token = headers.get("authorization", "")[7:].strip() if headers.get("authorization", "").lower().startswith("bearer ") else ""
        aid = self.core.ledger.kv_get(f"agent_token:{hashlib.sha256(token.encode()).hexdigest()}") if token else None
        if not aid:
            body = b'{"error":"create an agent token in the Criee buyer app and send it as a Bearer token"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": body})
            return
        tok = current_account.set(aid)
        try:
            await self.app(scope, receive, send)
        finally:
            current_account.reset(tok)


def build_mcp(core) -> MCPServer:
    server = MCPServer(name="criee", title="Criée",
                       instructions="Criée clears local sellers' end-of-day food against standing wishes. Use "
                                    "create_wish to draft a standing wish from the person's words; the person signs "
                                    "it with their passkey at the returned link. Use list_matches for pickup codes.")

    def acc():
        aid = current_account.get()
        a = core.accounts.get(aid) if aid else None
        if a is None:
            raise PermissionError("unknown agent token")
        return a

    @server.tool(description="Draft a standing wish from the person's own words (French or English), e.g. "
                             "'an organic sourdough loaf twice a week under 4.50 euros after 18:00'. Returns the "
                             "read-back sentence and a link the person must open to sign it. Agents cannot sign.")
    async def create_wish(request: str, neighbourhood: str = "Centre") -> dict[str, Any]:
        a = acc()
        lat, lon = PLACES.get(neighbourhood.strip().lower(), PLACES["centre"])
        d = await compile_wish(core.ai, request, GeoPoint(lat=lat, lon=lon), neighbourhood.strip()[:40] or "Centre",
                               utcnow(), visitor=f"mcp:{a.id}")
        if d.terms is None:
            return {"status": "needs_input", "questions": d.questions}
        did = new_id("d")
        import json
        core.ledger.kv_set(f"draft:{did}", json.dumps({
            "id": did, "account_id": a.id, "terms": d.terms.model_dump(mode="json"), "label": d.label,
            "readback": d.readback, "place": neighbourhood, "source": d.source, "via": "mcp",
            "digest": digest(d.terms), "created": iso(utcnow())}))
        core.emit("draft.proposed", {"draft_id": did, "account_id": a.id, "via": "mcp", "digest": digest(d.terms)})
        return {"status": "awaiting_signature", "readback": d.readback,
                "sign_url": f"{core.public_base}/sign/{did}", "draft_id": did}

    @server.tool(description="List the person's standing wishes with status, weekly usage and why open lots did not match.")
    def list_wishes() -> dict[str, Any]:
        from .api.views import wish_view
        a = acc()
        now = utcnow()
        out = []
        for w in core.market.wishes.values():
            if w.buyer_id == a.id:
                v = wish_view(core, w.id, now)
                out.append({k: v[k] for k in ("id", "label", "readback", "status", "week", "watching")}
                           | {"matches": len(v["matches"])})
        return {"wishes": out}

    def _wish_cmd(wish_id: str, event: str) -> dict:
        a = acc()
        w = core.market.wishes.get(wish_id)
        if not w or w.buyer_id != a.id:
            return {"error": "no such wish"}
        core.emit(event, {"wish_id": wish_id, "via": "mcp"})
        return {"wish_id": wish_id, "status": core.market.wishes[wish_id].status}

    @server.tool(description="Pause a standing wish. It stops matching until resumed.")
    def pause_wish(wish_id: str) -> dict[str, Any]:
        return _wish_cmd(wish_id, "wish.paused")

    @server.tool(description="Resume a paused standing wish.")
    def resume_wish(wish_id: str) -> dict[str, Any]:
        return _wish_cmd(wish_id, "wish.resumed")

    @server.tool(description="Revoke a standing wish permanently. Effective before the next clearing tick.")
    def revoke_wish(wish_id: str) -> dict[str, Any]:
        return _wish_cmd(wish_id, "wish.revoked")

    @server.tool(description="List matches cleared for the person: item, shop, price paid, PayPal status, pickup "
                             "window and the 6-digit pickup code to show at the counter.")
    def list_matches() -> dict[str, Any]:
        from .api.views import match_view
        a = acc()
        out = []
        for wid, mids in core.market.by_wish.items():
            if core.market.wishes[wid].buyer_id != a.id:
                continue
            for mid in mids:
                v = match_view(core, mid, for_buyer=True)
                out.append({k: v.get(k) for k in ("id", "name", "shop", "units", "total", "status", "pickup_code",
                                                  "pickup_by", "pickup_until", "rule")})
        return {"matches": out}

    @server.tool(description="What is clearing right now near Strasbourg: open lots with live price and units left.")
    def market_now(category: str = "") -> dict[str, Any]:
        from .api.views import lot_view
        now = utcnow()
        lots = [lot_view(core, l.id, now) for l in core.market.lots.values() if not l.closed]
        if category:
            lots = [l for l in lots if l["category"] == category]
        return {"lots": [{k: l[k] for k in ("id", "name", "mode", "price", "remaining", "closes_at", "status")}
                         | {"shop": l["shop"]["name"], "simulated": l["simulated"]} for l in lots[:30]]}

    return server


def mount_mcp(app: FastAPI, core) -> MCPServer:
    server = build_mcp(core)
    inner = server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                       host="0.0.0.0")
    app.mount("/mcp", _AuthMiddleware(inner, core))
    app.state.mcp = server
    return server
