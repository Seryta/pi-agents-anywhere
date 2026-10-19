"""Compatibility command delegating all Connector lifecycle to the official CLI."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    try:
        from connector.cli import main as connector_main
        from connector.runtimes.providers import RUNTIME_PROVIDER_ENTRY_POINT_GROUP
    except ImportError:
        print(
            "This Pi extension requires the Agents Anywhere Connector with runtime extension "
            "support. Install the tested shared-connector integration described in README.md.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    if RUNTIME_PROVIDER_ENTRY_POINT_GROUP != "agents_anywhere.runtime_providers.v1":
        raise SystemExit("Unsupported Connector runtime extension API")
    args = list(argv) if argv is not None else sys.argv[1:]
    connector_main(args or ["start"])


if __name__ == "__main__":
    main()
