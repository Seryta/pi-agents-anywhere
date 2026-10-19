"""Memory safety valve: oversized live sessions are reclaimed and resumed.

A single session whose pi process grows past the configured limit must be
reclaimed even while it runs: letting it continue risks a global host OOM
that kills peer processes. The reclaim waits for a busy turn to settle, and a
turn that had to be cut short is resumed through an automatic continue prompt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from connector.runtime_protocol import RuntimeConfig

from pi_aa.runtime import (
    AUTO_CONTINUE_PROMPT,
    PendingInteraction,
    PiLiveSession,
    PiRuntime,
)
from tests.conftest import FakeHost, wait_for


def make_runtime(fake_pi: Path, tmp_path: Path, host: FakeHost, **values) -> PiRuntime:
    config = RuntimeConfig(
        runtime="pi",
        revision=1,
        values={
            "executablePath": str(fake_pi),
            "sessionsDir": str(tmp_path / "sessions"),
            "defaultCwd": str(tmp_path),
            "requestTimeoutMs": 5000,
            **values,
        },
    )
    return PiRuntime(config=config, host=host)


def register_live(runtime: PiRuntime, session_id: str, tmp_path: Path) -> PiLiveSession:
    live = PiLiveSession(runtime, session_id, cwd=str(tmp_path))
    runtime._live[session_id] = live
    return live


def commands_received(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def test_memory_warning_logs_once_per_excursion(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Crossing the warn line logs once and re-arms after falling back."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime._warn_live_bytes = 1024
    runtime._max_live_bytes = 0  # warning only, reclaim disabled
    live = register_live(runtime, "sess-mem-warn", tmp_path)
    rss = {"value": 2048}
    monkeypatch.setattr(PiLiveSession, "rss_bytes", lambda self: rss["value"])

    with caplog.at_level(logging.WARNING):
        await runtime._reclaim_oversized_sessions()
        await runtime._reclaim_oversized_sessions()
        assert sum("warning line" in record.message for record in caplog.records) == 1

        rss["value"] = 512  # back under the line re-arms the warning
        await runtime._reclaim_oversized_sessions()
        assert "sess-mem-warn" not in runtime._memory_warned

        rss["value"] = 4096
        await runtime._reclaim_oversized_sessions()
    assert sum("warning line" in record.message for record in caplog.records) == 2
    assert runtime._live.get("sess-mem-warn") is live


async def test_memory_reclaim_publishes_and_resolves_notice(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform gets a notice for the reclaim and a resolve afterwards."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime._max_live_bytes = 1024
    register_live(runtime, "sess-mem-notice", tmp_path)
    rss = {"value": 2048}
    monkeypatch.setattr(PiLiveSession, "rss_bytes", lambda self: rss["value"])

    await runtime._reclaim_oversized_sessions()

    reclaims = [n for n in fake_host.notices if n.notice_id == "pi-memory-reclaim-sess-mem-notice"]
    assert [n.status for n in reclaims] == ["open"]
    assert "已自动回收" in reclaims[0].title
    assert "sess-mem-notice" not in runtime._live

    live = register_live(runtime, "sess-mem-notice", tmp_path)
    rss["value"] = 512
    await runtime._reclaim_oversized_sessions()

    reclaims = [n for n in fake_host.notices if n.notice_id == "pi-memory-reclaim-sess-mem-notice"]
    assert [n.status for n in reclaims] == ["open", "resolved"]
    assert runtime._live.get("sess-mem-notice") is live


async def test_memory_gaveup_notice_is_resolved_by_a_user_turn(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausting the budget tells the user, and their return closes it."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime._max_live_bytes = 1024
    runtime._memory_reclaim_grace = 0.0
    runtime._auto_continue_max_attempts = 1
    runtime._auto_continue_streaks["sess-mem-gaveup"] = 1
    live = register_live(runtime, "sess-mem-gaveup", tmp_path)
    live.is_streaming = True
    monkeypatch.setattr(PiLiveSession, "rss_bytes", lambda self: 2048)

    await runtime._reclaim_oversized_sessions()

    gaveup = [n for n in fake_host.notices if n.notice_id == "pi-memory-gaveup-sess-mem-gaveup"]
    assert [n.status for n in gaveup] == ["open"]
    assert "自动恢复已停止" in gaveup[0].title

    await runtime.start()
    try:
        await runtime.create_and_start_session("sess-mem-gaveup", "hello", cwd=str(tmp_path))
        gaveup = [
            n for n in fake_host.notices if n.notice_id == "pi-memory-gaveup-sess-mem-gaveup"
        ]
        assert [n.status for n in gaveup] == ["open", "resolved"]
    finally:
        await runtime.stop()


async def test_memory_reclaim_stops_oversized_idle_session(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle session over the limit is reclaimed, and not auto-continued.

    An idle session has no unfinished work, so reviving it and sending a
    continue prompt would spend tokens on nothing.
    """

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime._max_live_bytes = 1024
    live = register_live(runtime, "sess-mem-idle", tmp_path)
    monkeypatch.setattr(PiLiveSession, "rss_bytes", lambda self: 2048)

    await runtime._reclaim_oversized_sessions()

    assert "sess-mem-idle" not in runtime._live
    assert not any(cmd.get("type") == "prompt" for cmd in commands_received(tmp_path / "cmd.jsonl"))
    assert live.process is None or not live.alive


