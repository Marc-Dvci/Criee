"""HTTP API and static front end."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..ai.advisor import advise, demand_for
from ..ai.provider import AI, Media
from ..ai.supply import read_shelf
from ..ai.wish import compile_wish
from ..canonical import canonical, digest, iso
from ..config import ROOT, Config, load
from ..core import Core
from ..domain import (Category, ClearingMode, Crop, Fact, GeoPoint, LotListing, LotTerms, WishTerms, new_id,
                      parse_money, utcnow)
from ..engine import MatchStatus
from ..settlement.paypal import FakePayPal, PayPalClient
from . import auth
from .views import board, lot_view, match_view, nearby_wishes, shop_of, wish_view

log = logging.getLogger("criee.api")
PARIS = ZoneInfo("Europe/Paris")
WEB = ROOT / "web" / "dist"


class SessionIn(BaseModel):
    handle: str = Field(default="", max_length=40)
    role: str = Field(default="buyer", pattern="^(buyer|seller)$")

class DeviceKeyIn(BaseModel):
    jwk: dict

class CompileIn(BaseModel):
    text: str = Field(min_length=3, max_length=600)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    place: str = Field(default="my place", max_length=60)

class SignIn(BaseModel):
    kind: str = Field(pattern="^(passkey|device)$")
    assertion: Optional[dict] = None
    credential_id: Optional[str] = None
    signature: Optional[str] = None

class ShopIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    place: str = Field(default="", max_length=60)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    payout_email: Optional[str] = Field(default=None, max_length=120)

class LotIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    name_fr: Optional[str] = Field(default=None, max_length=80)
    unit: str = Field(default="item", max_length=30)
    category: Category
    quantity: int = Field(ge=1, le=500)
    mode: ClearingMode
    start: int = Field(gt=0)
    floor: int = Field(gt=0)
    opens_in_min: int = Field(default=0, ge=0, le=60 * 24 * 7)
    duration_min: int = Field(ge=2, le=60 * 24 * 30)
    pickup_after_min: int = Field(default=0, ge=0, le=60 * 24 * 30)
    pickup_window_min: int = Field(default=90, ge=10, le=60 * 24 * 7)
    threshold: Optional[int] = Field(default=None, ge=2, le=500)
    step_s: Optional[int] = Field(default=None, ge=5, le=3600)
    facts: list[dict] = Field(default_factory=list, max_length=24)
    crops: list[dict] = Field(default_factory=list, max_length=24)
    image_id: Optional[str] = None
    confirm_facts: list[str] = Field(default_factory=list, max_length=24)

class ScanIn(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")

class RefundIn(BaseModel):
    match_id: str
    cents: int = Field(gt=0)
    reason: str = Field(default="", max_length=120)


def visitor_of(request: Request) -> str:
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def build_core(cfg: Config) -> Core:
    paypal = PayPalClient(cfg.paypal_client_id, cfg.paypal_secret, cfg.currency) if cfg.paypal_live else FakePayPal()
    core = Core(cfg, paypal)
    core.ai = AI(cfg, core.ledger.kv_get, core.ledger.kv_set)
    if cfg.demo_vault_id and not core.ledger.kv_get("vault:demo"):
        core.ledger.kv_set("vault:demo", cfg.demo_vault_id)
    return core


def create_app(cfg: Optional[Config] = None, core: Optional[Core] = None, start_loops: bool = True) -> FastAPI:
    cfg = cfg or load()
    core = core or build_core(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import contextlib
        import os
        from ..demo import demo_loop
        from .webhooks import poll_disputes, register_webhook
        extra: list[asyncio.Task] = []
        if start_loops:
            await core.start()
            if cfg.paypal_live and cfg.public_url.startswith("https://"):
                extra.append(asyncio.create_task(register_webhook(core)))
            if cfg.paypal_live:
                extra.append(asyncio.create_task(poll_disputes(core)))
            if os.environ.get("CRIEE_DEMO", "1") == "1":
                extra.append(asyncio.create_task(demo_loop(core)))
        mcp = getattr(app.state, "mcp", None)
        async with (mcp.session_manager.run() if mcp is not None else contextlib.nullcontext()):
            yield
        for t in extra:
            t.cancel()
        await core.stop()

    app = FastAPI(title="Criée", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.core = core
    secure = cfg.public_url.startswith("https://")
    images = cfg.data_dir / "images"

    def me(request: Request, role: Optional[str] = None):
        return auth.require(request, core, role)

    # ------------------------------------------------------------------ status and session

    @app.get("/api/status")
    def status() -> dict:
        return {"paypal": "sandbox" if cfg.paypal_live else "offline double", "currency": cfg.currency,
                "ai": {"provider": cfg.ai_provider, "model": cfg.gemini_model,
                       "live_until": cfg.ai_until.isoformat() if cfg.ai_until else None,
                       "calls_today": core.ai.budget.calls_today() if core.ai else 0, "daily_cap": cfg.ai_daily_cap},
                "ledger": {"events": core.ledger.head[0], "head": core.ledger.head[1]},
                "fee_bps": cfg.fee_bps, "sandbox_buyer": cfg.sandbox_buyer_email}

    @app.get("/api/me")
    def get_me(request: Request) -> dict:
        acc = auth.current(request, core)
        if not acc:
            return {"account": None}
        creds = auth.credentials(core, acc.id)
        return {"account": {"id": acc.id, "handle": acc.handle, "roles": sorted(acc.roles), "vaulted": acc.vaulted,
                            "shop": acc.shop, "credentials": [{"id": c["id"], "kind": c["kind"]} for c in creds],
                            "vault_label": core.ledger.kv_get(f"vault_label:{acc.id}")}}


    @app.post("/api/session")
    def new_session(body: SessionIn, request: Request) -> JSONResponse:
        acc = auth.current(request, core)
        if acc is None:
            handle = body.handle.strip() or f"Guest {secrets.choice(['Lime', 'Plum', 'Rye', 'Fig', 'Sage', 'Quince'])}"
            acc = core.create_account(handle, [body.role])
        elif body.role not in acc.roles:
            core.emit("account.role_added", {"account_id": acc.id, "role": body.role})
        resp = JSONResponse({"id": acc.id, "handle": acc.handle, "roles": sorted(acc.roles)})
        auth.set_session(resp, core, acc.id, secure)
        return resp

    @app.post("/api/passkey/register/options")
    def passkey_options(request: Request) -> dict:
        return auth.registration_options(core, request, me(request))

    @app.post("/api/passkey/register/verify")
    async def passkey_verify(request: Request) -> dict:
        return auth.registration_verify(core, request, me(request), await request.json())


    @app.post("/api/devicekey/register")
    def devicekey(body: DeviceKeyIn, request: Request) -> dict:
        return auth.register_device_key(core, me(request), body.jwk)

    # ------------------------------------------------------------------ buyer: wishes


    def save_draft(account_id: Optional[str], terms: WishTerms, label: str, readback: str, place: str,
                   source: str, via: str) -> dict:
        did = new_id("d")
        rec = {"id": did, "account_id": account_id, "terms": terms.model_dump(mode="json"), "label": label,
               "readback": readback, "place": place, "source": source, "via": via, "digest": digest(terms),
               "created": iso(utcnow())}
        core.ledger.kv_set(f"draft:{did}", json.dumps(rec))
        return rec

    def load_draft(did: str) -> dict:
        raw = core.ledger.kv_get(f"draft:{did}")
        if not raw:
            raise HTTPException(404, "no such draft")
        return json.loads(raw)

    def draft_out(rec: dict, request: Request, acc) -> dict:
        out = {k: rec[k] for k in ("id", "terms", "label", "readback", "place", "source", "via", "digest")}
        out["passkey"] = auth.signing_options(core, request, acc, rec["digest"]) if acc else None
        return out

    @app.post("/api/wishes/compile")
    async def compile_(body: CompileIn, request: Request) -> dict:
        acc = me(request)
        d = await compile_wish(core.ai, body.text, GeoPoint(lat=body.lat, lon=body.lon), body.place, utcnow(),
                               visitor=visitor_of(request))
        if d.terms is None:
            return {"questions": d.questions, "source": d.source}
        rec = save_draft(acc.id, d.terms, d.label, d.readback, body.place, d.source, "chat")
        return draft_out(rec, request, acc)

    @app.get("/api/drafts/{did}")
    def get_draft(did: str, request: Request) -> dict:
        return draft_out(load_draft(did), request, auth.current(request, core))


    @app.post("/api/drafts/{did}/sign")
    def sign_draft(did: str, body: SignIn, request: Request) -> dict:
        acc = me(request)
        rec = load_draft(did)
        if rec.get("account_id") not in (None, acc.id):
            raise HTTPException(403, "this draft belongs to someone else")
        if rec.get("signed"):
            raise HTTPException(409, "already signed")
        terms = WishTerms.model_validate(rec["terms"])
        if digest(terms) != rec["digest"]:
            raise HTTPException(409, "draft changed")
        if body.kind == "passkey":
            sig = auth.verify_passkey_signature(core, request, acc, rec["digest"], body.assertion or {})
        else:
            sig = auth.verify_device_signature(core, acc, rec["digest"], body.credential_id or "", body.signature or "")
        if "buyer" not in acc.roles:
            core.emit("account.role_added", {"account_id": acc.id, "role": "buyer"})
        wid = core.sign_wish(acc.id, terms, sig, readback=rec["readback"], label=rec["label"], place=rec["place"])
        rec["signed"] = wid
        core.ledger.kv_set(f"draft:{did}", json.dumps(rec))
        return wish_view(core, wid, utcnow())

    @app.get("/api/wishes")
    def my_wishes(request: Request) -> dict:
        acc = me(request)
        now = utcnow()
        ws = [w for w in core.market.wishes.values() if w.buyer_id == acc.id]
        return {"wishes": [wish_view(core, w.id, now) for w in sorted(ws, key=lambda w: -w.seq)]}

    @app.post("/api/wishes/{wid}/{action}")
    def wish_action(wid: str, action: str, request: Request) -> dict:
        acc = me(request)
        w = core.market.wishes.get(wid)
        if not w or w.buyer_id != acc.id:
            raise HTTPException(404, "no such wish")
        if action not in ("pause", "resume", "revoke"):
            raise HTTPException(400, "unknown action")
        core.emit(f"wish.{action}d" if action != "revoke" else "wish.revoked", {"wish_id": wid})
        return wish_view(core, wid, utcnow())

    @app.get("/api/matches")
    def my_matches(request: Request) -> dict:
        acc = me(request)
        mids = [mid for wid, mm in core.market.by_wish.items()
                if core.market.wishes[wid].buyer_id == acc.id for mid in mm]
        ms = sorted((core.market.matches[m] for m in mids), key=lambda m: m.at, reverse=True)
        return {"matches": [match_view(core, m.fill.match_id, for_buyer=True) for m in ms]}

    # ------------------------------------------------------------------ buyer: PayPal vault

    @app.post("/api/vault/start")
    async def vault_start(request: Request) -> dict:
        acc = me(request)
        ref = f"{acc.id}:{secrets.token_hex(4)}"
        res = await core.paypal.setup_token(f"{cfg.public_url}/api/vault/return", f"{cfg.public_url}/wish?vault=cancel",
                                            "Criée standing wishes: charged only when a match clears, within your "
                                            "signed limits", ref)
        if not res.ok:
            raise HTTPException(502, f"PayPal: {res.issue()}")
        core.ledger.kv_set(f"setup:{res.body['id']}", acc.id)
        approve = next((l["href"] for l in res.body.get("links", []) if l.get("rel") == "approve"), None)
        return {"approve_url": approve, "setup_token": res.body["id"]}

    @app.get("/api/vault/return")
    async def vault_return(approval_token_id: str, request: Request) -> RedirectResponse:
        aid = core.ledger.kv_get(f"setup:{approval_token_id}")
        if not aid:
            raise HTTPException(404, "unknown setup token")
        res = await core.paypal.payment_token(approval_token_id, approval_token_id)
        if not res.ok:
            return RedirectResponse(f"/wish?vault=failed&reason={res.issue()}")
        core.ledger.kv_set(f"vault:{aid}", res.body["id"])
        core.ledger.kv_set(f"vault_label:{aid}", "PayPal (sandbox)")
        core.emit("buyer.vaulted", {"account_id": aid, "method": "paypal_wallet",
                                    "token_ref": hashlib.sha256(res.body["id"].encode()).hexdigest()[:12]})
        return RedirectResponse("/wish?vault=ok")

    @app.post("/api/vault/demo")
    def vault_demo(request: Request) -> dict:
        acc = me(request)
        demo = core.ledger.kv_get("vault:demo") or cfg.demo_vault_id
        if not demo:
            raise HTTPException(409, "no demo wallet configured")
        core.ledger.kv_set(f"vault:{acc.id}", demo)
        core.ledger.kv_set(f"vault_label:{acc.id}", "Demo PayPal wallet (sandbox)")
        core.emit("buyer.vaulted", {"account_id": acc.id, "method": "demo_wallet",
                                    "token_ref": hashlib.sha256(demo.encode()).hexdigest()[:12]})
        return {"ok": True}

    # ------------------------------------------------------------------ agent tokens (MCP)

    @app.post("/api/agent-token")
    def agent_token(request: Request) -> dict:
        acc = me(request)
        token = "crt_" + secrets.token_urlsafe(24)
        core.ledger.kv_set(f"agent_token:{hashlib.sha256(token.encode()).hexdigest()}", acc.id)
        return {"token": token, "mcp_url": f"{cfg.public_url}/mcp"}

    # ------------------------------------------------------------------ seller


    @app.post("/api/shop")
    def set_shop(body: ShopIn, request: Request) -> dict:
        acc = me(request)
        if "seller" not in acc.roles:
            core.emit("account.role_added", {"account_id": acc.id, "role": "seller"})
        if body.payout_email:
            core.ledger.kv_set(f"payout:{acc.id}", body.payout_email.strip())
        core.emit("seller.shop", {"account_id": acc.id, "shop": {"name": body.name, "place": body.place,
                                                                 "lat": body.lat, "lon": body.lon}})
        return {"shop": core.accounts[acc.id].shop}

    @app.post("/api/supply/read")
    async def supply_read(request: Request, images_in: list[UploadFile] = File(default=[], alias="images"),
                          audio: Optional[UploadFile] = File(default=None)) -> dict:
        acc = me(request, "seller")
        blobs = [await f.read() for f in images_in[:4]]
        if not blobs:
            raise HTTPException(400, "add at least one photo")
        if any(len(b) > 8_000_000 for b in blobs):
            raise HTTPException(413, "photos must be under 8 MB")
        media = None
        if audio is not None:
            data = await audio.read()
            if 0 < len(data) < 6_000_000:
                media = Media((audio.content_type or "audio/webm").split(";")[0], data)
        r = await read_shelf(core.ai, blobs, media, images, visitor=visitor_of(request))
        return {"source": r.source, "transcript": r.transcript, "floor": r.floor_cents, "close_hhmm": r.close_hhmm,
                "mode_hint": r.mode_hint.value if r.mode_hint else None, "image_ids": r.image_ids,
                "lots": [{"name": l.name, "name_fr": l.name_fr, "category": l.category.value, "quantity": l.quantity,
                          "unit": l.unit_label, "price": l.price_cents, "price_source": l.price_source,
                          "image_id": l.image_id, "box": l.box,
                          "facts": [f.model_dump(mode="json") for f in l.facts],
                          "crops": [c.model_dump(mode="json") for c in l.crops], "dropped": l.dropped}
                         for l in r.lots]}


    @app.post("/api/lots")
    def create_lot(body: LotIn, request: Request) -> dict:
        acc = me(request, "seller")
        if not acc.shop:
            raise HTTPException(409, "set up your shop first")
        now = utcnow().replace(microsecond=0)
        opens = now + timedelta(minutes=body.opens_in_min)
        closes = opens + timedelta(minutes=body.duration_min)
        pf = opens + timedelta(minutes=body.pickup_after_min)
        facts = []
        for f in body.facts:
            try:
                fact = Fact.model_validate(f)
                if fact.value is None and f.get("attr") in body.confirm_facts and f.get("value") is not None:
                    fact = Fact(attr=fact.attr, value=f["value"],
                                evidence="sc_" + hashlib.sha256(f"{acc.id}:{f['attr']}".encode()).hexdigest()[:12])
                facts.append(fact)
            except ValueError:
                continue
        step = body.step_s or (max(5, min(300, body.duration_min * 60 // 40)) if body.mode is ClearingMode.CLOCK else 60)
        try:
            terms = LotTerms(category=body.category, facts=tuple(facts), quantity=body.quantity, mode=body.mode,
                             start_cents=body.start,
                             floor_cents=body.start if body.mode is ClearingMode.THRESHOLD else body.floor,
                             opens_at=opens, closes_at=closes,
                             pickup=GeoPoint(lat=acc.shop["lat"], lon=acc.shop["lon"]), pickup_from=pf,
                             pickup_until=max(pf + timedelta(minutes=body.pickup_window_min), closes + timedelta(minutes=10)),
                             threshold_qty=body.threshold, clock_step_s=step)
        except ValueError as e:
            raise HTTPException(400, str(e))
        crops = []
        for c in body.crops:
            try:
                crops.append(Crop.model_validate(c))
            except ValueError:
                continue
        listing = LotListing(name=body.name, name_fr=body.name_fr, unit_label=body.unit, crops=crops,
                             image_id=body.image_id)
        lid = core.list_lot(acc.id, terms, listing)
        return lot_view(core, lid, utcnow(), detail=True)

    @app.get("/api/lots/mine")
    def my_lots(request: Request) -> dict:
        acc = me(request, "seller")
        now = utcnow()
        lots = sorted((l for l in core.market.lots.values() if l.seller_id == acc.id), key=lambda l: -l.seq)
        return {"lots": [lot_view(core, l.id, now, detail=True) for l in lots]}

    @app.post("/api/lots/{lid}/withdraw")
    def withdraw(lid: str, request: Request) -> dict:
        acc = me(request, "seller")
        lot = core.market.lots.get(lid)
        if not lot or lot.seller_id != acc.id:
            raise HTTPException(404, "no such lot")
        core.emit("lot.withdrawn", {"lot_id": lid})
        return lot_view(core, lid, utcnow(), detail=True)


    @app.post("/api/pickups/scan")
    def scan(body: ScanIn, request: Request) -> dict:
        acc = me(request, "seller")
        for m in core.market.matches.values():
            lot = core.market.lots[m.fill.lot_id]
            if lot.seller_id == acc.id and m.pickup_code == body.code:
                if m.status not in (MatchStatus.PENDING, MatchStatus.HELD) or m.scanned:
                    raise HTTPException(409, f"this code is {m.status.value}")
                core.emit("pickup.scanned", {"match_id": m.fill.match_id})
                return match_view(core, m.fill.match_id, for_buyer=False)
        raise HTTPException(404, "no open pickup with this code")

    @app.get("/api/advice")
    async def advice(request: Request, category: Category, quantity: int = 10, sentence: bool = False) -> dict:
        acc = me(request, "seller")
        if not acc.shop:
            raise HTTPException(409, "set up your shop first")
        now = utcnow()
        d = demand_for(core.market, GeoPoint(lat=acc.shop["lat"], lon=acc.shop["lon"]), category, quantity, now)
        if sentence:
            text, source = await advise(core.ai, d, category.value.replace("_", " "), visitor=visitor_of(request))
        else:
            from ..ai.advisor import template
            text, source = template(d, category.value.replace("_", " ")), "computed"
        return {"sentence": text, "source": source, "numbers": d.numbers(), "curve": d.curve, "hidden": d.hidden}


    @app.post("/api/refunds")
    async def refund(body: RefundIn, request: Request) -> dict:
        acc = me(request, "seller")
        m = core.market.matches.get(body.match_id)
        if not m or core.market.lots[m.fill.lot_id].seller_id != acc.id:
            raise HTTPException(404, "no such sale")
        try:
            res = await core.worker.refund(body.match_id, body.cents, body.reason or "Refund")
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not res.ok:
            raise HTTPException(502, f"PayPal: {res.issue()}")
        return match_view(core, body.match_id, for_buyer=False)

    @app.post("/api/assistant")
    async def assistant(request: Request) -> dict:
        from ..ai.assistant import run_assistant
        acc = me(request, "seller")
        body = await request.json()
        return await run_assistant(core, acc, body.get("messages") or [], visitor=visitor_of(request))

    # ------------------------------------------------------------------ console (seller scoped)

    def seller_scope(acc) -> set[str]:
        return {l.id for l in core.market.lots.values() if l.seller_id == acc.id}

    @app.get("/api/console")
    def console(request: Request) -> dict:
        acc = me(request, "seller")
        now = utcnow()
        lots = seller_scope(acc)
        mids = [m for lid in lots for m in core.market.by_lot.get(lid, [])]
        sales = [match_view(core, m, for_buyer=False) for m in mids]
        payouts = [vars(p) | {"at": iso(p.at) if p.at else None} for p in core.book.payouts.values()
                   if p.seller_id == acc.id]
        disputes = [vars(d) | {"at": iso(d.at)} for d in core.book.disputes.values()
                    if d.match_id in set(mids)]
        shop = acc.shop or {}
        book = nearby_wishes(core, shop.get("lat", 48.58), shop.get("lon", 7.75), now) if shop else []
        return {"sales": sales, "payouts": payouts, "disputes": disputes, "orderbook": book,
                "lots": [lot_view(core, l, now) for l in lots], "fee_bps": cfg.fee_bps}

    @app.post("/api/disputes/{dispute_id}/evidence")
    async def send_evidence(dispute_id: str, request: Request) -> dict:
        from .disputes import send_evidence_pack
        acc = me(request, "seller")
        d = core.book.disputes.get(dispute_id)
        if not d or not d.match_id or d.match_id not in {m for l in seller_scope(acc) for m in core.market.by_lot.get(l, [])}:
            raise HTTPException(404, "no such dispute")
        return await send_evidence_pack(core, dispute_id)

    @app.get("/api/evidence/{match_id}.pdf")
    def evidence_pdf(match_id: str, request: Request, dispute: str = "PREVIEW") -> Response:
        from .disputes import build_pack
        from ..evidence import build_evidence_pdf
        acc = me(request)
        m = core.market.matches.get(match_id)
        if not m:
            raise HTTPException(404, "no such match")
        lot = core.market.lots[m.fill.lot_id]
        if acc.id not in (lot.seller_id, core.market.wishes[m.fill.wish_id].buyer_id):
            raise HTTPException(403, "not your sale")
        pdf = build_evidence_pdf(build_pack(core, match_id, dispute, "Preview"))
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="criee-evidence-{match_id}.pdf"'})

    # ------------------------------------------------------------------ public

    @app.get("/api/board")
    def get_board() -> dict:
        return board(core, utcnow())

    @app.get("/api/lots/{lid}")
    def get_lot(lid: str) -> dict:
        if lid not in core.market.lots:
            raise HTTPException(404, "no such lot")
        v = lot_view(core, lid, utcnow())
        lot = core.market.lots[lid]
        v["seed"] = core.market.revealed.get(lid)
        return v

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        q = core.subscribe()

        async def gen():
            try:
                yield f"event: hello\ndata: {json.dumps({'head': core.ledger.head[0]})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                        safe = {"seq": msg["seq"], "type": msg["type"], "at": msg["at"],
                                "lot_id": msg["body"].get("lot_id") or (msg["body"].get("fill") or {}).get("lot_id"),
                                "match_id": msg["body"].get("match_id") or (msg["body"].get("fill") or {}).get("match_id"),
                                "hash": msg["hash"]}
                        yield f"event: ledger\ndata: {json.dumps(safe)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                core.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    PUBLIC_TYPES = {"wish.signed", "wish.paused", "wish.resumed", "wish.revoked", "lot.listed", "lot.withdrawn",
                    "clock.tick", "call.cleared", "match.made", "lot.closed", "threshold.committed",
                    "threshold.reached", "settlement.authorized", "settlement.auth_failed", "settlement.captured",
                    "settlement.voided", "settlement.refunded", "settlement.tracked", "pickup.scanned",
                    "pickup.no_show", "payout.sent", "payout.settled", "burst.measured", "dispute.opened",
                    "dispute.evidence_sent", "dispute.resolved"}

    def redact(ev) -> dict:
        d = ev.public()
        b = dict(d["body"])
        for k in ("signature", "pickup_code", "debug_id", "listing"):
            if k in b:
                b[k] = "…" if k != "listing" else {"name": (b[k] or {}).get("name")}
        if "terms" in b and ev.type == "wish.signed":
            b["terms"] = {"category": b["terms"]["category"], "limit_cents": b["terms"]["limit_cents"]}
        d["body"] = b
        return d

    @app.get("/api/ledger")
    def ledger(after: int = 0, limit: int = 200) -> dict:
        evs = [redact(e) for e in core.ledger.events(after=after, limit=min(limit, 1000))]
        return {"events": evs, "head": core.ledger.head}

    @app.get("/api/ledger/verify")
    def ledger_verify() -> dict:
        return core.ledger.verify() | {"public_key": core.ledger.signer.public_pem()}

    @app.get("/api/images/{name}")
    def image(name: str) -> FileResponse:
        if not (name.startswith(("img_", "crop_")) and name.endswith(".jpg")) or "/" in name or ".." in name:
            raise HTTPException(404, "not found")
        f = images / name
        if not f.exists():
            raise HTTPException(404, "not found")
        return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    # ------------------------------------------------------------------ judge mode

    @app.post("/api/play")
    def play(request: Request) -> JSONResponse:
        from ..demo import ensure_demo_market
        acc = auth.current(request, core)
        if acc is None:
            acc = core.create_account(f"Judge {secrets.choice(['Lime', 'Plum', 'Rye', 'Fig', 'Sage'])}",
                                      ["buyer", "seller"])
        for r in ("buyer", "seller"):
            if r not in acc.roles:
                core.emit("account.role_added", {"account_id": acc.id, "role": r})
        if not acc.vaulted and (core.ledger.kv_get("vault:demo") or cfg.demo_vault_id):
            demo = core.ledger.kv_get("vault:demo") or cfg.demo_vault_id
            core.ledger.kv_set(f"vault:{acc.id}", demo)
            core.ledger.kv_set(f"vault_label:{acc.id}", "Demo PayPal wallet (sandbox)")
            core.emit("buyer.vaulted", {"account_id": acc.id, "method": "demo_wallet",
                                        "token_ref": hashlib.sha256(demo.encode()).hexdigest()[:12]})
        if not acc.shop:
            core.emit("seller.shop", {"account_id": acc.id, "shop": {"name": f"{acc.handle}'s stall",
                                                                     "place": "Place Kléber", "lat": 48.5834,
                                                                     "lon": 7.7458}})
        ensure_demo_market(core)
        resp = JSONResponse({"id": acc.id, "handle": acc.handle})
        auth.set_session(resp, core, acc.id, secure)
        return resp

    # ------------------------------------------------------------------ webhooks, MCP, static

    from .webhooks import router as webhook_router
    app.include_router(webhook_router)
    try:
        from ..mcp_server import mount_mcp
        mount_mcp(app, core)
    except ImportError as e:
        log.warning("MCP server not mounted: %s", e)

    if WEB.exists():
        app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str) -> FileResponse:
            f = WEB / path
            if path and f.is_file() and WEB in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    return app
