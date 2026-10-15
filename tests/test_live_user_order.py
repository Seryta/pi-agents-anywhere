"""A consumed pi user message must precede its streamed reply on the wire."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from pi_aa.runtime import PiLiveSession
from tests.conftest import FakeHost, wait_for
from tests.test_runtime import make_runtime


def user_entry(entry_id: str, text: str, timestamp: int) -> dict:
    return {
        "type": "message",
        "id": entry_id,
        "message": {
            "role": "user",
            "content": text,
            "timestamp": timestamp,
        },
    }


async def test_consumed_user_is_acknowledged_before_first_assistant_delta(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, session_file: Path
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session(
            "live-order", "hello", cwd=str(tmp_path), client_message_id="optimistic-1"
        )
        await wait_for(lambda: bool(fake_host.turn_ends))
        assert fake_host.timeline_items[0].role == "user", (
            "the first live item must clear optimistic user"
        )
        assert fake_host.timeline_items[0].source["clientMessageId"] == "optimistic-1"
        final_user = next(i for i in fake_host.timeline_syncs[-1]["items"] if i.role == "user")
        assert final_user.id == fake_host.timeline_items[0].id
    finally:
        await runtime.stop()


async def test_confirmation_failure_retries_without_a_new_event_and_holds_reply(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, monkeypatch
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "retry", cwd=str(tmp_path), session_path="/tmp/retry.jsonl")
    runtime._live[live.platform_id] = live
    entry = user_entry("native-u", "hello", 1)

    async def command(payload, **kwargs):
        return {"data": {"entries": [entry], "leafId": "native-u"}}

    live.process = SimpleNamespace(request=command)
    calls = 0
    original = fake_host.timeline_item_upsert

    async def fail_once(item):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary host outage")
        await original(item)

    monkeypatch.setattr(fake_host, "timeline_item_upsert", fail_once)
    task = asyncio.create_task(
        runtime.handle_live_event(live, {"type": "message_end", "message": entry["message"]})
    )
    await asyncio.wait_for(task, timeout=3)
    assert calls == 2, "confirmation needs its own retry trigger"
    assert fake_host.timeline_items[0].role == "user"


async def test_unflushed_native_user_is_confirmed_without_reading_disk(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, monkeypatch
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    missing = tmp_path / "sessions" / "not-yet-written.jsonl"
    live = PiLiveSession(runtime, "memory", cwd=str(tmp_path), session_path=str(missing))
    runtime._live[live.platform_id] = live
    entry = user_entry("in-memory-u", "hello", 1)

    async def command(payload, **kwargs):
        assert payload["type"] == "get_entries"
        return {"data": {"entries": [entry], "leafId": "in-memory-u"}}

    live.process = SimpleNamespace(request=command)
    await runtime.handle_live_event(live, {"type": "message_end", "message": entry["message"]})
    assert not missing.exists()
    assert len(fake_host.timeline_items) == 1
    assert fake_host.timeline_items[0].source["itemId"] == "in-memory-u:user"


async def test_identical_pending_prompts_bind_only_the_consumed_native_entry(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "same", cwd=str(tmp_path), session_path="/tmp/same.jsonl")
    runtime._live[live.platform_id] = live
    entries = [user_entry("new-1", "again", 2), user_entry("new-2", "again", 3)]
    requests = []

    async def request(payload, **kwargs):
        requests.append(payload)
        return {"data": {"entries": entries, "leafId": "new-2"}}

    live.process = SimpleNamespace(request=request, alive=True)
    live.entry_cursor = "old-1"
    await live.send_prompt("again", client_message_id="immediate")
    await live.send_prompt("again", streaming_behavior="followUp", client_message_id="follow")
    await live.send_prompt("again", streaming_behavior="steer", client_message_id="steer")
    assert not fake_host.timeline_items
    assert not live.client_entry_bindings, "a queued send must not confirm an old same-text message"
    await runtime.handle_live_event(live, {"type": "message_end", "message": entries[0]["message"]})
    assert fake_host.timeline_items[-1].source["clientMessageId"] == "immediate"
    assert "new-2" not in live.client_entry_bindings
    await runtime.handle_live_event(live, {"type": "message_end", "message": entries[1]["message"]})
    assert fake_host.timeline_items[-1].source["clientMessageId"] == "steer"
    assert [p[1] for p in live.pending_client_messages] == ["follow"]
    reads = [r for r in requests if r["type"] == "get_entries"]
    assert reads == [{"type": "get_entries", "since": "old-1"}], (
        "one incremental read can buffer later consumed entries"
    )


async def test_image_and_document_prompt_confirm_with_native_id_and_survive_reload(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    path = tmp_path / "sessions" / "images.jsonl"
    live = PiLiveSession(runtime, "image", cwd=str(tmp_path), session_path=str(path))
    runtime._live[live.platform_id] = live
    prompt = "look\n[file: notes.txt]"
    image = {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}
    entry = user_entry("image-user", prompt, 1)
    entry["message"]["content"] = [{"type": "text", "text": prompt}, image]

    async def request(payload, **kwargs):
        return {"data": {"entries": [entry], "leafId": "image-user"}}

    live.process = SimpleNamespace(request=request, alive=True)
    await live.send_prompt(prompt, images=[image], client_message_id="with-attachments")
    await runtime.handle_live_event(live, {"type": "message_end", "message": entry["message"]})
    assert fake_host.timeline_items[0].source["clientMessageId"] == "with-attachments"
    # An unrelated historical message with identical content cannot borrow the binding.
    from pi_aa.projection import project_session

    restarted = make_runtime(fake_pi, tmp_path, fake_host)
    await restarted._load_client_bindings(str(path))
    items = project_session(
        [dict(entry, id="historical"), entry],
        session_id="image",
        external_session_id=str(path),
        client_entry_bindings=restarted._client_entry_bindings[str(path)],
    )
    users = [i for i in items if i.role == "user"]
    assert users[0].source.get("clientMessageId") is None
    assert users[1].id == fake_host.timeline_items[0].id
    assert users[1].source["clientMessageId"] == "with-attachments"


async def test_retry_holds_queued_assistant_events_without_losing_their_text(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, monkeypatch
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "hold", cwd=str(tmp_path), session_path="/tmp/hold.jsonl")
    entry = user_entry("hold-u", "hello", 1)

    async def request(payload, **kwargs):
        return {"data": {"entries": [entry]}}

    live.process = SimpleNamespace(request=request)
    failed = asyncio.Event()
    release = asyncio.Event()
    original = fake_host.publish_runtime_notifications

    async def publish(*args, **kwargs):
        if not failed.is_set():
            failed.set()
            raise RuntimeError("offline")
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(fake_host, "publish_runtime_notifications", publish)
    user = asyncio.create_task(
        runtime.handle_live_event(live, {"type": "message_end", "message": entry["message"]})
    )
    await failed.wait()
    assistant = asyncio.create_task(
        runtime.handle_live_event(live, {"type": "message_start", "message": {"role": "assistant"}})
    )
    delta = asyncio.create_task(
        runtime.handle_live_event(
            live,
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "preserved"},
            },
        )
    )
    await asyncio.sleep(0.01)
    assert not fake_host.timeline_items
    assert not assistant.done()
    release.set()
    try:
        await asyncio.wait_for(asyncio.gather(user, assistant, delta), timeout=3)
        await wait_for(lambda: len(fake_host.timeline_items) >= 2)
        assert [item.role for item in fake_host.timeline_items] == ["user", "assistant"]
        assert fake_host.timeline_items[-1].content["text"] == "preserved"
    finally:
        await live.cancel_stream_flush()


async def test_invalid_cursor_falls_back_once_then_uses_native_tail(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    from pi_aa.rpc import PiRpcRequestFailed

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "cursor", cwd=str(tmp_path), session_path="/tmp/cursor.jsonl")
    live.entry_cursor = "rewritten-away"
    entries = [user_entry("old", "hello", 1), user_entry("new", "hello", 2)]
    live.confirmed_user_entries.add("old")
    calls = []

    async def request(payload, **kwargs):
        calls.append(payload)
        if payload.get("since") == "rewritten-away":
            raise PiRpcRequestFailed("get_entries", "Entry not found")
        return {"data": {"entries": entries, "leafId": "new"}}

    live.process = SimpleNamespace(request=request)
    await runtime.handle_live_event(
        live, {"type": "message_end", "message": entries[-1]["message"]}
    )
    assert calls == [{"type": "get_entries", "since": "rewritten-away"}, {"type": "get_entries"}]
    assert fake_host.timeline_items[0].source["itemId"] == "new:user"
    assert live.entry_cursor == "new"


async def test_empty_user_event_never_blocks_subsequent_events(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "empty", cwd=str(tmp_path))
    await asyncio.wait_for(
        runtime.handle_live_event(
            live, {"type": "message_end", "message": {"role": "user", "content": []}}
        ),
        timeout=1,
    )
    assert not fake_host.timeline_items


async def test_inflight_binding_survives_restart_before_entry_lookup_finishes(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, session_file: Path, monkeypatch
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    lookup_started = asyncio.Event()
    original = runtime._confirm_live_user

    async def paused(live, message):
        lookup_started.set()
        await asyncio.Future()
        await original(live, message)

    monkeypatch.setattr(runtime, "_confirm_live_user", paused)
    try:
        await runtime.create_and_start_session("inflight", "again", client_message_id="in-flight")
        await asyncio.wait_for(lookup_started.wait(), timeout=2)
        assert session_file.is_file()
        assert not fake_host.timeline_items
    finally:
        await runtime.stop()
    restarted = make_runtime(fake_pi, tmp_path, fake_host)
    snapshot = await restarted.get_session_snapshot("inflight", str(session_file))
    users = [i for i in snapshot.items if i.role == "user"]
    assert users[-1].source["clientMessageId"] == "in-flight"


async def test_successful_clear_queue_removes_pending_bindings_before_repeated_send(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, monkeypatch
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "clear", cwd=str(tmp_path), session_path="/tmp/clear.jsonl")
    runtime._live[live.platform_id] = live
    live.is_streaming = True
    entry = user_entry("new", "again", 1)

    async def request(payload, **kwargs):
        if payload["type"] == "clear_queue":
            return {"data": {"steering": ["again"], "followUp": ["again"]}}
        return {"data": {"entries": [entry]}}

    live.process = SimpleNamespace(request=request, alive=True)
    await live.send_prompt(
        "again", streaming_behavior="followUp", client_message_id="cleared-follow"
    )
    await live.send_prompt("again", streaming_behavior="steer", client_message_id="cleared-steer")
    await runtime.interrupt_session(live.platform_id)
    assert not live.pending_client_messages
    await live.send_prompt("again", client_message_id="new-send")
    await runtime.handle_live_event(live, {"type": "message_end", "message": entry["message"]})
    assert fake_host.timeline_items[-1].source["clientMessageId"] == "new-send"


async def test_recovery_cursor_excludes_old_same_text_and_keeps_unconsumed_followup(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    path = "/tmp/recovery.jsonl"
    old = user_entry("old", "again", 1)
    consumed = user_entry("consumed", "again", 2)
    runtime._pending_client_bindings[path] = (
        ("again", "new-send", None, "again", "old"),
        ("again", "queued", "followUp", "again", "old"),
    )
    runtime._recover_pending_client_bindings(path, [old, consumed])
    assert runtime._client_entry_bindings[path] == {"consumed": "new-send"}
    assert [p[1] for p in runtime._pending_client_bindings[path]] == ["queued"]


async def test_replacement_process_discards_old_unconsumed_queue_bindings(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, session_file: Path
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session("restart-queue", "seed", client_message_id="seed")
        await wait_for(lambda: bool(fake_host.turn_ends))
        live = runtime._live["restart-queue"]
        # Fake pi acknowledges steer but does not consume it, mirroring a
        # process killed while a queued message is still pending.
        await live.send_prompt("again", streaming_behavior="steer", client_message_id="old-queue")
        assert live.pending_client_messages
    finally:
        await runtime.stop()
    restarted = make_runtime(fake_pi, tmp_path, fake_host)
    try:
        live = await restarted._ensure_live("restart-queue", str(session_file), None)
        assert not live.pending_client_messages
        stored = fake_host.sync_state[restarted._client_bindings_key(str(session_file))]
        assert not stored["pendingBindings"]
        assert "seed" in stored["entryBindings"].values()
    finally:
        await restarted.stop()


async def test_dead_unconsumed_followup_cannot_steal_new_followup_identity(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, session_file: Path, monkeypatch
):
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session("restart-order", "seed", client_message_id="seed")
        await wait_for(lambda: bool(fake_host.turn_ends))
        live = runtime._live["restart-order"]
        await live.send_prompt(
            "again", streaming_behavior="followUp", client_message_id="dead-followup"
        )
        await wait_for(lambda: live.pending_message_count == 1)
        previous = live.process
        assert previous is not None and previous._process is not None
        previous._process.kill()
        await previous._process.wait()
        await wait_for(lambda: live._exit_handled)
        # The dead fake process never consumed its queued 'again' entry.
        monkeypatch.setenv("PI_FAKE_BUSY_FIRST_PROMPT", "1")
        await runtime.start_turn(
            "restart-order", str(session_file), "new busy run", client_message_id="busy"
        )
        await wait_for(lambda: live.is_streaming)
        await runtime.start_turn(
            "restart-order", str(session_file), "again", client_message_id="new-followup"
        )
        await wait_for(
            lambda: any(
                i.role == "user" and i.content.get("text") == "again"
                for i in fake_host.timeline_items
            )
        )
        user = next(
            i
            for i in fake_host.timeline_items
            if i.role == "user" and i.content.get("text") == "again"
        )
        assert user.source["clientMessageId"] == "new-followup", (
            "a dead process's unconsumed pending binding stole a later native user entry"
        )
    finally:
        await runtime.stop()
