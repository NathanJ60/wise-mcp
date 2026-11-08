"""Shared tool plumbing: the App container, the scope-gated registrar and argument types."""

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypeVar

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from wise_mcp.audit import AuditLog
from wise_mcp.client import WiseClient
from wise_mcp.config import Settings
from wise_mcp.confirm import ConfirmationManager
from wise_mcp.errors import PolicyError, WiseMcpError
from wise_mcp.limits import SpendLimiter
from wise_mcp.policy import Policy, Scope

log = logging.getLogger("wise_mcp.tools")

ProfileId = Annotated[int, Field(gt=0, description="Wise profile id (use list_profiles).")]
BalanceId = Annotated[int, Field(gt=0, description="Balance id (use list_balances).")]
TransferId = Annotated[int, Field(gt=0, description="Transfer id (use list_transfers).")]
RecipientId = Annotated[int, Field(gt=0, description="Recipient account id (use list_recipients).")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$", description="ISO 4217 currency code, e.g. EUR.")]
Uuid = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,64}$", description="Wise resource id (UUID).")]
IsoDateTime = Annotated[
    str,
    Field(
        pattern=r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})?)?$",
        description="ISO 8601 date or date-time, e.g. 2026-01-31 or 2026-01-31T00:00:00Z.",
    ),
]
PositiveAmount = Annotated[float, Field(gt=0, le=10_000_000, description="Amount (> 0).")]
ConfirmToken = Annotated[
    str, Field(min_length=10, max_length=400, description="Token returned by the matching prepare_* tool.")
]


@dataclass
class App:
    settings: Settings
    policy: Policy
    audit: AuditLog
    client: WiseClient
    confirmations: ConfirmationManager
    limiter: SpendLimiter
    registered_tools: dict[str, str] = field(default_factory=dict)
    skipped_tools: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(cls, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> "App":
        policy = settings.policy()
        audit = AuditLog(settings.audit_path, secrets=(settings.token,))
        return cls(
            settings=settings,
            policy=policy,
            audit=audit,
            client=WiseClient(settings, policy, audit, transport=transport),
            confirmations=ConfirmationManager(settings.confirmation_ttl_seconds),
            limiter=SpendLimiter(settings.max_amount_per_tx, settings.max_amount_per_day, settings.spend_state_path),
        )

    def check_recipient(self, recipient_id: int | None, *, tool: str) -> None:
        allowed = self.settings.allowed_recipient_ids
        if allowed is None or recipient_id is None:
            return
        if int(recipient_id) not in allowed:
            self.audit.record("write_blocked", tool=tool, reason="recipient not allowlisted", recipient_id=recipient_id)
            raise PolicyError(f"Recipient {recipient_id} is not in WISE_ALLOWED_RECIPIENT_IDS")


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def _safe(fn: F) -> F:  # noqa: UP047 - TypeVar bound kept for readability
    """Convert our exceptions into MCP tool errors with safe, explicit messages."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except WiseMcpError as exc:
            raise ToolError(str(exc)) from None
        except ToolError:
            raise
        except Exception:
            log.exception("Unexpected error in tool %s", fn.__name__)
            raise ToolError(f"Unexpected internal error in {fn.__name__} (see server stderr)") from None

    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]


class Registrar:
    """Registers tools on the server only if their scope is enabled by the policy."""

    def __init__(self, server: MCPServer, app: App, *, enforce_scopes: bool = True) -> None:
        self.server = server
        self.app = app
        self.enforce_scopes = enforce_scopes

    def tool(
        self,
        scope: Scope,
        *,
        title: str,
        read_only: bool,
        destructive: bool = False,
        idempotent: bool = False,
        open_world: bool = False,
    ) -> Callable[[F], F]:
        def decorator(fn: F) -> F:
            name = fn.__name__
            if self.enforce_scopes and not self.app.policy.is_enabled(scope):
                self.app.skipped_tools[name] = scope.value
                return fn
            annotations = ToolAnnotations(
                title=title,
                read_only_hint=read_only,
                destructive_hint=False if read_only else destructive,
                idempotent_hint=True if read_only else idempotent,
                open_world_hint=open_world,
            )
            self.server.add_tool(
                _safe(fn),
                name=name,
                title=title,
                description=inspect.cleandoc(fn.__doc__ or ""),
                annotations=annotations,
                structured_output=True,
            )
            self.app.registered_tools[name] = scope.value
            return fn

        return decorator


def items(data: Any, **extra: Any) -> dict[str, Any]:
    """Wrap list responses in an object (structured output must be an object)."""
    out: dict[str, Any] = {"count": len(data) if isinstance(data, list) else None, "items": data}
    out.update(extra)
    return out
