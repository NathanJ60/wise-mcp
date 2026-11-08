from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.conftest import TOKEN
from wise_mcp.cli import main


def test_print_policy_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    monkeypatch.setenv("WISE_API_TOKEN", TOKEN)
    monkeypatch.setenv("WISE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WISE_ENABLE", "invoices,webhooks,recipients,quotes,balance_moves")
    monkeypatch.setenv("WISE_DENY", "transfers,batch_transfers")
    assert main(["--print-policy", "--json"]) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out
    report = json.loads(out)
    assert report["token_configured"] is True
    assert "execute_fund_transfer" in report["disabled_tools"]
    assert "execute_balance_move" in report["registered_tools"]
    assert main(["--print-policy"]) == 0
    assert "DENIED" in capsys.readouterr().out


def test_config_error_exit_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("WISE_ENABLE", "everything")
    assert main([]) == 2
    assert "unknown scope" in capsys.readouterr().err


def test_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    assert main([]) == 2


def test_stdio_server_lists_tools(tmp_path) -> None:
    """Spawn the real console entry point over stdio and list tools with the official client."""
    script = f"""
import anyio, os
from mcp import Client, StdioServerParameters
async def main():
    env = {{"WISE_API_TOKEN": "x", "WISE_DATA_DIR": {str(tmp_path)!r}, "PATH": os.environ.get("PATH", ""),
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}}
    params = StdioServerParameters(command={sys.executable!r}, args=["-m", "wise_mcp"], env=env)
    async with Client(params) as c:
        print(len((await c.list_tools()).tools))
anyio.run(main)
"""
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False)
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip().splitlines()[-1]) >= 25
