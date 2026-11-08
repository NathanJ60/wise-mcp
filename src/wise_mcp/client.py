"""The single HTTP client used by every tool.

Security properties:

* Base URL is fixed (production or sandbox). Callers only pass a *path*; absolute URLs are refused.
* Every call is authorised by :class:`~wise_mcp.policy.Policy` before any network I/O.
* Every non-GET attempt is written to the audit log first (fail closed if the log is unwritable),
  then its outcome is recorded. Blocked attempts are audited too.
* Only GET requests are retried (429 / 5xx / transport errors, exponential backoff).
* The token is never included in errors, logs or audit records.
* Optional SCA: if Wise answers 403 with a one-time token, it is signed with the private key at
  ``WISE_SCA_PRIVATE_KEY_PATH`` and the request is retried once.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from wise_mcp import __version__
from wise_mcp.audit import AuditLog
from wise_mcp.config import Settings
from wise_mcp.errors import ConfigError, PolicyError, ScaRequiredError, WiseApiError
from wise_mcp.policy import Policy, Route
from wise_mcp.redact import redact_text

log = logging.getLogger("wise_mcp.client")

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_GET_ATTEMPTS = 3
SCA_DOCS = "https://docs.wise.com/guides/developer/auth-and-security/sca-and-2fa"


def sign_sca_challenge(one_time_token: str, key_path: Path) -> str:
    """Sign a Wise SCA one-time token with an RSA private key (SHA256withRSA, base64)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ScaRequiredError(f"Cannot load SCA private key: {type(exc).__name__}") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ScaRequiredError("WISE_SCA_PRIVATE_KEY_PATH must be an RSA private key (PEM)")
    signature = key.sign(one_time_token.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


class WiseClient:
    def __init__(
        self,
        settings: Settings,
        policy: Policy,
        audit: AuditLog,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.audit = audit
        self._sleep = sleep
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.settings.base_url,
                timeout=httpx.Timeout(self.settings.timeout_seconds, connect=10.0),
                follow_redirects=False,
                transport=self._transport,
                headers={
                    "Accept": "application/json",
                    "User-Agent": f"wise-mcp/{__version__}",
                },
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------------------------------
    async def get(self, path: str, params: Mapping[str, Any] | None = None, *, tool: str = "") -> Any:
        return await self.request("GET", path, params=params, tool=tool)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        tool: str = "",
    ) -> Any:
        method = method.upper()
        is_write = method != "GET"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        if "://" in path or not path.startswith("/"):
            if is_write:
                self.audit.record("write_blocked", tool=tool, method=method, path=path, reason="absolute URL")
            raise PolicyError("Only relative API paths are allowed")
        try:
            route = self.policy.authorize(method, path, clean_params)
        except PolicyError as exc:
            if is_write:
                self.audit.record("write_blocked", tool=tool, method=method, path=path, reason=str(exc))
            raise
        if not self.settings.token:
            raise ConfigError("WISE_API_TOKEN is not set")
        if is_write:
            # Fail closed: if this raises (unwritable log), nothing is sent.
            self.audit.record("write_attempt", tool=tool, method=method, path=path, scope=route.scope.value, body=json)
        try:
            result = await self._send(method, path, route, clean_params, json, headers)
        except Exception as exc:
            if is_write:
                self.audit.record(
                    "write_failed",
                    tool=tool,
                    method=method,
                    path=path,
                    scope=route.scope.value,
                    error=str(exc),
                )
            raise
        if is_write:
            self.audit.record(
                "write_ok",
                tool=tool,
                method=method,
                path=path,
                scope=route.scope.value,
                result_id=result.get("id") if isinstance(result, dict) else None,
            )
        return result

    # ------------------------------------------------------------------------------------------
    async def _send(
        self,
        method: str,
        path: str,
        route: Route,
        params: Mapping[str, Any],
        body: Any,
        extra_headers: Mapping[str, str] | None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.settings.token}"}
        if extra_headers:
            headers.update(extra_headers)
        attempts = MAX_GET_ATTEMPTS if method == "GET" else 1
        sca_done = False
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._client().request(method, path, params=params, json=body, headers=headers)
            except httpx.TransportError as exc:
                if attempt < attempts:
                    await self._sleep(min(0.5 * 2 ** (attempt - 1), 8))
                    continue
                raise WiseApiError(0, f"Network error contacting Wise ({type(exc).__name__})") from None

            if resp.status_code == 403 and not sca_done and self._is_sca_challenge(resp):
                ott = resp.headers["x-2fa-approval"]
                key_path = self.settings.sca_private_key_path
                if key_path is None:
                    raise ScaRequiredError(
                        f"Wise requires Strong Customer Authentication for {method} {route.template}. "
                        "Upload a public key in Wise (Settings > API tokens > Manage public keys) and set "
                        f"WISE_SCA_PRIVATE_KEY_PATH to the matching RSA private key. See {SCA_DOCS}"
                    )
                headers["x-2fa-approval"] = ott
                headers["X-Signature"] = sign_sca_challenge(ott, key_path)
                sca_done = True
                continue

            if method == "GET" and resp.status_code in RETRY_STATUSES and attempt < attempts:
                await self._sleep(self._retry_delay(resp, attempt))
                continue
            return self._parse(resp, method, route)

    @staticmethod
    def _is_sca_challenge(resp: httpx.Response) -> bool:
        result = resp.headers.get("x-2fa-approval-result", "").upper()
        return result == "REJECTED" and bool(resp.headers.get("x-2fa-approval"))

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 10.0)
            except ValueError:
                pass
        return float(min(0.5 * 2 ** (attempt - 1), 8.0))

    def _parse(self, resp: httpx.Response, method: str, route: Route) -> Any:
        if resp.status_code >= 400:
            message, code = self._error_message(resp)
            if resp.status_code == 403 and resp.headers.get("x-2fa-approval-result"):
                raise ScaRequiredError(f"Wise rejected the SCA signature for {method} {route.template}: {message}")
            raise WiseApiError(resp.status_code, message, code=code, trace_id=resp.headers.get("x-trace-id"))
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": redact_text(resp.text[:2000], (self.settings.token,))}

    def _error_message(self, resp: httpx.Response) -> tuple[str, str | None]:
        code: str | None = None
        message = resp.reason_phrase or "error"
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            errors = data.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = str(errors[0].get("message") or message)
                code = errors[0].get("code")
            else:
                message = str(data.get("message") or data.get("error_description") or data.get("error") or message)
                code = data.get("code") or (data.get("error") if data.get("message") else None)
        elif resp.text:
            message = resp.text[:300]
        if resp.status_code == 401:
            message += " (check WISE_API_TOKEN; some endpoints are not available to personal tokens)"
        return redact_text(message[:500], (self.settings.token,)), code
