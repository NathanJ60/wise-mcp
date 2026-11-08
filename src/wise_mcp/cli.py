"""Command line entry point: ``wise-mcp`` (stdio server) and ``wise-mcp --print-policy``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from wise_mcp import __version__
from wise_mcp.config import Settings
from wise_mcp.errors import ConfigError


def _setup_logging() -> None:
    level = os.environ.get("WISE_LOG_LEVEL", "WARNING").upper()
    # stdout is the MCP stdio channel: logs must go to stderr only.
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def policy_report(settings: Settings) -> dict[str, object]:
    from wise_mcp.server import build_server

    _, app = build_server(settings)
    report = app.policy.describe()
    report.update(
        {
            "version": __version__,
            "environment": "sandbox" if settings.sandbox else "production",
            "base_url": settings.base_url,
            "token_configured": bool(settings.token),
            "allowed_recipient_ids": sorted(settings.allowed_recipient_ids)
            if settings.allowed_recipient_ids is not None
            else None,
            "max_amount_per_tx": {k: str(v) for k, v in (settings.max_amount_per_tx or {}).items()} or None,
            "max_amount_per_day": {k: str(v) for k, v in (settings.max_amount_per_day or {}).items()} or None,
            "audit_log": str(settings.audit_path),
            "sca_private_key_configured": settings.sca_private_key_path is not None,
            "registered_tools": dict(sorted(app.registered_tools.items())),
            "disabled_tools": dict(sorted(app.skipped_tools.items())),
        }
    )
    return report


def _print_human(report: dict[str, object]) -> None:
    out = sys.stdout
    out.write(f"wise-mcp {report['version']}  ({report['environment']}: {report['base_url']})\n")
    out.write(f"token configured: {report['token_configured']}   read-only mode: {report['read_only_mode']}\n")
    out.write(f"audit log: {report['audit_log']}\n\n")
    out.write(f"{'SCOPE':<17}{'STATE':<10}{'MONEY':<7}TOOLS\n")
    registered: dict[str, str] = report["registered_tools"]  # type: ignore[assignment]
    disabled: dict[str, str] = report["disabled_tools"]  # type: ignore[assignment]
    for s in report["scopes"]:  # type: ignore[attr-defined]
        tools = [t for t, sc in {**registered, **disabled}.items() if sc == s["scope"]]
        out.write(f"{s['scope']:<17}{s['state']:<10}{'yes' if s['money_moving'] else '':<7}{len(tools)}\n")
    out.write("\nallowed profile ids:   " + str(report["allowed_profile_ids"]) + "\n")
    out.write("allowed recipient ids: " + str(report["allowed_recipient_ids"]) + "\n")
    out.write("max amount per tx:     " + str(report["max_amount_per_tx"]) + "\n")
    out.write("max amount per day:    " + str(report["max_amount_per_day"]) + "\n")
    out.write(f"\nregistered tools ({len(registered)}): " + ", ".join(registered) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wise-mcp", description="Secure MCP server for the Wise API (stdio).")
    parser.add_argument("--version", action="version", version=f"wise-mcp {__version__}")
    parser.add_argument("--print-policy", action="store_true", help="Print effective permissions and exit.")
    parser.add_argument("--json", action="store_true", help="With --print-policy: machine-readable output.")
    args = parser.parse_args(argv)
    _setup_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.stderr.write(f"wise-mcp: configuration error: {exc}\n")
        return 2

    if args.print_policy:
        report = policy_report(settings)
        if args.json:
            sys.stdout.write(json.dumps(report, indent=2) + "\n")
        else:
            _print_human(report)
        return 0

    if not settings.token:
        sys.stderr.write("wise-mcp: WISE_API_TOKEN is not set\n")
        return 2
    from wise_mcp.server import build_server

    server, app = build_server(settings)
    logging.getLogger("wise_mcp").info(
        "Starting wise-mcp %s with scopes %s", __version__, sorted(s.value for s in app.policy.effective)
    )
    server.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
