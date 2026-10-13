from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

import pytest
from connector.runtime_protocol import (
    CAPABILITY_RUNTIME_ATTACHMENT,
    CAPABILITY_SESSION_SEND_MESSAGE,
    RuntimeAttachment,
    RuntimeConfig,
    attachments_root,
    session_attachments_dir,
)

from pi_aa.rpc import PiRpcProcess, PiRpcTimeout
from pi_aa.runtime import (
    REANNOUNCE_MIN_INTERVAL_SECONDS,
    PendingInteraction,
    PiLiveSession,
    PiRuntime,
    platform_session_id,
)
from tests.conftest import FAKE_PNG_BYTES, FAKE_TEXT_BYTES, FakeHost, wait_for


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


def session_user_contents(session_file: Path) -> list[object]:
    """Content of every user message pi persisted, in order."""

    records = [
        json.loads(line)
        for line in session_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [
        record["message"]["content"]
        for record in records
        if record.get("type") == "message" and (record.get("message") or {}).get("role") == "user"
    ]


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


async def test_state_change_publishes_session_capabilities(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """Capability facts must be published, not only answered on request.

    The platform caches capability facts only from runtime notifications, and
    snapshot and websocket projections fall back to that cache when a live
    read fails; without a publish the cached set stays empty and clients see
    every session action as unavailable.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session(
            "sess-caps",
            "hello",
            cwd=str(tmp_path),
        )
        await wait_for(lambda: len(fake_host.capability_sets) >= 1)
    finally:
        await runtime.stop()

    published = fake_host.capability_sets[-1]
    assert published.session_id == "sess-caps"
    by_id = {capability.capability_id: capability for capability in published.capabilities}
    assert "session.send_message" in by_id
    send = by_id["session.send_message"]
    assert send.supported and send.available and send.allowed


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
        assert notice.interaction_type == "confirmation"
        assert notice.context["method"] == "confirm"
        assert any(action["actionId"] == "confirm" for action in notice.actions)

        result = await runtime.respond_interaction("sess-ui", notice.notice_id, "confirm")
        assert result.ok is True
        # The fake settles after the UI response and the runtime publishes it.
        await wait_for(lambda: len(fake_host.timeline_syncs) >= 1)
        resolved = [n for n in fake_host.notices if n.status == "resolved"]
        assert resolved, "interaction notice should be resolved"
    finally:
        await runtime.stop()


async def test_extension_set_status_reaches_session_metadata(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fire-and-forget ``setStatus`` must land in the published metadata.

    pi extensions announce progress through ``setStatus``; the platform only
    sees what the state push carries, so dropping the event leaves the session
    looking idle while an extension is still running.
    """

    monkeypatch.setenv("PI_FAKE_SET_STATUS", "1")
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session("sess-status", "hello", cwd=str(tmp_path))

        def status_pushed() -> bool:
            return any(
                state.get("metadata", {}).get("extensionStatus") == {"fake-ext": "fake status"}
                for state in fake_host.states
            )

        await wait_for(status_pushed)
        state = await runtime.get_session_state("sess-status")
        assert state is not None
        assert state.metadata["extensionStatus"] == {"fake-ext": "fake status"}
    finally:
        await runtime.stop()


async def test_extension_set_status_tracks_changes_and_clears(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """``setStatus`` rewrites one key at a time and a missing text clears it.

    The state push is deduplicated through ``state_key``, so the extension map
    must be part of that key or a second update for the same key never reaches
    the platform.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "sess-status-direct", cwd=str(tmp_path))
    runtime._live["sess-status-direct"] = live

    await runtime._handle_ui_request(
        live,
        {
            "type": "extension_ui_request",
            "id": "status-1",
            "method": "setStatus",
            "statusKey": "my-ext",
            "statusText": "Turn 3 running...",
        },
    )
    state = await runtime.get_session_state("sess-status-direct")
    assert state is not None
    assert state.metadata["extensionStatus"] == {"my-ext": "Turn 3 running..."}

    # Same key, new text: the state key must change so the platform sees it.
    await runtime._handle_ui_request(
        live,
        {
            "type": "extension_ui_request",
            "id": "status-2",
            "method": "setStatus",
            "statusKey": "my-ext",
            "statusText": "Turn 4 running...",
        },
    )
    assert fake_host.states[-1]["metadata"]["extensionStatus"] == {"my-ext": "Turn 4 running..."}

    # Omitting statusText clears the entry, dropping it from the metadata.
    await runtime._handle_ui_request(
        live,
        {
            "type": "extension_ui_request",
            "id": "status-3",
            "method": "setStatus",
            "statusKey": "my-ext",
        },
    )
    state = await runtime.get_session_state("sess-status-direct")
    assert state is not None
    assert "extensionStatus" not in state.metadata
    assert "extensionStatus" not in fake_host.states[-1]["metadata"]

    # Non-string keys and texts are ignored, never coerced.
    pushed = len(fake_host.states)
    await runtime._handle_ui_request(
        live,
        {
            "type": "extension_ui_request",
            "id": "status-4",
            "method": "setStatus",
            "statusKey": 7,
            "statusText": "nope",
        },
    )
    await runtime._handle_ui_request(
        live,
        {
            "type": "extension_ui_request",
            "id": "status-5",
            "method": "setStatus",
            "statusKey": "my-ext",
            "statusText": 9,
        },
    )
    assert len(fake_host.states) == pushed
    assert live.extension_status == {}


async def test_notify_toast_resolves_when_turn_settles(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """``notify`` is transient and must close when the run that sent it ends.

    pi never emits a resolve event for fire-and-forget notifications, so the
    runtime tracks the notices it opened and resolves them on settle; a second
    settle must not publish the resolution twice.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "sess-notify", cwd=str(tmp_path))
    runtime._live["sess-notify"] = live

    await runtime._handle_ui_request(
        live,
        {
            "type": "extension_ui_request",
            "id": "notify-1",
            "method": "notify",
            "message": "Build finished",
            "notifyType": "warning",
        },
    )
    notice_id = "pi-notify-notify-1"
    assert [notice.status for notice in fake_host.notices] == ["open"]
    assert fake_host.notices[0].notice_id == notice_id
    assert fake_host.notices[0].severity == "warning"
    assert live.open_notifications.keys() == {notice_id}

    await runtime.handle_live_event(live, {"type": "agent_settled"})
    assert [notice.status for notice in fake_host.notices] == ["open", "resolved"]
    assert fake_host.notices[-1].notice_id == notice_id
    assert live.open_notifications == {}

    # A repeated settle has nothing left to resolve.
    await runtime.handle_live_event(live, {"type": "agent_settled"})
    assert [notice.status for notice in fake_host.notices] == ["open", "resolved"]


async def test_extension_notify_round_trip_through_fake(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake's ``PI_FAKE_NOTIFY`` switch drives the full settle path."""

    monkeypatch.setenv("PI_FAKE_NOTIFY", "1")
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session("sess-notify-fake", "hello", cwd=str(tmp_path))
        notice_id = "pi-notify-ext-notify-1"

        def resolved_notice_seen() -> bool:
            return any(
                notice.notice_id == notice_id and notice.status == "resolved"
                for notice in fake_host.notices
            )

        await wait_for(resolved_notice_seen)
        statuses = [n.status for n in fake_host.notices if n.notice_id == notice_id]
        assert statuses == ["open", "resolved"]
        assert runtime._live["sess-notify-fake"].open_notifications == {}
    finally:
        await runtime.stop()


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("confirm", "confirmation"),
        ("select", "input_request"),
        ("input", "input_request"),
        ("editor", "input_request"),
        ("unsupported", "unknown"),
    ],
)
def test_pending_interaction_maps_method_to_server_type(method: str, expected: str) -> None:
    """Pi dialog methods must map onto the server's InteractionType literal.

    The server validates interactionType with pydantic and rejects anything
    outside the literal, so a private ``pi.<method>`` value makes the notice
    undeliverable. This mirrors ``InteractionType`` in server core/models.py
    (verified against server 2.0.3); the original method must stay in the
    notice context.
    """

    interaction = PendingInteraction({"id": "req-1", "method": method, "title": "t"})
    notice = interaction.as_notice("sess-1")
    assert notice.interaction_type == expected
    assert notice.context["method"] == method
    assert notice.interaction_type in {
        "approval",
        "execution_error",
        "confirmation",
        "input_request",
        "unknown",
    }


def test_pending_interaction_without_timeout_never_expires() -> None:
    """Requests without a pi-side timeout keep the old behavior.

    pi only starts its auto-resolve timer for a truthy ``timeout`` (its
    ``if (opts?.timeout)``), so both an absent field and zero mean the client
    must still answer and the entry must never expire on its own.
    """

    absent = PendingInteraction({"id": "req-1", "method": "confirm", "title": "t"})
    assert absent.timeout_ms is None
    assert absent.deadline is None
    assert absent.expired(time.monotonic() + 3600) is False
    zero = PendingInteraction({"id": "req-2", "method": "input", "title": "t", "timeout": 0})
    assert zero.deadline is None
    assert zero.expired(time.monotonic() + 3600) is False


async def test_timed_out_ui_request_is_expired_on_settle(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """A dialog pi auto-resolved must not keep the session waiting.

    pi enforces a request's ``timeout`` itself and emits no event when it
    fires (docs/rpc-extension-ui.md), so the runtime only learns about the
    resolution by checking the deadline it recorded with the request. Without
    that, the pending entry, its open notice, and ``waiting_approval`` survive
    the whole run and the idle reclaim loop can never close the pi process.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "sess-timeout", cwd=str(tmp_path))
    runtime._live["sess-timeout"] = live
    await runtime.handle_live_event(
        live,
        {
            "type": "extension_ui_request",
            "id": "ui-timeout-1",
            "method": "confirm",
            "title": "Fake dialog",
            "timeout": 5000,
        },
    )
    pending = live.pending_ui["ui-timeout-1"]
    assert pending.deadline is not None
    assert [notice.status for notice in fake_host.notices] == ["open"]

    # Simulate the pi-side timeout without sleeping: the deadline is now past.
    pending.deadline = time.monotonic() - 0.001

    # Reads must not report a dialog pi already resolved, and must not push.
    state = await runtime.get_session_state("sess-timeout")
    assert state is not None and state.status != "waiting_approval"
    assert await runtime.get_session_notices("sess-timeout") == ()
    assert [notice.status for notice in fake_host.notices] == ["open"]

    await runtime.handle_live_event(live, {"type": "agent_settled"})

    assert live.pending_ui == {}
    state = await runtime.get_session_state("sess-timeout")
    assert state is not None and state.status != "waiting_approval"
    assert [notice.status for notice in fake_host.notices] == ["open", "expired"]
    expired = fake_host.notices[-1]
    assert expired.notice_id == pending.notice_id
    assert expired.response_required is False
    assert expired.blocking is None


async def test_agent_settled_aborted_reports_interrupted_turn(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """An aborted agent turn must be reported as ``interrupted``.

    pi's ``agent_settled`` event carries an ``aborted`` boolean; ignoring it
    labels an interrupted turn as ``completed``, which misrepresents turn ends
    to the platform (alerts, turn-counting, timeline markers).
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "sess-abort-1", cwd=str(tmp_path))
    runtime._live["sess-abort-1"] = live

    await runtime.handle_live_event(live, {"type": "agent_settled", "aborted": True})
    assert fake_host.turn_ends[-1]["outcome"] == "interrupted"

    # A normal (non-aborted) settle keeps reporting ``completed``.
    await runtime.handle_live_event(live, {"type": "agent_settled", "aborted": False})
    assert fake_host.turn_ends[-1]["outcome"] == "completed"


async def test_reclaim_clears_timed_out_pending_interaction(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """The reclaim pass expires timed-out dialogs and frees the process."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "sess-timeout-reclaim", cwd=str(tmp_path))
    runtime._live["sess-timeout-reclaim"] = live
    pending = PendingInteraction(
        {"id": "ui-timeout-2", "method": "select", "title": "t", "timeout": 5000}
    )
    live.pending_ui[pending.request_id] = pending
    pending.deadline = time.monotonic() - 0.001

    runtime._idle_timeout = 0.05
    live.last_activity -= 1.0
    await runtime._reclaim_idle_sessions()

    assert "sess-timeout-reclaim" not in runtime._live
    assert [notice.status for notice in fake_host.notices] == ["expired"]
    assert fake_host.notices[0].notice_id == pending.notice_id


async def test_ensure_started_tolerates_get_state_timeout(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A get_state timeout must degrade to a warning, not fail the start.

    ``request`` raises ``PiRpcTimeout``, which is not a ``TimeoutError``
    subclass (PiRpcTimeout -> PiRpcError -> Exception), so the original
    capture tuple let it escape through ``create_and_start_session`` and the
    first ``start_turn`` instead of continuing with a usable process.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "sess-timeout-start", cwd=str(tmp_path))
    runtime._live["sess-timeout-start"] = live

    async def timing_out(self: PiRpcProcess, command, **kwargs) -> None:
        raise PiRpcTimeout(f"no answer for {command.get('type')!r}")

    monkeypatch.setattr(PiRpcProcess, "request", timing_out)
    try:
        await live.ensure_started()
        assert live.alive is True
        # The degraded start is not retried from scratch by the next call.
        await live.ensure_started()
        assert live.alive is True
    finally:
        runtime._live.pop("sess-timeout-start", None)
        await live.stop()


async def test_superseded_live_exit_does_not_push_idle(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """An exited process must not overwrite the state of its replacement.

    Reclaim pops the live entry before stopping the process; if the user
    sends a new message right away, the replacement already pushed running
    and the old process's exit callback must not push idle over it.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    old = PiLiveSession(runtime, "sess-superseded", cwd=str(tmp_path))
    pending = PendingInteraction({"id": "ui-old", "method": "confirm", "title": "t"})
    old.pending_ui[pending.request_id] = pending
    replacement = PiLiveSession(runtime, "sess-superseded", cwd=str(tmp_path))
    runtime._live["sess-superseded"] = replacement

    await runtime.handle_live_exit(old, 0)

    # The dead process's dialog can never be answered, so its notice still
    # closes; only the stale idle state push is skipped.
    assert [notice.status for notice in fake_host.notices] == ["resolved"]
    assert fake_host.states == []

    # A live session that is still current keeps the original behaviour.
    await runtime.handle_live_exit(replacement, 0)
    assert len(fake_host.states) == 1
    assert fake_host.states[0]["status"] == "idle"


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
        # stay unique or the platform rejects the whole catalog. Providers are
        # sorted so the picker stays deterministic and grouped, and each group
        # starts with a disabled heading entry the clients render as a label.
        assert ids == [
            "pi-aa-group:alt",
            "alt:test-model",
            "pi-aa-group:test",
            "test:test-model",
            "test:other-model",
        ]
        assert len(ids) == len(set(ids))
        assert catalog.models[0].enabled is False
        assert catalog.models[0].selection_id == "pi-aa-group:alt"
        # Providers render as group headings; titles stay plain so identical
        # names under different providers read the same inside their groups.
        titles = [model.title for model in catalog.models]
        assert titles == [
            "alt",
            "Test Model",
            "test",
            "Test Model",
            "Other Model",
        ]
        filtered = await runtime.list_model_catalog(query="alt")
        assert [model.id for model in filtered.models] == ["pi-aa-group:alt", "alt:test-model"]
        # Labels are computed over the full directory, so filtering keeps them.
        assert filtered.models[-1].title == "Test Model"

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


async def test_single_provider_catalog_stays_flat(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """No headings and no provider suffixes when only one provider exists."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:

        async def fake_command(_payload):
            return {
                "data": {
                    "models": [
                        {"id": "solo-a", "name": "Solo A", "provider": "solo"},
                        {"id": "solo-b", "name": "Solo B", "provider": "solo"},
                    ]
                }
            }

        runtime._utility_command = fake_command  # type: ignore[method-assign]
        catalog = await runtime.list_model_catalog()
        assert [model.id for model in catalog.models] == ["solo:solo-a", "solo:solo-b"]
        assert [model.title for model in catalog.models] == ["Solo A", "Solo B"]
        assert all(model.enabled for model in catalog.models)
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
        # The builtin proxies come first; the pi-provided commands follow.
        pi_commands = [
            command for command in commands if command.metadata.get("source") != "pi-builtin"
        ]
        assert [command.id for command in pi_commands] == ["fix-tests"]
    finally:
        await runtime.stop()


async def test_execute_command_reports_accepted_state(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
) -> None:
    """AA 2.0.3 derives command follow-up behavior from ``executionState``.

    Dispatch only hands the command to the pi child process; the native work
    completes asynchronously, so ``accepted`` is the only honest outcome here.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session("sess-command", "hi", cwd=str(tmp_path))
        result = await runtime.execute_command("sess-command", "fix-tests")
        assert result.ok is True
        assert result.result["executionState"] == "accepted"
    finally:
        await runtime.stop()


async def test_list_commands_prepends_builtin_commands(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """The TUI-only slash commands are proxied ahead of pi's own commands.

    pi's RPC ``get_commands`` only returns prompt commands; the TUI builtins
    (compact/name/stats/export) must still be offered by the platform.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        commands = await runtime.list_commands("sess-none")
        assert [command.id for command in commands] == [
            "compact",
            "name",
            "stats",
            "export",
            "fix-tests",
        ]
        builtins = commands[:4]
        assert all(command.metadata["source"] == "pi-builtin" for command in builtins)
        assert all(command.description for command in builtins)
        assert commands[4].metadata["source"] == "prompt"
        # Search covers the builtins too.
        filtered = await runtime.list_commands("sess-none", query="stat")
        assert [command.id for command in filtered] == ["stats"]
    finally:
        await runtime.stop()


async def test_list_commands_carries_command_source_info(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """pi's ``sourceInfo`` block lands on the platform command metadata.

    The known string fields (path/source/scope/origin/baseDir) are picked
    verbatim; the legacy ``source`` key stays untouched for compatibility.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        commands = await runtime.list_commands("sess-none")
        pi_commands = [
            command for command in commands if command.metadata.get("source") != "pi-builtin"
        ]
        assert pi_commands[0].id == "fix-tests"
        assert pi_commands[0].metadata["source"] == "prompt"
        assert pi_commands[0].metadata["sourceInfo"] == {
            "path": "/home/user/myproject/.pi/agent/prompts/fix-tests.md",
            "source": "local",
            "scope": "project",
            "origin": "top-level",
        }
    finally:
        await runtime.stop()


async def test_list_commands_without_source_info_stays_clean(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """Commands without (or with an unusable) ``sourceInfo`` are tolerated.

    The metadata key is simply omitted, and no validation error from a
    malformed block may escape ``list_commands``.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:

        async def fake_command(_payload):
            return {
                "data": {
                    "commands": [
                        {
                            "name": "plain-cmd",
                            "description": "no source info",
                            "source": "extension",
                        },
                        {
                            "name": "odd-cmd",
                            "description": "bad source info",
                            "source": "prompt",
                            "sourceInfo": 42,
                        },
                        {
                            "name": "empty-cmd",
                            "description": "empty source info",
                            "source": "prompt",
                            "sourceInfo": {},
                        },
                        {
                            "name": "mixed-cmd",
                            "description": "mixed source info",
                            "source": "prompt",
                            "sourceInfo": {
                                "path": "/tmp/x.md",
                                "scope": 7,
                                "unknown": "kept?",
                            },
                        },
                    ]
                }
            }

        runtime._utility_command = fake_command  # type: ignore[method-assign]
        commands = await runtime.list_commands("sess-none")
        pi_commands = [
            command for command in commands if command.metadata.get("source") != "pi-builtin"
        ]
        assert [command.id for command in pi_commands] == [
            "plain-cmd",
            "odd-cmd",
            "empty-cmd",
            "mixed-cmd",
        ]
        # Missing, non-mapping, and empty ``sourceInfo`` blocks leave no key.
        assert all("sourceInfo" not in command.metadata for command in pi_commands[:3])
        # Only known string fields are forwarded; the rest is dropped.
        assert pi_commands[3].metadata["sourceInfo"] == {"path": "/tmp/x.md"}
        assert "unknown" not in pi_commands[3].metadata["sourceInfo"]
    finally:
        await runtime.stop()


async def test_execute_builtin_compact_sends_custom_instructions(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``compact`` must call the RPC instead of sending a ``/compact`` prompt."""

    commands_file = tmp_path / "pi-commands.jsonl"
    monkeypatch.setenv("PI_FAKE_COMMANDS_FILE", str(commands_file))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        result = await runtime.execute_command("sess-compact", "compact", args=("focus",))
        assert result.ok is True
        assert result.result["executionState"] == "accepted"
        assert result.message == "已压缩 150000 → 32000 tokens"
        await wait_for(lambda: commands_file.is_file())
        commands = [
            json.loads(line) for line in commands_file.read_text(encoding="utf-8").splitlines()
        ]
        compacts = [command for command in commands if command["type"] == "compact"]
        assert compacts[0]["customInstructions"] == "focus"
        assert not [command for command in commands if command["type"] == "prompt"]

        # Several args join with spaces; no args omits the key entirely.
        result = await runtime.execute_command("sess-compact", "compact", args=("take", "five"))
        assert result.ok is True
        result = await runtime.execute_command("sess-compact", "compact")
        assert result.ok is True
        commands = [
            json.loads(line) for line in commands_file.read_text(encoding="utf-8").splitlines()
        ]
        compacts = [command for command in commands if command["type"] == "compact"]
        assert [command.get("customInstructions") for command in compacts] == [
            "focus",
            "take five",
            None,
        ]
    finally:
        await runtime.stop()


async def test_execute_builtin_name_sets_and_reads_session_name(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """``name`` writes through to pi, refreshes the meta, and reads back."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session("sess-name", "你好", cwd=str(tmp_path))
        live = runtime._live["sess-name"]
        assert live.session_name is None

        metas_before = len(fake_host.metas)
        result = await runtime.execute_command("sess-name", "name", args=("my-session",))
        assert result.ok is True
        assert live.session_name == "my-session"
        assert len(fake_host.metas) > metas_before
        state = await runtime.get_session_state("sess-name")
        assert state is not None
        assert state.metadata["sessionName"] == "my-session"

        result = await runtime.execute_command("sess-name", "name")
        assert result.ok is True
        assert "my-session" in (result.message or "")

        await runtime.create_and_start_session("sess-noname", "你好", cwd=str(tmp_path))
        result = await runtime.execute_command("sess-noname", "name")
        assert result.ok is True
        assert "未设置" in (result.message or "")
    finally:
        await runtime.stop()


async def test_execute_builtin_stats_reports_and_caches_context_usage(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """``stats`` returns a summary and caches it for the session state."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        result = await runtime.execute_command("sess-stats", "stats")
        assert result.ok is True
        assert "30%" in (result.message or "")
        assert "21500" in (result.message or "")

        state = await runtime.get_session_state("sess-stats")
        assert state is not None
        assert state.metadata["contextUsage"] == {
            "tokens": 38400,
            "contextWindow": 128000,
            "percent": 30.0,
        }
        assert state.metadata["tokens"]["total"] == 21500
        assert state.metadata["cost"] == 0.0123
    finally:
        await runtime.stop()


async def test_execute_builtin_export_returns_html_path(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """``export`` returns the path pi wrote, honouring an explicit output path."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        result = await runtime.execute_command("sess-export", "export")
        assert result.ok is True
        assert result.result["path"].endswith("session-export.html")
        assert "session-export.html" in (result.message or "")

        target = str(tmp_path / "exports" / "custom.html")
        result = await runtime.execute_command("sess-export", "export", args=(target,))
        assert result.ok is True
        assert result.result["path"] == target
        assert target in (result.message or "")
    finally:
        await runtime.stop()


async def test_non_builtin_command_still_sends_slash_prompt(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names outside the builtin set keep the ``/command`` prompt behaviour."""

    commands_file = tmp_path / "pi-commands.jsonl"
    monkeypatch.setenv("PI_FAKE_COMMANDS_FILE", str(commands_file))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session("sess-slash", "你好", cwd=str(tmp_path))
        result = await runtime.execute_command("sess-slash", "fix-tests", args=("now",))
        assert result.ok is True
        assert result.result["executionState"] == "accepted"
        assert result.message == "已发送 /fix-tests now"
        commands = [
            json.loads(line) for line in commands_file.read_text(encoding="utf-8").splitlines()
        ]
        prompts = [command for command in commands if command["type"] == "prompt"]
        assert any(command["message"] == "/fix-tests now" for command in prompts)
    finally:
        await runtime.stop()


async def test_context_usage_warning_opens_on_settle_and_resolves(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nearly full context warns once and resolves when usage drops."""

    monkeypatch.setenv("PI_FAKE_CONTEXT_PERCENT", "95")
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.start_turn("sess-ctx", None, "hello")
        notice_id = "pi-context-sess-ctx"
        await wait_for(lambda: any(notice.notice_id == notice_id for notice in fake_host.notices))
        opened = [notice for notice in fake_host.notices if notice.notice_id == notice_id]
        assert len(opened) == 1
        assert opened[0].status == "open"
        assert opened[0].severity == "warning"
        assert opened[0].type == "notification"
        assert "95" in (opened[0].message or "")
        live = runtime._live["sess-ctx"]
        assert live.context_warning_open is True
        state = await runtime.get_session_state("sess-ctx")
        assert state is not None
        assert state.metadata["contextUsage"]["percent"] == 95.0

        # A second settle at the same percent must not stack another notice.
        await runtime.start_turn("sess-ctx", None, "again")
        await wait_for(lambda: len(fake_host.turn_ends) >= 2)
        opened = [notice for notice in fake_host.notices if notice.notice_id == notice_id]
        assert len(opened) == 1

        # pi may report a healthy percent after a compact: the same notice id
        # must resolve instead of leaving a stale warning open.
        await runtime._sync_context_warning(
            live,
            {"contextUsage": {"tokens": 38400, "contextWindow": 128000, "percent": 30}},
        )
        opened = [notice for notice in fake_host.notices if notice.notice_id == notice_id]
        assert [notice.status for notice in opened] == ["open", "resolved"]
        assert live.context_warning_open is False

        # Missing usage resolves as well, without crashing on the absent percent.
        live.context_warning_open = True
        await runtime._sync_context_warning(live, {})
        assert fake_host.notices[-1].status == "resolved"
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


def test_reannounce_seed_never_throttles_the_first_call(tmp_path: Path) -> None:
    """The first re-announce runs even on a host booted seconds ago.

    ``_last_reannounce_at`` is compared against monotonic time, whose zero
    is the host boot; seeding it with 0.0 skipped the startup re-announce
    while the machine uptime was shorter than the throttle window.
    """

    runtime = PiRuntime(
        config=RuntimeConfig(
            runtime="pi",
            revision=1,
            values={
                "executablePath": "pi",
                "sessionsDir": str(tmp_path / "sessions"),
                "defaultCwd": str(tmp_path),
            },
        ),
        host=FakeHost(),
    )
    assert runtime._last_reannounce_at <= -REANNOUNCE_MIN_INTERVAL_SECONDS


def test_model_display_title_rules() -> None:
    from pi_aa.runtime import _model_display_title

    directory = [
        ("DeepSeek V4.1 Flash", "deepseek", "deepseek-flash"),
        ("DeepSeek V4.1 Flash", "opencode-go", "deepseek-v4.1-flash"),
        ("DeepSeek V4 Pro", "deepseek", "deepseek-v4-pro"),
        ("Routed", "test", "a"),
        ("Routed", "test", "b"),
    ]
    # Several providers: titles stay plain; providers act as group headings.
    assert (
        _model_display_title("DeepSeek V4.1 Flash", "deepseek", "deepseek-flash", directory)
        == "DeepSeek V4.1 Flash"
    )
    assert (
        _model_display_title("DeepSeek V4 Pro", "deepseek", "deepseek-v4-pro", directory)
        == "DeepSeek V4 Pro"
    )
    # Same provider, several ids under one name: append the model id.
    assert _model_display_title("Routed", "test", "a", directory) == "Routed [a]"
    assert _model_display_title("Routed", "test", "b", directory) == "Routed [b]"
    # A single-provider catalog stays untouched.
    single = [("Solo", "test", "s"), ("Other", "test", "o")]
    assert _model_display_title("Solo", "test", "s", single) == "Solo"


def test_catalog_revision_is_monotonic_across_restarts(tmp_path: Path) -> None:
    """The platform drops catalogs whose revision does not exceed the stored
    one, so a restarted connector must still produce larger revisions."""

    def make() -> PiRuntime:
        return PiRuntime(
            config=RuntimeConfig(
                runtime="pi",
                revision=1,
                values={
                    "executablePath": "pi",
                    "sessionsDir": str(tmp_path / "sessions"),
                    "defaultCwd": str(tmp_path),
                    "requestTimeoutMs": 5000,
                },
            ),
            host=FakeHost(),
        )

    first = make()
    revisions = [first._next_catalog_revision() for _ in range(3)]
    assert revisions == sorted(revisions)
    assert len(set(revisions)) == 3
    # A real connector restart takes at least milliseconds; the clock-based
    # revision then exceeds the previous process even though it restarted from
    # an empty in-memory counter.
    time.sleep(0.005)
    restarted = make()
    assert restarted._next_catalog_revision() > revisions[-1]


async def test_start_turn_forwards_image_attachments(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        attachment = RuntimeAttachment(
            file_id="file-1",
            name="shot.png",
            media_type="image/png",
            size=len(FAKE_PNG_BYTES),
            sha256="0" * 64,
        )
        result = await runtime.start_turn(
            "sess-img",
            None,
            "看看这张图",
            attachments=(attachment,),
        )
        assert result.ok is True
        await wait_for(
            lambda: (
                session_file.is_file() and "看看这张图" in session_file.read_text(encoding="utf-8")
            )
        )
    finally:
        await runtime.stop()

    assert fake_host.attachment_downloads == [{"session_id": "sess-img", "file_id": "file-1"}]
    records = [
        json.loads(line)
        for line in session_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    user_message = next(
        record["message"]
        for record in records
        if record.get("type") == "message" and (record.get("message") or {}).get("role") == "user"
    )
    blocks = user_message["content"]
    assert isinstance(blocks, list)
    image = next(block for block in blocks if block.get("type") == "image")
    assert image["mimeType"] == "image/png"
    assert image["data"] == base64.b64encode(FAKE_PNG_BYTES).decode("ascii")


async def test_start_turn_saves_non_image_attachments(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-image attachments reach pi as a path note instead of being skipped."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        image_attachment = RuntimeAttachment(
            file_id="file-1",
            name="shot.png",
            media_type="image/png",
            size=len(FAKE_PNG_BYTES),
            sha256="0" * 64,
        )
        text_attachment = RuntimeAttachment(
            file_id="text-1",
            name="notes.txt",
            media_type="text/plain",
            size=len(FAKE_TEXT_BYTES),
            sha256="0" * 64,
        )
        result = await runtime.start_turn(
            "sess-mixed",
            None,
            "看看这些附件",
            attachments=(image_attachment, text_attachment),
        )
        assert result.ok is True
        await wait_for(
            lambda: (
                session_file.is_file()
                and "看看这些附件" in session_file.read_text(encoding="utf-8")
            )
        )
    finally:
        await runtime.stop()

    saved = tmp_path / "attachments" / "sess-mixed" / "text-1-notes.txt"
    assert saved.read_bytes() == FAKE_TEXT_BYTES
    # Images stay out of the attachments directory.
    assert sorted(path.name for path in saved.parent.iterdir()) == ["text-1-notes.txt"]
    assert fake_host.attachment_downloads == [
        {"session_id": "sess-mixed", "file_id": "file-1"},
        {"session_id": "sess-mixed", "file_id": "text-1"},
    ]

    contents = session_user_contents(session_file)
    assert len(contents) == 1
    blocks = contents[0]
    assert isinstance(blocks, list)
    text = next(block for block in blocks if block.get("type") == "text")
    assert text["text"] == (
        "看看这些附件\n\n"
        "用户上传了以下文件附件，已保存到本机，可用工具读取：\n"
        f"- notes.txt: {saved}"
    )
    image = next(block for block in blocks if block.get("type") == "image")
    assert image["mimeType"] == "image/png"
    assert image["data"] == base64.b64encode(FAKE_PNG_BYTES).decode("ascii")


async def test_create_session_appends_attachment_notes_without_images(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-image-only send stays plain text and carries the saved path."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        attachment = RuntimeAttachment(
            file_id="text-1",
            name="notes.txt",
            media_type="text/plain",
            size=len(FAKE_TEXT_BYTES),
        )
        result = await runtime.create_and_start_session(
            "sess-create",
            "读一下附件",
            cwd=str(tmp_path),
            attachments=(attachment,),
        )
        assert result.ok is True
        await wait_for(
            lambda: (
                session_file.is_file() and "读一下附件" in session_file.read_text(encoding="utf-8")
            )
        )
    finally:
        await runtime.stop()

    saved = tmp_path / "attachments" / "sess-create" / "text-1-notes.txt"
    assert saved.read_bytes() == FAKE_TEXT_BYTES
    contents = session_user_contents(session_file)
    assert len(contents) == 1
    # No images means pi persists the prompt as a plain string.
    assert contents[0] == (
        f"读一下附件\n\n用户上传了以下文件附件，已保存到本机，可用工具读取：\n- notes.txt: {saved}"
    )


async def test_attachment_download_failure_keeps_the_turn(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failing download must not drop the turn or the other attachments."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        broken = RuntimeAttachment(
            file_id="fail-1",
            name="broken.bin",
            media_type="application/octet-stream",
        )
        good = RuntimeAttachment(
            file_id="text-1",
            name="notes.txt",
            media_type="text/plain",
        )
        result = await runtime.start_turn("sess-fail", None, "继续", attachments=(broken, good))
        assert result.ok is True
        await wait_for(
            lambda: session_file.is_file() and "继续" in session_file.read_text(encoding="utf-8")
        )
    finally:
        await runtime.stop()

    saved = tmp_path / "attachments" / "sess-fail" / "text-1-notes.txt"
    assert saved.read_bytes() == FAKE_TEXT_BYTES
    assert session_user_contents(session_file) == [
        (f"继续\n\n用户上传了以下文件附件，已保存到本机，可用工具读取：\n- notes.txt: {saved}")
    ]
    assert sorted(path.name for path in saved.parent.iterdir()) == ["text-1-notes.txt"]


async def test_oversized_attachment_is_skipped(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files over the size cap stay out of the prompt and off the disk."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    monkeypatch.setattr("pi_aa.runtime.MAX_ATTACHMENT_BYTES", 4)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        attachment = RuntimeAttachment(
            file_id="text-1",
            name="notes.txt",
            media_type="text/plain",
        )
        result = await runtime.start_turn("sess-big", None, "大文件", attachments=(attachment,))
        assert result.ok is True
        await wait_for(
            lambda: session_file.is_file() and "大文件" in session_file.read_text(encoding="utf-8")
        )
    finally:
        await runtime.stop()

    assert session_user_contents(session_file) == ["大文件"]
    assert not (tmp_path / "attachments" / "sess-big").exists()
    # The runtime learns the real size only after downloading.
    assert fake_host.attachment_downloads == [{"session_id": "sess-big", "file_id": "text-1"}]


async def test_steer_turn_saves_non_image_attachments(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """steer_turn appends the same saved-file note as start_turn."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        attachment = RuntimeAttachment(
            file_id="text-1",
            name="notes.txt",
            media_type="text/plain",
        )
        result = await runtime.steer_turn(
            "sess-steer",
            None,
            "先看这个",
            attachments=(attachment,),
        )
        assert result.ok is True
        assert result.result == {"sessionId": "sess-steer", "steered": False}
        await wait_for(
            lambda: (
                session_file.is_file() and "先看这个" in session_file.read_text(encoding="utf-8")
            )
        )
    finally:
        await runtime.stop()

    saved = tmp_path / "attachments" / "sess-steer" / "text-1-notes.txt"
    assert saved.read_bytes() == FAKE_TEXT_BYTES
    assert session_user_contents(session_file) == [
        (f"先看这个\n\n用户上传了以下文件附件，已保存到本机，可用工具读取：\n- notes.txt: {saved}")
    ]


async def test_start_resets_stale_running_states(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """A restart re-announces idle for sessions the platform may see as running."""

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session = sessions_dir / "restart.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": "sess-restart",
                "cwd": str(tmp_path),
                "timestamp": "t",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "u1",
                "parentId": None,
                "timestamp": "t",
                "message": {"role": "user", "content": "你好", "timestamp": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await wait_for(lambda: any(state.get("status") == "idle" for state in fake_host.states))
        state = next(item for item in fake_host.states if item.get("status") == "idle")
        assert state["runtime"] == "pi"
        assert state["session_id"].startswith("sess_pi_")
    finally:
        await runtime.stop()


def write_pi_session_file(path: Path, *, session_id: str, cwd: str) -> None:
    """A minimal pi session file: header line plus one user message."""

    path.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "cwd": cwd,
                "timestamp": "t",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "u1",
                "parentId": None,
                "timestamp": "t",
                "message": {"role": "user", "content": "你好", "timestamp": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )


async def test_reannounce_session_states_after_reconnect(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend state caches converge after a reconnect without killing live sessions."""

    import pi_aa.runtime as runtime_module

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    imported = sessions_dir / "imported.jsonl"
    write_pi_session_file(imported, session_id="sess-imported", cwd=str(tmp_path))

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        imported_session_id = platform_session_id(fake_host.session_namespace, str(imported))
        await wait_for(
            lambda: any(
                state.get("session_id") == imported_session_id for state in fake_host.states
            )
        )
        # A platform-created session keeps its platform id as the live key,
        # while the inventory knows the same session under a path-derived id.
        await runtime.create_and_start_session("sess-live", "你好", cwd=str(tmp_path))
        await wait_for(lambda: "sess-live" in runtime._live)
        await wait_for(lambda: runtime._live["sess-live"].status == "idle")
        live = runtime._live["sess-live"]

        monkeypatch.setattr(runtime_module, "REANNOUNCE_MIN_INTERVAL_SECONDS", 0.0)
        fake_host.states.clear()
        await runtime.reannounce_session_states(reason="reconnect")

        announced = {state["session_id"]: state for state in fake_host.states}
        assert set(announced) == {imported_session_id, "sess-live"}
        assert announced[imported_session_id]["status"] == "idle"
        assert announced["sess-live"]["status"] == live.status
        assert announced["sess-live"]["metadata"]["sessionFile"] == live.session_file
    finally:
        await runtime.stop()


async def test_reannounce_session_states_skips_repeats_within_interval(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """A reconnect storm must not trigger repeated full inventory scans."""

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    write_pi_session_file(
        sessions_dir / "imported.jsonl", session_id="sess-imported", cwd=str(tmp_path)
    )

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await wait_for(lambda: any(state.get("status") == "idle" for state in fake_host.states))
        fake_host.states.clear()
        await runtime.reannounce_session_states(reason="reconnect")
        assert fake_host.states == []
    finally:
        await runtime.stop()


async def test_start_turn_attaches_client_message_id(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """The projected user item carries the id the platform dedupes with."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.start_turn("sess-cm", None, "你好", client_message_id="cm-42")
        await wait_for(
            lambda: any(
                item.source.get("clientMessageId") == "cm-42"
                for sync in fake_host.timeline_syncs
                for item in sync["items"]
            )
        )
    finally:
        await runtime.stop()


async def test_start_turn_client_message_id_covers_attachment_note(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optimistic-send id must match the prompt pi actually persisted.

    pi saves the prompt with the attachment note appended, so binding only
    the raw content leaves the platform unable to reconcile the echo and the
    message renders twice (optimistic copy plus transcript copy).
    """

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        attachment = RuntimeAttachment(
            file_id="text-1",
            name="notes.txt",
            media_type="text/plain",
        )
        result = await runtime.start_turn(
            "sess-cm-attach",
            None,
            "看看附件",
            attachments=(attachment,),
            client_message_id="cm-attach",
        )
        assert result.ok is True
        await wait_for(lambda: len(fake_host.turn_ends) >= 1)
        snapshot = await runtime.get_session_snapshot(
            "sess-cm-attach",
            external_session_id=str(session_file),
        )
    finally:
        await runtime.stop()

    user_items = [
        item
        for item in snapshot.items
        if item.type == "message" and getattr(item, "role", None) == "user"
    ]
    assert user_items, "expected the projected user message"
    assert user_items[-1].source["clientMessageId"] == "cm-attach"


async def test_create_session_client_message_id_covers_attachment_note(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_and_start_session binds the same prompt text it sends to pi."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        attachment = RuntimeAttachment(
            file_id="text-1",
            name="notes.txt",
            media_type="text/plain",
        )
        result = await runtime.create_and_start_session(
            "sess-cm-create",
            "读一下附件",
            cwd=str(tmp_path),
            attachments=(attachment,),
            client_message_id="cm-create",
        )
        assert result.ok is True
        await wait_for(lambda: len(fake_host.turn_ends) >= 1)
        snapshot = await runtime.get_session_snapshot(
            "sess-cm-create",
            external_session_id=str(session_file),
        )
    finally:
        await runtime.stop()

    user_items = [
        item
        for item in snapshot.items
        if item.type == "message" and getattr(item, "role", None) == "user"
    ]
    assert user_items, "expected the projected user message"
    assert user_items[-1].source["clientMessageId"] == "cm-create"


async def test_reclaims_idle_sessions(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """Idle pi processes are closed; the session file remains for revival."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session("sess-idle", "你好", cwd=str(tmp_path))
        live = runtime._live["sess-idle"]
        assert live.alive
        # The turn settles asynchronously; wait for the settled state so the
        # idle expiry below cannot race the still-running turn.
        await wait_for(lambda: len(fake_host.turn_ends) >= 1)
        runtime._idle_timeout = 0.05
        live.last_activity -= 1.0
        await runtime._reclaim_idle_sessions()
        assert "sess-idle" not in runtime._live
        await wait_for(lambda: not live.alive)
    finally:
        await runtime.stop()


async def test_keeps_active_sessions_during_reclaim(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """A streaming session must survive an idle reclaim pass."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session("sess-busy", "你好", cwd=str(tmp_path))
        live = runtime._live["sess-busy"]
        runtime._idle_timeout = 0.05
        live.is_streaming = True
        live.last_activity -= 1.0
        await runtime._reclaim_idle_sessions()
        assert runtime._live.get("sess-busy") is live
        assert live.alive
    finally:
        runtime._live["sess-busy"].is_streaming = False
        await runtime.stop()


async def test_create_session_skips_placeholder_title(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """AA's default title must not be written into the pi session file."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session(
            "sess-placeholder",
            "你好",
            title="新建会话",
            cwd=str(tmp_path),
        )
        live = runtime._live["sess-placeholder"]
        assert live.session_name is None
    finally:
        await runtime.stop()


async def test_runtime_start_publishes_runtime_capabilities(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """start() pushes runtime-scoped facts so the platform persists them."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        published = [item for item in fake_host.capability_sets if item.session_id is None]
    finally:
        await runtime.stop()

    assert published, "start() should publish runtime-scoped capability facts"
    by_id = {capability.capability_id: capability for capability in published[-1].capabilities}
    assert by_id[CAPABILITY_RUNTIME_ATTACHMENT].scope == "runtime"
    assert by_id[CAPABILITY_SESSION_SEND_MESSAGE].scope == "runtime"


async def test_runtime_capabilities_advertise_attachments(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """The device-runtime endpoint (new-session composer) reads this set."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        capability_set = await runtime.get_runtime_capabilities()
    finally:
        await runtime.stop()

    by_id = {capability.capability_id: capability for capability in capability_set.capabilities}
    attachment = by_id[CAPABILITY_RUNTIME_ATTACHMENT]
    assert attachment.scope == "runtime"
    assert attachment.supported is True
    assert attachment.available is True
    assert attachment.allowed is True
    assert attachment.metadata["allowedMimeTypes"]
    # Session actions are advertised at runtime scope too so the platform's
    # per-session projection has a fallback for sessions without own facts.
    assert by_id[CAPABILITY_SESSION_SEND_MESSAGE].scope == "runtime"


async def test_client_message_bindings_survive_connector_restart(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
) -> None:
    """A restart must not orphan an in-flight optimistic send.

    The platform reconciles optimistic sends by client message id from the
    transcript projection; losing the id across a connector restart leaves
    the echo showing next to the local copy (duplicate messages).
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.create_and_start_session(
            "sess-live",
            "你好",
            cwd=str(tmp_path),
            client_message_id="opt_test_restart",
        )
        await wait_for(lambda: session_file.is_file())
    finally:
        await runtime.stop()

    restarted = make_runtime(fake_pi, tmp_path, fake_host)
    await restarted.start()
    try:
        snapshot = await restarted.get_session_snapshot(
            "sess-live",
            external_session_id=str(session_file),
        )
        user_items = [
            item
            for item in snapshot.items
            if item.type == "message" and getattr(item, "role", None) == "user"
        ]
        assert user_items, "expected the projected user message"
        assert user_items[-1].source.get("clientMessageId") == "opt_test_restart"
    finally:
        await restarted.stop()


async def test_queue_update_event_pushes_pending_queue_metadata(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """Queue changes must reach the platform even when status stays put."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "sess-queue-meta", cwd=str(tmp_path))
    runtime._live["sess-queue-meta"] = live

    # Non-string queue entries are dropped; the queues hold complete snapshots.
    await runtime.handle_live_event(
        live,
        {"type": "queue_update", "steering": ["steer-a", 42], "followUp": ["f1", "f2"]},
    )
    state = fake_host.states[-1]
    assert state["metadata"]["pendingSteering"] == ["steer-a"]
    assert state["metadata"]["pendingFollowUp"] == ["f1", "f2"]
    assert state["metadata"]["pendingMessageCount"] == 3
    assert live.pending_message_count == 3

    # The queue participates in the state key: clearing it is a new state even
    # though the status is still idle.
    await runtime.handle_live_event(live, {"type": "queue_update", "steering": [], "followUp": []})
    assert len(fake_host.states) == 2
    assert fake_host.states[-1]["metadata"]["pendingMessageCount"] == 0
    assert live.steering_queue == []
    assert live.followup_queue == []


async def test_followup_message_is_queued_while_busy(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second message during streaming queues and is still answered.

    ``start_turn`` switches to ``streamingBehavior: followUp`` while the
    session is busy; the fake mirrors pi in reporting the queued text via
    ``queue_update`` and running it once the busy turn settles.
    """

    monkeypatch.setenv("PI_FAKE_BUSY_FIRST_PROMPT", "1")
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.start_turn("sess-queue-followup", None, "first question")
        live = runtime._live["sess-queue-followup"]
        await wait_for(lambda: live.is_streaming)

        second = await runtime.start_turn("sess-queue-followup", None, "second question")
        assert second.result["queued"] is True
        await wait_for(
            lambda: any(
                state.get("metadata", {}).get("pendingFollowUp") == ["second question"]
                for state in fake_host.states
            )
        )

        # pi eventually answers the queued text and the session settles idle.
        await wait_for(lambda: len(fake_host.turn_ends) >= 2)
        await wait_for(lambda: live.status == "idle")
        replies = [
            item.content["text"]
            for sync in fake_host.timeline_syncs
            for item in sync["items"]
            if item.type == "message" and item.role == "assistant"
        ]
        assert "reply to second question" in replies
    finally:
        await runtime.stop()


async def test_interrupt_clears_queue_before_abort(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupt mirrors the TUI's Esc: clear the queue, then abort.

    A plain abort would keep running whatever is still queued; the cleared
    texts ride back in the state metadata so the client can restore them.
    """

    monkeypatch.setenv("PI_FAKE_BUSY_FIRST_PROMPT", "1")
    monkeypatch.setenv(
        "PI_FAKE_CLEAR_QUEUE_DATA",
        json.dumps({"steering": [], "followUp": ["queued idea"]}),
    )
    commands_file = tmp_path / "pi-commands.jsonl"
    monkeypatch.setenv("PI_FAKE_COMMANDS_FILE", str(commands_file))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.start_turn("sess-queue-interrupt", None, "first question")
        live = runtime._live["sess-queue-interrupt"]
        await wait_for(lambda: live.is_streaming)

        result = await runtime.interrupt_session("sess-queue-interrupt")
        assert result.ok is True
        assert result.result["clearedQueue"] == {
            "steering": [],
            "followUp": ["queued idea"],
        }
        await wait_for(lambda: commands_file.is_file())
        commands = [
            json.loads(line)["type"]
            for line in commands_file.read_text(encoding="utf-8").splitlines()
        ]
        sequence = [entry for entry in commands if entry in ("clear_queue", "abort")]
        assert sequence == ["clear_queue", "abort"]
        await wait_for(
            lambda: any(
                state.get("metadata", {}).get("clearedQueue")
                == {"steering": [], "followUp": ["queued idea"]}
                for state in fake_host.states
            )
        )
    finally:
        await runtime.stop()


def _upserts_by_id(host: FakeHost, item_type: str) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for item in host.timeline_items:
        if item.type == item_type:
            grouped.setdefault(item.id, []).append(item)
    return grouped


async def test_stream_pushes_running_then_done_assistant_text(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deltas must surface a running message that settles on the full reply."""

    monkeypatch.setenv("PI_FAKE_STREAM", "1")
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.start_turn("sess-stream-text", None, "hello")
        await wait_for(
            lambda: any(
                item.type == "message" and item.status == "done"
                for item in fake_host.timeline_items
            )
        )
    finally:
        await runtime.stop()

    streamed = [
        entries
        for entries in _upserts_by_id(fake_host, "message").values()
        if entries[-1].content.get("text") == "reply to hello"
    ]
    assert streamed, "expected streamed assistant message upserts"
    entries = streamed[0]
    assert entries[0].status == "running"
    assert entries[-1].status == "done"
    assert all(item.metadata.get("stream") is True for item in entries)

    reasoning = [
        item
        for item in fake_host.timeline_items
        if item.type == "system" and item.content.get("kind") == "reasoning"
    ]
    assert reasoning, "expected a streamed reasoning item"
    assert reasoning[-1].content["text"] == "check the question"


async def test_stream_deltas_are_coalesced_into_few_upserts(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst of deltas must not turn into an upsert per delta."""

    monkeypatch.setenv("PI_FAKE_STREAM", "1")
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.start_turn("sess-stream-throttle", None, "hello")
        await wait_for(
            lambda: any(
                item.type == "message" and item.status == "done"
                for item in fake_host.timeline_items
            )
        )
    finally:
        await runtime.stop()

    # The fake sends 3 text deltas and 2 thinking deltas = 5 streaming events;
    # the text item must cost far fewer upserts than that.
    counts: dict[str, int] = {}
    for item in fake_host.timeline_items:
        if item.type == "message" and item.metadata.get("stream") is True:
            counts[item.id] = counts.get(item.id, 0) + 1
    assert counts, "expected streamed upserts"
    assert max(counts.values()) <= 4
    assert sum(counts.values()) < 5


async def test_tool_execution_upserts_running_and_terminal_cards(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool cards stream partial output and settle with exit code and diffs."""

    monkeypatch.setenv("PI_FAKE_STREAM", "1")
    monkeypatch.setenv("PI_FAKE_TOOLS", "1")
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.start_turn("sess-stream-tools", None, "run tools")
        await wait_for(
            lambda: any(
                item.type == "tool"
                and item.content.get("callId") == "call-edit"
                and item.status == "done"
                for item in fake_host.timeline_items
            )
        )
    finally:
        await runtime.stop()

    def tool_items(call_id: str) -> list:
        return [
            item
            for item in fake_host.timeline_items
            if item.type == "tool" and item.content.get("callId") == call_id
        ]

    bash = tool_items("call-bash")
    assert bash, "expected bash tool upserts"
    assert bash[0].status == "running"
    assert bash[-1].status == "done"
    assert any(item.content.get("output") in ("partial output", "final output") for item in bash)
    assert bash[-1].content["output"] == "final output"
    assert bash[-1].content["exitCode"] == 0

    edit = tool_items("call-edit")
    assert edit, "expected edit tool upserts"
    assert edit[0].status == "running"
    assert edit[-1].status == "done"
    assert edit[-1].content["changes"][0]["diff"] == "-old\n+new"
    assert edit[-1].content["changes"][0]["path"] == "/tmp/example.txt"


async def test_tool_execution_update_pushes_partial_output(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """A partial result must reach the platform through the throttled flush."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "sess-tool-partial", cwd=str(tmp_path))
    runtime._live["sess-tool-partial"] = live
    try:
        await runtime.handle_live_event(
            live,
            {
                "type": "tool_execution_start",
                "toolCallId": "call-partial",
                "toolName": "bash",
                "args": {"command": "long-running"},
            },
        )
        await runtime.handle_live_event(
            live,
            {
                "type": "tool_execution_update",
                "toolCallId": "call-partial",
                "toolName": "bash",
                "args": {"command": "long-running"},
                "partialResult": {
                    "content": [{"type": "text", "text": "partial output"}],
                    "details": {},
                },
            },
        )
        await wait_for(
            lambda: any(
                item.type == "tool"
                and item.content.get("callId") == "call-partial"
                and item.content.get("output") == "partial output"
                for item in fake_host.timeline_items
            )
        )
        await runtime.handle_live_event(
            live,
            {
                "type": "tool_execution_end",
                "toolCallId": "call-partial",
                "toolName": "bash",
                "result": {
                    "content": [{"type": "text", "text": "final output"}],
                    "details": {"exitCode": 0},
                },
                "isError": False,
            },
        )
        terminal = [
            item
            for item in fake_host.timeline_items
            if item.type == "tool" and item.content.get("callId") == "call-partial"
        ][-1]
        assert terminal.status == "done"
        assert terminal.content["output"] == "final output"
        assert terminal.content["exitCode"] == 0
    finally:
        await runtime.stop()


async def test_streaming_keeps_full_timeline_sync_on_settle(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streamed temp items must still be replaced by the complete projection."""

    monkeypatch.setenv("PI_FAKE_STREAM", "1")
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.start_turn("sess-stream-settle", None, "hello")
        await wait_for(lambda: len(fake_host.timeline_syncs) >= 1)
        await wait_for(lambda: runtime._live["sess-stream-settle"].status == "idle")
    finally:
        await runtime.stop()

    assert fake_host.timeline_syncs[-1]["complete"] is True
    assert fake_host.timeline_syncs[-1]["items"]


async def test_stop_cancels_pending_stream_flush(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """Stopping the runtime must not leak the per-session flush task."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "sess-stream-stop", cwd=str(tmp_path))
    runtime._live["sess-stream-stop"] = live
    live.begin_stream_message()
    live.apply_stream_delta({"type": "text_delta", "contentIndex": 0, "delta": "hello"})
    assert live.stream_task is not None

    await runtime.stop()

    assert live.stream_task is None or live.stream_task.done()


async def test_interrupt_aborts_when_clear_queue_fails(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``clear_queue`` must never block the abort."""

    monkeypatch.setenv("PI_FAKE_BUSY_FIRST_PROMPT", "1")
    monkeypatch.setenv("PI_FAKE_CLEAR_QUEUE_FAIL", "1")
    commands_file = tmp_path / "pi-commands.jsonl"
    monkeypatch.setenv("PI_FAKE_COMMANDS_FILE", str(commands_file))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        await runtime.start_turn("sess-queue-clear-fail", None, "first question")
        live = runtime._live["sess-queue-clear-fail"]
        await wait_for(lambda: live.is_streaming)

        result = await runtime.interrupt_session("sess-queue-clear-fail")
        assert result.ok is True
        assert "clearedQueue" not in result.result
        await wait_for(lambda: commands_file.is_file())
        commands = [
            json.loads(line)["type"]
            for line in commands_file.read_text(encoding="utf-8").splitlines()
        ]
        assert commands[-2:] == ["clear_queue", "abort"]
        await wait_for(lambda: not live.is_streaming)
        await wait_for(lambda: live.status == "idle")
    finally:
        await runtime.stop()


async def test_cleanup_attachments_deletes_old_files_keeps_new(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files older than the retention window are removed, newer ones kept."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    directory = attachments_root() / "sess-stale"
    directory.mkdir(parents=True)
    old_file = directory / "text-1-notes.txt"
    old_file.write_bytes(FAKE_TEXT_BYTES)
    old = time.time() - 31 * 86400
    os.utime(old_file, (old, old))
    new_file = directory / "text-2-notes.txt"
    new_file.write_bytes(FAKE_TEXT_BYTES)

    deleted = await runtime.cleanup_attachments()

    assert deleted == 1
    assert not old_file.exists()
    assert new_file.exists()


async def test_cleanup_attachments_skips_live_session_directories(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directories of live sessions must survive a cleanup pass."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "sess-live-att", cwd=str(tmp_path))
    runtime._live["sess-live-att"] = live
    old = time.time() - 31 * 86400

    live_dir = session_attachments_dir("sess-live-att")
    live_dir.mkdir(parents=True)
    live_file = live_dir / "text-1-notes.txt"
    live_file.write_bytes(FAKE_TEXT_BYTES)
    os.utime(live_file, (old, old))

    stale_dir = attachments_root() / "sess-stale"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "text-1-notes.txt"
    stale_file.write_bytes(FAKE_TEXT_BYTES)
    os.utime(stale_file, (old, old))

    deleted = await runtime.cleanup_attachments()

    assert deleted == 1
    assert live_file.exists()
    assert not stale_file.exists()


async def test_cleanup_attachments_disabled_when_retention_zero(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attachmentRetentionDays=0 disables cleanup entirely."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    config = RuntimeConfig(
        runtime="pi",
        revision=1,
        values={
            "executablePath": str(fake_pi),
            "sessionsDir": str(tmp_path / "sessions"),
            "defaultCwd": str(tmp_path),
            "requestTimeoutMs": 5000,
            "attachmentRetentionDays": 0,
        },
    )
    runtime = PiRuntime(config=config, host=fake_host)
    directory = attachments_root() / "sess-kept"
    directory.mkdir(parents=True)
    old_file = directory / "text-1-notes.txt"
    old_file.write_bytes(FAKE_TEXT_BYTES)
    old = time.time() - 60 * 86400
    os.utime(old_file, (old, old))

    deleted = await runtime.cleanup_attachments()

    assert deleted == 0
    assert old_file.exists()


async def test_cleanup_attachments_removes_empty_directories(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directories left empty after expiry are removed; other content kept."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    root = attachments_root()
    (root / "sess-empty").mkdir(parents=True)
    stale_dir = root / "sess-stale"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "text-1-notes.txt"
    stale_file.write_bytes(FAKE_TEXT_BYTES)
    old = time.time() - 31 * 86400
    os.utime(stale_file, (old, old))
    # Nested subdirectories are never recursed into.
    nested = root / "sess-nested" / "sub"
    nested.mkdir(parents=True)
    nested_file = nested / "text-1-notes.txt"
    nested_file.write_bytes(FAKE_TEXT_BYTES)
    os.utime(nested_file, (old, old))

    deleted = await runtime.cleanup_attachments()

    assert deleted == 1
    assert not (root / "sess-empty").exists()
    assert not stale_dir.exists()
    assert nested_file.exists()


async def test_attachment_cleanup_task_runs_immediately_and_stops(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start() schedules an immediate cleanup pass; stop() cancels the task."""

    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    directory = attachments_root() / "sess-start"
    directory.mkdir(parents=True)
    old_file = directory / "text-1-notes.txt"
    old_file.write_bytes(FAKE_TEXT_BYTES)
    old = time.time() - 31 * 86400
    os.utime(old_file, (old, old))

    await runtime.start()
    cleanup_task = runtime._cleanup_task
    assert cleanup_task is not None and not cleanup_task.done()

    # The first pass runs immediately without waiting for the interval.
    await asyncio.sleep(0)
    assert not old_file.exists()

    await runtime.stop()
    assert runtime._cleanup_task is None
    assert cleanup_task.done()
