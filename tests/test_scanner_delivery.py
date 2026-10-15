from __future__ import annotations

import asyncio

import pytest
from connector.runtime_protocol import RuntimeConfig, RuntimeTimelineItem

from pi_aa.runtime import _scanner_item_payload
from tests.conftest import FakeHost
from tests.test_runtime import make_runtime, write_messages_session


async def emit_answer(runtime, live):
    await runtime.handle_live_event(live, {"type": "agent_start"})
    await runtime.handle_live_event(
        live,
        {
            "type": "message_start",
            "message": {"role": "assistant"},
        },
    )
    await runtime.handle_live_event(
        live,
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "new live answer"}],
                "stopReason": "stop",
            },
        },
    )


def platform_delivery(fake_host, monkeypatch):
    platform = {}

    async def publish(*args, **kwargs):
        if kwargs.get("complete"):
            platform.clear()
        platform.update((item.id, item) for item in kwargs["items"])

    async def upsert(item):
        platform[item.id] = item

    monkeypatch.setattr(fake_host, "timeline_sync", publish)
    monkeypatch.setattr(fake_host, "timeline_item_upsert", upsert)
    return platform


@pytest.mark.parametrize("delta", [False, True])
async def test_offline_scanner_delivery_preserves_later_activated_turn(
    fake_pi, tmp_path, fake_host, monkeypatch, delta
):
    path = tmp_path / "sessions" / "history.jsonl"
    write_messages_session(path, 20)
    monkeypatch.setenv("PI_FAKE_SESSION_FILE", str(path))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    platform = platform_delivery(fake_host, monkeypatch)
    session = (await runtime.list_sessions())[0]
    try:
        if delta:
            initial = await runtime.prepare_session_timeline_sync(session.session_id, str(path))
            await initial.commit()
            write_messages_session(path, 21)
        old_state = await runtime.get_session_state(session.session_id, str(path))
        prepared = await runtime.prepare_session_timeline_sync(session.session_id, str(path))
        if delta:
            assert prepared.snapshot is not None and not prepared.snapshot.complete
        # Use the real registration/start path, with a different platform id.
        live = await runtime._ensure_live("activated-session", str(path), None)
        await emit_answer(runtime, live)
        if prepared.snapshot is not None:
            await fake_host.timeline_sync(
                session_id=session.session_id,
                runtime="pi",
                items=prepared.snapshot.items,
                complete=prepared.snapshot.complete,
            )
        await fake_host.session_state_update(session.session_id, "pi", status=old_state.status)
        await prepared.commit()
        assert any(item.content.get("text") == "new live answer" for item in platform.values())
        assert fake_host.states[-1]["status"] == "running"
    finally:
        await runtime.stop()


