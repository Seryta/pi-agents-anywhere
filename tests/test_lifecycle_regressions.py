from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from connector.runtime_protocol import RuntimeConfig, RuntimeInvalidRequestError

from pi_aa.config import normalized_config_values
from pi_aa.provider import PiProvider
from pi_aa.rpc import PiRpcProcess, PiRpcProcessExited
from pi_aa.runtime import PiLiveSession, PiRuntime, platform_session_id
from tests.conftest import FakeHost
from tests.test_runtime import make_runtime
from tests.test_sessions import write_session


@pytest.mark.parametrize("escape", ["absolute", "traversal", "symlink", "new-symlink"])
async def test_resume_rejects_paths_outside_session_root(
    escape: str, fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    outside = tmp_path / "private" / "history.jsonl"
    write_session(outside, cwd=str(tmp_path))
    root = tmp_path / "sessions"
    root.mkdir()
    if escape == "absolute":
        target = outside
    elif escape == "traversal":
        target = root / ".." / "private" / "history.jsonl"
    else:
        (root / "link").symlink_to(outside.parent, target_is_directory=True)
        target = root / "link" / ("new.jsonl" if escape == "new-symlink" else outside.name)
    original = outside.read_bytes()
    try:
        with pytest.raises(RuntimeInvalidRequestError, match="outside sessionsDir"):
            await runtime._ensure_live("session", str(target), None)
        assert runtime._live == {}
        assert outside.read_bytes() == original
    finally:
        await runtime.stop()


async def test_existing_live_cannot_bypass_external_path_validation(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    live = PiLiveSession(runtime, "session", cwd=str(tmp_path))
    runtime._live[live.platform_id] = live
    with pytest.raises(RuntimeInvalidRequestError):
        await runtime._ensure_live(live.platform_id, str(tmp_path / "private.jsonl"), None)
    assert live.process is None
    await runtime.stop()


@pytest.mark.parametrize("existing", [False, True])
async def test_resume_accepts_session_files_inside_root(
    existing: bool, fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    target = tmp_path / "sessions" / "project" / "history.jsonl"
    if existing:
        write_session(target, cwd=str(tmp_path))
    try:
        live = await runtime._ensure_live("session", str(target), None)
        assert live.alive
        assert live.process is not None
        assert live.process.argv[-2:] == ["--session", str(target)]
    finally:
        await runtime.stop()


async def test_discovery_and_known_paths_cannot_resume_symlink_escape(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    outside = tmp_path / "private" / "history.jsonl"
    write_session(outside, cwd=str(tmp_path))
    link = tmp_path / "sessions" / "history.jsonl"
    link.parent.mkdir()
    link.symlink_to(outside)
    session_id = platform_session_id(fake_host.session_namespace, str(link))
    assert runtime._resolve_session_path(session_id, None) is None
    runtime._known_paths[session_id] = str(link)
    assert runtime._resolve_session_path(session_id, None) is None


async def test_new_session_restart_uses_the_file_reported_by_pi(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    try:
        live = await runtime._ensure_live("session", None, None)
        original_file = live.session_file
        assert live.session_path is None and original_file
        original_process = live.process
        assert original_process is not None
        await original_process.close()
        await live.ensure_started()
        assert live.process is not None and live.process is not original_process
        assert live.process.argv[-2:] == ["--session", original_file]
    finally:
        await runtime.stop()


async def test_late_old_process_exit_does_not_clear_replacement_state(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    try:
        live = await runtime._ensure_live("session", None, None)
        previous = live.process
        assert previous is not None
        previous._closing = True
        await live.ensure_started()
        live.is_streaming = True
        live.stream_settled = False
        fake_host.states.clear()
        await previous.close()
        assert live.alive
        assert live.is_streaming
        assert not live.stream_settled
        assert fake_host.states == []
    finally:
        await runtime.stop()


async def test_restart_waits_for_exit_cleanup_already_in_progress(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    entered, release = asyncio.Event(), asyncio.Event()
    previous = None
    restart = None

    async def slow_cleanup(live: PiLiveSession, code: int | None) -> None:
        entered.set()
        await release.wait()
        live.is_streaming = False

    monkeypatch.setattr(runtime, "handle_live_exit", slow_cleanup)
    try:
        live = await runtime._ensure_live("session", None, None)
        previous = live.process
        assert previous is not None and previous._process is not None
        previous._process.kill()
        await previous._process.wait()
        await entered.wait()
        restart = asyncio.create_task(live.ensure_started())
        await asyncio.sleep(0)
        assert not restart.done()
        release.set()
        await restart
        live.is_streaming = True
        assert previous._reader_task is not None
        await previous._reader_task
        assert live.alive and live.is_streaming
    finally:
        release.set()
        if restart is not None:
            await restart
        if previous is not None:
            await previous.close()
        await runtime.stop()


@pytest.mark.skipif(os.environ.get("PI_AA_TRUE_PI") != "1", reason="requires real pi opt-in")
async def test_real_pi_restart_restores_original_file_id_and_history(
    tmp_path: Path, fake_host: FakeHost
) -> None:
    executable = shutil.which("pi")
    assert executable
    runtime = make_runtime(Path(executable), tmp_path, fake_host)
    try:
        live = await runtime._ensure_live("session", None, None)
        original_file, original_id = live.session_file, live.pi_session_id
        assert original_file and original_id
        path = Path(original_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"type": "session", "version": 3, "id": original_id, "cwd": str(tmp_path)},
            {
                "type": "message",
                "id": "user-1",
                "parentId": None,
                "message": {"role": "user", "content": "recover this history"},
            },
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        process = live.process
        assert process is not None and process._process is not None
        process._process.kill()
        await process._process.wait()
        await live.ensure_started()
        assert live.session_file == original_file
        assert live.pi_session_id == original_id
        messages = await live.command({"type": "get_messages"})
        assert messages["data"]["messages"][0]["content"] == "recover this history"
    finally:
        await runtime.stop()


async def test_concurrent_utility_queries_share_one_owned_process(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    processes = []
    try:
        processes = await asyncio.gather(*(runtime._ensure_utility() for _ in range(5)))
        assert all(process is processes[0] for process in processes)
        results = await asyncio.gather(
            *(runtime._utility_command({"type": "get_commands"}) for _ in range(5))
        )
        assert all(result["success"] for result in results)
        assert runtime._utility is processes[0]
    finally:
        await runtime.stop()
        # Close every returned object even if ownership unexpectedly diverges.
        for process in processes:
            await process.close()
    assert runtime._utility is None
    assert all(not process.alive and process.returncode is not None for process in processes)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_utility_start_failure_or_cancellation_closes_child_and_allows_retry(
    cancelled: bool,
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    entered = asyncio.Event()
    children = []
    original_start = PiRpcProcess.start

    async def failing_start(process: PiRpcProcess) -> None:
        await original_start(process)
        children.append(process)
        entered.set()
        if cancelled:
            await asyncio.Event().wait()
        raise OSError("startup failed after child spawn")

    monkeypatch.setattr(PiRpcProcess, "start", failing_start)
    task = asyncio.create_task(runtime._ensure_utility())
    try:
        await entered.wait()
        if cancelled:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancelled else OSError):
            await task
        assert runtime._utility is None
        assert len(children) == 1
        assert not children[0].alive
        assert children[0].returncode is not None
        monkeypatch.setattr(PiRpcProcess, "start", original_start)
        replacement = await runtime._ensure_utility()
        assert replacement.alive and replacement is not children[0]
    finally:
        if not task.done():
            task.cancel()
        await runtime.stop()
        for child in children:
            await child.close()


async def test_stop_waits_for_utility_start_and_rejects_new_requests(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    entered, release = asyncio.Event(), asyncio.Event()
    children = []
    original_start = PiRpcProcess.start

    async def slow_start(process: PiRpcProcess) -> None:
        await original_start(process)
        children.append(process)
        entered.set()
        await release.wait()

    monkeypatch.setattr(PiRpcProcess, "start", slow_start)
    starting = asyncio.create_task(runtime._ensure_utility())
    stopping = None
    try:
        await entered.wait()
        stopping = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
        with pytest.raises(PiRpcProcessExited, match="stopping"):
            await starting
        await stopping
        assert runtime._utility is None
        assert children[0].returncode is not None
        with pytest.raises(PiRpcProcessExited, match="stopping"):
            await runtime._ensure_utility()
        assert len(children) == 1
    finally:
        release.set()
        if stopping is not None:
            await stopping
        await runtime.stop()
        for child in children:
            await child.close()


@pytest.mark.parametrize("symlink_kind", ["root", "inner"])
@pytest.mark.parametrize(
    "use_real_pi",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                os.environ.get("PI_AA_TRUE_PI") != "1", reason="requires real pi opt-in"
            ),
        ),
    ],
)
async def test_safe_symlinks_preserve_discovery_snapshot_and_resume_identity(
    symlink_kind: str,
    use_real_pi: bool,
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fake_pi prepends its directory to PATH; select the installed real pi.
    executable = Path("/usr/local/bin/pi") if use_real_pi else fake_pi
    root = tmp_path / "sessions"
    if symlink_kind == "root":
        storage = tmp_path / "storage"
        storage.mkdir()
        root.symlink_to(storage, target_is_directory=True)
        target = root / "history.jsonl"
    else:
        storage = root / "storage"
        storage.mkdir(parents=True)
        original = storage / "history.jsonl"
        write_session(original, cwd=str(tmp_path))
        target = root / "alias.jsonl"
        target.symlink_to(original)
    if symlink_kind == "root":
        write_session(target, cwd=str(tmp_path))
    monkeypatch.setenv("PI_FAKE_SESSION_FILE", str(target))
    runtime = make_runtime(executable, tmp_path, fake_host)
    try:
        inventory = await runtime.list_sessions()
        session = next(item for item in inventory if item.external_session_id == str(target))
        snapshot = await runtime.get_session_snapshot(
            session.session_id, session.external_session_id
        )
        assert snapshot.complete
        assert snapshot.external_session_id == session.external_session_id
        live = await runtime._ensure_live(session.session_id, session.external_session_id, None)
        assert live.external_id == session.external_session_id
        assert live.process is not None
        assert live.process.argv[-2:] == ["--session", session.external_session_id]
        assert (
            platform_session_id(fake_host.session_namespace, live.external_id) == session.session_id
        )
    finally:
        await runtime.stop()


async def test_relative_config_uses_connector_paths_for_discovery_and_pi(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "sessions" / "history.jsonl"
    write_session(target, cwd=str(project))
    monkeypatch.setenv("PI_FAKE_SESSION_FILE", str(target))
    config = RuntimeConfig(
        runtime="pi",
        revision=1,
        values={
            "executablePath": str(fake_pi),
            "sessionsDir": "sessions",
            "defaultCwd": "project",
        },
    )
    values = normalized_config_values(config.values)
    assert values["sessionsDir"] == str(target.parent)
    assert values["defaultCwd"] == str(project)
    provider = PiProvider()
    assert provider.resource_claims(config)[0].key == provider.session_source_key(config).key
    runtime = PiRuntime(config, fake_host)
    try:
        session = (await runtime.list_sessions())[0]
        assert session.external_session_id == str(target)
        snapshot = await runtime.get_session_snapshot(session.session_id, "sessions/history.jsonl")
        assert snapshot.external_session_id == str(target)
        live = await runtime._ensure_live(session.session_id, "sessions/history.jsonl", "project")
        assert live.cwd == str(project)
        assert live.process is not None
        argv = live.process.argv
        assert argv[argv.index("--session-dir") + 1] == str(target.parent)
        assert argv[-2:] == ["--session", str(target)]
        assert runtime._resolve_cwd("project") == str(project)
    finally:
        await runtime.stop()


async def test_restart_drains_old_events_before_starting_replacement(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed_event(live: PiLiveSession, record) -> None:
        entered.set()
        try:
            await release.wait()
            live.is_streaming = False
            live.stream_settled = True
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(runtime, "handle_live_event", delayed_event)
    previous = None
    try:
        live = await runtime._ensure_live("session", None, None)
        previous = live.process
        assert previous is not None and previous._process is not None
        previous._dispatch_line(b'{"type":"agent_settled"}')
        await entered.wait()
        previous._process.kill()
        await previous._process.wait()
        replacements = await asyncio.gather(*(live.ensure_started() for _ in range(3)))
        assert replacements == [None, None, None]
        assert live.process is not previous and live.alive
        assert cancelled.is_set()
        live.is_streaming = True
        live.stream_settled = False
        release.set()
        await asyncio.sleep(0)
        assert live.is_streaming and not live.stream_settled
    finally:
        release.set()
        if previous is not None:
            await previous.close()
        await runtime.stop()


@pytest.mark.parametrize("method", ["confirm", "notify"])
async def test_restart_resolves_old_dialog_when_eof_event_drain_is_cancelled(
    method: str,
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    entered, release = asyncio.Event(), asyncio.Event()
    original_notice_upsert = fake_host.notice_upsert

    async def delayed_notice(notice) -> None:
        await original_notice_upsert(notice)
        if notice.status == "open":
            entered.set()
            await release.wait()

    monkeypatch.setattr(fake_host, "notice_upsert", delayed_notice)
    previous = None
    try:
        live = await runtime._ensure_live("session", None, None)
        previous = live.process
        assert previous is not None and previous._process is not None
        await runtime.handle_live_event(
            live,
            {
                "type": "extension_ui_request",
                "method": "setStatus",
                "statusKey": "old-extension",
                "statusText": "old process running",
            },
        )
        assert live.extension_status
        previous._dispatch_line(
            json.dumps(
                {
                    "type": "extension_ui_request",
                    "id": "old-dialog",
                    "method": method,
                    "title": "Continue?",
                    "message": "Old process notice",
                }
            ).encode()
        )
        await entered.wait()
        assert live.status == ("waiting_approval" if method == "confirm" else "idle")
        previous._process.kill()
        await previous._process.wait()
        await live.ensure_started()
        assert live.alive and live.process is not previous
        assert live.pending_ui == {}
        assert live.open_notifications == {}
        assert live.extension_status == {}
        assert live.status == "idle"
        assert [notice.status for notice in fake_host.notices] == ["open", "resolved"]
    finally:
        release.set()
        if previous is not None:
            await previous.close()
        await runtime.stop()


async def test_restart_drops_dead_process_queues_and_retry_reason(
    fake_pi: Path, tmp_path: Path, fake_host: FakeHost
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    try:
        live = await runtime._ensure_live("session", None, None)
        original_file, original_id = live.session_file, live.pi_session_id
        live.session_stats = {"tokens": {"total": 42}, "cost": 0.25}
        await runtime.handle_live_event(
            live,
            {
                "type": "queue_update",
                "steering": ["old steering"],
                "followUp": ["old followup"],
            },
        )
        await runtime.handle_live_event(
            live,
            {
                "type": "auto_retry_start",
                "errorMessage": "old failure",
            },
        )
        live.cleared_queue = {"steering": ["previous interrupt receipt"], "followUp": []}
        state = await runtime.get_session_state(live.platform_id)
        assert state is not None and state.metadata["pendingMessageCount"] == 2
        assert state.status_reason == "retrying: old failure"
        previous = live.process
        assert previous is not None and previous._process is not None
        previous._process.kill()
        await previous._process.wait()
        await live.ensure_started()
        state = await runtime.get_session_state(live.platform_id)
        assert state is not None and state.status == "idle"
        assert state.metadata["pendingMessageCount"] == 0
        assert state.metadata["pendingSteering"] == []
        assert state.metadata["pendingFollowUp"] == []
        assert state.status_reason is None
        assert "clearedQueue" not in state.metadata
        assert state.external_session_id == original_file
        assert state.metadata["piSessionId"] == original_id
        assert state.metadata["tokens"] == {"total": 42}
        assert state.metadata["cost"] == 0.25
    finally:
        await runtime.stop()


@pytest.mark.parametrize("method", ["confirm", "notify"])
async def test_restart_recovers_when_old_notice_close_fails(
    method: str,
    fake_pi: Path,
    tmp_path: Path,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    try:
        live = await runtime._ensure_live("session", None, None)
        original_file = live.session_file
        await runtime.handle_live_event(
            live,
            {
                "type": "extension_ui_request",
                "id": "old-notice",
                "method": method,
                "title": "Old dialog",
                "message": "Old toast",
            },
        )

        async def failing_notice(notice) -> None:
            raise OSError("notice endpoint temporarily unavailable")

        monkeypatch.setattr(fake_host, "notice_upsert", failing_notice)
        previous = live.process
        assert previous is not None and previous._process is not None
        previous._process.kill()
        await previous._process.wait()
        await live.ensure_started()
        assert live.alive and live.process is not previous
        assert not live.pending_ui and not live.open_notifications
        state = await runtime.get_session_state(live.platform_id)
        assert state is not None and state.status == "idle"
        assert state.external_session_id == original_file
        assert "notice endpoint temporarily unavailable" in caplog.text
    finally:
        await runtime.stop()
