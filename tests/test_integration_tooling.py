"""Maintainers can refresh a replayable patch before and after committing changes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "integrations/agents-anywhere/update.py"


def git(checkout, *args):
    return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()


@pytest.fixture
def integration(tmp_path):
    checkout = tmp_path / "upstream"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(checkout, "config", "user.name", "Integration Test")
    git(checkout, "config", "user.email", "integration@example.test")
    module = checkout / "connector" / "runtime.py"
    module.parent.mkdir()
    module.write_text("base\n")
    git(checkout, "add", "connector")
    git(checkout, "commit", "-qm", "chore(test): seed reviewed upstream")
    base = git(checkout, "rev-parse", "HEAD")
    package = tmp_path / "integration"
    package.mkdir()
    tool = package / "update.py"
    shutil.copy2(TOOL, tool)
    (package / "upstream.json").write_text(json.dumps({"commit": base}))
    return checkout, module, package, tool, base


@pytest.mark.parametrize("committed", [False, True])
def test_patch_refresh_includes_changes_after_the_reviewed_base(integration, committed, tmp_path):
    checkout, module, package, tool, base = integration
    module.write_text("extension\n")
    if committed:
        git(checkout, "add", "connector")
        git(checkout, "commit", "-qm", "feat(test): install extension")
    (module.parent / "new.py").write_text("new extension\n")
    subprocess.run([sys.executable, str(tool), str(checkout)], check=True)
    subprocess.run([sys.executable, str(tool), str(checkout), "--check"], check=True)
    clean = tmp_path / "clean"
    git(checkout, "worktree", "add", "--detach", str(clean), base)
    git(clean, "apply", str(package / "upstream.patch"))
    assert (clean / "connector/runtime.py").read_text() == "extension\n"
    assert (clean / "connector/new.py").read_text() == "new extension\n"


def test_patch_refresh_rejects_an_unreviewed_history(integration):
    checkout, module, package, tool, _base = integration
    git(checkout, "checkout", "--orphan", "unrelated")
    module.write_text("different upstream\n")
    git(checkout, "add", "connector")
    git(checkout, "commit", "-qm", "chore(test): unrelated upstream")
    result = subprocess.run(
        [sys.executable, str(tool), str(checkout)], check=False, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "reviewed upstream base" in result.stderr
    assert not (package / "upstream.patch").exists()