@pytest.mark.parametrize("symlink", [False, True])
async def test_blocked_offline_replacement_orders_real_activation_by_canonical_path(
    fake_pi, tmp_path, fake_host, monkeypatch, symlink
):
    path = tmp_path / "sessions" / "history.jsonl"
    write_messages_session(path, 3)
    alias = path
    if symlink:
        alias = path.with_name("alias.jsonl")
        alias.symlink_to(path)
    monkeypatch.setenv("PI_FAKE_SESSION_FILE", str(alias))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    platform = platform_delivery(fake_host, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_batch(*args, **kwargs):
        entered.set()
        await release.wait()
        await FakeHost.publish_runtime_notifications(fake_host, *args, **kwargs)

    monkeypatch.setattr(fake_host, "publish_runtime_notifications", blocked_batch)
    retry = asyncio.create_task(runtime.prepare_session_timeline_sync("scanner-id", str(path)))
    await asyncio.wait_for(entered.wait(), 1)
    activation = asyncio.create_task(runtime._ensure_live("activated-id", str(alias), None))
    try:
        await asyncio.sleep(0)
        assert not runtime._live
        release.set()
        prepared, live = await asyncio.wait_for(asyncio.gather(retry, activation), 3)
        assert live.session_path == str(alias)
        assert runtime._session_path_lock(path) is runtime._session_path_lock(alias)
        await emit_answer(runtime, live)
        await fake_host.session_state_update("scanner-id", "pi", status="idle")
        await prepared.commit()
        assert any(item.content.get("text") == "new live answer" for item in platform.values())
        assert fake_host.states[-1]["status"] == "running"
    finally:
        release.set()
        await asyncio.gather(retry, activation, return_exceptions=True)
        await runtime.stop()


@pytest.mark.parametrize("failure", ["meta", "timeline", "checkpoint", "cancel"])
async def test_scanner_batch_failure_stays_pending_and_releases_activation_lock(
    fake_pi, tmp_path, fake_host, monkeypatch, failure
):
    path = tmp_path / "sessions" / "failure.jsonl"
    write_messages_session(path, 3)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    session = (await runtime.list_sessions())[0]

    async def fail(*args, **kwargs):
        raise OSError("backend unavailable")

    with monkeypatch.context() as patch:
        if failure == "cancel":
            entered = asyncio.Event()

            async def block(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()

            patch.setattr(fake_host, "publish_runtime_notifications", block)
            task = asyncio.create_task(
                runtime.prepare_session_timeline_sync(session.session_id, str(path))
            )
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            target = {
                "meta": "session_meta_upsert",
                "timeline": "timeline_sync",
                "checkpoint": "sync_state_write",
            }[failure]
            patch.setattr(fake_host, target, fail)
            await runtime.prepare_session_timeline_sync(session.session_id, str(path))
    assert (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"]
    # Retry succeeds after the failed/cancelled batch releases its path lock.
    await asyncio.wait_for(runtime.prepare_session_timeline_sync(session.session_id, str(path)), 1)
    assert not (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"]


async def test_scanner_batch_creates_session_before_replacement_and_filters_turns(
    fake_pi, tmp_path, fake_host
):
    path = tmp_path / "sessions" / "contract.jsonl"
    write_messages_session(path, 3)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime.config = RuntimeConfig(runtime="pi", revision=1, runtime_id="pi-instance", values={})
    prepared = await runtime.prepare_session_timeline_sync("unknown-session", str(path))
    assert prepared.snapshot is None
    runtime_name, batch, runtime_id = fake_host.notification_batches[-1]
    assert runtime_name == "pi" and runtime_id == "pi-instance"
    assert [notice["method"] for notice in batch] == ["session.meta.upsert", "timeline.sync"]
    assert all(notice["params"]["sessionId"] == "unknown-session" for notice in batch)
    assert batch[0]["params"]["externalSessionId"] == str(path)
    assert batch[1]["params"]["complete"]
    assert len(batch[1]["params"]["items"]) == 3
    assert all(item["type"] == "message" for item in batch[1]["params"]["items"])


def test_scanner_wire_contract_preserves_reconciliation_source_and_excludes_turn_data():
    item = RuntimeTimelineItem(
        id="message",
        session_id="session",
        type="message",
        status="done",
        order_seq=3,
        content_hash="hash",
        role="assistant",
        turn_id="private-turn",
        content={"text": "answer"},
        revision=2,
        source={
            "runtime": "pi",
            "runtimeType": "pi",
            "runtimeId": "instance",
            "threadId": "external",
            "rawType": "message",
            "event": "end",
            "clientMessageId": "optimistic",
            "derivedKey": "key",
            "turnId": "private",
        },
        metadata={"private": "local"},
    )
    assert _scanner_item_payload(item, "observed") == {
        "id": "message",
        "sessionId": "session",
        "type": "message",
        "status": "done",
        "role": "assistant",
        "content": {"text": "answer"},
        "orderSeq": 3,
        "revision": 2,
        "contentHash": "hash",
        "createdAt": "observed",
        "updatedAt": "observed",
        "source": {
            "runtime": "pi",
            "runtimeType": "pi",
            "runtimeId": "instance",
            "sessionId": "external",
            "itemId": "message",
            "itemType": "message",
            "event": "end",
            "clientMessageId": "optimistic",
            "derivedKey": "key",
        },
    }
