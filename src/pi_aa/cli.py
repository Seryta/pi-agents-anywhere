"""pi-aa-connector: Agents Anywhere Connector CLI with the Pi runtime injected.

Subcommands mirror the upstream ``anywhere-cli`` (``start`` / ``pair`` /
``configure``) so the connector can be paired and run exactly like the official
one. The only difference is that ``BackendRpcClient`` is constructed with the
Pi provider appended to the default provider registry.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

INSTALL_HINT = (
    "anywhere-cli (the Agents Anywhere connector) is not importable.\n"
    "Install the package, or a source checkout:\n"
    "    uv pip install anywhere-cli\n"
    "    # or: uv pip install -e /path/to/Agents-Anywhere/connector"
)

SHELL_LIFETIME_WARNING = (
    "Note: this connector runs in the current shell session. "
    "If you do not run it with a tool such as systemd, tmux, screen, "
    "or a background service, the connection will stop when this shell session ends."
)

DEFAULT_PAIR_TIMEOUT = 600.0


def _require_connector() -> tuple[Any, ...]:
    try:
        from connector.core.config import ConnectorConfig
        from connector.core.runtime_owner import RuntimeLease
        from connector.runtimes.providers import default_runtime_providers
        from connector.server.client import BackendRpcClient
    except ImportError as exc:
        print(INSTALL_HINT, file=sys.stderr)
        print(f"underlying import error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    return ConnectorConfig, RuntimeLease, default_runtime_providers, BackendRpcClient


def build_client(config: Any) -> Any:
    """Construct the connector client with the Pi provider appended."""

    _, _, default_runtime_providers, backend_rpc_client = _require_connector()
    from pi_aa.provider import PiProvider

    providers = (*default_runtime_providers(), PiProvider())
    return backend_rpc_client(config, agent_runtime_providers=providers)


def _positive_seconds(value: str) -> float:
    """Argparse type for durations that must be greater than zero."""

    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return seconds


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-aa-connector",
        description="Agents Anywhere connector with the Pi runtime",
    )
    # A bare invocation is the implicit `start` command, but argparse only fills
    # attributes for the selected subparser: seed the shared defaults here.
    parser.set_defaults(
        config=None,
        debug=False,
        server_url=None,
        connector_id=None,
        connector_token=None,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="{start,pair,configure}")

    start = subparsers.add_parser("start", help="start the connector (default)")
    _add_common_args(start)
    start.add_argument("--server-url", help="backend server URL")
    start.add_argument("--connector-id", help="connector id")
    start.add_argument("--connector-token", help="connector token")

    pair = subparsers.add_parser(
        "pair",
        help="pair with a backend, save credentials, and start the connector",
    )
    _add_common_args(pair)
    pair.add_argument("server", nargs="?", help="backend server URL")
    pair.add_argument("--poll-interval", type=_positive_seconds, default=2.0)
    pair.add_argument("--timeout", type=_positive_seconds, default=DEFAULT_PAIR_TIMEOUT)
    pair.add_argument(
        "--no-start",
        action="store_true",
        help="save credentials without starting the connector",
    )

    configure = subparsers.add_parser("configure", help="save connector credentials to local JSON")
    _add_common_args(configure)
    configure.add_argument("--server-url", required=True)
    configure.add_argument("--connector-id", required=True)
    configure.add_argument("--connector-token", required=True)
    return parser


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="local connector config JSON path")
    parser.add_argument("--debug", action="store_true", help="show debug-level logs")


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    connector_config, runtime_lease, _, _ = _require_connector()
    import httpx

    if args.config is None:
        args.config = str(connector_config.default_path())

    try:
        from connector.logging import configure_connector_logging

        configure_connector_logging(debug=bool(getattr(args, "debug", False)))
    except Exception as exc:  # noqa: BLE001 - logging must never block startup
        print(f"warning: connector logging setup failed: {exc}", file=sys.stderr)

    try:
        if args.command == "configure":
            _configure(args, connector_config)
        elif args.command in ("pair", "login"):
            asyncio.run(_pair(args, connector_config, runtime_lease))
        elif args.command in (None, "start"):
            asyncio.run(_start(args, connector_config, runtime_lease))
        else:
            parser.print_help()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except httpx.TimeoutException as exc:
        request = exc.request
        target = request.url if request is not None else exc
        print(f"error: request timed out: {target}", file=sys.stderr)
        raise SystemExit(2) from None
    except httpx.HTTPStatusError as exc:
        print(
            f"error: server returned HTTP {exc.response.status_code}: "
            f"{_response_detail(exc.response)}",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except httpx.RequestError as exc:
        print(f"error: cannot reach server: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except (TimeoutError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def _configure(args: argparse.Namespace, connector_config: Any) -> None:
    config = connector_config(
        server_url=args.server_url.rstrip("/"),
        connector_id=args.connector_id,
        connector_token=args.connector_token,
    )
    path = config.save(args.config)
    print(f"Saved connector config: {path}")


def _resolve_config(args: argparse.Namespace, connector_config: Any) -> Any:
    server_url = args.server_url or os.environ.get("AGENT_SERVER_URL")
    connector_id = args.connector_id or os.environ.get("AGENT_CONNECTOR_ID")
    connector_token = args.connector_token or os.environ.get("AGENT_CONNECTOR_TOKEN")
    if server_url and connector_id and connector_token:
        return connector_config(
            server_url=server_url.rstrip("/"),
            connector_id=connector_id,
            connector_token=connector_token,
        )
    config_path = Path(args.config)
    if config_path.exists():
        return connector_config.load(config_path)
    missing = []
    if not server_url:
        missing.append("--server-url")
    if not connector_id:
        missing.append("--connector-id")
    if not connector_token:
        missing.append("--connector-token")
    missing.append(f"or config file {config_path}")
    raise RuntimeError("missing connector credentials: " + ", ".join(missing))


async def _start(args: argparse.Namespace, connector_config: Any, runtime_lease: Any) -> None:
    config = _resolve_config(args, connector_config)
    await _run_connector(args, runtime_lease, config)


async def _run_connector(
    args: argparse.Namespace,
    runtime_lease: Any,
    config: Any,
    *,
    lease: Any | None = None,
) -> None:
    if lease is None:
        lease = runtime_lease(legacy_paths=[Path(args.config).with_name("connector-runtime.json")])
    # Claim with the config even when `pair` already claimed anonymously, so the
    # runtime state records connectorId/serverUrl exactly like upstream does.
    lease.claim(config)
    print(SHELL_LIFETIME_WARNING)
    try:
        client = build_client(config)
        await client.run_forever()
    finally:
        lease.release()


async def _pair(args: argparse.Namespace, connector_config: Any, runtime_lease: Any) -> None:
    import httpx
    from connector.server.pairing import (
        poll_pairing,
        resolve_pair_server_url,
        start_pairing,
    )

    lease = runtime_lease(legacy_paths=[Path(args.config).with_name("connector-runtime.json")])
    if not args.no_start:
        lease.claim()
    try:
        try:
            server_url = await resolve_pair_server_url(
                args.server,
                timeout=10,
                missing_message=("missing server address. Usage: pi-aa-connector pair <server>"),
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        async with httpx.AsyncClient(timeout=30) as client:
            pairing = await start_pairing(client, server_url, args.timeout)
            print(f"Pairing code: {pairing['code']}")
            print("在工作台的「连接工作设备 / 添加设备」里输入上面的配对码。")
            print("Waiting for credentials...")
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                payload = await poll_pairing(client, server_url, str(pairing["pairingId"]))
                if payload["status"] == "claimed" and payload.get("config"):
                    config = connector_config.from_mapping(payload["config"])
                    path = config.save(args.config)
                    print(f"Saved connector config: {path}")
                    if args.no_start:
                        return
                    await _run_connector(
                        args,
                        runtime_lease,
                        config,
                        lease=lease,
                    )
                    return
                if payload["status"] in {"expired", "consumed"}:
                    raise RuntimeError(f"pairing ended with status: {payload['status']}")
                await asyncio.sleep(args.poll_interval)
            raise TimeoutError("pairing timed out")
    finally:
        if not args.no_start:
            lease.release()


def _response_detail(response: Any) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:300] if text else response.reason_phrase
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload
        return str(detail)
    return str(payload)


if __name__ == "__main__":
    main()
