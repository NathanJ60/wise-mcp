from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx

from wise_mcp.config import Settings
from wise_mcp.server import build_server
from wise_mcp.tools.common import App

TOKEN = "test-token-DO-NOT-LEAK-123"
BASE = "https://api.wise.com"
ALL_BUT_TRANSFERS = "invoices,webhooks,recipients,quotes,balance_moves"
EVERYTHING = "invoices,webhooks,recipients,quotes,transfers,batch_transfers,balance_moves"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read the developer's real Wise configuration."""
    import os

    for key in list(os.environ):
        if key.startswith("WISE_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**env: str) -> Settings:
        base = {"WISE_API_TOKEN": TOKEN, "WISE_DATA_DIR": str(tmp_path / "data")}
        base.update(env)
        return Settings.from_env(base)

    return factory


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
def make_server(make_settings: Callable[..., Settings]) -> Callable[..., tuple[Any, App]]:
    def factory(enforce_scopes: bool = True, **env: str) -> tuple[Any, App]:
        return build_server(make_settings(**env), enforce_scopes=enforce_scopes)

    return factory


def audit_lines(app: App) -> list[dict[str, Any]]:
    path = app.settings.audit_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def writes_sent(router: respx.MockRouter) -> list[str]:
    return [f"{c.request.method} {c.request.url.path}" for c in router.calls if c.request.method != "GET"]
