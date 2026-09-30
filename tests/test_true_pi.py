"""Integration tests against a real ``pi`` install.

These are skipped unless ``PI_AA_TRUE_PI=1``. Run them through
``docker/run-tests.sh`` so nothing executes on the host.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from connector.runtime_protocol import RuntimeConfig

from pi_aa.config import probe_pi_version
from pi_aa.provider import PiProvider
from pi_aa.runtime import PiLiveSession, PiRuntime
from tests.conftest import FakeHost, wait_for
from tests.test_sessions import write_session

pytestmark = pytest.mark.skipif(
    os.environ.get("PI_AA_TRUE_PI") != "1",
    reason="set PI_AA_TRUE_PI=1 to run against a real pi install",
)

REAL_PI = shutil.which("pi")


def make_runtime(tmp_path: Path, host: FakeHost) -> PiRuntime:
    config = RuntimeConfig(
        runtime="pi",
        revision=1,
        values={
            "executablePath": REAL_PI,
            "sessionsDir": str(tmp_path / "sessions"),
            "defaultCwd": str(tmp_path),
            "requestTimeoutMs": 30_000,
        },
    )
    return PiRuntime(config=config, host=host)


def test_real_pi_available() -> None:
    assert REAL_PI, "real pi executable not found in PATH"


async def test_real_discover(fake_host: FakeHost) -> None:
    version = await probe_pi_version(REAL_PI)
    assert version
    descriptor = await PiProvider().discover()
    assert descriptor.runtime_type == "pi"


async def test_real_catalogs(tmp_path: Path, fake_host: FakeHost) -> None:
    runtime = make_runtime(tmp_path, fake_host)
    await runtime.start()
    try:
        catalog = await runtime.list_model_catalog()
        assert catalog.runtime == "pi"
        # A bare container has no configured models; verify shape, not count.
        for model in catalog.models:
            assert model.id
            assert model.selection_id
        commands = await runtime.list_commands("sess-none")
        assert isinstance(commands, tuple)
    finally:
        await runtime.stop()


async def test_real_open_existing_session_file(tmp_path: Path, fake_host: FakeHost) -> None:
    project = tmp_path / "project"
    project.mkdir()
    session_file = tmp_path / "sessions" / "--tmp-project--" / "2026_sess.jsonl"
    write_session(session_file, name="真实会话", cwd=str(project))
    runtime = make_runtime(tmp_path, fake_host)
    await runtime.start()
    live = PiLiveSession(
        runtime,
        "sess-real",
        cwd=str(project),
        session_path=str(session_file),
    )
    try:
        await live.ensure_started()
        assert live.alive
        assert live.pi_session_id == "session-uuid-1"
        assert Path(str(live.session_file)).name == session_file.name
    finally:
        await live.stop()
    try:
        sessions = await runtime.list_sessions()
        assert len(sessions) == 1
        state = await runtime.get_session_state(
            sessions[0].session_id, sessions[0].external_session_id
        )
        assert state is not None
        assert state.status == "idle"
    finally:
        await runtime.stop()

    summary = json.loads(session_file.read_text(encoding="utf-8").splitlines()[0])
    assert summary["type"] == "session"


@pytest.mark.skipif(
    os.environ.get("PI_AA_TRUE_PI_MODEL") != "1",
    reason="set PI_AA_TRUE_PI_MODEL=1 to spend one real model turn",
)
async def test_real_model_turn(tmp_path: Path, fake_host: FakeHost) -> None:
    runtime = make_runtime(tmp_path, fake_host)
    await runtime.start()
    try:
        result = await runtime.create_and_start_session(
            "sess-turn",
            "Reply with exactly: pong",
            cwd=str(tmp_path),
        )
        assert result.ok is True
        await wait_for(lambda: len(fake_host.timeline_syncs) >= 1, timeout=180)
        synced = fake_host.timeline_syncs[-1]
        texts = [
            item.content.get("text", "")
            for item in synced["items"]
            if item.type == "message" and item.role == "assistant"
        ]
        assert any("pong" in text for text in texts), texts
    finally:
        await runtime.stop()
