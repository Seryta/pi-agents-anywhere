"""Refresh or check the integration patch from its fixed upstream checkout."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    manifest_path = root / "upstream.json"
    manifest = json.loads(manifest_path.read_text())
    checkout = Path(sys.argv[1]).resolve()
    base = manifest["commit"]
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, "HEAD"],
        cwd=checkout,
        capture_output=True,
        check=False,
    )
    if ancestry.returncode != 0:
        raise SystemExit("Checkout must descend from the reviewed upstream base in upstream.json")
    paths = [
        "connector",
        "dsh-bridge-next",
        "contracts/connector-local-control",
        "contracts/dsh-bridge",
        "docs/runtime-protocol/runtime-extensions.md",
    ]
    patch = subprocess.check_output(["git", "diff", "--binary", base, "--", *paths], cwd=checkout)
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *paths], cwd=checkout, text=True
    ).splitlines()
    for path in untracked:
        diff = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", path],
            cwd=checkout,
            capture_output=True,
            check=False,
        )
        if diff.returncode not in (0, 1):
            raise SystemExit(diff.stderr.decode())
        patch += diff.stdout
    digest = hashlib.sha256(patch).hexdigest()
    if "--check" in sys.argv[2:]:
        if patch != (root / "upstream.patch").read_bytes() or digest != manifest["patchSha256"]:
            raise SystemExit("Integration patch or manifest is out of date")
        print("Integration patch matches checkout and checksum")
        return
    (root / "upstream.patch").write_bytes(patch)
    manifest["patchSha256"] = digest
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