async def test_memory_reclaim_waits_for_busy_turn_within_grace(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy turn gets the grace window before the reclaim is forced."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime._max_live_bytes = 1024
    runtime._memory_reclaim_grace = 3600.0
    live = register_live(runtime, "sess-mem-busy", tmp_path)
    live.is_streaming = True
    monkeypatch.setattr(PiLiveSession, "rss_bytes", lambda self: 2048)

    await runtime._reclaim_oversized_sessions()

    assert runtime._live.get("sess-mem-busy") is live
    assert "sess-mem-busy" in runtime._memory_pressure_since


async def test_memory_reclaim_settled_turn_is_not_auto_continued(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that settled during the grace window is reclaimed without a nudge."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime._max_live_bytes = 1024
    runtime._memory_reclaim_grace = 3600.0
    live = register_live(runtime, "sess-mem-settled", tmp_path)
    live.is_streaming = True
    monkeypatch.setattr(PiLiveSession, "rss_bytes", lambda self: 2048)

    await runtime._reclaim_oversized_sessions()
    assert runtime._live.get("sess-mem-settled") is live

    live.is_streaming = False  # the turn settled on its own
    await runtime._reclaim_oversized_sessions()

    assert "sess-mem-settled" not in runtime._live
    assert not any(cmd.get("type") == "prompt" for cmd in commands_received(tmp_path / "cmd.jsonl"))


async def test_memory_reclaim_forces_after_grace_and_auto_continues(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn still running past the grace is cut and resumed by itself.

    The replacement process must come up against the same session file and
    receive the continue prompt, so the agent picks up where it left off.
    """

    commands_file = tmp_path / "cmd.jsonl"
    monkeypatch.setenv("PI_FAKE_COMMANDS_FILE", str(commands_file))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime._max_live_bytes = 1024
    runtime._memory_reclaim_grace = 0.0
    live = register_live(runtime, "sess-mem-continue", tmp_path)
    live.is_streaming = True
    monkeypatch.setattr(PiLiveSession, "rss_bytes", lambda self: 2048)

    await runtime._reclaim_oversized_sessions()

    assert runtime._live.get("sess-mem-continue") is not live
    await wait_for(
        lambda: any(
            cmd.get("type") == "prompt" and cmd.get("message") == AUTO_CONTINUE_PROMPT
            for cmd in commands_received(commands_file)
        )
    )
    assert runtime._auto_continue_streaks["sess-mem-continue"] == 1


async def test_memory_reclaim_auto_continue_stops_at_budget(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive revivals stop at the attempt budget instead of looping."""

    commands_file = tmp_path / "cmd.jsonl"
    monkeypatch.setenv("PI_FAKE_COMMANDS_FILE", str(commands_file))
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime._max_live_bytes = 1024
    runtime._memory_reclaim_grace = 0.0
    runtime._auto_continue_max_attempts = 1
    runtime._auto_continue_streaks["sess-mem-budget"] = 1
    live = register_live(runtime, "sess-mem-budget", tmp_path)
    live.is_streaming = True
    monkeypatch.setattr(PiLiveSession, "rss_bytes", lambda self: 2048)

    await runtime._reclaim_oversized_sessions()

    assert "sess-mem-budget" not in runtime._live
    prompts = [cmd for cmd in commands_received(commands_file) if cmd.get("type") == "prompt"]
    assert not prompts


async def test_memory_reclaim_skips_sessions_with_pending_dialog(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dialog blocks the turn on the user; the reclaim must not drop it."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime._max_live_bytes = 1024
    runtime._memory_reclaim_grace = 0.0
    live = register_live(runtime, "sess-mem-dialog", tmp_path)
    live.is_streaming = True
    pending = PendingInteraction({"id": "ui-mem", "method": "confirm", "title": "t"})
    live.pending_ui[pending.request_id] = pending
    monkeypatch.setattr(PiLiveSession, "rss_bytes", lambda self: 2048)

    await runtime._reclaim_oversized_sessions()

    assert runtime._live.get("sess-mem-dialog") is live


async def test_memory_reclaim_below_limit_refreshes_budget(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process back under the limit clears pressure state and the budget."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    runtime._max_live_bytes = 1024
    live = register_live(runtime, "sess-mem-small", tmp_path)
    runtime._memory_pressure_since["sess-mem-small"] = 1.0
    runtime._auto_continue_streaks["sess-mem-small"] = 2
    monkeypatch.setattr(PiLiveSession, "rss_bytes", lambda self: 512)

    await runtime._reclaim_oversized_sessions()

    assert runtime._live.get("sess-mem-small") is live
    assert "sess-mem-small" not in runtime._memory_pressure_since
    assert "sess-mem-small" not in runtime._auto_continue_streaks


async def test_user_turn_resets_auto_continue_budget(
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
) -> None:
    """A manual user turn starts a fresh auto-continue budget."""

    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    await runtime.start()
    try:
        runtime._auto_continue_streaks["sess-mem-user"] = 3
        await runtime.create_and_start_session("sess-mem-user", "hello", cwd=str(tmp_path))

        assert "sess-mem-user" not in runtime._auto_continue_streaks
    finally:
        await runtime.stop()
