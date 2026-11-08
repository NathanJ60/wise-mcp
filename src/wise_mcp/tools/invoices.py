"""Invoice tools (scope: invoices). Receive-only: these never move money out."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from wise_mcp.policy import Scope
from wise_mcp.tools.common import BalanceId, IsoDateTime, ProfileId, Registrar, Uuid
from wise_mcp.tools.money import require_confirm


class InvoiceTax(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=50)] = "VAT"
    percentage: Annotated[float, Field(ge=0, le=100)]
    behaviour: Literal["INCLUDED", "EXCLUDED"] = "EXCLUDED"


class InvoiceLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=200)]
    unit_price: Annotated[float, Field(ge=0, le=10_000_000)]
    quantity: Annotated[int, Field(ge=1, le=100_000)] = 1
    tax: InvoiceTax | None = None


class PayerAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country_iso3_code: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")]
    first_line: Annotated[str | None, Field(max_length=200)] = None


class Payer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=200)]
    email: Annotated[str | None, Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)] = None
    address: PayerAddress | None = None
    locale: Annotated[str, Field(pattern=r"^[a-z]{2}$")] = "en"


PaymentMethod = Literal["BANK_TRANSFER", "PAY_WITH_WISE"]
DEFAULT_PAYMENT_METHODS: tuple[str, ...] = ("BANK_TRANSFER",)


def _ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _norm(value: str | None, default: datetime) -> str:
    if not value:
        return _ts(default)
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v if "T" in v else v + "T12:00:00+00:00")
    return _ts(dt if dt.tzinfo else dt.replace(tzinfo=UTC))


def build_invoice_body(
    *,
    balance_id: int,
    currency: str,
    due_at: str,
    issue_date: str,
    line_items: list[InvoiceLine],
    payer: Payer | None,
    invoice_number: str | None,
    message: str | None,
    payment_methods: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "requestType": "INVOICE",
        # Verified on production: "ACCOUNT_DETAILS" is rejected with 400 bad-request by the PUT;
        # accepted values are BANK_TRANSFER and PAY_WITH_WISE.
        "selectedPaymentMethods": list(payment_methods or DEFAULT_PAYMENT_METHODS),
        "balanceId": balance_id,
        "dueAt": due_at,
        "issueDate": issue_date,
        "lineItems": [
            {
                "name": li.name,
                "unitPrice": {"value": li.unit_price, "currency": currency},
                "quantity": li.quantity,
                **(
                    {"tax": {"name": li.tax.name, "percentage": li.tax.percentage, "behaviour": li.tax.behaviour}}
                    if li.tax
                    else {}
                ),
            }
            for li in line_items
        ],
    }
    if invoice_number:
        body["invoiceNumber"] = invoice_number
    if message:
        body["message"] = message
    if payer:
        p: dict[str, Any] = {"name": payer.name, "locale": payer.locale}
        if payer.email:
            p["email"] = payer.email
        if payer.address:
            p["address"] = {
                "countryIso3Code": payer.address.country_iso3_code.upper(),
                **({"firstLine": payer.address.first_line} if payer.address.first_line else {}),
            }
        body["payer"] = p
    return body


def register(reg: Registrar) -> None:
    app = reg.app
    c = app.client
    S = Scope.INVOICES

    async def _currency(profile_id: int, balance_id: int, tool: str) -> str:
        bal = await c.get(f"/v4/profiles/{profile_id}/balances/{balance_id}", tool=tool)
        return str(bal["currency"])

    LineItems = Annotated[list[InvoiceLine], Field(min_length=1, max_length=100)]
    InvoiceNumber = Annotated[str | None, Field(max_length=50, pattern=r"^[A-Za-z0-9._/-]+$")]
    Message = Annotated[str | None, Field(max_length=1000, description="Free text, e.g. tax ids.")]
    Methods = Annotated[
        list[PaymentMethod],
        Field(min_length=1, max_length=2, description="How the payer can pay. Default: BANK_TRANSFER."),
    ]

    @reg.tool(S, title="Create invoice draft", read_only=False, destructive=False, idempotent=False)
    async def create_invoice_draft(
        profile_id: ProfileId,
        balance_id: BalanceId,
        line_items: LineItems,
        payer: Payer,
        due_days: Annotated[int, Field(ge=0, le=365)] = 30,
        issue_date: IsoDateTime | None = None,
        invoice_number: InvoiceNumber = None,
        message: Message = None,
        payment_methods: Methods = ["BANK_TRANSFER"],  # noqa: B006 - schema default, never mutated
    ) -> dict[str, Any]:
        """Create an invoice as a DRAFT (not sent to anyone). Business profiles only.
        Line item prices use the currency of `balance_id`. Publish separately with publish_invoice.
        POST then PUT /v2/profiles/{profileId}/acquiring/payment-requests[/{id}]."""
        currency = await _currency(profile_id, balance_id, "create_invoice_draft")
        now = datetime.now(UTC)
        issue = _norm(issue_date, now)
        due = _ts(now + timedelta(days=due_days))
        base = f"/v2/profiles/{profile_id}/acquiring/payment-requests"
        empty = await c.request(
            "POST",
            base,
            json={
                "requestType": "INVOICE",
                "selectedPaymentMethods": [],
                "balanceId": balance_id,
                "dueAt": due,
                "issueDate": issue,
                "lineItems": [],
            },
            tool="create_invoice_draft",
        )
        pr_id = empty["id"]
        number = invoice_number or (empty.get("invoice") or {}).get("invoiceNumber")
        body = build_invoice_body(
            balance_id=balance_id,
            currency=currency,
            due_at=due,
            issue_date=issue,
            line_items=line_items,
            payer=payer,
            invoice_number=number,
            message=message,
            payment_methods=list(payment_methods),
        )
        filled = await c.request("PUT", f"{base}/{pr_id}", json=body, tool="create_invoice_draft")
        return {
            "status": filled.get("status"),
            "payment_request_id": pr_id,
            "invoice": filled,
            "next_step": "Review the draft, then call publish_invoice with confirm=true.",
        }

    @reg.tool(S, title="Update invoice draft", read_only=False, destructive=True, idempotent=True)
    async def update_invoice_draft(
        profile_id: ProfileId,
        payment_request_id: Uuid,
        balance_id: BalanceId,
        line_items: LineItems,
        payer: Payer,
        due_date: IsoDateTime,
        issue_date: IsoDateTime | None = None,
        invoice_number: InvoiceNumber = None,
        message: Message = None,
        payment_methods: Methods = ["BANK_TRANSFER"],  # noqa: B006 - schema default, never mutated
    ) -> dict[str, Any]:
        """Replace the content of a DRAFT invoice.
        PUT /v2/profiles/{profileId}/acquiring/payment-requests/{id}."""
        currency = await _currency(profile_id, balance_id, "update_invoice_draft")
        now = datetime.now(UTC)
        body = build_invoice_body(
            balance_id=balance_id,
            currency=currency,
            due_at=_norm(due_date, now),
            issue_date=_norm(issue_date, now),
            line_items=line_items,
            payer=payer,
            invoice_number=invoice_number,
            message=message,
            payment_methods=list(payment_methods),
        )
        result: dict[str, Any] = await c.request(
            "PUT",
            f"/v2/profiles/{profile_id}/acquiring/payment-requests/{payment_request_id}",
            json=body,
            tool="update_invoice_draft",
        )
        return result

    @reg.tool(S, title="Publish invoice", read_only=False, destructive=True, idempotent=True, open_world=True)
    async def publish_invoice(
        profile_id: ProfileId,
        payment_request_id: Uuid,
        confirm: Annotated[bool, Field(description="Must be true: publishing makes the invoice payable.")] = False,
    ) -> dict[str, Any]:
        """Publish a DRAFT invoice: it gets a payment link and becomes payable. Requires confirm=true.
        PUT /v2/profiles/{profileId}/acquiring/payment-requests/{id}/status."""
        require_confirm(app, "publish_invoice", confirm, "Publishing an invoice makes it payable.")
        result: dict[str, Any] = await c.request(
            "PUT",
            f"/v2/profiles/{profile_id}/acquiring/payment-requests/{payment_request_id}/status",
            json={"status": "PUBLISHED"},
            tool="publish_invoice",
        )
        return result

    @reg.tool(S, title="Cancel invoice", read_only=False, destructive=True, idempotent=True, open_world=True)
    async def cancel_invoice(
        profile_id: ProfileId,
        payment_request_id: Uuid,
        confirm: Annotated[bool, Field(description="Must be true: the payment link stops working.")] = False,
    ) -> dict[str, Any]:
        """Cancel (invalidate) a published invoice so it can no longer be paid. Requires confirm=true.
        PUT /v2/profiles/{profileId}/acquiring/payment-requests/{id}/status with status INVALIDATED."""
        require_confirm(app, "cancel_invoice", confirm, "Cancelling invalidates the invoice payment link.")
        result: dict[str, Any] = await c.request(
            "PUT",
            f"/v2/profiles/{profile_id}/acquiring/payment-requests/{payment_request_id}/status",
            json={"status": "INVALIDATED"},
            tool="cancel_invoice",
        )
        return result
