from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
AA_SOURCE = Path(
    os.environ.get(
        "PI_AA_CONNECTOR_SOURCE",
        ROOT.parent / "Agents-Anywhere" / "connector",
    )
)
if AA_SOURCE.is_dir() and str(AA_SOURCE) not in sys.path:
    sys.path.insert(0, str(AA_SOURCE))

from connector.runtime_protocol.host import RuntimeHostClient


class FakeHost(RuntimeHostClient):
    """Records every runtime -> connector callback for assertions."""

    def __init__(self) -> None:
        self.notices: list[Any] = []
        self.timeline_syncs: list[dict[str, Any]] = []
        self.timeline_items: list[Any] = []
        self.states: list[dict[str, Any]] = []
        self.metas: list[dict[str, Any]] = []
        self.turn_ends: list[dict[str, Any]] = []

    @property
    def connector_id(self) -> str:
        return "test-connector"

    async def notice_upsert(self, notice: Any) -> None:
        self.notices.append(notice)

    async def timeline_sync(
        self,
        session_id: str,
        runtime: str,
        items: Any,
        external_session_id: str | None = None,
        complete: bool = False,
        metadata: Any = None,
    ) -> None:
        self.timeline_syncs.append(
            {
                "session_id": session_id,
                "runtime": runtime,
                "items": tuple(items),
                "external_session_id": external_session_id,
                "complete": complete,
            }
        )

    async def timeline_item_upsert(self, item: Any) -> None:
        self.timeline_items.append(item)

    async def session_state_update(self, session_id: str, runtime: str, **kwargs: Any) -> None:
        self.states.append({"session_id": session_id, "runtime": runtime, **kwargs})

    async def session_meta_upsert(self, session_id: str, runtime: str, **kwargs: Any) -> None:
        self.metas.append({"session_id": session_id, "runtime": runtime, **kwargs})

    async def session_turn_ended(self, session_id: str, runtime: str, **kwargs: Any) -> None:
        self.turn_ends.append({"session_id": session_id, "runtime": runtime, **kwargs})

    async def publish_runtime_notifications(
        self, runtime: str, notifications: list[dict[str, Any]], runtime_id: str | None = None
    ) -> None:
        return None


@pytest.fixture
def fake_host() -> FakeHost:
    return FakeHost()


@pytest.fixture
def fake_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An executable fake ``pi`` on PATH (under the name ``pi``)."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "pi"
    shutil.copy(Path(__file__).parent / "fake_pi_rpc.py", script)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PI_FAKE_SESSIONS_DIR", str(tmp_path / "sessions"))
    return script


@pytest.fixture
def session_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sessions" / "fake-session.jsonl"
    monkeypatch.setenv("PI_FAKE_SESSION_FILE", str(path))
    return path


async def wait_for(predicate, timeout: float = 8.0, interval: float = 0.02) -> None:
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition was not met before the timeout")
