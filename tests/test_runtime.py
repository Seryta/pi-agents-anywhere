from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from connector.runtime_protocol import RuntimeConfig

from pi_aa.runtime import PiRuntime, platform_session_id
from tests.conftest import FakeHost, wait_for


def make_runtime(fake_pi: Path, tmp_path: Path, host: FakeHost) -> PiRuntime:
    config = RuntimeConfig(
        runtime="pi",
        revision=1,
        values={
            "executablePath": str(fake_pi),
            "sessionsDir": str(tmp_path / "sessions"),
            "defaultCwd": str(tmp_path),
            "requestTimeoutMs": 5000,
        },
    )
    return PiRuntime(config=config, host=host)


async def test_start_stop(fake_pi: Path, tmp_path: Path, fake_host: FakeHost) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    assert runtime.identity.runtime_version == "9.9.9-fake"
    await runtime.stop()


async def test_create_and_start_session(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        result = await runtime.create_and_start_session(
            "sess-abc",
            "你好",
            title="测试会话",
            cwd=str(tmp_path),
        )
        assert result.ok is True
        await wait_for(lambda: len(fake_host.timeline_syncs) >= 1)
        await wait_for(lambda: any(state.get("status") == "idle" for state in fake_host.states))
    finally:
        await runtime.stop()

    synced = fake_host.timeline_syncs[-1]
    assert synced["runtime"] == "pi"
    assert synced["complete"] is True
    kinds = [item.type for item in synced["items"]]
    assert "message" in kinds
    # session metadata carries the pi file path
    assert session_file.is_file()


async def test_list_sessions_and_snapshot(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        result = await runtime.create_and_start_session("sess-abc", "你好", cwd=str(tmp_path))
        assert result.ok is True
        await wait_for(lambda: session_file.is_file())
        # The settle push records the file as synced before notifying the host;
        # wait for it so the scanner check below cannot race the async push.
        await wait_for(lambda: len(fake_host.timeline_syncs) >= 1)

        sessions = await runtime.list_sessions()
        assert len(sessions) == 1
        session = sessions[0]
        expected_platform_id = platform_session_id(fake_host.session_namespace, str(session_file))
        assert session.session_id == expected_platform_id
        assert session.external_session_id == str(session_file)
        # The settle push already published this session's timeline, so the
        # scanner has nothing to re-sync until the file changes again.
        assert session.metadata["sync"]["requires_timeline_sync"] is False

        snapshot = await runtime.get_session_snapshot(session.session_id, str(session_file))
        assert snapshot.complete is True
        assert snapshot.external_session_id == str(session_file)
        assert any(item.type == "message" for item in snapshot.items)

        # Touching the file marks the session for a fresh timeline sync.
        session_file.write_text(session_file.read_text(encoding="utf-8") + "\n")
        sessions = await runtime.list_sessions()
        assert sessions[0].metadata["sync"]["changed"] is True

        await runtime.get_session_snapshot(sessions[0].session_id, sessions[0].external_session_id)
        sessions = await runtime.list_sessions()
        assert sessions[0].metadata["sync"]["changed"] is False

        state = await runtime.get_session_state(session.session_id, str(session_file))
        assert state is not None
        assert state.status == "idle"
    finally:
        await runtime.stop()


async def test_extension_ui_interaction_flow(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PI_FAKE_UI", "confirm")
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session("sess-ui", "确认一下", cwd=str(tmp_path))
        await wait_for(lambda: any(notice.type == "interaction" for notice in fake_host.notices))
        notices = await runtime.get_session_notices("sess-ui")
        assert len(notices) == 1
        notice = notices[0]
        assert notice.response_required is True
        assert any(action["actionId"] == "confirm" for action in notice.actions)

        result = await runtime.respond_interaction("sess-ui", notice.notice_id, "confirm")
        assert result.ok is True
        # The fake settles after the UI response and the runtime publishes it.
        await wait_for(lambda: len(fake_host.timeline_syncs) >= 1)
        resolved = [n for n in fake_host.notices if n.status == "resolved"]
        assert resolved, "interaction notice should be resolved"
    finally:
        await runtime.stop()


async def test_model_catalog_and_selections(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        catalog = await runtime.list_model_catalog()
        ids = [model.id for model in catalog.models]
        # Pi can expose the same model name under several providers; ids must
        # stay unique or the platform rejects the whole catalog.
        assert ids == ["test:test-model", "test:other-model", "alt:test-model"]
        assert len(ids) == len(set(ids))
        assert catalog.models[0].selection_id == "test:test-model"
        filtered = await runtime.list_model_catalog(query="alt")
        assert [model.id for model in filtered.models] == ["alt:test-model"]

        await runtime.create_and_start_session("sess-model", "hi", cwd=str(tmp_path))
        result = await runtime.update_session_selections(
            "sess-model", None, {"model": "test:other-model", "thinkingLevel": "high"}
        )
        assert result.ok is True
        state = await runtime.get_session_state("sess-model")
        assert state is not None
        assert state.selections["model"] == "test:other-model"
        assert state.selections["thinkingLevel"] == "high"
    finally:
        await runtime.stop()


async def test_commands_are_listed(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        commands = await runtime.list_commands("sess-none")
        assert [command.id for command in commands] == ["fix-tests"]
    finally:
        await runtime.stop()


def write_messages_session(path: Path, count: int) -> None:
    """Write a session file holding ``count`` chained user messages."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "session",
            "version": 3,
            "id": "session-many",
            "timestamp": "2026-01-01T00:00:00.000Z",
            "cwd": "/tmp/project",
        }
    ]
    parent: str | None = None
    for index in range(count):
        entry_id = f"ent{index:05d}"
        lines.append(
            {
                "type": "message",
                "id": entry_id,
                "parentId": parent,
                "timestamp": "2026-01-01T00:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": f"message {index}",
                    "timestamp": 1767225602000 + index,
                },
            }
        )
        parent = entry_id
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


async def test_truncated_snapshot_is_not_complete(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    session_file = tmp_path / "sessions" / "--tmp--" / "many.jsonl"
    write_messages_session(session_file, 10)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        full = await runtime.get_session_snapshot("sess-many", str(session_file))
        assert full.complete is True
        assert len(full.items) > 3

        limited = await runtime.get_session_snapshot("sess-many", str(session_file), limit=3)
        # A truncated tail must not claim to be the Runtime-owned full history;
        # the platform replaces stored timelines from complete snapshots.
        assert limited.complete is False
        assert [item.id for item in limited.items] == [item.id for item in full.items[-3:]]
    finally:
        await runtime.stop()


async def test_concurrent_ensure_live_shares_one_session(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    # The session file must exist so resolution goes through the slow branch
    # below; a missing file would resolve synchronously without a race window.
    write_messages_session(session_file, 3)

    # Widen the window between the duplicate check and the registration so the
    # race is deterministic instead of relying on event-loop luck.
    def slow_session_cwd(path: Path) -> str | None:
        time.sleep(0.05)
        return None

    monkeypatch.setattr(PiRuntime, "_session_cwd", staticmethod(slow_session_cwd))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        first, second = await asyncio.gather(
            runtime._ensure_live("sess-conc", str(session_file), None),
            runtime._ensure_live("sess-conc", str(session_file), None),
        )
        assert first is second
        assert len(runtime._live) == 1
    finally:
        await runtime.stop()


async def test_snapshot_rejects_paths_outside_sessions_dir(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    outside = tmp_path / "outside" / "stray.jsonl"
    write_messages_session(outside, 2)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        snapshot = await runtime.get_session_snapshot("sess-outside", str(outside))
        assert snapshot.complete is False
        assert snapshot.metadata.get("reason") == "session file not found"
        assert snapshot.items == ()
    finally:
        await runtime.stop()
