"""Exception hierarchy. Every message here is safe to show to the MCP client (no secrets)."""

from __future__ import annotations


class WiseMcpError(Exception):
    """Base class for all errors raised by this package."""


class ConfigError(WiseMcpError):
    """Invalid or missing configuration."""


class PolicyError(WiseMcpError):
    """A request was refused by the local permission policy (before any network call)."""


class LimitError(WiseMcpError):
    """A local amount cap (per transaction or per day) would be exceeded."""


class ConfirmationError(WiseMcpError):
    """A two-step confirmation token is missing, invalid, expired, reused or bound to other params."""


class ScaRequiredError(WiseMcpError):
    """Wise requires Strong Customer Authentication (SCA) and it could not be completed."""


class WiseApiError(WiseMcpError):
    """The Wise API returned an error response."""

    def __init__(self, status: int, message: str, *, code: str | None = None, trace_id: str | None = None):
        self.status = status
        self.code = code
        self.trace_id = trace_id
        suffix = f" (code={code})" if code else ""
        trace = f" [x-trace-id={trace_id}]" if trace_id else ""
        super().__init__(f"Wise API error {status}: {message}{suffix}{trace}")
