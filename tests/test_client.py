from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tests.conftest import TOKEN, audit_lines, writes_sent
from wise_mcp.errors import PolicyError, ScaRequiredError, WiseApiError

pytestmark = pytest.mark.anyio


async def _nosleep(_: float) -> None:
    return None


async def test_get_is_retried_on_5xx_and_429(make_server, mock_api: respx.MockRouter) -> None:
    _, app = make_server()
    app.client._sleep = _nosleep
    route = mock_api.get("/v1/me").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(429, headers={"retry-after": "1"}),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    assert await app.client.get("/v1/me") == {"id": 1}
    assert route.call_count == 3


async def test_writes_are_never_retried(make_server, mock_api: respx.MockRouter) -> None:
    _, app = make_server(WISE_ENABLE="invoices")
    app.client._sleep = _nosleep
    route = mock_api.post("/v2/profiles/1/acquiring/payment-requests").mock(return_value=httpx.Response(503))
    with pytest.raises(WiseApiError):
        await app.client.request("POST", "/v2/profiles/1/acquiring/payment-requests", json={})
    assert route.call_count == 1


async def test_token_sent_but_never_echoed_in_errors(make_server, mock_api: respx.MockRouter) -> None:
    _, app = make_server()
    route = mock_api.get("/v1/me").mock(return_value=httpx.Response(401, json={"message": f"bad token {TOKEN}"}))
    with pytest.raises(WiseApiError) as err:
        await app.client.get("/v1/me")
    assert TOKEN not in str(err.value)
    assert route.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in repr(app.settings)


async def test_blocked_write_never_hits_network_and_is_audited(make_server, mock_api: respx.MockRouter) -> None:
    _, app = make_server(WISE_ENABLE="invoices")
    mock_api.route().mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(PolicyError):
        await app.client.request("POST", "/v1/transfers", json={"targetAccount": 1})
    with pytest.raises(PolicyError):
        await app.client.request("POST", "https://evil.example/v1/transfers")
    assert mock_api.calls.call_count == 0
    events = [e["event"] for e in audit_lines(app)]
    assert events == ["write_blocked", "write_blocked"]


async def test_allowed_write_is_audited_without_secrets(make_server, mock_api: respx.MockRouter) -> None:
    _, app = make_server(WISE_ENABLE="recipients")
    mock_api.post("/v1/accounts").mock(return_value=httpx.Response(200, json={"id": 9}))
    await app.client.request(
        "POST", "/v1/accounts", json={"details": {"iban": "DE89370400440532013000"}, "accountHolderName": "A"}
    )
    raw = app.settings.audit_path.read_text(encoding="utf-8")
    assert "DE89370400440532013000" not in raw
    assert TOKEN not in raw
    assert [e["event"] for e in audit_lines(app)] == ["write_attempt", "write_ok"]


async def test_write_fails_closed_if_audit_log_unwritable(make_server, mock_api: respx.MockRouter, tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    _, app = make_server(WISE_ENABLE="recipients", WISE_AUDIT_LOG=str(blocker / "sub" / "audit.jsonl"))
    route = mock_api.post("/v1/accounts").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(Exception, match="audit"):
        await app.client.request("POST", "/v1/accounts", json={})
    assert route.call_count == 0


async def test_sca_challenge_without_key_gives_clear_error(make_server, mock_api: respx.MockRouter) -> None:
    _, app = make_server()
    mock_api.get("/v1/profiles/1/balance-statements/2/statement.json").mock(
        return_value=httpx.Response(403, headers={"x-2fa-approval-result": "REJECTED", "x-2fa-approval": "ott-1"})
    )
    with pytest.raises(ScaRequiredError, match="WISE_SCA_PRIVATE_KEY_PATH"):
        await app.client.get("/v1/profiles/1/balance-statements/2/statement.json", {"currency": "EUR"})


async def test_sca_challenge_is_signed_and_retried(make_server, mock_api: respx.MockRouter, tmp_path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "private.pem"
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    _, app = make_server(WISE_SCA_PRIVATE_KEY_PATH=str(key_path))
    route = mock_api.get("/v1/profiles/1/balance-statements/2/statement.json").mock(
        side_effect=[
            httpx.Response(403, headers={"x-2fa-approval-result": "REJECTED", "x-2fa-approval": "ott-xyz"}),
            httpx.Response(200, json={"transactions": []}),
        ]
    )
    assert await app.client.get("/v1/profiles/1/balance-statements/2/statement.json") == {"transactions": []}
    retry = route.calls[1].request
    assert retry.headers["x-2fa-approval"] == "ott-xyz"
    key.public_key().verify(
        base64.b64decode(retry.headers["x-signature"]), b"ott-xyz", padding.PKCS1v15(), hashes.SHA256()
    )


async def test_sandbox_base_url(make_server) -> None:
    _, app = make_server(WISE_SANDBOX="true")
    with respx.mock(base_url="https://api.wise-sandbox.com") as router:
        router.get("/v1/me").mock(return_value=httpx.Response(200, json={"ok": True}))
        assert await app.client.get("/v1/me") == {"ok": True}


async def test_non_json_and_empty_bodies(make_server, mock_api: respx.MockRouter) -> None:
    _, app = make_server(WISE_ENABLE="webhooks")
    mock_api.delete("/v3/profiles/1/subscriptions/abc").mock(return_value=httpx.Response(204))
    assert await app.client.request("DELETE", "/v3/profiles/1/subscriptions/abc") == {}
    mock_api.get("/v1/me").mock(return_value=httpx.Response(200, text="hello"))
    assert await app.client.get("/v1/me") == {"raw": "hello"}
    assert writes_sent(mock_api) == ["DELETE /v3/profiles/1/subscriptions/abc"]
    json.dumps(audit_lines(app))
