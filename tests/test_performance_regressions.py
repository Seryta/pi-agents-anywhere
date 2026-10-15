"""Blocking projection and file writes must leave the event loop responsive."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from connector.runtime_protocol import RuntimeAttachment

from pi_aa import projection
from pi_aa.sessions import load_session_doc
from tests.conftest import FakeHost
from tests.test_runtime import make_runtime


async def test_projection_keeps_loop_responsive(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text('{"type":"session","id":"responsive"}\n')
    doc = load_session_doc(session_file)
    assert doc is not None
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocking_projection(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        # A timer on the event loop releases this work. Running it directly on
        # that loop times out, proving responsiveness rather than code shape.
        assert release.wait(timeout=2), "projection blocked the event loop"
        return ()

    monkeypatch.setattr(projection, "project_session", blocking_projection)
    task = asyncio.create_task(runtime._project_session_items("responsive", doc))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        release.set()
        assert await task == ()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_attachment_write_keeps_loop_responsive(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_write = Path.write_bytes

    def blocking_write(path, content):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=2), "attachment write blocked the event loop"
        return original_write(path, content)

    monkeypatch.setattr(Path, "write_bytes", blocking_write)
    task = asyncio.create_task(
        runtime._prepare_attachments(
            "responsive",
            (RuntimeAttachment(file_id="text-1", name="notes.txt", media_type="text/plain"),),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        release.set()
        images, notes = await task
        assert images == ()
        assert len(notes) == 1
        assert Path(notes[0][1]).read_bytes() == b"hello attachment"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_checkpoint_hash_keeps_loop_responsive(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pi_aa.runtime as runtime_module

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_hash = runtime_module._timeline_prefix_hash

    def blocking_hash(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=2), "checkpoint hashing blocked the event loop"
        return original_hash(*args)

    monkeypatch.setattr(runtime_module, "_timeline_prefix_hash", blocking_hash)
    task = asyncio.create_task(
        runtime._write_timeline_checkpoint("responsive", "session.jsonl", ())
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        release.set()
        assert await task is True
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
