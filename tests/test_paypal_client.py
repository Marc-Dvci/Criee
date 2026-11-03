"""Browser authentication must never reuse the merchant's privileged bearer token."""
import asyncio

import httpx
from types import SimpleNamespace

from criee.settlement.paypal import PayPalClient


def test_v6_token_uses_the_browser_safe_oauth_response_type():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "BROWSER-SAFE", "expires_in": 900})

    async def run():
        client = PayPalClient("client", "secret")
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        client._token = "PRIVILEGED-SERVER-TOKEN"
        result = await client.browser_client_token()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result.ok and result.body["access_token"] == "BROWSER-SAFE"
    [request] = requests
    assert str(request.url).endswith("/v1/oauth2/token")
    assert b"response_type=client_token" in request.content
    assert b"grant_type=client_credentials" in request.content
    assert request.headers["authorization"].startswith("Basic ")
    assert "PRIVILEGED" not in str(request.headers)


def test_toolkit_shared_core_exposes_invoice_and_dispute_tools_without_framework_adapters():
    from criee.ai.assistant import _toolkit
    core = SimpleNamespace(cfg=SimpleNamespace(paypal_live=True, paypal_client_id="client", paypal_secret="secret"))
    api, declarations = _toolkit(core)
    assert api is not None, "the deployed assistant must load the genuine PayPal Agent Toolkit"
    assert {"create_invoice", "send_invoice", "list_disputes", "get_dispute"} <= {d["name"] for d in declarations}
    assert _toolkit(core)[0] is api
