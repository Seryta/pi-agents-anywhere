"""PiRuntime: one ``pi --mode rpc`` process per live session.

The runtime is a *polling* runtime: the connector periodically asks it for the
session inventory and reshapes Pi session files into platform timeline items.
Live sessions additionally push state and a fresh snapshot when a run settles,
so the workbench does not have to wait for the next polling pass.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from connector.runtime_protocol import (
    CAPABILITY_CATALOG_EFFORT,
    CAPABILITY_CATALOG_MODEL,
    CAPABILITY_RUNTIME_ATTACHMENT,
    CAPABILITY_SESSION_COMMANDS,
    CAPABILITY_SESSION_INTERACTION_APPROVAL,
    CAPABILITY_SESSION_INTERRUPT,
    CAPABILITY_SESSION_SEND_MESSAGE,
    CAPABILITY_SESSION_STEER,
    AgentRuntime,
    RuntimeAttachment,
    RuntimeCapability,
    RuntimeCapabilitySet,
    RuntimeCommand,
    RuntimeCommandResult,
    RuntimeConfig,
    RuntimeIdentity,
    RuntimeInvalidRequestError,
    RuntimeModelCatalog,
    RuntimeModelItem,
    RuntimeOperationResult,
    RuntimePermissionCatalog,
    RuntimeTimelineSnapshot,
    RuntimeUnavailableError,
    SessionMeta,
    SessionNotice,
    SessionSourceState,
    SessionState,
)
from connector.runtime_protocol.host import RuntimeHostClient

from pi_aa import projection
from pi_aa.config import normalized_config_values, probe_pi_version
from pi_aa.rpc import (
    PiRpcProcess,
    PiRpcProcessExited,
    PiRpcRequestFailed,
    response_data,
)
from pi_aa.sessions import (
    PiSessionSummary,
    SessionDirectory,
    is_meaningful_title,
    load_session_doc,
)

logger = logging.getLogger(__name__)

RUNTIME = "pi"
PLATFORM_SESSION_PREFIX = "sess_pi_"

# How often the idle reclaim loop inspects live sessions.
RECLAIM_INTERVAL_SECONDS = 15.0

# Pi consumes images as base64 ``ImageContent``; these are the formats the
# model providers behind Pi accept in practice.
PI_IMAGE_MIME_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)
DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})


def platform_session_id(namespace: str, external_id: str) -> str:
    """Derive the stable platform session id for a Pi session file."""

    digest = hashlib.sha256(f"{namespace}:pi:{external_id}".encode()).hexdigest()
    return f"{PLATFORM_SESSION_PREFIX}{digest[:24]}"


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat()


def _model_selection_id(model: Mapping[str, Any]) -> str | None:
    model_id = model.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    provider = model.get("provider")
    if isinstance(provider, str) and provider:
        return f"{provider}:{model_id}"
    return model_id


def _split_model_selection(value: str) -> tuple[str | None, str]:
    provider, separator, model_id = value.partition(":")
    if separator and provider and model_id:
        return provider, model_id
    return None, value


def _model_display_title(
    name: str,
    provider: str | None,
    model_id: str,
    directory: Sequence[tuple[str, str | None, str]],
) -> str:
    """Label a model so identical names from different routes stay distinct.

    Mirrors the platform's own labeling (dsh-bridge-next ``labelModels``):
    when the same display name exists under several providers, append the
    provider; when one provider exposes several ids under the same name,
    append the model id as well. Labels are computed over the full directory
    so search and pagination cannot change them.
    """

    same_name = [entry for entry in directory if entry[0] == name]
    multiple_providers = any(entry[1] != provider for entry in same_name)
    duplicate_model = any(entry[1] == provider and entry[2] != model_id for entry in same_name)
    title = name
    if multiple_providers and provider:
        title += f"（{provider}）"
    if duplicate_model:
        title += f" [{model_id}]"
    return title


class PendingInteraction:
    """One open extension UI dialog waiting for a platform response."""

    def __init__(self, request: Mapping[str, Any]) -> None:
        self.request_id = str(request.get("id"))
        self.method = str(request.get("method"))
        self.title = str(request.get("title") or "Pi 需要你的输入")
        message = request.get("message")
        self.message = message if isinstance(message, str) else None
        options = request.get("options")
        self.options = (
            [str(option) for option in options if isinstance(option, str)]
            if isinstance(options, list)
            else []
        )
        timeout = request.get("timeout")
        self.timeout_ms = timeout if isinstance(timeout, int) else None

    @property
    def notice_id(self) -> str:
        return f"pi-ui-{self.request_id}"

    def as_notice(self, session_id: str, *, status: str = "open") -> SessionNotice:
        actions: list[dict[str, Any]] = []
        context: dict[str, Any] = {"method": self.method}
        if self.method == "confirm":
            actions = [
                {"actionId": "confirm", "label": "确认", "style": "primary"},
                {"actionId": "cancel", "label": "取消", "style": "secondary"},
            ]
        elif self.method == "select":
            actions = [
                {"actionId": option, "label": option, "style": "secondary"}
                for option in self.options
            ]
            if actions:
                actions[0]["style"] = "primary"
            actions.append({"actionId": "cancel", "label": "取消", "style": "secondary"})
        else:
            context["inputKind"] = "text"
            context["multiline"] = self.method == "editor"
            if isinstance(self.request_id, str):
                context["requestId"] = self.request_id
        return SessionNotice(
            notice_id=self.notice_id,
            session_id=session_id,
            runtime=RUNTIME,
            type="interaction",
            interaction_type=f"pi.{self.method}",
            title=self.title,
            message=self.message,
            severity="warning",
            status=status,
            response_required=status == "open",
            blocking={"scope": "session", "targetId": session_id} if status == "open" else None,
            actions=tuple(actions) if status == "open" else (),
            source={"runtime": RUNTIME, "component": "pi.extension-ui"},
            context=context,
        )

    def response_payload(
        self,
        action_id: str,
        input_data: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        data = dict(input_data or {})
        if action_id in ("cancel", "dismiss", "reject", "no"):
            if self.method == "confirm" and action_id in ("reject", "no"):
                return {"type": "extension_ui_response", "id": self.request_id, "confirmed": False}
            return {"type": "extension_ui_response", "id": self.request_id, "cancelled": True}
        if self.method == "confirm":
            confirmed = data.get("confirmed")
            if isinstance(confirmed, bool):
                return {
                    "type": "extension_ui_response",
                    "id": self.request_id,
                    "confirmed": confirmed,
                }
            return {
                "type": "extension_ui_response",
                "id": self.request_id,
                "confirmed": action_id in ("confirm", "yes", "allow", "approve"),
            }
        # select / input / editor all answer with a string value.
        value = data.get("value")
        if not isinstance(value, str):
            value = data.get("text") if isinstance(data.get("text"), str) else None
        if value is None and self.method == "select":
            value = action_id
        return {
            "type": "extension_ui_response",
            "id": self.request_id,
            "value": value if value is not None else "",
        }


class PiLiveSession:
    """A platform session backed by one running Pi RPC process."""

    def __init__(
        self,
        runtime: PiRuntime,
        platform_id: str,
        *,
        cwd: str,
        session_path: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.platform_id = platform_id
        self.cwd = cwd
        self.session_path = session_path
        self.process: PiRpcProcess | None = None
        self.session_file: str | None = session_path
        self.pi_session_id: str | None = None
        self.session_name: str | None = None
        self.model: Mapping[str, Any] | None = None
        self.thinking_level: str | None = None
        self.is_streaming = False
        self.is_compacting = False
        self.message_count = 0
        self.status_reason: str | None = None
        self.pending_ui: dict[str, PendingInteraction] = {}
        # (text, clientMessageId) pairs waiting for their projected user message.
        self.client_messages: list[tuple[str, str]] = []
        self.last_activity = time.monotonic()
        self._start_lock = asyncio.Lock()
        self.last_state_key: tuple[Any, ...] | None = None

    @property
    def external_id(self) -> str | None:
        return self.session_file or self.session_path

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.alive

    @property
    def status(self) -> str:
        if any(True for _ in self.pending_ui.values()):
            return "waiting_approval"
        if self.is_streaming or self.is_compacting:
            return "running"
        return "idle"

    def argv(self) -> list[str]:
        argv = [
            self.runtime.executable,
            "--mode",
            "rpc",
            "--session-dir",
            str(self.runtime.sessions_dir),
        ]
        if self.session_path:
            argv.extend(["--session", self.session_path])
        return argv

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self.alive:
                return
            process = PiRpcProcess(
                self.argv(),
                cwd=self.cwd,
                on_event=self._on_event,
                on_exit=self._handle_exit,
                request_timeout=self.runtime.request_timeout,
            )
            await process.start()
            self.process = process
            try:
                await self.refresh_state()
            except (PiRpcProcessExited, PiRpcRequestFailed, TimeoutError) as exc:
                logger.warning(
                    "pi session %s started but get_state failed: %s",
                    self.platform_id,
                    exc,
                )

    async def refresh_state(self) -> Mapping[str, Any]:
        process = self.process
        if process is None:
            raise PiRpcProcessExited("Pi RPC process is not running")
        data = response_data(await process.request({"type": "get_state"}))
        self.apply_state(data)
        return data

    def touch(self) -> None:
        """Record activity so the idle reclaim loop leaves this session alone."""

        self.last_activity = time.monotonic()

    def apply_state(self, data: Mapping[str, Any]) -> None:
        model = data.get("model")
        if isinstance(model, Mapping):
            self.model = dict(model)
        thinking = data.get("thinkingLevel")
        if isinstance(thinking, str):
            self.thinking_level = thinking
        self.is_streaming = data.get("isStreaming") is True
        self.is_compacting = data.get("isCompacting") is True
        session_file = data.get("sessionFile")
        if isinstance(session_file, str) and session_file:
            self.session_file = session_file
        session_id = data.get("sessionId")
        if isinstance(session_id, str) and session_id:
            self.pi_session_id = session_id
        name = data.get("sessionName")
        self.session_name = name if isinstance(name, str) and name else None
        count = data.get("messageCount")
        if isinstance(count, int) and not isinstance(count, bool):
            self.message_count = count

    async def command(
        self,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        await self.ensure_started()
        self.touch()
        process = self.process
        if process is None:
            raise PiRpcProcessExited("Pi RPC process is not running")
        return await process.request(payload, timeout=timeout)

    async def send_prompt(
        self,
        content: str,
        *,
        streaming_behavior: str | None = None,
        images: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        if streaming_behavior == "steer":
            payload: dict[str, Any] = {"type": "steer", "message": content}
            if images:
                payload["images"] = list(images)
            await self.command(payload)
            return
        payload = {"type": "prompt", "message": content}
        if images:
            payload["images"] = list(images)
        if streaming_behavior:
            payload["streamingBehavior"] = streaming_behavior
        await self.command(payload)

    async def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None:
            await process.close()

    async def _handle_exit(self, code: int | None) -> None:
        await self.runtime.handle_live_exit(self, code)

    async def _on_event(self, record: Mapping[str, Any]) -> None:
        await self.runtime.handle_live_event(self, record)

    def state_key(self) -> tuple[Any, ...]:
        return (
            self.status,
            _model_selection_id(self.model or {}),
            self.thinking_level,
        )


class PiRuntime(AgentRuntime):
    """AgentRuntime implementation for the Pi coding agent."""

    def __init__(self, config: RuntimeConfig, host: RuntimeHostClient) -> None:
        self.config = config
        self.host = host
        values = normalized_config_values(dict(config.values))
        self.executable = str(values["executablePath"])
        self.request_timeout = int(values["requestTimeoutMs"]) / 1000
        self._idle_timeout = float(values["idleTimeoutSeconds"])
        self.default_cwd = str(values["defaultCwd"])
        self.sessions_dir = Path(str(values["sessionsDir"]))
        self.directory = SessionDirectory(self.sessions_dir)
        self._live: dict[str, PiLiveSession] = {}
        self._live_locks: dict[str, asyncio.Lock] = {}
        self._known_paths: dict[str, str] = {}
        self._synced: dict[str, tuple[float, int]] = {}
        self._utility: PiRpcProcess | None = None
        self._catalog_revision = 0
        self._stopping = False
        self._reclaim_task: asyncio.Task[None] | None = None
        self._stale_reset_task: asyncio.Task[None] | None = None
        self._identity = RuntimeIdentity(
            runtime=RUNTIME,
            runtime_version="unknown",
            display_name="Pi Coding Agent",
            runtime_id=config.runtime_id,
        )

    # -- lifecycle ----------------------------------------------------------

    @property
    def identity(self) -> RuntimeIdentity:
        return self._identity

    async def start(self) -> None:
        version = await probe_pi_version(self.executable)
        if version is None:
            raise RuntimeUnavailableError(f"pi executable is unavailable: {self.executable!r}")
        self._identity = replace(self._identity, runtime_version=version)
        logger.info("pi runtime started version=%s", version)
        await self._publish_runtime_capabilities()
        self._stale_reset_task = asyncio.create_task(self._reset_stale_running_states())
        if self._idle_timeout > 0:
            self._reclaim_task = asyncio.create_task(self._reclaim_loop())

    async def _publish_runtime_capabilities(self) -> None:
        """Push runtime-scoped facts so the platform persists them.

        The connector's discovery publication drops runtime types outside its
        hard-coded allowlist, so pi's runtime-scoped entries would never reach
        the platform's persisted capability facts. Without them, sessions that
        never published their own facts project as unsupported. Publishing
        through the runtime capability channel merges them into that store.
        """

        try:
            await self.host.session_capabilities_update(await self.get_runtime_capabilities())
        except Exception:  # publishing must not block startup
            logger.exception("failed to publish pi runtime capabilities")

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._reclaim_task, self._stale_reset_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._reclaim_task = None
        self._stale_reset_task = None
        for live in list(self._live.values()):
            await live.stop()
        self._live.clear()
        self._live_locks.clear()
        if self._utility is not None:
            await self._utility.close()
            self._utility = None

    async def _reset_stale_running_states(self) -> None:
        """Re-announce idle for sessions left running by a previous process.

        A connector restart kills live pi processes before their exit state
        can be published, so the platform keeps showing "running" until the
        next turn. Right after start() this runtime owns no live process, so
        idle is accurate; the platform skips states that did not change.
        """

        try:
            summaries = await self.list_complete_session_inventory()
        except asyncio.CancelledError:
            raise
        except Exception:  # a failed scan must not break startup
            logger.exception("failed to list sessions for stale state reset")
            return
        reset = 0
        for summary in summaries:
            try:
                await self.host.session_state_update(
                    session_id=summary.session_id,
                    runtime=RUNTIME,
                    external_session_id=summary.external_session_id,
                    status="idle",
                    metadata={"sessionFile": summary.external_session_id},
                )
                reset += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to reset idle state for %s", summary.session_id)
        logger.info("reset idle state for %d sessions after restart", reset)

    async def _reclaim_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(RECLAIM_INTERVAL_SECONDS)
                await self._reclaim_idle_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:  # a failed pass must not kill the loop
                logger.exception("pi idle reclaim pass failed")

    async def _reclaim_idle_sessions(self) -> None:
        """Close pi processes whose sessions stayed idle past the timeout."""

        if self._idle_timeout <= 0:
            return
        now = time.monotonic()
        for session_id in list(self._live):
            live = self._live.get(session_id)
            if live is None:
                continue
            if now - live.last_activity < self._idle_timeout:
                continue
            if live.is_streaming or live.is_compacting or live.pending_ui:
                continue
            # Serialize with _ensure_live so a reviving turn cannot race the close.
            async with self._live_lock(session_id):
                if self._live.get(session_id) is not live:
                    continue
                if now - live.last_activity < self._idle_timeout:
                    continue
                if live.is_streaming or live.is_compacting or live.pending_ui:
                    continue
                self._live.pop(session_id, None)
            logger.info(
                "reclaimed idle pi session session_id=%s idle_seconds=%.0f",
                session_id,
                now - live.last_activity,
            )
            await live.stop()

    # -- discovery / inventory ---------------------------------------------

    async def list_sessions(
        self,
        limit: int = 100,
        cursor: str | None = None,
        force: bool = False,
    ) -> tuple[SessionMeta, ...]:
        _ = cursor, force
        summaries = await asyncio.to_thread(
            self.directory.list_sessions, limit if limit and limit > 0 else 0
        )
        return tuple(self._session_meta(summary) for summary in summaries)

    async def list_complete_session_inventory(
        self,
        page_size: int = 100,
        force: bool = False,
    ) -> tuple[SessionMeta, ...]:
        _ = page_size, force
        summaries = await asyncio.to_thread(self.directory.list_sessions, 0)
        return tuple(self._session_meta(summary) for summary in summaries)

    def _session_meta(self, summary: PiSessionSummary) -> SessionMeta:
        platform_id = platform_session_id(self.host.session_namespace, summary.path)
        self._known_paths[platform_id] = summary.path
        stamp = (summary.modified_at, summary.size)
        changed = self._synced.get(summary.path) != stamp
        metadata: dict[str, Any] = dict(summary.metadata())
        metadata["sync"] = {
            "changed": changed,
            "requires_timeline_sync": changed,
        }
        live = self._live.get(platform_id)
        if live is not None and live.session_name:
            metadata["sessionName"] = live.session_name
        return SessionMeta(
            session_id=platform_id,
            external_session_id=summary.path,
            runtime=RUNTIME,
            runtime_id=self.config.runtime_id,
            title=summary.title,
            cwd=summary.cwd,
            ordering_time=_iso_from_epoch(summary.modified_at),
            source_state=SessionSourceState(
                availability="available",
                observed_at=_iso_from_epoch(summary.modified_at),
                observation_origin="inventory",
            ),
            metadata=metadata,
        )

    # -- snapshots ----------------------------------------------------------

    async def get_session_snapshot(
        self,
        session_id: str,
        external_session_id: str | None = None,
        limit: int | None = None,
    ) -> RuntimeTimelineSnapshot:
        path = self._resolve_session_path(session_id, external_session_id)
        if path is None:
            return RuntimeTimelineSnapshot(
                session_id=session_id,
                external_session_id=external_session_id,
                runtime=RUNTIME,
                runtime_id=self.config.runtime_id,
                items=(),
                complete=False,
                metadata={"reason": "session file not found"},
            )
        doc = await asyncio.to_thread(load_session_doc, path)
        if doc is None:
            return RuntimeTimelineSnapshot(
                session_id=session_id,
                external_session_id=str(path),
                runtime=RUNTIME,
                runtime_id=self.config.runtime_id,
                items=(),
                complete=False,
                metadata={"reason": "session file unreadable"},
            )
        live = self._live.get(session_id)
        items = projection.project_session(
            doc.entries,
            session_id=session_id,
            external_session_id=doc.summary.path,
            client_messages=tuple(live.client_messages) if live is not None else (),
        )
        truncated = limit is not None and limit > 0
        if truncated:
            items = items[-limit:]
        self._synced[doc.summary.path] = (doc.summary.modified_at, doc.summary.size)
        self._known_paths[session_id] = doc.summary.path
        return RuntimeTimelineSnapshot(
            session_id=session_id,
            external_session_id=doc.summary.path,
            runtime=RUNTIME,
            runtime_id=self.config.runtime_id,
            items=items,
            # Only a full read is a Runtime-owned snapshot; a truncated tail must
            # not let the platform replace (and delete) the stored history.
            complete=not truncated,
            metadata={
                "itemCount": len(items),
                "piSessionId": doc.summary.session_id,
            },
        )

    # -- state / notices ----------------------------------------------------

    async def get_session_state(
        self,
        session_id: str,
        external_session_id: str | None = None,
    ) -> SessionState | None:
        live = self._live.get(session_id)
        if live is not None:
            return self._live_state(live)
        path = self._resolve_session_path(session_id, external_session_id)
        if path is None:
            return None
        return SessionState(
            session_id=session_id,
            external_session_id=str(path),
            runtime=RUNTIME,
            runtime_id=self.config.runtime_id,
            status="idle",
            metadata={"sessionFile": str(path)},
        )

    def _live_state(self, live: PiLiveSession) -> SessionState:
        selections: dict[str, str | None] = {}
        selection_id = _model_selection_id(live.model or {})
        if selection_id:
            selections["model"] = selection_id
        if live.thinking_level:
            selections["thinkingLevel"] = live.thinking_level
        status = live.status
        metadata: dict[str, Any] = {"sessionFile": live.session_file}
        if live.pi_session_id:
            metadata["piSessionId"] = live.pi_session_id
        if live.session_name:
            metadata["sessionName"] = live.session_name
        if live.pending_ui:
            metadata["pendingInteractions"] = len(live.pending_ui)
        return SessionState(
            session_id=live.platform_id,
            external_session_id=live.external_id,
            runtime=RUNTIME,
            runtime_id=self.config.runtime_id,
            status=status,  # type: ignore[arg-type]
            status_reason=live.status_reason,
            selections=selections,
            metadata=metadata,
        )

    async def get_session_notices(
        self,
        session_id: str,
        external_session_id: str | None = None,
    ) -> tuple[SessionNotice, ...]:
        _ = external_session_id
        live = self._live.get(session_id)
        if live is None:
            return ()
        return tuple(pending.as_notice(session_id) for pending in live.pending_ui.values())

    async def get_runtime_capabilities(self) -> RuntimeCapabilitySet:
        """Runtime-scoped capability facts advertised to the platform.

        The device-runtime capability endpoint (and the new-session composer
        that reads it) uses this set, so attachments must be advertised here as
        well as on individual sessions. It also gives the platform's session
        projection a runtime-scoped fallback for sessions that never published
        their own facts.
        """

        return RuntimeCapabilitySet(
            runtime=RUNTIME,
            revision=1,
            runtime_id=self.config.runtime_id,
            capabilities=tuple(
                RuntimeCapability(
                    capability_id=capability_id,
                    scope="runtime",
                    runtime=RUNTIME,
                    runtime_id=self.config.runtime_id,
                    metadata=(
                        {"allowedMimeTypes": list(PI_IMAGE_MIME_TYPES)}
                        if capability_id == CAPABILITY_RUNTIME_ATTACHMENT
                        else {}
                    ),
                )
                for capability_id in (
                    CAPABILITY_SESSION_SEND_MESSAGE,
                    CAPABILITY_SESSION_INTERRUPT,
                    CAPABILITY_SESSION_STEER,
                    CAPABILITY_SESSION_INTERACTION_APPROVAL,
                    CAPABILITY_SESSION_COMMANDS,
                    CAPABILITY_RUNTIME_ATTACHMENT,
                    CAPABILITY_CATALOG_MODEL,
                    CAPABILITY_CATALOG_EFFORT,
                )
            ),
            metadata={"source": "pi.runtime"},
        )

    async def get_session_capabilities(
        self,
        session_id: str,
        external_session_id: str | None = None,
    ) -> RuntimeCapabilitySet:
        _ = external_session_id
        return self._session_capability_set(session_id)

    def _session_capability_set(self, session_id: str) -> RuntimeCapabilitySet:
        """Capability facts the pi runtime exposes for one platform session."""

        capabilities = tuple(
            RuntimeCapability(
                capability_id=capability_id,
                scope=scope,  # type: ignore[arg-type]
                runtime=RUNTIME,
                runtime_id=self.config.runtime_id,
                session_id=session_id if scope == "session" else None,
                metadata=(
                    {"allowedMimeTypes": list(PI_IMAGE_MIME_TYPES)}
                    if capability_id == CAPABILITY_RUNTIME_ATTACHMENT
                    else {}
                ),
            )
            for capability_id, scope in (
                (CAPABILITY_SESSION_SEND_MESSAGE, "session"),
                (CAPABILITY_SESSION_INTERRUPT, "session"),
                (CAPABILITY_SESSION_STEER, "session"),
                (CAPABILITY_SESSION_INTERACTION_APPROVAL, "session"),
                (CAPABILITY_SESSION_COMMANDS, "session"),
                (CAPABILITY_RUNTIME_ATTACHMENT, "session"),
                (CAPABILITY_CATALOG_MODEL, "runtime"),
                (CAPABILITY_CATALOG_EFFORT, "session"),
            )
        )
        return RuntimeCapabilitySet(
            runtime=RUNTIME,
            revision=1,
            capabilities=capabilities,
            session_id=session_id,
            runtime_id=self.config.runtime_id,
            metadata={"source": "pi.runtime"},
        )

    async def _publish_session_capabilities(self, session_id: str) -> None:
        """Push capability facts so the platform's cached set stays current.

        The platform falls back to persisted capability facts when a live read
        fails, and only runtime capability notifications feed that store. The
        built-in runtimes publish on every state change; pi must do the same
        or snapshot and websocket projections keep an empty capability set.
        """

        try:
            await self.host.session_capabilities_update(self._session_capability_set(session_id))
        except Exception:
            logger.exception("failed to publish pi session capabilities for %s", session_id)

    # -- catalogs -----------------------------------------------------------

    async def list_model_catalog(
        self,
        query: str | None = None,
        limit: int = 100,
    ) -> RuntimeModelCatalog:
        data = response_data(await self._utility_command({"type": "get_available_models"}))
        # Pi allows the same model name under several providers; the platform
        # rejects catalogs with duplicate ids, so ids are provider-qualified
        # and titles carry the provider when a name is ambiguous.
        entries: list[tuple[Mapping[str, Any], str, str, str | None, str]] = []
        for raw in _as_list(data.get("models")):
            if not isinstance(raw, Mapping):
                continue
            model_id = raw.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            catalog_id = _model_selection_id(raw) or model_id
            name = raw.get("name")
            name = name if isinstance(name, str) and name else model_id
            provider = raw.get("provider")
            provider = provider if isinstance(provider, str) and provider else None
            entries.append((raw, catalog_id, name, provider, model_id))
        directory = [(name, provider, model_id) for _, _, name, provider, model_id in entries]
        models: list[RuntimeModelItem] = []
        for raw, catalog_id, name, provider, model_id in entries:
            title = _model_display_title(name, provider, model_id, directory)
            haystack = f"{catalog_id} {name} {title}".lower()
            if query and query.lower() not in haystack:
                continue
            models.append(
                RuntimeModelItem(
                    id=catalog_id,
                    title=title,
                    selection_id=catalog_id,
                    description=provider,
                    metadata={
                        "provider": raw.get("provider"),
                        "contextWindow": raw.get("contextWindow"),
                        "reasoning": raw.get("reasoning") is True,
                    },
                )
            )
            if len(models) >= limit:
                break
        return RuntimeModelCatalog(
            runtime=RUNTIME,
            revision=self._next_catalog_revision(),
            models=tuple(models),
            runtime_id=self.config.runtime_id,
        )

    async def list_permission_catalog(
        self,
        query: str | None = None,
        limit: int = 100,
    ) -> RuntimePermissionCatalog:
        _ = query, limit
        # Pi permission presets are not modeled; the catalog stays empty and
        # keeps revision 0 so the platform stores it once as idempotent.
        return RuntimePermissionCatalog(
            runtime=RUNTIME,
            revision=0,
            permissions=(),
            runtime_id=self.config.runtime_id,
        )

    def _next_catalog_revision(self) -> int:
        """Monotonic revision for runtime catalogs.

        The platform ignores a catalog whose revision does not exceed the
        stored one, so a per-process counter stalls every update after a
        connector restart; a millisecond clock stays monotonic across them.
        """

        self._catalog_revision = max(int(time.time() * 1000), self._catalog_revision + 1)
        return self._catalog_revision

    # -- commands -----------------------------------------------------------

    async def list_commands(
        self,
        session_id: str,
        external_session_id: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> tuple[RuntimeCommand, ...]:
        _ = external_session_id
        live = self._live.get(session_id)
        data = (
            await live.command({"type": "get_commands"})
            if live is not None
            else await self._utility_command({"type": "get_commands"})
        )
        commands: list[RuntimeCommand] = []
        for raw in _as_list(response_data(data).get("commands")):
            if not isinstance(raw, Mapping):
                continue
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                continue
            if query and query.lower() not in name.lower():
                continue
            commands.append(
                RuntimeCommand(
                    id=name,
                    title=name,
                    description=raw.get("description")
                    if isinstance(raw.get("description"), str)
                    else None,
                    scope="session",
                    metadata={"source": raw.get("source")},
                )
            )
            if len(commands) >= limit:
                break
        return tuple(commands)

    async def execute_command(
        self,
        session_id: str,
        command: str,
        external_session_id: str | None = None,
        raw: str | None = None,
        args: tuple[str, ...] = (),
    ) -> RuntimeCommandResult:
        live = await self._ensure_live(session_id, external_session_id, None)
        text = raw if raw else "/" + command + (" " + " ".join(args) if args else "")
        await live.send_prompt(text)
        return RuntimeCommandResult(
            command=command,
            ok=True,
            message=f"已发送 {text}",
            result={"sessionId": session_id},
        )

    # -- session operations -------------------------------------------------

    async def create_and_start_session(
        self,
        session_id: str,
        content: str,
        title: str | None = None,
        cwd: str | None = None,
        selections: Mapping[str, str | None] | None = None,
        attachments: tuple[RuntimeAttachment, ...] = (),
        client_message_id: str | None = None,
        runtime_options: Mapping[str, Any] | None = None,
    ) -> RuntimeOperationResult:
        _ = runtime_options
        images = await self._attachment_images(session_id, attachments)
        workdir = self._resolve_cwd(cwd)
        live = PiLiveSession(self, session_id, cwd=workdir)
        if client_message_id:
            live.client_messages.append((content, client_message_id))
        self._live[session_id] = live
        await live.ensure_started()
        if is_meaningful_title(title):
            await live.command({"type": "set_session_name", "name": title})
        if selections:
            await self._apply_selections(live, selections)
        await live.send_prompt(content, images=images)
        await self._push_meta(live)
        await self._push_state(live, force=True)
        return RuntimeOperationResult(
            result={"sessionId": session_id, "externalSessionId": live.external_id}
        )

    async def start_turn(
        self,
        session_id: str,
        external_session_id: str | None,
        content: str,
        selections: Mapping[str, str | None] | None = None,
        attachments: tuple[RuntimeAttachment, ...] = (),
        client_message_id: str | None = None,
        cwd: str | None = None,
    ) -> RuntimeOperationResult:
        images = await self._attachment_images(session_id, attachments)
        live = await self._ensure_live(session_id, external_session_id, cwd)
        logger.info(
            "pi turn start session_id=%s client_message_id=%s",
            session_id,
            client_message_id,
        )
        if client_message_id:
            live.client_messages.append((content, client_message_id))
        if selections:
            await self._apply_selections(live, selections)
        behavior = "followUp" if (live.is_streaming or live.is_compacting) else None
        await live.send_prompt(content, streaming_behavior=behavior, images=images)
        return RuntimeOperationResult(
            result={"sessionId": session_id, "queued": behavior is not None}
        )

    async def steer_turn(
        self,
        session_id: str,
        external_session_id: str | None,
        content: str,
        attachments: tuple[RuntimeAttachment, ...] = (),
        client_message_id: str | None = None,
    ) -> RuntimeOperationResult:
        images = await self._attachment_images(session_id, attachments)
        live = await self._ensure_live(session_id, external_session_id, None)
        if client_message_id:
            live.client_messages.append((content, client_message_id))
        if live.is_streaming:
            await live.send_prompt(content, streaming_behavior="steer", images=images)
            return RuntimeOperationResult(result={"sessionId": session_id, "steered": True})
        await live.send_prompt(content, images=images)
        return RuntimeOperationResult(result={"sessionId": session_id, "steered": False})

    async def interrupt_session(
        self,
        session_id: str,
        reason: str | None = None,
    ) -> RuntimeOperationResult:
        _ = reason
        live = self._live.get(session_id)
        if live is None or not live.alive:
            return RuntimeOperationResult(result={"sessionId": session_id, "noop": True})
        if not (live.is_streaming or live.is_compacting):
            return RuntimeOperationResult(result={"sessionId": session_id, "noop": True})
        await live.command({"type": "abort"}, timeout=max(self.request_timeout, 120.0))
        return RuntimeOperationResult(result={"sessionId": session_id})

    async def update_session_selections(
        self,
        session_id: str,
        external_session_id: str | None,
        selections: Mapping[str, str | None],
    ) -> RuntimeOperationResult:
        live = await self._ensure_live(session_id, external_session_id, None)
        await self._apply_selections(live, selections)
        await self._push_state(live, force=True)
        return RuntimeOperationResult(result={"sessionId": session_id})

    async def respond_interaction(
        self,
        session_id: str,
        notice_id: str,
        action_id: str,
        input_data: Mapping[str, Any] | None = None,
    ) -> RuntimeOperationResult:
        live = self._live.get(session_id)
        if live is None:
            return RuntimeOperationResult(
                ok=False,
                code="pi_interaction_expired",
                message="该交互已失效（会话进程不在运行）。",
            )
        request_id = notice_id.removeprefix("pi-ui-")
        pending = live.pending_ui.get(request_id)
        if pending is None:
            return RuntimeOperationResult(
                ok=False,
                code="pi_interaction_expired",
                message="该交互已处理或已失效。",
            )
        payload = pending.response_payload(action_id, input_data)
        assert live.process is not None
        await live.process.notify(payload)
        live.pending_ui.pop(request_id, None)
        await self.host.notice_upsert(pending.as_notice(session_id, status="resolved"))
        await self._push_state(live, force=True)
        return RuntimeOperationResult(
            result={"resolved": True, "noticeId": notice_id, "sessionId": session_id}
        )

    # -- live event handling ------------------------------------------------

    async def handle_live_event(self, live: PiLiveSession, record: Mapping[str, Any]) -> None:
        live.touch()
        event_type = record.get("type")
        if event_type == "extension_ui_request":
            await self._handle_ui_request(live, record)
            return
        if event_type == "message_end":
            message = record.get("message")
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                live.message_count += 1
            return
        if event_type == "agent_start":
            live.status_reason = None
            live.is_streaming = True
            await self._push_state(live)
            return
        if event_type == "agent_settled":
            live.is_streaming = False
            live.is_compacting = False
            live.status_reason = None
            await self._push_state(live, force=True)
            await self._publish_timeline(live)
            await self._push_meta(live)
            try:
                await self.host.session_turn_ended(
                    session_id=live.platform_id,
                    runtime=RUNTIME,
                    external_session_id=live.external_id,
                    outcome="completed",
                )
            except Exception:
                logger.exception("failed to publish turn end for %s", live.platform_id)
            return
        if event_type == "compaction_start":
            live.is_compacting = True
            await self._push_state(live)
            return
        if event_type == "compaction_end":
            live.is_compacting = False
            await self._push_state(live, force=True)
            return
        if event_type == "thinking_level_changed":
            level = record.get("level")
            if isinstance(level, str):
                live.thinking_level = level
                await self._push_state(live)
            return
        if event_type == "session_info_changed":
            name = record.get("name")
            live.session_name = name if isinstance(name, str) and name else None
            await self._push_meta(live)
            return
        if event_type == "auto_retry_start":
            message = record.get("errorMessage")
            live.status_reason = f"retrying: {message}" if isinstance(message, str) else "retrying"
            await self._push_state(live, force=True)
            return
        if event_type == "extension_error":
            logger.warning(
                "pi extension error session=%s extension=%s event=%s",
                live.platform_id,
                record.get("extensionPath"),
                record.get("event"),
            )

    async def handle_live_exit(self, live: PiLiveSession, code: int | None) -> None:
        logger.info("pi session %s process exited code=%s", live.platform_id, code)
        if self._stopping:
            return
        live.is_streaming = False
        live.is_compacting = False
        for pending in list(live.pending_ui.values()):
            await self.host.notice_upsert(pending.as_notice(live.platform_id, status="resolved"))
        live.pending_ui.clear()
        try:
            await self.host.session_state_update(
                session_id=live.platform_id,
                runtime=RUNTIME,
                external_session_id=live.external_id,
                status="idle",
                metadata={
                    "sessionFile": live.session_file,
                    "processExitCode": code,
                },
            )
        except Exception:
            logger.exception("failed to publish exit state for %s", live.platform_id)

    async def _handle_ui_request(self, live: PiLiveSession, record: Mapping[str, Any]) -> None:
        method = record.get("method")
        if method in DIALOG_METHODS:
            pending = PendingInteraction(record)
            live.pending_ui[pending.request_id] = pending
            await self.host.notice_upsert(pending.as_notice(live.platform_id))
            await self._push_state(live, force=True)
            return
        if method == "notify":
            message = record.get("message")
            if not isinstance(message, str) or not message:
                return
            notify_type = record.get("notifyType")
            severity = notify_type if notify_type in ("info", "warning", "error") else "info"
            request_id = str(record.get("id"))
            await self.host.notice_upsert(
                SessionNotice(
                    notice_id=f"pi-notify-{request_id}",
                    session_id=live.platform_id,
                    runtime=RUNTIME,
                    type="notification",
                    title=message,
                    severity=severity,  # type: ignore[arg-type]
                    status="open",
                    source={"runtime": RUNTIME, "component": "pi.extension-ui"},
                )
            )
            return
        logger.debug("ignoring pi extension UI method %r", method)

    # -- internal helpers ---------------------------------------------------

    async def _ensure_live(
        self,
        session_id: str,
        external_session_id: str | None,
        cwd: str | None,
    ) -> PiLiveSession:
        live = self._live.get(session_id)
        if live is None:
            # Resolve the target first; installation below is synchronous, so two
            # concurrent RPCs for the same session share one live session (and one
            # pi process) instead of overwriting each other.
            session_path: str | None = None
            workdir = self._resolve_cwd(cwd)
            resolved = self._resolve_session_path(session_id, external_session_id)
            if resolved is not None:
                session_path = str(resolved)
                doc_cwd = await asyncio.to_thread(self._session_cwd, resolved)
                if doc_cwd:
                    workdir = doc_cwd
            elif external_session_id:
                session_path = external_session_id
            async with self._live_lock(session_id):
                live = self._live.get(session_id)
                if live is None:
                    live = PiLiveSession(
                        self,
                        session_id,
                        cwd=workdir,
                        session_path=session_path,
                    )
                    self._live[session_id] = live
        await live.ensure_started()
        return live

    def _live_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._live_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._live_locks[session_id] = lock
        return lock

    @staticmethod
    def _session_cwd(path: Path) -> str | None:
        try:
            with path.open("rb") as handle:
                header = json.loads(handle.readline())
        except (OSError, ValueError):
            return None
        cwd = header.get("cwd") if isinstance(header, Mapping) else None
        if isinstance(cwd, str) and Path(cwd).is_dir():
            return cwd
        return None

    def _resolve_cwd(self, cwd: str | None) -> str:
        candidate = Path(cwd).expanduser() if cwd else Path(self.default_cwd).expanduser()
        if not candidate.is_dir():
            raise RuntimeInvalidRequestError(f"working directory does not exist: {candidate}")
        return str(candidate)

    def _resolve_session_path(
        self,
        session_id: str,
        external_session_id: str | None,
    ) -> Path | None:
        candidates: list[str] = []
        if external_session_id:
            external_path = Path(external_session_id)
            if self._within_sessions_root(external_path):
                candidates.append(external_session_id)
            else:
                logger.warning("ignoring session path outside sessionsDir: %s", external_session_id)
        live = self._live.get(session_id)
        if live is not None and live.session_file:
            candidates.append(live.session_file)
        known = self._known_paths.get(session_id)
        if known:
            candidates.append(known)
        for candidate in candidates:
            path = Path(candidate)
            if path.is_file():
                return path
        # Fall back to a scan when the platform id is not yet known.
        for summary in self.directory.list_sessions(limit=0):
            if platform_session_id(self.host.session_namespace, summary.path) == session_id:
                self._known_paths[session_id] = summary.path
                return Path(summary.path)
        return None

    def _within_sessions_root(self, path: Path) -> bool:
        """Whether a platform-supplied path stays inside the sessions directory."""

        try:
            path.resolve().relative_to(self.directory.root.resolve())
        except (OSError, ValueError):
            return False
        return True

    async def _attachment_images(
        self,
        session_id: str,
        attachments: tuple[RuntimeAttachment, ...],
    ) -> tuple[Mapping[str, Any], ...]:
        """Download image attachments and reshape them into Pi RPC image content.

        Pi accepts images as base64 ``ImageContent`` entries on ``prompt`` and
        ``steer`` commands. Non-image attachments are skipped so a mixed
        selection still delivers the images Pi can consume.
        """

        if not attachments:
            return ()
        images: list[Mapping[str, Any]] = []
        for attachment in attachments:
            media_type = (attachment.media_type or "").strip().lower()
            if not media_type.startswith("image/"):
                logger.warning(
                    "pi runtime skipped non-image attachment file_id=%s media_type=%s",
                    attachment.file_id,
                    attachment.media_type,
                )
                continue
            try:
                downloaded = await self.host.attachment_download(session_id, attachment.file_id)
            except Exception:  # skipped attachments must not block the turn
                logger.exception(
                    "pi attachment download failed file_id=%s",
                    attachment.file_id,
                )
                continue
            resolved_media_type = (downloaded.media_type or media_type).strip().lower()
            images.append(
                {
                    "type": "image",
                    "data": base64.b64encode(downloaded.content).decode("ascii"),
                    "mimeType": resolved_media_type,
                }
            )
        return tuple(images)

    async def _apply_selections(
        self,
        live: PiLiveSession,
        selections: Mapping[str, str | None],
    ) -> None:
        for key, value in selections.items():
            if value is None:
                continue
            if key == "model":
                provider, model_id = _split_model_selection(value)
                payload: dict[str, Any] = {"type": "set_model", "modelId": model_id}
                if provider:
                    payload["provider"] = provider
                data = response_data(await live.command(payload))
                nested = data.get("model")
                model = nested if isinstance(nested, Mapping) else data
                if isinstance(model.get("id"), str):
                    live.model = dict(model)
            elif key in ("thinkingLevel", "effort", "reasoningEffort"):
                await live.command({"type": "set_thinking_level", "level": value})
                live.thinking_level = value
            else:
                logger.debug("ignoring unknown selection key %r", key)

    async def _push_state(self, live: PiLiveSession, *, force: bool = False) -> None:
        key = live.state_key()
        if not force and key == live.last_state_key:
            return
        state = self._live_state(live)
        try:
            await self.host.session_state_update(
                session_id=state.session_id,
                runtime=RUNTIME,
                status=state.status,  # type: ignore[arg-type]
                selections=state.selections,
                external_session_id=state.external_session_id,
                status_reason=state.status_reason,
                error=state.error,
                metadata=state.metadata,
            )
            live.last_state_key = key
        except Exception:
            logger.exception("failed to push pi session state for %s", live.platform_id)
            return
        await self._publish_session_capabilities(live.platform_id)

    async def _push_meta(self, live: PiLiveSession) -> None:
        path = live.session_file or live.session_path
        summary = await asyncio.to_thread(self.directory.find_by_path, path) if path else None
        try:
            await self.host.session_meta_upsert(
                session_id=live.platform_id,
                runtime=RUNTIME,
                external_session_id=path,
                title=(summary.title if summary is not None else live.session_name),
                cwd=(summary.cwd if summary is not None else live.cwd),
                ordering_time=(
                    _iso_from_epoch(summary.modified_at) if summary is not None else None
                ),
                metadata=(summary.metadata() if summary is not None else {"piSessionFile": path}),
            )
        except Exception:
            logger.exception("failed to push pi session meta for %s", live.platform_id)

    async def _publish_timeline(self, live: PiLiveSession) -> None:
        path = live.session_file or live.session_path
        if not path or not Path(path).is_file():
            return
        snapshot = await self.get_session_snapshot(live.platform_id, path)
        if not snapshot.complete:
            return
        try:
            await self.host.timeline_sync(
                session_id=live.platform_id,
                runtime=RUNTIME,
                items=snapshot.items,
                external_session_id=snapshot.external_session_id,
                complete=True,
            )
        except Exception:
            logger.exception("failed to push pi timeline for %s", live.platform_id)

    # -- utility process ----------------------------------------------------

    async def _ensure_utility(self) -> PiRpcProcess:
        if self._utility is not None and self._utility.alive:
            return self._utility
        process = PiRpcProcess(
            [self.executable, "--mode", "rpc", "--no-session"],
            cwd=self.default_cwd,
            request_timeout=self.request_timeout,
        )
        await process.start()
        self._utility = process
        return process

    async def _utility_command(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        process = await self._ensure_utility()
        return await process.request(payload)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
