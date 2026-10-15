"""Transport-level ordering: host delays must not reorder pi lifecycle events."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pi_aa.rpc import PiRpcProcess
from pi_aa.runtime import PiLiveSession
from tests.conftest import FakeHost, wait_for
from tests.test_runtime import make_runtime


def dispatch(process: PiRpcProcess, **record) -> None:
    process._dispatch_line(json.dumps(record).encode())


async def test_slow_settle_finishes_before_next_turn(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "ordered-turns", cwd=str(tmp_path))
    runtime._live[live.platform_id] = live
    process = PiRpcProcess([str(fake_pi), "--mode", "rpc"], on_event=live._on_event)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = fake_host.notice_upsert

    async def slow_resolve(notice):
        if notice.status == "resolved":
            entered.set()
            await release.wait()
        await original(notice)

    await runtime.handle_live_event(
        live, {"type": "extension_ui_request", "method": "notify", "message": "first turn"}
    )
    monkeypatch.setattr(fake_host, "notice_upsert", slow_resolve)
    try:
        dispatch(process, type="agent_settled")
        await asyncio.wait_for(entered.wait(), timeout=2)
        dispatch(process, type="agent_start")
        dispatch(process, type="message_start", message={"role": "assistant"})
        dispatch(
            process,
            type="message_update",
            assistantMessageEvent={"type": "text_delta", "contentIndex": 0, "delta": "next"},
        )
        release.set()
        await wait_for(lambda: bool(fake_host.turn_ends))
        await asyncio.sleep(0)
        await wait_for(lambda: bool(fake_host.timeline_items))
        assert live.is_streaming is True
        assert live.stream_settled is False
        assert fake_host.states[-1]["status"] == "running"
        assert fake_host.timeline_items[-1].content["text"] == "next"
        assert len(fake_host.turn_ends) == 1
    finally:
        release.set()
        await process.close()
        await live.cancel_stream_flush()


async def test_event_callback_can_request_rpc_and_receive_ui(fake_pi: Path) -> None:
    """A queued event must not block reader responses or a dialog it needs."""
    ui_done = asyncio.Event()
    callback_done = asyncio.Event()
    entered = asyncio.Event()
    order = []

    async def on_event(record):
        if record["type"] == "extension_ui_request":
            order.append("ui")
            ui_done.set()
        elif record["type"] == "needs_response":
            entered.set()
            response = await process.request({"type": "get_state"})
            assert response["success"] is True
            await ui_done.wait()
            order.append("response")
            callback_done.set()
        elif record["type"] == "after":
            order.append("after")

    process = PiRpcProcess([str(fake_pi), "--mode", "rpc"], on_event=on_event)
    await process.start()
    try:
        dispatch(process, type="needs_response")
        await asyncio.wait_for(entered.wait(), timeout=2)
        dispatch(process, type="after")
        dispatch(process, type="extension_ui_request", method="confirm")
        await asyncio.wait_for(callback_done.wait(), timeout=2)
        await wait_for(lambda: len(order) == 3)
        assert order == ["ui", "response", "after"]
    finally:
        await process.close()


async def test_close_cancels_and_joins_event_callbacks(fake_pi: Path) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    late_pushes = []
    release = asyncio.Event()

    async def on_event(record):
        entered.set()
        try:
            await release.wait()
            late_pushes.append(record)
        finally:
            cancelled.set()

    process = PiRpcProcess([str(fake_pi), "--mode", "rpc"], on_event=on_event)
    await process.start()
    dispatch(process, type="agent_settled")
    await asyncio.wait_for(entered.wait(), timeout=2)
    await asyncio.wait_for(process.close(), timeout=5)
    assert cancelled.is_set()
    assert not process._event_tasks
    release.set()
    dispatch(process, type="agent_start")
    await asyncio.sleep(0)
    assert late_pushes == []


async def test_natural_exit_drains_terminal_event_before_exit_callback(tmp_path: Path) -> None:
    """An EOF must not publish exit state ahead of a read terminal event."""
    order = []

    async def on_event(record):
        await asyncio.sleep(0.02)
        order.append(record["type"])

    async def on_exit(code):
        order.append("exit")

    # Use a real subprocess pipe; this process closes stdout immediately after
    # emitting the terminal record, while the event callback is still waiting.
    import sys

    process = PiRpcProcess(
        [sys.executable, "-c", 'print(\'{"type":"agent_settled"}\')'],
        on_event=on_event,
        on_exit=on_exit,
    )
    await process.start()
    try:
        await wait_for(lambda: "exit" in order)
        assert order == ["agent_settled", "exit"]
    finally:
        await process.close()


@pytest.mark.parametrize(
    ("stop_reason", "outcome"),
    [("aborted", "interrupted"), ("error", "failed"), ("stop", "completed")],
)
async def test_native_settle_uses_assistant_stop_reason(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, stop_reason: str, outcome: str
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "native-outcome", cwd=str(tmp_path))
    await runtime.handle_live_event(live, {"type": "agent_start"})
    await runtime.handle_live_event(
        live,
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [], "stopReason": stop_reason},
        },
    )
    # pi 0.87.1 / 1.1.0 emit this shape, with no aborted flag.
    await runtime.handle_live_event(live, {"type": "agent_settled"})
    assert fake_host.turn_ends[-1]["outcome"] == outcome


async def test_outcome_resets_for_successful_retry_and_next_turn(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "retry-outcome", cwd=str(tmp_path))
    await runtime.handle_live_event(live, {"type": "agent_start"})
    for reason in ("error", "stop"):
        await runtime.handle_live_event(
            live,
            {
                "type": "message_end",
                "message": {"role": "assistant", "content": [], "stopReason": reason},
            },
        )
    await runtime.handle_live_event(live, {"type": "agent_settled"})
    assert fake_host.turn_ends[-1]["outcome"] == "completed"

    await runtime.handle_live_event(live, {"type": "agent_start"})
    await runtime.handle_live_event(
        live,
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "aborted"}},
    )
    await runtime.handle_live_event(live, {"type": "agent_settled"})
    assert fake_host.turn_ends[-1]["outcome"] == "interrupted"
    await runtime.handle_live_event(live, {"type": "agent_start"})
    await runtime.handle_live_event(live, {"type": "agent_settled"})
    assert fake_host.turn_ends[-1]["outcome"] == "completed"
