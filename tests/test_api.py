from __future__ import annotations

import asyncio
import base64
import json
import time

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
    stranger = TestClient(client.app)
    stranger.post("/api/session", json={"role": "seller"}).raise_for_status()
    assert stranger.post("/api/pickups/scan", json={"code": code}).status_code == 404
    sale_id = sales[0]["id"]
    assert stranger.get(f"/api/evidence/{sale_id}.pdf").status_code == 403
    assert stranger.post("/api/refunds", json={"match_id": sale_id, "cents": 1}).status_code == 404
    assert stranger.get("/api/console").json()["sales"] == []
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


def test_agent_bearer_cannot_register_a_signing_key_or_use_browser_authority(client):
    _signed_wish(client)
    token = client.post("/api/agent-token").json()["token"]
    headers = {"authorization": f"Bearer {token}"}
    # Even a caller holding a browser cookie cannot upgrade the bearer capability.
    assert client.get("/api/me", headers=headers).json()["account"] is None
    assert client.post("/api/devicekey/register", headers=headers, json={"jwk": Device().jwk}).status_code == 401
    assert client.post("/api/vault/start", headers=headers).status_code == 401
    assert client.post("/api/agent-token", headers=headers).status_code == 401


def test_vault_approval_is_owned_by_session_and_completion_is_idempotent(client):
    client.post("/api/session", json={"role": "buyer"}).raise_for_status()
    start = client.post("/api/vault/start")
    assert start.headers["cache-control"] == "no-store"
    token = start.json()["setup_token"]
    other = TestClient(client.app)
    other.post("/api/session", json={"role": "buyer"}).raise_for_status()
    body = {"setup_token": token}
    assert other.post("/api/vault/complete", json=body).status_code == 404
    assert other.get(f"/api/vault/return?approval_token_id={token}").status_code == 404
    assert not other.get("/api/me").json()["account"]["vaulted"]
    assert client.post("/api/vault/complete", json=body).json() == {"ok": True}
    head = client.core.ledger.head
    assert client.post("/api/vault/complete", json=body).json() == {"ok": True}
    assert client.core.ledger.head == head
    assert client.get("/api/me").json()["account"]["vault_label"] == "PayPal (sandbox)"


def test_browser_client_token_never_returns_server_credentials(tmp_path):
    from dataclasses import replace
    cfg = replace(make_config(tmp_path), paypal_client_id="client", paypal_secret="PRIVATE-SECRET")
    core = Core(cfg, FakePayPal())
    with TestClient(create_app(cfg, core, start_loops=False)) as c:
        assert c.get("/api/paypal/client-token").status_code == 401
        c.post("/api/session", json={"role": "buyer"}).raise_for_status()
        result = c.get("/api/paypal/client-token")
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json() == {"client_token": "OFFLINE-CLIENT-TOKEN", "currency": "EUR"}
        assert "PRIVATE-SECRET" not in result.text


def test_ledger_stream_has_progress_scope_and_a_terminal_result(client):
    client.post("/api/session", json={"role": "buyer"}).raise_for_status()
    for i in range(8):
        client.core.emit("test", {"i": i})
    result = client.get("/api/ledger/verify/stream?recent=3")
    frames = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
    assert result.headers["x-accel-buffering"] == "no"
    assert frames[0]["checked"] == 0 and not frames[0]["done"]
    assert frames[-1]["ok"] and frames[-1]["done"] and frames[-1]["events"] == 3
    assert frames[-1]["scope"] == "recent" and "PUBLIC KEY" in frames[-1]["public_key"]
    assert client.get("/api/ledger/verify/stream?recent=0").status_code == 422
    assert client.get("/api/ledger/verify/stream?recent=1001").status_code == 422
    assert client.get("/api/ledger?limit=-1").status_code == 422
    assert client.post("/api/ledger/verify/jobs?recent=0").status_code == 422
    assert client.get("/api/ledger/verify/jobs/missing").status_code == 404


def test_json_audit_progress_is_visible_before_completion_and_shares_running_work(client, monkeypatch):
    for i in range(768):
        client.core.emit("test", {"i": i})
    original = client.core.ledger.signer.verify

    def slow_verify(*args):
        time.sleep(0.001)
        return original(*args)

    monkeypatch.setattr(client.core.ledger.signer, "verify", slow_verify)
    start = client.post("/api/ledger/verify/jobs")
    assert start.status_code == 200 and start.headers["cache-control"] == "no-store"
    first = start.json()
    assert not first["done"] and first["checked"] == 0 and first["total"] == 768
    assert client.post("/api/ledger/verify/jobs").json()["id"] == first["id"]
    client.post("/api/session", json={"role": "buyer"}).raise_for_status()
    assert client.core.ledger.head[0] > first["to_seq"], "the market must remain responsive during the audit"
    intermediate = False
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = client.get(f"/api/ledger/verify/jobs/{first['id']}").json()
        intermediate |= 0 < result["checked"] < result["total"] and not result["done"]
        if result["done"]:
            break
        time.sleep(0.01)
    assert intermediate, "progress must be observable before the final result, without relying on SSE"
    assert result["done"] and result["ok"] and result["events"] == 768
    assert result["snapshot_head"] == first["snapshot_head"] and result["scope"] == "full"


def test_public_ledger_hides_account_data_and_private_wish_details(client):
    wish, *_ = _signed_wish(client)
    events = client.get("/api/ledger?limit=1000").json()["events"]
    account = next(e for e in events if e["type"] == "account.created")
    assert account["body"] == {"redacted": True}
    signed = next(e for e in events if e["type"] == "wish.signed")
    assert "readback" not in signed["body"] and "place" not in signed["body"]
    assert "buyer_id" not in signed["body"] and signed["body"]["signature"] == "…"
    assert set(signed["body"]["terms"]) == {"category", "limit_cents"}
    assert client.get("/api/ledger/verify").json()["ok"], "redaction must not alter the signed original"
    other = TestClient(client.app)
    other.post("/api/session", json={"role": "buyer"})
    draft = client.post("/api/wishes/compile", json={"text": "a baguette under 2 euros", "lat": 48.58, "lon": 7.75}).json()
    assert other.get(f"/api/drafts/{draft['id']}").status_code == 404


def test_offline_play_works_without_any_keys_or_demo_wallet(tmp_path):
    from dataclasses import replace
    cfg = replace(make_config(tmp_path), demo_vault_id=None, seller_payout_email=None)
    with TestClient(create_app(cfg, start_loops=False)) as c:
        assert c.get("/api/status").json()["paypal"] == "offline double"
        c.post("/api/play").raise_for_status()
        account = c.get("/api/me").json()["account"]
        assert account["vaulted"] and account["shop"] and set(account["roles"]) == {"buyer", "seller"}
        assert c.get("/api/paypal/client-token").status_code == 409
        assert c.get("/api/samples/shelf.jpg").content.startswith(b"\xff\xd8")
