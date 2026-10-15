"""A thin typed client for the PayPal REST endpoints Criée uses, plus an in-memory double.

Every mutating call carries a `PayPal-Request-Id` derived from the Criée object it settles, so a
retry after a timeout returns the original result instead of charging twice.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import httpx

from ..domain import money

SANDBOX = "https://api-m.sandbox.paypal.com"


@dataclass
class PayPalResult:
    status: int
    body: dict[str, Any]
    debug_id: Optional[str]
    ms: float

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def issue(self) -> str:
        d = self.body.get("details") or []
        if d and isinstance(d, list):
            return d[0].get("issue") or self.body.get("name", "")
        return self.body.get("name") or self.body.get("error") or f"HTTP {self.status}"


class PayPalError(RuntimeError):
    def __init__(self, op: str, res: PayPalResult):
        super().__init__(f"{op}: {res.issue()} (HTTP {res.status}, debug {res.debug_id})")
        self.res = res


class PayPalAPI(Protocol):
    currency: str

    async def authorize_vaulted(self, vault_id: str, cents: int, ref: str, description: str) -> PayPalResult: ...
    async def capture(self, authorization_id: str, cents: int, ref: str, invoice_id: str) -> PayPalResult: ...
    async def void(self, authorization_id: str, ref: str) -> PayPalResult: ...
    async def refund(self, capture_id: str, cents: int, ref: str, note: str) -> PayPalResult: ...
    async def track(self, order_id: str, capture_id: str, code: str, ref: str) -> PayPalResult: ...
    async def payout(self, receiver_email: str, cents: int, ref: str, note: str) -> PayPalResult: ...
    async def get_payout(self, batch_id: str) -> PayPalResult: ...
    async def setup_token(self, return_url: str, cancel_url: str, description: str, ref: str) -> PayPalResult: ...
    async def get_setup_token(self, setup_token_id: str) -> PayPalResult: ...
    async def payment_token(self, setup_token_id: str, ref: str) -> PayPalResult: ...
    async def list_disputes(self) -> PayPalResult: ...
    async def get_dispute(self, dispute_id: str) -> PayPalResult: ...
    async def provide_evidence(self, dispute_id: str, evidence: list[dict], files: list[tuple[str, bytes]]) -> PayPalResult: ...
    async def sandbox_adjudicate(self, dispute_id: str, outcome: str) -> PayPalResult: ...
    async def verify_webhook(self, headers: dict[str, str], event: dict, webhook_id: str) -> bool: ...


def auth_of(order: dict) -> dict:
    pu = (order.get("purchase_units") or [{}])[0]
    return ((pu.get("payments") or {}).get("authorizations") or [{}])[0]


def request_id(*parts: str) -> str:
    return "criee-" + hashlib.sha256(":".join(parts).encode()).hexdigest()[:40]


class PayPalClient:
    def __init__(self, client_id: str, secret: str, currency: str = "EUR", base: str = SANDBOX,
                 brand: str = "Criée", max_concurrency: int = 40):
        self.client_id, self.secret, self.currency, self.base, self.brand = client_id, secret, currency, base, brand
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0),
                                       limits=httpx.Limits(max_connections=max_concurrency))
        self._sem = asyncio.Semaphore(max_concurrency)
        self._token_lock = asyncio.Lock()

    def amount(self, cents: int) -> dict:
        return {"currency_code": self.currency, "value": money(cents)}

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            cred = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
            r = await self._http.post(f"{self.base}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                                      headers={"Authorization": f"Basic {cred}"})
            r.raise_for_status()
            body = r.json()
            self._token, self._token_exp = body["access_token"], time.time() + int(body.get("expires_in", 3000))
            return self._token

    async def call(self, method: str, path: str, body: Optional[dict] = None, rid: Optional[str] = None,
                   headers: Optional[dict] = None, files: Any = None) -> PayPalResult:
        async with self._sem:
            h = {"Authorization": f"Bearer {await self._access_token()}", "Prefer": "return=representation"}
            if rid:
                h["PayPal-Request-Id"] = rid
            if headers:
                h.update(headers)
            t0 = time.perf_counter()
            for attempt in range(3):
                try:
                    if files is not None:
                        r = await self._http.request(method, self.base + path, headers=h, files=files)
                    else:
                        h["Content-Type"] = "application/json"
                        r = await self._http.request(method, self.base + path, headers=h,
                                                     content=json.dumps(body) if body is not None else None)
                    break
                except (httpx.TransportError, httpx.TimeoutException):
                    if attempt == 2 or not rid and method != "GET":
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
            ms = (time.perf_counter() - t0) * 1000
            try:
                data = r.json() if r.content else {}
            except ValueError:
                data = {"raw": r.text[:500]}
            return PayPalResult(r.status_code, data, r.headers.get("paypal-debug-id"), ms)

    # -- payments ----------------------------------------------------------------------

    async def authorize_vaulted(self, vault_id: str, cents: int, ref: str, description: str) -> PayPalResult:
        """Create an AUTHORIZE order against a saved PayPal wallet. The buyer is not present."""
        return await self.call("POST", "/v2/checkout/orders", {
            "intent": "AUTHORIZE",
            "purchase_units": [{"reference_id": ref, "custom_id": ref, "invoice_id": ref,
                                "description": description[:127], "amount": self.amount(cents)}],
            "payment_source": {"paypal": {
                "vault_id": vault_id,
                "stored_credential": {"payment_initiator": "MERCHANT", "usage": "SUBSEQUENT",
                                      "usage_pattern": "UNSCHEDULED_POSTPAID"}}}}, rid=request_id("auth", ref))

    async def capture(self, authorization_id: str, cents: int, ref: str, invoice_id: str) -> PayPalResult:
        return await self.call("POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                               {"amount": self.amount(cents), "final_capture": True, "invoice_id": invoice_id},
                               rid=request_id("capture", ref))

    async def void(self, authorization_id: str, ref: str) -> PayPalResult:
        return await self.call("POST", f"/v2/payments/authorizations/{authorization_id}/void", {},
                               rid=request_id("void", ref))

    async def refund(self, capture_id: str, cents: int, ref: str, note: str) -> PayPalResult:
        return await self.call("POST", f"/v2/payments/captures/{capture_id}/refund",
                               {"amount": self.amount(cents), "note_to_payer": note[:255]},
                               rid=request_id("refund", ref))

    async def track(self, order_id: str, capture_id: str, code: str, ref: str) -> PayPalResult:
        return await self.call("POST", f"/v2/checkout/orders/{order_id}/track", {
            "capture_id": capture_id, "tracking_number": f"CRIEE-{code}", "carrier": "OTHER",
            "carrier_name_other": "Collected in store", "notify_payer": False}, rid=request_id("track", ref))

    async def payout(self, receiver_email: str, cents: int, ref: str, note: str) -> PayPalResult:
        return await self.call("POST", "/v1/payments/payouts", {
            "sender_batch_header": {"sender_batch_id": ref, "email_subject": f"{self.brand}: you have been paid"},
            "items": [{"recipient_type": "EMAIL", "receiver": receiver_email, "sender_item_id": ref,
                       "amount": {"currency": self.currency, "value": money(cents)}, "note": note[:4000]}]},
            rid=request_id("payout", ref))

    async def get_payout(self, batch_id: str) -> PayPalResult:
        return await self.call("GET", f"/v1/payments/payouts/{batch_id}")

    # -- vault ---------------------------------------------------------------------------

    async def setup_token(self, return_url: str, cancel_url: str, description: str, ref: str) -> PayPalResult:
        return await self.call("POST", "/v3/vault/setup-tokens", {
            "payment_source": {"paypal": {
                "description": description[:127], "usage_pattern": "UNSCHEDULED_POSTPAID",
                "usage_type": "MERCHANT", "customer_type": "CONSUMER", "permit_multiple_payment_tokens": True,
                "experience_context": {"brand_name": self.brand, "shipping_preference": "NO_SHIPPING",
                                       "vault_instruction": "ON_PAYER_APPROVAL",
                                       "return_url": return_url, "cancel_url": cancel_url}}}},
            rid=request_id("setup", ref))

    async def get_setup_token(self, setup_token_id: str) -> PayPalResult:
        return await self.call("GET", f"/v3/vault/setup-tokens/{setup_token_id}")

    async def payment_token(self, setup_token_id: str, ref: str) -> PayPalResult:
        return await self.call("POST", "/v3/vault/payment-tokens",
                               {"payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}}},
                               rid=request_id("ptoken", ref))

    # -- disputes ------------------------------------------------------------------------

    async def list_disputes(self) -> PayPalResult:
        return await self.call("GET", "/v1/customer/disputes?page_size=50")

    async def get_dispute(self, dispute_id: str) -> PayPalResult:
        return await self.call("GET", f"/v1/customer/disputes/{dispute_id}")

    async def provide_evidence(self, dispute_id: str, evidence: list[dict], files: list[tuple[str, bytes]]) -> PayPalResult:
        parts: list = [("input", (None, json.dumps({"evidences": evidence}), "application/json"))]
        parts += [("evidence-file", (name, data, "application/pdf")) for name, data in files]
        return await self.call("POST", f"/v1/customer/disputes/{dispute_id}/provide-evidence", files=parts)

    async def sandbox_require_evidence(self, dispute_id: str, action: str = "SELLER_EVIDENCE") -> PayPalResult:
        return await self.call("POST", f"/v1/customer/disputes/{dispute_id}/require-evidence", {"action": action})

    async def sandbox_adjudicate(self, dispute_id: str, outcome: str) -> PayPalResult:
        return await self.call("POST", f"/v1/customer/disputes/{dispute_id}/adjudicate",
                               {"adjudication_outcome": outcome})

    # -- webhooks ------------------------------------------------------------------------

    async def verify_webhook(self, headers: dict[str, str], event: dict, webhook_id: str) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        res = await self.call("POST", "/v1/notifications/verify-webhook-signature", {
            "auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"), "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"), "webhook_id": webhook_id,
            "webhook_event": event})
        return res.ok and res.body.get("verification_status") == "SUCCESS"

    async def ensure_webhook(self, url: str, events: list[str]) -> str:
        existing = await self.call("GET", "/v1/notifications/webhooks")
        for wh in existing.body.get("webhooks", []):
            if wh.get("url") == url:
                return wh["id"]
            if wh.get("url", "").endswith("/api/paypal/webhook"):
                await self.call("DELETE", f"/v1/notifications/webhooks/{wh['id']}")
        res = await self.call("POST", "/v1/notifications/webhooks",
                              {"url": url, "event_types": [{"name": e} for e in events]})
        if not res.ok:
            raise PayPalError("webhook", res)
        return res.body["id"]

    async def aclose(self) -> None:
        await self._http.aclose()


# ----------------------------------------------------------------------------- in-memory double

@dataclass
class FakePayPal:
    """Deterministic stand-in for tests and keyless local runs. Same signatures, same shapes."""
    currency: str = "EUR"
    decline: set[str] = field(default_factory=set)       # refs whose authorization is refused
    latency_s: float = 0.0
    calls: list[tuple[str, str]] = field(default_factory=list)
    _seen: dict[str, PayPalResult] = field(default_factory=dict)
    _auths: dict[str, dict] = field(default_factory=dict)

    def _id(self, kind: str, ref: str) -> str:
        return kind.upper()[:2] + hashlib.sha256(f"{kind}:{ref}".encode()).hexdigest()[:15].upper()

    async def _idem(self, key: str, fn) -> PayPalResult:
        if key in self._seen:
            return self._seen[key]
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        res = fn()
        self._seen[key] = res
        return res

    def _ok(self, body: dict, status: int = 201) -> PayPalResult:
        return PayPalResult(status, body, "fake" + uuid.uuid4().hex[:8], 1.0)

    async def authorize_vaulted(self, vault_id, cents, ref, description):
        self.calls.append(("authorize", ref))

        def go():
            if ref in self.decline:
                return PayPalResult(422, {"name": "UNPROCESSABLE_ENTITY",
                                          "details": [{"issue": "INSTRUMENT_DECLINED"}]}, "fake", 1.0)
            aid = self._id("auth", ref)
            self._auths[aid] = {"cents": cents, "status": "CREATED"}
            return self._ok({"id": self._id("order", ref), "status": "COMPLETED", "purchase_units": [
                {"payments": {"authorizations": [{"id": aid, "status": "CREATED"}]}}]})
        return await self._idem("auth:" + ref, go)

    async def capture(self, authorization_id, cents, ref, invoice_id):
        self.calls.append(("capture", ref))
        return await self._idem("cap:" + ref, lambda: self._ok({"id": self._id("cap", ref), "status": "COMPLETED"}))

    async def void(self, authorization_id, ref):
        self.calls.append(("void", ref))
        return await self._idem("void:" + ref, lambda: self._ok({}, 204))

    async def refund(self, capture_id, cents, ref, note):
        self.calls.append(("refund", ref))
        return await self._idem("ref:" + ref, lambda: self._ok({"id": self._id("ref", ref), "status": "COMPLETED"}))

    async def track(self, order_id, capture_id, code, ref):
        self.calls.append(("track", ref))
        return await self._idem("trk:" + ref, lambda: self._ok({"id": order_id}))

    async def payout(self, receiver_email, cents, ref, note):
        self.calls.append(("payout", ref))
        return await self._idem("pay:" + ref, lambda: self._ok(
            {"batch_header": {"payout_batch_id": self._id("batch", ref), "batch_status": "PENDING"}}))

    async def get_payout(self, batch_id):
        return self._ok({"batch_header": {"payout_batch_id": batch_id, "batch_status": "SUCCESS"},
                         "items": [{"transaction_status": "SUCCESS"}]}, 200)

    async def setup_token(self, return_url, cancel_url, description, ref):
        st = self._id("setup", ref)
        return self._ok({"id": st, "status": "PAYER_ACTION_REQUIRED",
                         "links": [{"rel": "approve", "href": f"{return_url}?approval_token_id={st}"}]})

    async def get_setup_token(self, setup_token_id):
        return self._ok({"id": setup_token_id, "status": "APPROVED"}, 200)

    async def payment_token(self, setup_token_id, ref):
        return self._ok({"id": self._id("vault", ref), "customer": {"id": self._id("cust", ref)}})

    async def list_disputes(self):
        return self._ok({"items": []}, 200)

    async def get_dispute(self, dispute_id):
        return self._ok({"dispute_id": dispute_id, "status": "WAITING_FOR_SELLER_RESPONSE"}, 200)

    async def provide_evidence(self, dispute_id, evidence, files):
        self.calls.append(("evidence", dispute_id))
        return self._ok({"links": []}, 200)

    async def sandbox_require_evidence(self, dispute_id, action="SELLER_EVIDENCE"):
        return self._ok({}, 200)

    async def sandbox_adjudicate(self, dispute_id, outcome):
        return self._ok({}, 200)

    async def verify_webhook(self, headers, event, webhook_id):
        return headers.get("x-fake-signature") == "ok"

    async def aclose(self):
        return None
