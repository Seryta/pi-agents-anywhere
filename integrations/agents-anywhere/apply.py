"""Verify the fixed upstream and apply the reviewed integration without a fork copy."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "upstream.json").read_text())
    patch = root / "upstream.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["patchSha256"]:
        raise SystemExit("Integration patch checksum mismatch")
    checkout = Path(sys.argv[1]).resolve()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    if head != manifest["commit"]:
        raise SystemExit("Upstream revision differs from the reviewed integration manifest")
    base = ["git", "apply"]
    reverse = subprocess.run(
        [*base, "--reverse", "--check", str(patch)], cwd=checkout, capture_output=True, check=False
    )
    if reverse.returncode == 0:
        print("Integration already applied")
        return
    subprocess.run([*base, "--check", str(patch)], cwd=checkout, check=True)
    subprocess.run([*base, str(patch)], cwd=checkout, check=True)


if __name__ == "__main__":
    main()
