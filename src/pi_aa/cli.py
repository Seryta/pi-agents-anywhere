"""pi-aa-connector: run the Agents Anywhere connector with the Pi runtime."""

from __future__ import annotations

import asyncio
import sys

INSTALL_HINT = (
    "anywhere-cli (the Agents Anywhere connector) is not importable.\n"
    "Install it from the Agents Anywhere repository:\n"
    "    uv pip install /path/to/Agents-Anywhere/connector"
)


def main() -> None:
    try:
        from connector.core.config import ConnectorConfig
        from connector.runtimes.providers import default_runtime_providers
        from connector.server.client import BackendRpcClient
    except ImportError:
        print(INSTALL_HINT, file=sys.stderr)
        raise SystemExit(2) from None

    from pi_aa.provider import PiProvider

    providers = (*default_runtime_providers(), PiProvider())
    client = BackendRpcClient(
        ConnectorConfig.load(),
        agent_runtime_providers=providers,
    )
    asyncio.run(client.run_forever())


if __name__ == "__main__":
    main()
