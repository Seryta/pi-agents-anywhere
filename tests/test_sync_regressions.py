from __future__ import annotations

import pytest

from pi_aa.runtime import PiLiveSession
from tests.conftest import FakeHost
from tests.test_runtime import make_runtime, write_messages_session


@pytest.mark.parametrize("limit", [None, 3])
async def test_snapshot_read_does_not_acknowledge_delivery(fake_pi, tmp_path, fake_host, limit):
    path = tmp_path / "sessions" / "snapshot.jsonl"
    write_messages_session(path, 20)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    session = (await runtime.list_sessions())[0]
    await runtime.get_session_snapshot(session.session_id, str(path), limit=limit)
    assert (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"]
    prepared = await runtime.prepare_session_timeline_sync(session.session_id, str(path))
    assert prepared.snapshot.complete
    await prepared.commit()
    assert not (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"]


@pytest.mark.parametrize("failure", ["publish", "checkpoint", None])
async def test_settle_acknowledges_only_successful_delivery(
    fake_pi, tmp_path, fake_host, monkeypatch, failure
):
    path = tmp_path / "sessions" / "settle.jsonl"
    write_messages_session(path, 3)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    session = (await runtime.list_sessions())[0]
    live = PiLiveSession(runtime, session.session_id, cwd=str(tmp_path), session_path=str(path))

    async def fail(*args, **kwargs):
        raise OSError("backend unavailable")

    with monkeypatch.context() as patch:
        if failure:
            patch.setattr(
                fake_host, "timeline_sync" if failure == "publish" else "sync_state_write", fail
            )
        await runtime._publish_timeline(live)
    assert (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"] == bool(
        failure
    )
    if failure:
        # A failed settle remains eligible for a full scanner retry.
        prepared = await runtime.prepare_session_timeline_sync(session.session_id, str(path))
        assert prepared.snapshot.complete
        await fake_host.timeline_sync(
            session.session_id, "pi", prepared.snapshot.items, complete=True
        )
        await prepared.commit()
        assert not (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"]


async def test_file_append_during_publish_remains_unsynced(
    fake_pi, tmp_path, fake_host, monkeypatch
):
    path = tmp_path / "sessions" / "growing.jsonl"
    write_messages_session(path, 3)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    session = (await runtime.list_sessions())[0]
    live = PiLiveSession(runtime, session.session_id, cwd=str(tmp_path), session_path=str(path))

    async def append_during_send(*args, **kwargs):
        await FakeHost.timeline_sync(fake_host, *args, **kwargs)
        write_messages_session(path, 4)

    monkeypatch.setattr(fake_host, "timeline_sync", append_during_send)
    await runtime._publish_timeline(live)
    assert (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"]
    prepared = await runtime.prepare_session_timeline_sync(session.session_id, str(path))
    assert prepared.snapshot is not None
    assert any(item.content.get("text") == "message 3" for item in prepared.snapshot.items)
