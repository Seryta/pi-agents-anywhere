"""Side answers must arrive before the main run settles, without replacing it."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from connector.runtime_protocol import RuntimeConfig

from pi_aa.runtime import PiLiveSession, PiRuntime
from tests.conftest import FakeHost, wait_for
from tests.test_runtime import make_runtime


@pytest.mark.parametrize("result", [{"answer": "side answer"}, {"error": "side failed"}])
async def test_btw_upsert_preserves_running_reply_and_final_identity(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, result: dict
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    path = tmp_path / "sessions" / "btw.jsonl"
    live = PiLiveSession(runtime, "btw", cwd=str(tmp_path), session_path=str(path))
    runtime._live[live.platform_id] = live
    entry = {
        "type": "custom",
        "id": "side-1",
        "parentId": None,
        "customType": "pi.btw",
        "data": {"question": "side question", **result},
    }
    try:
        await runtime.handle_live_event(live, {"type": "agent_start"})
        live.begin_stream_message()
        live.apply_stream_delta({"type": "text_delta", "delta": "main is working"})
        await live.publish_stream_items()
        stream = next(i for i in fake_host.timeline_items if i.type == "message")
        await runtime.handle_live_event(live, {"type": "entry_appended", "entry": entry})

        side = next(i for i in fake_host.timeline_items if i.type == "system")
        assert side.content["text"] == f"/btw side question\n\n{next(iter(result.values()))}"
        assert side.content["kind"] == ("error" if "error" in result else "system")
        assert side.order_seq == 0, "let the server append after existing live items"
        assert live.is_streaming and not fake_host.turn_ends
        assert not fake_host.timeline_syncs, "a complete replace would wipe the live reply"
        assert not path.exists(), "live answers must not depend on a flushed session file"

        live.apply_stream_delta({"type": "text_delta", "delta": " and continues"})
        await live.publish_stream_items()
        assert any(
            i.id == stream.id and i.content["text"] == "main is working and continues"
            for i in fake_host.timeline_items
        )

        # pi later flushes the same entry. Settle must retain its identity.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                json.dumps(record)
                for record in [
                    {"type": "session", "version": 3, "id": "btw", "cwd": str(tmp_path)},
                    entry,
                ]
            )
            + "\n"
        )
        await runtime.handle_live_event(live, {"type": "agent_settled"})
        final = [i for i in fake_host.timeline_syncs[-1]["items"] if i.type == "system"]
        assert len(final) == 1
        assert final[0].id == side.id and final[0].content_hash == side.content_hash
    finally:
        await runtime.stop()


async def test_idle_btw_is_visible_before_a_session_file_exists(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "empty", cwd=str(tmp_path))
    runtime._live[live.platform_id] = live
    entry = {"type": "custom", "id": "side", "customType": "pi.btw"}
    try:
        for ignored in [
            {**entry, "customType": "pi.state", "data": {"question": "q", "answer": "a"}},
            {**entry, "data": {"question": "q"}},
            {**entry, "data": "malformed"},
        ]:
            await runtime.handle_live_event(live, {"type": "entry_appended", "entry": ignored})
        assert not fake_host.timeline_items

        valid = {**entry, "data": {"question": "q", "answer": "a"}}
        await runtime.handle_live_event(live, {"type": "entry_appended", "entry": valid})
        assert len(fake_host.timeline_items) == 1
        assert fake_host.timeline_items[0].content["text"] == "/btw q\n\na"
        assert not fake_host.timeline_syncs
    finally:
        await runtime.stop()


async def test_failed_side_upsert_does_not_stop_main_and_settle_recovers_answer(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, monkeypatch
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    path = tmp_path / "sessions" / "btw.jsonl"
    live = PiLiveSession(runtime, "btw", cwd=str(tmp_path), session_path=str(path))
    runtime._live[live.platform_id] = live
    entry = {
        "type": "custom",
        "id": "side",
        "customType": "pi.btw",
        "data": {"question": "q", "answer": "a"},
    }
    original = fake_host.timeline_item_upsert

    async def fail_side(item):
        if item.type == "system":
            raise ConnectionError("temporary side answer delivery failure")
        await original(item)

    monkeypatch.setattr(fake_host, "timeline_item_upsert", fail_side)
    try:
        await runtime.handle_live_event(live, {"type": "agent_start"})
        await runtime.handle_live_event(live, {"type": "entry_appended", "entry": entry})
        live.apply_stream_delta({"type": "text_delta", "delta": "main continues"})
        await live.publish_stream_items()
        assert live.is_streaming
        assert any(i.content.get("text") == "main continues" for i in fake_host.timeline_items)

        path.parent.mkdir(parents=True, exist_ok=True)
        header = {"type": "session", "version": 3, "id": "btw", "cwd": str(tmp_path)}
        path.write_text(json.dumps(header) + "\n" + json.dumps(entry) + "\n")
        await runtime.handle_live_event(live, {"type": "agent_settled"})
        assert any(
            i.content.get("text") == "/btw q\n\na" for i in fake_host.timeline_syncs[-1]["items"]
        )
        assert fake_host.turn_ends[-1]["outcome"] == "completed"
    finally:
        await runtime.stop()


@pytest.fixture
async def concurrent_btw_provider():
    """Hold the main HTTP response until the test observes the side answer."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []
    tasks = set()

    async def respond(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            length = next(
                int(h.split(":", 1)[1]) for h in header if h.lower().startswith("content-length:")
            )
            request = json.loads(await reader.readexactly(length))
            side = "Side question:" in json.dumps(request["messages"])
            calls.append("side" if side else "main")
            if not side:
                started.set()
                await release.wait()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
            )
            for choice in [
                {"delta": {"role": "assistant", "content": ""}, "finish_reason": None},
                {
                    "delta": {"content": "side answer" if side else "main answer"},
                    "finish_reason": None,
                },
                {"delta": {}, "finish_reason": "stop"},
            ]:
                chunk = {"id": "fixture", "model": "btw", "choices": [{"index": 0, **choice}]}
                writer.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                await writer.drain()
            writer.write(b"data: [DONE]\n\n")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            # pi may probe the local endpoint or close it during teardown.
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], started, release, calls
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


