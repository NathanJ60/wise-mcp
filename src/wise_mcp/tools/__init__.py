"""Tool modules. Each ``register*`` function declares its tools through a scope-gated Registrar."""

from wise_mcp.tools import account_writes, invoices, money_writes, read
from wise_mcp.tools.common import Registrar


def register_all(reg: Registrar) -> None:
    read.register(reg)
    invoices.register(reg)
    account_writes.register_webhooks(reg)
    account_writes.register_recipients(reg)
    account_writes.register_quotes(reg)
    money_writes.register_transfers(reg)
    money_writes.register_batch(reg)
    money_writes.register_balance_moves(reg)
