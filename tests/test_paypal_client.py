"""Browser authentication must never reuse the merchant's privileged bearer token."""
import asyncio

import httpx

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
