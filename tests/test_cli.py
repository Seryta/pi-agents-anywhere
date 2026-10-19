"""The Pi alias uses the official lifecycle and packaged provider registry."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from unittest.mock import AsyncMock

import pytest
from connector import cli as official_cli
from connector.runtimes import providers

from pi_aa import cli
from pi_aa.runtime import PiRuntime


@pytest.mark.parametrize(
    "args, expected",
    [
        ([], ["start"]),
        (["start"], ["start"]),
        (["serve", "--socket", "/tmp/control.sock"], ["serve", "--socket", "/tmp/control.sock"]),
        (["rpc"], ["rpc"]),
        (
            ["pair", "https://aa.example.test", "--no-start"],
            ["pair", "https://aa.example.test", "--no-start"],
        ),
    ],
)
def test_alias_delegates_to_official_cli(monkeypatch, args, expected):
    calls = []
    monkeypatch.setattr(official_cli, "main", lambda argv: calls.append(argv))
    cli.main(args)
    assert calls == [expected]


def test_pi_is_loaded_as_an_extension_alongside_official_providers(monkeypatch):
    point = EntryPoint(
        name="pi", value="pi_aa.provider:PiProvider", group="agents_anywhere.runtime_providers.v1"
    )
    monkeypatch.setattr(providers, "entry_points", lambda **kwargs: [point])
    loaded = providers.default_runtime_providers()
    assert [provider.runtime_type for provider in loaded] == ["codex", "claude", "dsh", "pi"]
    assert type(loaded[2]) is providers.DshProvider


@pytest.mark.asyncio
async def test_reconnect_uses_the_public_runtime_callback():
    runtime = object.__new__(PiRuntime)
    runtime.reannounce_session_states = AsyncMock()
    runtime._publish_runtime_capabilities = AsyncMock()
    await runtime.on_backend_reconnect()
    runtime._publish_runtime_capabilities.assert_awaited_once_with()
    runtime.reannounce_session_states.assert_awaited_once_with(reason="backend-reconnect")


def test_alias_reports_incompatible_upstream(monkeypatch, capsys):
    monkeypatch.delattr(providers, "RUNTIME_PROVIDER_ENTRY_POINT_GROUP")
    with pytest.raises(SystemExit) as error:
        cli.main(["start"])
    assert error.value.code == 2
    assert "tested shared-connector integration" in capsys.readouterr().err


@pytest.mark.parametrize(
    "option,value",
    [("--poll-interval", "0"), ("--poll-interval", "-1"), ("--timeout", "0"), ("--timeout", "nan")],
)
def test_delegated_pairing_preserves_duration_validation(option, value, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["pair", "https://example.test", option, value])
    assert error.value.code == 2
    assert "must be greater than zero" in capsys.readouterr().err
