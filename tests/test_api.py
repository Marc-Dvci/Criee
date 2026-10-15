from __future__ import annotations

import asyncio
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient

from criee.api.app import create_app
from criee.core import Core
from criee.settlement.paypal import FakePayPal
from criee.settlement.worker import SettlementWorker
from test_settlement import make_config


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Device:
    """A browser's WebCrypto P-256 key, signing raw (r||s) over the mandate digest."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        n = self.key.public_key().public_numbers()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big"))}

    def sign(self, digest_hex: str) -> str:
        r, s = decode_dss_signature(self.key.sign(bytes.fromhex(digest_hex), ec.ECDSA(hashes.SHA256())))
        return b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


@pytest.fixture
def client(tmp_path):
    cfg = make_config(tmp_path)
    core = Core(cfg, FakePayPal())
    core.worker = SettlementWorker(core)
    app = create_app(cfg, core, start_loops=False)
    with TestClient(app) as c:
        c.core = core
        yield c


def _settle(core, n=4):
    for _ in range(n):
        core.tick()
        asyncio.run(core.worker.step())


def _signed_wish(c, text="An organic sourdough loaf under 6 euros", place=(48.5797, 7.7540)):
    dev = Device()
    c.post("/api/session", json={"handle": "Léa", "role": "buyer"}).raise_for_status()
    cid = c.post("/api/devicekey/register", json={"jwk": dev.jwk}).json()["credential_id"]
    c.post("/api/vault/demo").raise_for_status()
    draft = c.post("/api/wishes/compile", json={"text": text, "lat": place[0], "lon": place[1], "place": "Krutenau"}).json()
    assert "readback" in draft, draft
    r = c.post(f"/api/drafts/{draft['id']}/sign", json={"kind": "device", "credential_id": cid,
                                                         "signature": dev.sign(draft["digest"])})
    r.raise_for_status()
    return r.json(), dev, cid, draft


def test_wish_is_signed_over_its_exact_terms(client):
    wish, dev, cid, draft = _signed_wish(client)
    assert wish["readback"].startswith("Buy one") and "€6.00" in wish["readback"]
    assert wish["terms_hash"] == draft["digest"]
    second = client.post("/api/wishes/compile", json={"text": "a baguette under 2 euros", "lat": 48.58, "lon": 7.75}).json()
    bad = client.post(f"/api/drafts/{second['id']}/sign", json={"kind": "device", "credential_id": cid,
                                                                "signature": dev.sign(draft["digest"])})
    assert bad.status_code == 400, "a signature over other terms is refused"


def test_seller_lot_clears_against_a_signed_wish_and_pays_out(client):
    _signed_wish(client)
    core = client.core
    seller = TestClient(client.app)
    seller.post("/api/session", json={"handle": "Fournil", "role": "seller"}).raise_for_status()
    seller.post("/api/shop", json={"name": "Fournil", "lat": 48.5797, "lon": 7.7540,
                                   "payout_email": "seller@example.com"}).raise_for_status()
    facts = [{"attr": "organic", "value": True, "evidence": "crop_abcdef123456"}]
    lot = seller.post("/api/lots", json={"name": "Pain au levain", "category": "bread", "quantity": 4, "mode": "clock",
                                         "start": 550, "floor": 250, "duration_min": 30, "facts": facts}).json()
    assert lot["status"] == "open"
    _settle(core)
    sales = seller.get("/api/console").json()["sales"]
    assert len(sales) == 1 and sales[0]["status"] == "held"
    code = client.get("/api/matches").json()["matches"][0]["pickup_code"]
    seller.post("/api/pickups/scan", json={"code": code}).raise_for_status()
    _settle(core)
    con = seller.get("/api/console").json()
    assert con["sales"][0]["status"] == "captured" and con["payouts"][0]["net"] == 528
    assert client.get("/api/ledger/verify").json()["ok"]
    board = client.get("/api/board").json()
    assert board["totals"]["ledger_events"] > 5


def test_buyer_can_pause_and_revoke_only_their_own_wish(client):
    wish, *_ = _signed_wish(client)
    assert client.post(f"/api/wishes/{wish['id']}/pause").json()["status"] == "paused"
    other = TestClient(client.app)
    other.post("/api/session", json={"role": "buyer"})
    assert other.post(f"/api/wishes/{wish['id']}/revoke").status_code == 404
    assert client.post(f"/api/wishes/{wish['id']}/revoke").json()["status"] == "revoked"


def test_agent_drafts_but_cannot_sign(client):
    wish, *_ = _signed_wish(client)
    token = client.post("/api/agent-token").json()["token"]
    agent = TestClient(client.app)
    hdr = {"authorization": f"Bearer {token}", "accept": "application/json, text/event-stream",
           "content-type": "application/json"}
    init = agent.post("/mcp/", headers=hdr, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    assert init.status_code == 200, init.text
    call = agent.post("/mcp/", headers=hdr, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "create_wish", "arguments": {"request": "two croissants under 1.50 euros on Saturday",
                                             "neighbourhood": "Krutenau"}}})
    assert call.status_code == 200, call.text
    text = json.dumps(call.json())
    assert "awaiting_signature" in text and "/sign/d_" in text
    assert len([w for w in client.core.market.wishes.values()]) == 1, "nothing is signed by the agent"
    assert TestClient(client.app).post("/mcp/", headers={**hdr, "authorization": "Bearer nope"},
                                       json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}).status_code == 401


def test_webhook_twice_changes_nothing(client):
    ev = {"id": "WH-1", "event_type": "CUSTOMER.DISPUTE.CREATED",
          "resource": {"dispute_id": "PP-D-1", "reason": "UNAUTHORISED", "status": "OPEN",
                       "dispute_amount": {"value": "4.20"}, "disputed_transactions": []}}
    h = {"x-fake-signature": "ok"}
    assert client.post("/api/paypal/webhook", json=ev, headers=h).json() == {"ok": True}
    head = client.core.ledger.head
    assert client.post("/api/paypal/webhook", json=ev, headers=h).json()["duplicate"]
    assert client.core.ledger.head == head
    assert client.post("/api/paypal/webhook", json={**ev, "id": "WH-2"}).status_code == 400


def test_ai_key_never_leaves_the_server(tmp_path):
    from dataclasses import replace
    cfg = replace(make_config(tmp_path), gemini_api_key="SECRET-KEY-123", ai_provider="gemini")
    core = Core(cfg, FakePayPal())
    app = create_app(cfg, core, start_loops=False)
    with TestClient(app) as c:
        assert "SECRET-KEY-123" not in c.get("/api/status").text
        assert "SECRET-KEY-123" not in c.get("/api/openapi.json").text