@pytest.mark.skipif(os.environ.get("PI_AA_TRUE_PI") != "1", reason="set PI_AA_TRUE_PI=1")
async def test_real_pi_btw_answer_arrives_before_main_settles(
    tmp_path: Path, fake_host: FakeHost, monkeypatch, concurrent_btw_provider
) -> None:
    assert shutil.which("pi"), "real pi executable missing"
    port, started, release, calls = concurrent_btw_provider
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "btw": {
                        "api": "openai-completions",
                        "baseUrl": f"http://127.0.0.1:{port}/v1",
                        "apiKey": "local-fixture",
                        "models": [
                            {
                                "id": "btw",
                                "name": "Local BTW fixture",
                                "reasoning": False,
                                "input": ["text"],
                                "contextWindow": 32768,
                                "maxTokens": 512,
                                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                            }
                        ],
                    }
                }
            }
        )
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    extension = Path(__file__).resolve().parents[1] / "examples/extensions/btw.ts"
    wrapper = tmp_path / "pi-fixture"
    wrapper.write_text(
        "#!/bin/sh\nexec pi --provider btw --model btw --no-extensions --no-skills "
        f'-e "{extension}" "$@"\n'
    )
    wrapper.chmod(0o755)
    runtime = PiRuntime(
        config=RuntimeConfig(
            runtime="pi",
            revision=1,
            values={
                "executablePath": str(wrapper),
                "sessionsDir": str(tmp_path / "sessions"),
                "defaultCwd": str(tmp_path),
                "requestTimeoutMs": 15000,
            },
        ),
        host=fake_host,
    )
    await runtime.start()
    try:
        await runtime.create_and_start_session("real-btw", "main task", cwd=str(tmp_path))
        await asyncio.wait_for(started.wait(), timeout=15)
        commands = await runtime.list_commands("real-btw")
        btw = next(c for c in commands if c.id == "btw")
        assert "running" in btw.metadata["ui"]["allowedStatuses"]
        result = await asyncio.wait_for(
            runtime.execute_command("real-btw", "btw", raw="/btw side question"), timeout=3
        )
        assert result.ok
        await wait_for(
            lambda: any(i.type == "system" for i in fake_host.timeline_items), timeout=15
        )
        side = next(i for i in fake_host.timeline_items if i.type == "system")
        assert side.content["text"] == "/btw side question\n\nside answer"
        assert runtime._live["real-btw"].is_streaming
        assert not fake_host.turn_ends and not fake_host.timeline_syncs
        assert calls == ["main", "side"]
        release.set()
        await wait_for(lambda: bool(fake_host.turn_ends), timeout=15)
        final = fake_host.timeline_syncs[-1]["items"]
        assert sum(i.id == side.id for i in final) == 1
        assert any(i.role == "assistant" and i.content.get("text") == "main answer" for i in final)
        assert fake_host.turn_ends[-1]["outcome"] == "completed"
    finally:
        release.set()
        await runtime.stop()
