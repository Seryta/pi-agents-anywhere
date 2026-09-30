"""CLI behaviour tests: implicit start, error mapping, lease bookkeeping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from pi_aa import cli


class FakeLease:
    def __init__(self, **_: Any) -> None:
        self.claims: list[Any] = []
        self.released = 0

    def claim(self, config: Any = None) -> None:
        self.claims.append(config)

    def release(self) -> None:
        self.released += 1


class FakeConfig:
    def __init__(self, **values: Any) -> None:
        self.values = values
        self.saved_to: Path | None = None

    @staticmethod
    def default_path() -> Path:
        return Path("/tmp/pi-aa-test/connector.json")

    @staticmethod
    def load(path: str | Path) -> FakeConfig:
        raise FileNotFoundError(path)

    @staticmethod
    def from_mapping(payload: dict[str, Any]) -> FakeConfig:
        return FakeConfig(**payload)

    def save(self, path: str | Path) -> Path:
        self.saved_to = Path(path)
        return self.saved_to


class FakeClient:
    def __init__(self) -> None:
        self.ran = False

    async def run_forever(self) -> None:
        self.ran = True


def install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    lease: FakeLease,
    client: FakeClient,
) -> None:
    """Replace the connector import surface with test doubles."""

    def require() -> tuple[Any, ...]:
        return (
            FakeConfig,
            lambda **_: lease,
            lambda: (),
            lambda config, agent_runtime_providers=None: client,
        )

    monkeypatch.setattr(cli, "_require_connector", require)


def fail_with(exc: Exception) -> Any:
    async def _pair(args: Any, connector_config: Any, runtime_lease: Any) -> None:
        raise exc

    return _pair


def test_bare_invocation_starts_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    lease, client = FakeLease(), FakeClient()
    install_fakes(monkeypatch, lease, client)
    monkeypatch.setenv("AGENT_SERVER_URL", "https://example.test")
    monkeypatch.setenv("AGENT_CONNECTOR_ID", "conn_test")
    monkeypatch.setenv("AGENT_CONNECTOR_TOKEN", "tok_test")

    cli.main([])

    assert client.ran is True
    assert lease.released == 1
    # The lease keeps the resolved config so the runtime state records the
    # connector identity, exactly like upstream does.
    assert len(lease.claims) == 1
    assert lease.claims[0].values["connector_id"] == "conn_test"


def test_pair_network_error_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_fakes(monkeypatch, FakeLease(), FakeClient())
    monkeypatch.setattr(
        cli,
        "_pair",
        fail_with(
            httpx.ConnectError(
                "All connection attempts failed",
                request=httpx.Request("POST", "https://example.test/api/v2/pairing"),
            )
        ),
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["pair", "https://example.test"])

    assert excinfo.value.code == 2
    assert "cannot reach server" in capsys.readouterr().err


def test_pair_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_fakes(monkeypatch, FakeLease(), FakeClient())
    monkeypatch.setattr(
        cli,
        "_pair",
        fail_with(
            httpx.ConnectTimeout(
                "timed out",
                request=httpx.Request("POST", "https://example.test/api/v2/pairing"),
            )
        ),
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["pair", "https://example.test"])

    assert excinfo.value.code == 2
    assert "request timed out" in capsys.readouterr().err


def test_pair_http_error_uses_server_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_fakes(monkeypatch, FakeLease(), FakeClient())
    response = httpx.Response(400, json={"detail": "pairing code expired"})
    monkeypatch.setattr(
        cli,
        "_pair",
        fail_with(
            httpx.HTTPStatusError(
                "bad request",
                request=httpx.Request("POST", "https://example.test/api/v2/pairing"),
                response=response,
            )
        ),
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["pair", "https://example.test"])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "HTTP 400" in err
    assert "pairing code expired" in err


def test_configure_saves_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_fakes(monkeypatch, FakeLease(), FakeClient())

    cli.main(
        [
            "configure",
            "--server-url",
            "https://example.test/",
            "--connector-id",
            "conn_test",
            "--connector-token",
            "tok_test",
        ]
    )

    assert "Saved connector config" in capsys.readouterr().out
