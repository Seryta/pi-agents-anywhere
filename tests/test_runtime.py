from __future__ import annotations

from pathlib import Path

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
