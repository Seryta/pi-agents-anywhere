from __future__ import annotations

from pathlib import Path

import pytest
from connector.runtime_protocol import RuntimeInvalidRequestError

from pi_aa.runtime import PiLiveSession, platform_session_id
from tests.conftest import FakeHost
from tests.test_runtime import make_runtime
from tests.test_sessions import write_session


@pytest.mark.parametrize("escape", ["absolute", "traversal", "symlink", "new-symlink"])
async def test_resume_rejects_paths_outside_session_root(
    escape: str, fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    outside = tmp_path / "private" / "history.jsonl"
    write_session(outside, cwd=str(tmp_path))
    root = tmp_path / "sessions"
    root.mkdir()
    if escape == "absolute":
        target = outside
    elif escape == "traversal":
        target = root / ".." / "private" / "history.jsonl"
    else:
        (root / "link").symlink_to(outside.parent, target_is_directory=True)
        target = root / "link" / ("new.jsonl" if escape == "new-symlink" else outside.name)
    original = outside.read_bytes()
    try:
        with pytest.raises(RuntimeInvalidRequestError, match="outside sessionsDir"):
            await runtime._ensure_live("session", str(target), None)
        assert runtime._live == {}
        assert outside.read_bytes() == original
    finally:
        await runtime.stop()


async def test_existing_live_cannot_bypass_external_path_validation(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "session", cwd=str(tmp_path))
    runtime._live[live.platform_id] = live
    with pytest.raises(RuntimeInvalidRequestError):
        await runtime._ensure_live(live.platform_id, str(tmp_path / "private.jsonl"), None)
    assert live.process is None
    await runtime.stop()


@pytest.mark.parametrize("existing", [False, True])
async def test_resume_accepts_session_files_inside_root(
    existing: bool, fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    target = tmp_path / "sessions" / "project" / "history.jsonl"
    if existing:
        write_session(target, cwd=str(tmp_path))
    try:
        live = await runtime._ensure_live("session", str(target), None)
        assert live.alive
        assert live.process is not None
        assert live.process.argv[-2:] == ["--session", str(target)]
    finally:
        await runtime.stop()


async def test_discovery_and_known_paths_cannot_resume_symlink_escape(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    outside = tmp_path / "private" / "history.jsonl"
    write_session(outside, cwd=str(tmp_path))
    link = tmp_path / "sessions" / "history.jsonl"
    link.parent.mkdir()
    link.symlink_to(outside)
    session_id = platform_session_id(fake_host.session_namespace, str(link))
    assert runtime._resolve_session_path(session_id, None) is None
    runtime._known_paths[session_id] = str(link)
    assert runtime._resolve_session_path(session_id, None) is None
