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
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
    RuntimeTimelineItem,
    RuntimeTimelineSnapshot,
    RuntimeUnavailableError,
    SessionMeta,
    SessionNotice,
    SessionSourceState,
    SessionState,
    attachment_target,
    attachments_root,
    session_attachments_dir,
)
from connector.runtime_protocol.host import RuntimeHostClient
from connector.runtime_protocol.timeline import TimelineSource, timeline_content_hash

from pi_aa import projection
from pi_aa.config import normalized_config_values, probe_pi_version
from pi_aa.rpc import (
    PiRpcProcess,
    PiRpcProcessExited,
    PiRpcRequestFailed,
    PiRpcTimeout,
    response_data,
)
from pi_aa.sessions import (
    PiSessionSummary,
    SessionDirectory,
    is_meaningful_title,
    load_session_doc,
)

logger = logging.getLogger(__name__)

# Assistant output and tool activity are coalesced per live session: one flush
# loop pushes the current snapshot immediately and then at most once per
# interval, so a burst of deltas cannot turn into a burst of platform upserts.
STREAM_FLUSH_INTERVAL_SECONDS = 0.3

# Optimistic sends are reconciled by client message id; the pairs live in the
# session transcript projection. Keep the most recent ones persisted per
# session so a connector restart cannot orphan the echo of an in-flight send.
CLIENT_MESSAGE_BINDINGS_VERSION = 1
MAX_CLIENT_MESSAGE_BINDINGS_PER_SESSION = 200

# The platform may empty or stale its runtime-state caches when the backend
# restarts (for example the server host rebooting) while this connector keeps
# running. Re-announcing every session's state converges them again; repeated
# reconnects within this window skip the full inventory scan.
REANNOUNCE_MIN_INTERVAL_SECONDS = 60.0

RUNTIME = "pi"
PLATFORM_SESSION_PREFIX = "sess_pi_"
# Id prefix for the disabled heading entries that group catalog models by
# provider; it must not collide with the provider-qualified model ids.
MODEL_GROUP_ID_PREFIX = "pi-aa-group:"

# How often the idle reclaim loop inspects live sessions.
RECLAIM_INTERVAL_SECONDS = 15.0

# How often the attachment cleanup loop expires inactive session attachment
# directories (files older than the retention window, then empty directories).
ATTACHMENT_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60

# Pi consumes images as base64 ``ImageContent``; these are the formats the
# model providers behind Pi accept in practice.
PI_IMAGE_MIME_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)

# Non-image attachments are copied into the connector's per-session attachment
# directory and referenced by path in the prompt. Anything larger is skipped
# so a single upload cannot exhaust connector memory or disk.
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024

DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})

# Pi's TUI-only slash commands never show up in the ``get_commands`` RPC
# payload, so the connector proxies them as regular platform commands and
# performs the matching RPC call itself.
BUILTIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("compact", "压缩当前会话上下文，可带自定义指令"),
    ("name", "设置或查看会话名称"),
    ("stats", "查看 token/成本/上下文占用"),
    ("export", "导出会话为 HTML 文件"),
)
BUILTIN_COMMAND_NAMES = frozenset(name for name, _ in BUILTIN_COMMANDS)

# The AA server validates ``SessionCommandRequest.command`` against
# ``^[A-Za-z][A-Za-z0-9_-]*$`` with a 64 character cap, while pi names skills
# ``skill:<name>`` and disambiguates duplicate extensions with ``:<n>``.
# Platform command ids are sanitized to that alphabet; the pi-native name stays
# on the command title and in ``PiRuntime._command_native_names`` for dispatch.
COMMAND_ID_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")
COMMAND_ID_MAX_LENGTH = 64
COMMAND_ID_FALLBACK_PREFIX = "cmd-"


def _platform_command_id(name: str, taken: set[str]) -> str:
    """Return a unique, platform-safe command id for a pi command name.

    Unsupported characters become ``-``; a name that would not start with a
    letter gets the ``cmd-`` prefix; a collision with a builtin or an earlier
    command gets a numeric suffix. The caller keeps the native name in the
    command title and in the runtime's id-to-name map.
    """

    sanitized = COMMAND_ID_UNSAFE_CHARS.sub("-", name)
    if not sanitized or not sanitized[0].isalpha():
        sanitized = COMMAND_ID_FALLBACK_PREFIX + sanitized
    sanitized = sanitized[:COMMAND_ID_MAX_LENGTH]
    if sanitized not in taken:
        return sanitized
    base = sanitized
    suffix = 2
    while True:
        tail = f"-{suffix}"
        candidate = base[: COMMAND_ID_MAX_LENGTH - len(tail)] + tail
        if candidate not in taken:
            return candidate
        suffix += 1


def _restore_native_command_text(raw: str, command: str, native: str) -> str:
    """Swap the platform command token in a raw line for the pi-native name.

    The client builds ``raw`` from the platform id (``/skill-pdf-tools now``);
    when sanitizing changed the id, dispatching that line verbatim would reach
    pi as an unknown command. Only the leading ``/<id>`` token is rewritten;
    the argument text is forwarded untouched. Lines that do not start with the
    platform id (or whose id already is native) are returned unchanged.
    """

    if native == command:
        return raw
    prefix = "/" + command
    if not raw.lower().startswith(prefix.lower()):
        return raw
    if len(raw) > len(prefix) and not raw[len(prefix)].isspace():
        return raw
    return "/" + native + raw[len(prefix) :]


# Fields of pi's per-command ``sourceInfo`` block that are stable metadata for
# the platform; the rest (if pi ever adds any) is dropped, not forwarded.
COMMAND_SOURCE_INFO_KEYS = ("path", "source", "scope", "origin", "baseDir")


def _command_source_info(raw: Mapping[str, Any]) -> dict[str, str] | None:
    """Pick the known string fields of a command's ``sourceInfo`` block.

    Returns ``None`` when the block is missing or has no usable fields, so
    callers can skip the metadata key altogether instead of carrying an empty
    dict; non-string values and unknown keys are ignored rather than raised.
    """

    info = raw.get("sourceInfo")
    if not isinstance(info, Mapping):
        return None
    picked = {key: info[key] for key in COMMAND_SOURCE_INFO_KEYS if isinstance(info.get(key), str)}
    return picked or None


# Warn once when the model context passes this utilization; dropping below it
# (or pi no longer reporting usage) resolves the same notice.
CONTEXT_WARNING_PERCENT = 90

# Compacting a long transcript can take minutes, far past the configured
# request timeout.
COMPACT_REQUEST_TIMEOUT_SECONDS = 180.0

# RPC failures builtin handlers translate into ``ok=False`` results instead
# of letting them escape into the platform command dispatch.
BUILTIN_RPC_ERRORS = (PiRpcProcessExited, PiRpcRequestFailed, PiRpcTimeout, TimeoutError)

# Notice.interactionType is validated against the server's ``InteractionType``
# literal (server ``agent_server/core/models.py``; identical in 2.0.0 and
# 2.0.3), which rejects pi's private ``pi.<method>`` values outright. Collapse
# pi's extension UI methods onto that enum; the exact pi method stays available
# in the notice context under "method".
SERVER_INTERACTION_TYPE_BY_METHOD: dict[str, str] = {
    "confirm": "confirmation",
    "select": "input_request",
    "input": "input_request",
    "editor": "input_request",
}
SERVER_INTERACTION_TYPE_UNKNOWN = "unknown"


def _merge_client_message_pairs(
    *groups: Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Merge client message pairs, keeping the newest entry per (text, id)."""

    seen: set[tuple[str, str]] = set()
    merged: list[tuple[str, str]] = []
    for group in groups:
        for text, client_message_id in group:
            marker = (text.strip(), client_message_id)
            if marker in seen:
                continue
            seen.add(marker)
            merged.append((text, client_message_id))
    return tuple(merged)


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
    """Label a model so ids under one name stay distinct.

    Providers already act as group headings in the picker, so titles do not
    repeat them; only the model id is appended when one provider exposes
    several ids under the same name. Labels are computed over the full
    directory so search and pagination cannot change them.
    """

    same_name = [entry for entry in directory if entry[0] == name]
    duplicate_model = any(entry[1] == provider and entry[2] != model_id for entry in same_name)
    title = name
    if duplicate_model:
        title += f" [{model_id}]"
    return title


def _model_group_item(provider: str) -> RuntimeModelItem:
    """A disabled heading entry that groups a provider's models in the picker.

    The platform renders catalogs as a flat list and clients treat ``enabled``
    as the clickability gate, so a disabled entry named after the provider
    doubles as a section heading. The unique id keeps the platform's catalog
    validation happy.
    """

    group_id = f"{MODEL_GROUP_ID_PREFIX}{provider}"
    return RuntimeModelItem(
        id=group_id,
        title=provider,
        selection_id=group_id,
        enabled=False,
        metadata={"provider": provider, "group": True},
    )


def _prompt_with_attachment_notes(
    content: str,
    notes: Sequence[tuple[str, str]],
) -> str:
    """Append saved non-image attachment references to the prompt text."""

    if not notes:
        return content
    lines = ["用户上传了以下文件附件，已保存到本机，可用工具读取："]
    lines.extend(f"- {name}: {path}" for name, path in notes)
    return content + "\n\n" + "\n".join(lines)


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
        self.timeout_ms: float | None = (
            float(timeout)
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
            else None
        )
        # pi auto-resolves a dialog that carries a timeout and emits no event
        # when it fires (docs/rpc-extension-ui.md), so the runtime records the
        # deadline itself to clear the pending entry later. A zero timeout
        # means "no timeout" on the pi side (its ``if (opts?.timeout)``).
        self.deadline: float | None = (
            time.monotonic() + self.timeout_ms / 1000.0 if self.timeout_ms else None
        )

    def expired(self, now: float | None = None) -> bool:
        """Whether pi has already auto-resolved this dialog on its side."""

        if self.deadline is None:
            return False
        return (time.monotonic() if now is None else now) >= self.deadline

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
            interaction_type=SERVER_INTERACTION_TYPE_BY_METHOD.get(
                self.method, SERVER_INTERACTION_TYPE_UNKNOWN
            ),
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


@dataclass(slots=True)
class _StreamMessage:
    """Buffers for the assistant message currently being streamed."""

    index: int
    text: dict[int, str] = field(default_factory=dict)
    thinking: dict[int, str] = field(default_factory=dict)

    def text_value(self) -> str:
        return "\n\n".join(self.text[key] for key in sorted(self.text))

    def thinking_value(self) -> str:
        return "\n\n".join(self.thinking[key] for key in sorted(self.thinking))


@dataclass(slots=True)
class _StreamTool:
    """Live state of one tool call between start and end."""

    call_id: str
    name: str
    content: dict[str, Any]
    output: str = ""
    status: str = "running"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _context_usage_percent(data: Mapping[str, Any]) -> float | None:
    """The ``contextUsage.percent`` value, when pi reports one."""

    usage = data.get("contextUsage")
    if not isinstance(usage, Mapping):
        return None
    percent = usage.get("percent")
    return float(percent) if _is_number(percent) else None


def _format_session_stats(data: Mapping[str, Any]) -> str:
    """Human-readable one-liner for the ``stats`` command result."""

    parts: list[str] = []
    tokens = data.get("tokens")
    total: Any = None
    if isinstance(tokens, Mapping):
        total = tokens.get("total")
        if not _is_number(total):
            fallback = [
                value
                for value in (
                    tokens.get("input"),
                    tokens.get("output"),
                    tokens.get("cacheRead"),
                    tokens.get("cacheWrite"),
                )
                if _is_number(value)
            ]
            total = sum(fallback) if fallback else None
    if _is_number(total):
        parts.append(f"tokens {total}")
    cost = data.get("cost")
    if _is_number(cost):
        parts.append(f"成本 ${cost:.6f}")
    percent = _context_usage_percent(data)
    if percent is not None:
        parts.append(f"上下文 {percent:g}%")
    return "，".join(parts) if parts else "暂无法读取会话统计"


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
        # Latest ``get_session_stats`` payload, cached so state reads do not
        # need an RPC of their own.
        self.session_stats: dict[str, Any] | None = None
        self.pending_ui: dict[str, PendingInteraction] = {}
        # pi extension ``setStatus`` entries, keyed by statusKey; published as
        # ``extensionStatus`` metadata and cleared by an event without text.
        self.extension_status: dict[str, str] = {}
        # Fire-and-forget ``notify`` toasts still open for this run, keyed by
        # notice id; settle and process exit resolve them.
        self.open_notifications: dict[str, SessionNotice] = {}
        # (text, clientMessageId) pairs waiting for their projected user message.
        self.client_messages: list[tuple[str, str]] = []
        # Queued steering/follow-up texts pi reports via ``queue_update``; the
        # event carries the complete queues, so these are replaced wholesale.
        self.steering_queue: list[str] = []
        self.followup_queue: list[str] = []
        self.pending_message_count = 0
        # Steering/follow-up texts pi returned from the last ``clear_queue``.
        self.cleared_queue: dict[str, list[str]] | None = None
        # Streaming assistant/tool state. ``stream_task`` is the single flush
        # loop per session; deltas themselves never upsert an item.
        self.stream_index = 0
        self.stream_message: _StreamMessage | None = None
        self.stream_tools: dict[str, _StreamTool] = {}
        self.stream_task: asyncio.Task[None] | None = None
        self.stream_dirty = False
        # Stream operations (a terminal message/tool push, one flush-loop
        # publish) currently in flight. ``settle_stream`` waits for them so
        # their upserts land ahead of the complete replace, while the
        # ``stream_settled`` gate drops operations started afterwards.
        self.stream_ops = 0
        self.stream_ops_idle = asyncio.Event()
        self.stream_ops_idle.set()
        self.stream_settled = False
        self.last_activity = time.monotonic()
        self._start_lock = asyncio.Lock()
        self.last_state_key: tuple[Any, ...] | None = None

    @property
    def external_id(self) -> str | None:
        return self.session_file or self.session_path

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.alive

    def active_pending_ui(self) -> list[PendingInteraction]:
        """Open dialogs pi has not auto-resolved yet.

        A timed-out dialog can still sit in ``pending_ui`` until a lifecycle
        path clears it; reads filter it out here instead of mutating state or
        pushing notices, so they never report a dialog pi already resolved.
        """

        now = time.monotonic()
        return [pending for pending in self.pending_ui.values() if not pending.expired(now)]

    def expire_pending_ui(self, now: float | None = None) -> list[PendingInteraction]:
        """Remove and return the pending dialogs pi already auto-resolved."""

        now = time.monotonic() if now is None else now
        expired = [pending for pending in self.pending_ui.values() if pending.expired(now)]
        for pending in expired:
            self.pending_ui.pop(pending.request_id, None)
        return expired

    @property
    def status(self) -> str:
        if self.active_pending_ui():
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
            except (PiRpcProcessExited, PiRpcRequestFailed, PiRpcTimeout, TimeoutError) as exc:
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
        pending = data.get("pendingMessageCount")
        if isinstance(pending, int) and not isinstance(pending, bool):
            self.pending_message_count = pending

    # -- streaming projection -----------------------------------------------

    def begin_stream_message(self) -> None:
        """Start buffering a new assistant message under a fresh stream id."""

        if self.stream_settled:
            return
        self.stream_index += 1
        self.stream_message = _StreamMessage(index=self.stream_index)

    def apply_stream_delta(self, event: Mapping[str, Any]) -> None:
        """Fold one ``assistantMessageEvent`` into the active message buffers.

        Tool call events are ignored here: ``tool_execution_*`` owns tool
        cards, and creating them from both sources would duplicate them.
        """

        if self.stream_settled:
            return
        event_type = event.get("type")
        if event_type not in ("text_delta", "text_end", "thinking_delta", "thinking_end"):
            return
        message = self.stream_message
        if message is None:
            self.begin_stream_message()
            message = self.stream_message
        assert message is not None
        index = event.get("contentIndex")
        content_index = index if isinstance(index, int) and not isinstance(index, bool) else 0
        buffer = message.thinking if event_type.startswith("thinking") else message.text
        if event_type.endswith("_delta"):
            delta = event.get("delta")
            if not isinstance(delta, str) or not delta:
                return
            buffer[content_index] = buffer.get(content_index, "") + delta
        else:
            content = event.get("content")
            if isinstance(content, str):
                buffer[content_index] = content
        self.notify_stream_change()

    async def finish_stream_message(self, message: Mapping[str, Any]) -> None:
        """Rebuild the message from its final content and push it immediately."""

        if self.stream_settled:
            return
        self._begin_stream_op()
        try:
            stream = self.stream_message
            if stream is None:
                self.begin_stream_message()
                stream = self.stream_message
            assert stream is not None
            text: dict[int, str] = {}
            thinking: dict[int, str] = {}
            blocks = message.get("content")
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, Mapping):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        text[len(text)] = block["text"]
                    elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                        thinking[len(thinking)] = block["thinking"]
            stream.text = text
            stream.thinking = thinking
            items = self._stream_message_items(stream, "done")
            self.stream_message = None
            await self.cancel_stream_flush()
            await self._publish_stream_items(items)
        finally:
            self._end_stream_op()

    def start_tool_call(self, call_id: str, name: str, arguments: Any) -> None:
        if self.stream_settled:
            return
        self._ensure_stream_tool(call_id, name, arguments)
        self.notify_stream_change()

    def update_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: Any,
        partial_result: Any,
    ) -> None:
        if self.stream_settled:
            return
        tool = self._ensure_stream_tool(call_id, name, arguments)
        content = partial_result.get("content") if isinstance(partial_result, Mapping) else None
        tool.output = projection.content_text(content)
        self.notify_stream_change()

    async def finish_tool_call(
        self,
        call_id: str,
        name: str,
        result: Any,
        is_error: bool,
    ) -> None:
        if self.stream_settled:
            return
        self._begin_stream_op()
        try:
            tool = self.stream_tools.pop(call_id, None)
            if tool is None:
                # A result without a recorded start (connector restarted mid-call):
                # still surface the terminal card instead of dropping the activity.
                tool = self._make_stream_tool(call_id, name, {})
            details = result.get("details") if isinstance(result, Mapping) else None
            output = result.get("content") if isinstance(result, Mapping) else None
            tool.output = projection.content_text(output)
            tool.status = "failed" if is_error else "done"
            exit_code = projection.tool_exit_code(details)
            if exit_code is not None:
                tool.content["exitCode"] = exit_code
            if tool.content.get("kind") == "file_change":
                changes = projection.tool_result_changes(tool.content, details)
                if changes is not None:
                    tool.content["changes"] = changes
            await self.cancel_stream_flush()
            await self._publish_stream_items([self._stream_tool_item(tool)])
        finally:
            self._end_stream_op()

    def clear_stream_state(self) -> None:
        """Drop temporary stream buffers after the full projection replaced them."""

        self.stream_message = None
        self.stream_tools.clear()

    def notify_stream_change(self) -> None:
        """Mark the session dirty and make sure one flush loop is running."""

        if self.stream_settled:
            return
        self.stream_dirty = True
        task = self.stream_task
        if task is None or task.done():
            self.stream_task = asyncio.create_task(self._flush_stream_loop())

    async def _flush_stream_loop(self) -> None:
        """Push snapshots immediately, then at most once per interval.

        The loop must wait out the interval before deciding it is done:
        exiting as soon as nothing is dirty would let the next delta create a
        fresh task and push immediately, defeating the coalescing exactly
        when the deltas arrive just slower than one loop iteration.
        """

        while True:
            self.stream_dirty = False
            await self.publish_stream_items()
            await asyncio.sleep(STREAM_FLUSH_INTERVAL_SECONDS)
            if not self.stream_dirty:
                self.stream_task = None
                return

    async def cancel_stream_flush(self) -> None:
        """Stop the pending flush loop; the caller pushes terminal items itself."""

        task = self.stream_task
        self.stream_task = None
        self.stream_dirty = False
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise

    def _begin_stream_op(self) -> None:
        self.stream_ops += 1
        self.stream_ops_idle.clear()

    def _end_stream_op(self) -> None:
        self.stream_ops -= 1
        if self.stream_ops <= 0:
            self.stream_ops = 0
            self.stream_ops_idle.set()

    async def settle_stream(self) -> None:
        """Fence stream publishes behind the upcoming complete replace.

        Settles the stream gate so operations started afterwards drop, then
        waits for the operations already in flight (a terminal message/tool
        push, the flush loop's current upsert). Their upserts therefore land
        ahead of the complete timeline sync that follows, which replaces
        every temporary item the settled run produced -- instead of letting
        a late ``message_end`` resurrect one behind the replace. Stream
        operations only wait on the host, never on this method, so the wait
        cannot deadlock.
        """

        self.stream_settled = True
        if self.stream_ops:
            await self.stream_ops_idle.wait()
        await self.cancel_stream_flush()
        self.clear_stream_state()

    async def publish_stream_items(
        self,
        terminal: Sequence[RuntimeTimelineItem] = (),
    ) -> None:
        """Upsert the current stream snapshot through the host."""

        if self.stream_settled:
            return
        self._begin_stream_op()
        try:
            await self._publish_stream_items(terminal)
        finally:
            self._end_stream_op()

    async def _publish_stream_items(
        self,
        terminal: Sequence[RuntimeTimelineItem] = (),
    ) -> None:
        """Push one snapshot; the caller has fenced it against settle."""

        pending = list(terminal)
        if self.stream_message is not None:
            pending.extend(self._stream_message_items(self.stream_message, "running"))
        pending.extend(self._stream_tool_item(tool) for tool in self.stream_tools.values())
        for item in pending:
            try:
                await self.runtime.host.timeline_item_upsert(item)
            except Exception:  # a stream push must never kill the event loop
                logger.exception("failed to upsert pi stream item for %s", self.platform_id)

    @staticmethod
    def _make_stream_tool(call_id: str, name: str, arguments: Any) -> _StreamTool:
        content = projection.tool_content(name, arguments)
        content["callId"] = call_id
        return _StreamTool(call_id=call_id, name=name, content=content)

    def _ensure_stream_tool(self, call_id: str, name: str, arguments: Any) -> _StreamTool:
        tool = self.stream_tools.get(call_id)
        if tool is None:
            tool = self._make_stream_tool(call_id, name, arguments)
            self.stream_tools[call_id] = tool
        return tool

    def _stream_message_items(
        self,
        message: _StreamMessage,
        status: str,
    ) -> list[RuntimeTimelineItem]:
        items: list[RuntimeTimelineItem] = []
        text = message.text_value()
        if text:
            items.append(
                self._stream_item(
                    item_type="message",
                    key=f"stream:{message.index}",
                    status=status,
                    role="assistant",
                    content={"kind": "markdown", "format": "markdown", "text": text},
                )
            )
        thinking = message.thinking_value()
        if thinking:
            items.append(
                self._stream_item(
                    item_type="system",
                    key=f"stream:{message.index}:thinking",
                    status=status,
                    role="assistant",
                    content={"kind": "reasoning", "text": thinking},
                )
            )
        return items

    def _stream_tool_item(self, tool: _StreamTool) -> RuntimeTimelineItem:
        content = dict(tool.content)
        if tool.output:
            content["output"] = tool.output
        return self._stream_item(
            item_type="tool",
            key=f"stream-tool:{tool.call_id}",
            status=tool.status,
            role="assistant",
            content=content,
        )

    def _stream_item(
        self,
        *,
        item_type: str,
        key: str,
        status: str,
        role: str,
        content: Mapping[str, Any],
    ) -> RuntimeTimelineItem:
        external = self.external_id or self.platform_id
        return RuntimeTimelineItem(
            id=projection.item_id(external, item_type, key),
            session_id=self.platform_id,
            type=item_type,
            status=status,
            order_seq=0,
            content_hash=timeline_content_hash(
                item_type=item_type,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
                role=role,  # type: ignore[arg-type]
                content=dict(content),
            ),
            role=role,
            content=dict(content),
            source=TimelineSource(
                runtime=RUNTIME,
                external_session_id=external,
                native_item_id=key,
                native_item_type="stream",
            ).to_mapping(),
            revision=1,
            metadata={"stream": True},
        )

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
        await self.cancel_stream_flush()
        self.clear_stream_state()
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
            tuple(self.steering_queue),
            tuple(self.followup_queue),
            tuple(sorted(self.extension_status.items())),
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
        self._attachment_retention_days = int(values["attachmentRetentionDays"])
        self.default_cwd = str(values["defaultCwd"])
        self.sessions_dir = Path(str(values["sessionsDir"]))
        self.directory = SessionDirectory(self.sessions_dir)
        self._live: dict[str, PiLiveSession] = {}
        self._live_locks: dict[str, asyncio.Lock] = {}
        self._known_paths: dict[str, str] = {}
        self._synced: dict[str, tuple[float, int]] = {}
        self._utility: PiRpcProcess | None = None
        self._catalog_revision = 0
        # Platform command id -> pi-native command name, filled by
        # ``list_commands`` and consumed by ``execute_command``.
        self._command_native_names: dict[str, str] = {}
        # Platform session ids whose "context nearly full" notice is open.
        # Runtime-level so a reclaimed process (idle reclaim swaps the
        # PiLiveSession object) cannot lose the flag and leave the notice
        # open forever when the context usage recovers.
        self._context_warning_sessions: set[str] = set()
        self._stopping = False
        self._reclaim_task: asyncio.Task[None] | None = None
        self._stale_reset_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._client_bindings: dict[str, tuple[tuple[str, str], ...]] = {}
        self._last_reannounce_at = -REANNOUNCE_MIN_INTERVAL_SECONDS
        self._reannounce_lock = asyncio.Lock()
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
        self._cleanup_task = asyncio.create_task(self._attachment_cleanup_loop())

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
        for task in (self._reclaim_task, self._stale_reset_task, self._cleanup_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._reclaim_task = None
        self._stale_reset_task = None
        self._cleanup_task = None
        for live in list(self._live.values()):
            await live.stop()
        self._live.clear()
        self._live_locks.clear()
        if self._utility is not None:
            await self._utility.close()
            self._utility = None

    async def reannounce_session_states(self, *, reason: str = "reconnect") -> None:
        """Publish the authoritative state of every known session.

        The platform stores runtime state in server-side caches that a backend
        restart (for example the server host rebooting) can empty or stale
        while this connector keeps running. Clients then refuse input based on
        the stale state until a connector restart pushes fresh facts — which
        would kill live sessions. Re-announcing every session's true state
        lets the platform converge without that restart: sessions with a live
        process keep their live status, everything else is idle.

        Repeats within ``REANNOUNCE_MIN_INTERVAL_SECONDS`` are skipped so a
        reconnect storm cannot trigger repeated full scans; the first call
        after start (or after a long outage) always runs.
        """

        async with self._reannounce_lock:
            now = time.monotonic()
            if now - self._last_reannounce_at < REANNOUNCE_MIN_INTERVAL_SECONDS:
                logger.debug(
                    "skipping session state re-announce reason=%s (ran %.1fs ago)",
                    reason,
                    now - self._last_reannounce_at,
                )
                return
            self._last_reannounce_at = now
            try:
                summaries = await self.list_complete_session_inventory()
            except asyncio.CancelledError:
                raise
            except Exception:  # a failed scan must not break the caller
                logger.exception("failed to list sessions for state re-announce")
                return
            announced = 0
            for summary in summaries:
                live = self._live_for_meta(summary)
                try:
                    if live is not None:
                        await self._push_state(live, force=True)
                    else:
                        await self.host.session_state_update(
                            session_id=summary.session_id,
                            runtime=RUNTIME,
                            external_session_id=summary.external_session_id,
                            status="idle",
                            metadata={"sessionFile": summary.external_session_id},
                        )
                    announced += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "failed to re-announce session state for %s",
                        summary.session_id,
                    )
            logger.info(
                "reannounced session states reason=%s sessions=%d",
                reason,
                announced,
            )

    def _live_for_meta(self, summary: SessionMeta) -> PiLiveSession | None:
        """Match an inventory entry to its live session.

        Platform-created sessions keep the platform-allocated id as the live
        key while the inventory identifies the same session by a path-derived
        id, so fall back to matching on the session file.
        """

        live = self._live.get(summary.session_id)
        if live is not None:
            return live
        external_id = summary.external_session_id
        if external_id is None:
            return None
        for candidate in self._live.values():
            if candidate.external_id == external_id:
                return candidate
        return None

    async def _reset_stale_running_states(self) -> None:
        """Re-announce states for sessions left running by a previous process.

        A connector restart kills live pi processes before their exit state
        can be published, so the platform keeps showing "running" until the
        next turn. Right after start() this runtime owns no live process,
        which makes the full re-announcement accurate for every session.
        """

        await self.reannounce_session_states(reason="runtime-start")

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
            # A timed-out dialog must not pin the process forever; clear it
            # even when the session is not idle yet so its notice closes.
            await self._expire_pending_interactions(live)
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

    async def _attachment_cleanup_loop(self) -> None:
        """Run the attachment cleanup pass immediately, then on an interval.

        A failing pass is logged and the loop still waits out the interval, so
        a transient error cannot turn the loop into a hot retry.
        """

        while True:
            try:
                await self.cleanup_attachments()
            except asyncio.CancelledError:
                raise
            except Exception:  # a failed pass must not kill the loop
                logger.exception("pi attachment cleanup pass failed")
            await asyncio.sleep(ATTACHMENT_CLEANUP_INTERVAL_SECONDS)

    async def cleanup_attachments(self) -> int:
        """Expire files of pi-owned inactive session attachment directories.

        Only the one directory level under the attachments root is considered,
        and only directories that provably belong to pi are touched: a session
        must be live, be known to this runtime, or match a pi session file on
        disk. Directories of other runtimes (or unknown owners) and symlinks
        are left alone. Files older than the retention window are removed from
        pi-owned inactive directories, and a directory left empty is deleted.
        A retention of zero disables cleanup and touches nothing. Returns the
        number of deleted files.
        """

        if self._attachment_retention_days <= 0:
            return 0
        root = attachments_root()
        cutoff = time.time() - self._attachment_retention_days * 86400
        live_names = {session_attachments_dir(session_id).name for session_id in self._live}
        owned = set(live_names)
        owned.update(session_attachments_dir(session_id).name for session_id in self._known_paths)
        try:
            for summary in self.directory.list_sessions(0):
                session_id = platform_session_id(self.host.session_namespace, summary.path)
                owned.add(session_attachments_dir(session_id).name)
        except OSError:
            logger.warning("attachment cleanup: pi session scan failed")
        try:
            entries = list(root.iterdir())
        except OSError:
            logger.warning("attachment cleanup: attachments root unreadable: %s", root)
            return 0
        deleted = 0
        for entry in entries:
            # is_dir() follows symlinks; never walk or unlink through one.
            if entry.is_symlink() or not entry.is_dir() or entry.name in live_names:
                continue
            if entry.name not in owned:
                continue
            try:
                for child in list(entry.iterdir()):
                    if child.is_symlink() or not child.is_file():
                        continue
                    try:
                        if child.stat().st_mtime < cutoff:
                            child.unlink()
                            deleted += 1
                    except OSError:
                        logger.warning(
                            "attachment cleanup: failed to remove %s",
                            child,
                        )
                if not any(entry.iterdir()):
                    entry.rmdir()
            except OSError:
                logger.warning(
                    "attachment cleanup: failed to clean directory %s",
                    entry,
                )
        logger.info("attachment cleanup deleted files=%d", deleted)
        return deleted

    # -- discovery / inventory ---------------------------------------------

    async def list_sessions(
        self,
        limit: int = 100,
        cursor: str | None = None,
        force: bool = False,
    ) -> tuple[SessionMeta, ...]:
        _ = cursor, force  # Pagination is not implemented: no server 2.0.x
        # call site uses session.discover today, so a cursor would never be
        # issued; if it ever is, this method needs real cursor/force handling.
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

    def _client_bindings_key(self, external_session_id: str) -> str:
        return f"pi/client-message-bindings/{self.host.connector_id}/{external_session_id}"

    async def _load_client_bindings(
        self,
        external_session_id: str | None,
    ) -> tuple[tuple[str, str], ...]:
        """Client message id pairs persisted for this session, if any."""

        if not external_session_id:
            return ()
        cached = self._client_bindings.get(external_session_id)
        if cached is not None:
            return cached
        pairs: tuple[tuple[str, str], ...] = ()
        try:
            value = await self.host.sync_state_read(self._client_bindings_key(external_session_id))
        except Exception:
            logger.exception(
                "failed to read client message bindings session=%s",
                external_session_id,
            )
            value = None
        if isinstance(value, Mapping):
            raw_bindings = value.get("bindings")
            if isinstance(raw_bindings, list):
                collected: list[tuple[str, str]] = []
                for raw in raw_bindings:
                    if not isinstance(raw, Mapping):
                        continue
                    text = raw.get("text")
                    client_message_id = raw.get("clientMessageId")
                    if isinstance(text, str) and isinstance(client_message_id, str):
                        collected.append((text, client_message_id))
                pairs = tuple(collected)
        self._client_bindings[external_session_id] = pairs
        return pairs

    async def _persist_client_bindings(self, live: PiLiveSession) -> None:
        """Persist the session's optimistic-send pairs for future projections."""

        external_session_id = live.external_id
        if not external_session_id:
            return
        stored = await self._load_client_bindings(external_session_id)
        merged = _merge_client_message_pairs(stored, tuple(live.client_messages))[
            -MAX_CLIENT_MESSAGE_BINDINGS_PER_SESSION:
        ]
        self._client_bindings[external_session_id] = merged
        try:
            await self.host.sync_state_write(
                self._client_bindings_key(external_session_id),
                {
                    "version": CLIENT_MESSAGE_BINDINGS_VERSION,
                    "bindings": [
                        {"text": text, "clientMessageId": client_message_id}
                        for text, client_message_id in merged
                    ],
                },
            )
        except Exception:
            logger.exception(
                "failed to persist client message bindings session=%s",
                external_session_id,
            )

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
        client_messages = tuple(live.client_messages) if live is not None else ()
        client_messages = _merge_client_message_pairs(
            await self._load_client_bindings(doc.summary.path),
            client_messages,
        )
        logger.info(
            "pi project session_id=%s live=%s client_message_pairs=%d",
            session_id,
            live is not None,
            len(client_messages),
        )
        truncated = limit is not None and limit > 0
        items = projection.project_session(
            doc.entries,
            session_id=session_id,
            external_session_id=doc.summary.path,
            client_messages=client_messages,
            # Build only the tail the caller asked for; full construction is
            # wasted work for long transcripts (the platform polls snapshots).
            max_items=limit if truncated else None,
        )
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
        metadata["pendingMessageCount"] = max(
            live.pending_message_count,
            len(live.steering_queue) + len(live.followup_queue),
        )
        metadata["pendingSteering"] = list(live.steering_queue)
        metadata["pendingFollowUp"] = list(live.followup_queue)
        if live.extension_status:
            metadata["extensionStatus"] = dict(live.extension_status)
        if live.cleared_queue is not None:
            metadata["clearedQueue"] = live.cleared_queue
        active_pending = live.active_pending_ui()
        if active_pending:
            metadata["pendingInteractions"] = len(active_pending)
        stats = live.session_stats
        if stats is not None:
            tokens = stats.get("tokens")
            if isinstance(tokens, Mapping):
                metadata["tokens"] = dict(tokens)
            cost = stats.get("cost")
            if _is_number(cost):
                metadata["cost"] = cost
            context_usage = stats.get("contextUsage")
            if isinstance(context_usage, Mapping):
                metadata["contextUsage"] = dict(context_usage)
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
        return tuple(pending.as_notice(session_id) for pending in live.active_pending_ui())

    async def refresh_session_stats(self, live: PiLiveSession) -> Mapping[str, Any] | None:
        """Refresh the cached ``get_session_stats`` payload for a live session.

        Called after a run settles so the published state carries fresh token
        and context usage. pi rejects the call before the first model turn, so
        a failure only logs and leaves the previous cache untouched.
        """

        process = live.process
        if process is None or not process.alive:
            return None
        try:
            data = response_data(await process.request({"type": "get_session_stats"}))
        except BUILTIN_RPC_ERRORS:
            logger.warning("pi get_session_stats failed session_id=%s", live.platform_id)
            return None
        live.session_stats = dict(data)
        await self._sync_context_warning(live, data)
        return data

    async def _sync_context_warning(
        self,
        live: PiLiveSession,
        data: Mapping[str, Any],
    ) -> None:
        """Open or resolve the context-usage notice at threshold crossings.

        The notice id is stable per session, so repeated settles above the
        threshold keep the open notice untouched instead of re-upserting it.
        The open flag lives on the runtime instead of the live session:
        idle reclaim replaces the process (and its ``PiLiveSession``), and the
        notice the old process opened must still resolve when usage drops.
        """

        percent = _context_usage_percent(data)
        notice_id = f"pi-context-{live.platform_id}"
        if percent is not None and percent >= CONTEXT_WARNING_PERCENT:
            if live.platform_id in self._context_warning_sessions:
                return
            try:
                await self.host.notice_upsert(
                    SessionNotice(
                        notice_id=notice_id,
                        session_id=live.platform_id,
                        runtime=RUNTIME,
                        type="notification",
                        title="上下文接近上限",
                        message=f"会话上下文已使用 {percent:g}%，请考虑压缩会话 (compact)。",
                        severity="warning",
                        status="open",
                        source={"runtime": RUNTIME, "component": "pi.context"},
                    )
                )
            except Exception:  # a failed notice must not affect the settle path
                logger.exception("failed to publish pi context warning for %s", live.platform_id)
                return
            self._context_warning_sessions.add(live.platform_id)
            return
        if live.platform_id not in self._context_warning_sessions:
            return
        try:
            await self.host.notice_upsert(
                SessionNotice(
                    notice_id=notice_id,
                    session_id=live.platform_id,
                    runtime=RUNTIME,
                    type="notification",
                    title="上下文接近上限",
                    message=(
                        f"会话上下文已回落到 {percent:g}%。"
                        if percent is not None
                        else "会话上下文占用已回落。"
                    ),
                    severity="warning",
                    status="resolved",
                    source={"runtime": RUNTIME, "component": "pi.context"},
                )
            )
        except Exception:  # a failed notice must not affect the settle path
            logger.exception("failed to resolve pi context warning for %s", live.platform_id)
            return
        self._context_warning_sessions.discard(live.platform_id)

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
        # Pi allows the same model name under several providers; ids stay
        # provider-qualified and the providers render as group headings, so
        # titles do not repeat the provider.
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
        # A stable provider order keeps the picker grouped and deterministic;
        # pi's own order inside a provider is preserved.
        entries.sort(key=lambda entry: entry[3] or "")
        multiple_providers = len({entry[3] for entry in entries if entry[3]}) > 1
        models: list[RuntimeModelItem] = []
        current_group: str | None = None
        for raw, catalog_id, name, provider, model_id in entries:
            title = _model_display_title(name, provider, model_id, directory)
            haystack = f"{catalog_id} {name} {title}".lower()
            if query and query.lower() not in haystack:
                continue
            if multiple_providers and provider and provider != current_group:
                current_group = provider
                models.append(_model_group_item(provider))
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
        needle = query.lower() if query else None
        # Every builtin id stays reserved even when the needle filters the
        # command out, so a colliding pi command keeps one id across listings.
        taken = set(BUILTIN_COMMAND_NAMES)
        commands: list[RuntimeCommand] = [
            RuntimeCommand(
                id=name,
                title=name,
                description=description,
                scope="session",
                # compact/name/export proxy a text argument; stats ignores one.
                accepts_args=name != "stats",
                metadata={"source": "pi-builtin"},
            )
            for name, description in BUILTIN_COMMANDS
            if not needle or needle in name.lower()
        ]
        for raw in _as_list(response_data(data).get("commands")):
            if not isinstance(raw, Mapping):
                continue
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                continue
            if needle and needle not in name.lower():
                continue
            command_id = _platform_command_id(name, taken)
            taken.add(command_id)
            self._command_native_names[command_id] = name
            metadata: dict[str, Any] = {"source": raw.get("source")}
            source_info = _command_source_info(raw)
            if source_info is not None:
                metadata["sourceInfo"] = source_info
            commands.append(
                RuntimeCommand(
                    id=command_id,
                    title=name,
                    description=raw.get("description")
                    if isinstance(raw.get("description"), str)
                    else None,
                    scope="session",
                    accepts_args=True,
                    metadata=metadata,
                )
            )
        if limit > 0:
            commands = commands[:limit]
        return tuple(commands)

    async def execute_command(
        self,
        session_id: str,
        command: str,
        external_session_id: str | None = None,
        raw: str | None = None,
        args: tuple[str, ...] = (),
    ) -> RuntimeCommandResult:
        if command in BUILTIN_COMMAND_NAMES:
            return await self._execute_builtin_command(
                session_id, command, external_session_id, args
            )
        native = self._command_native_names.get(command)
        if native is None:
            # The platform may dispatch a command cached from an earlier
            # catalog; refresh the full catalog once so the id map can
            # translate it.
            try:
                await self.list_commands(session_id, external_session_id, limit=0)
            except Exception:  # the catalog refresh is best-effort
                logger.exception("failed to refresh the pi command catalog")
            native = self._command_native_names.get(command, command)
        live = await self._ensure_live(session_id, external_session_id, None)
        if raw:
            # ``raw`` is authoritative except for the command token, which the
            # platform may have sanitized away from the pi-native name.
            text = _restore_native_command_text(raw, command, native)
        else:
            text = "/" + native + (" " + " ".join(args) if args else "")
        logger.info("pi command execute session_id=%s text=%r", session_id, text)
        await live.send_prompt(text)
        return RuntimeCommandResult(
            command=command,
            ok=True,
            message=f"已发送 {text}",
            # AA 2.0.3 command contract: dispatch only accepts the native work,
            # completion is reported later through the timeline.
            result={"sessionId": session_id, "executionState": "accepted"},
        )

    async def _execute_builtin_command(
        self,
        session_id: str,
        command: str,
        external_session_id: str | None,
        args: tuple[str, ...],
    ) -> RuntimeCommandResult:
        """Run one of the proxied TUI commands through the RPC interface."""

        if command == "compact":
            return await self._execute_compact_command(session_id, external_session_id, args)
        if command == "name":
            return await self._execute_name_command(session_id, external_session_id, args)
        if command == "stats":
            return await self._execute_stats_command(session_id, external_session_id)
        return await self._execute_export_command(session_id, external_session_id, args)

    async def _execute_compact_command(
        self,
        session_id: str,
        external_session_id: str | None,
        args: tuple[str, ...],
    ) -> RuntimeCommandResult:
        live = await self._ensure_live(session_id, external_session_id, None)
        custom_instructions = " ".join(args).strip() if args else ""
        payload: dict[str, Any] = {"type": "compact"}
        if custom_instructions:
            payload["customInstructions"] = custom_instructions
        logger.info("pi builtin compact session_id=%s has_args=%s", session_id, bool(args))
        try:
            data = response_data(
                await live.command(
                    payload,
                    timeout=max(self.request_timeout, COMPACT_REQUEST_TIMEOUT_SECONDS),
                )
            )
        except BUILTIN_RPC_ERRORS as exc:
            return RuntimeCommandResult(
                command="compact",
                ok=False,
                code="pi_compact_failed",
                message=f"压缩失败：{exc}",
            )
        tokens_before = data.get("tokensBefore")
        tokens_after = data.get("estimatedTokensAfter")
        if _is_number(tokens_before) and _is_number(tokens_after):
            message = f"已压缩 {tokens_before} → {tokens_after} tokens"
        else:
            message = "会话上下文已压缩"
        return RuntimeCommandResult(
            command="compact",
            ok=True,
            message=message,
            result={"sessionId": session_id, "executionState": "accepted"},
        )

    async def _execute_name_command(
        self,
        session_id: str,
        external_session_id: str | None,
        args: tuple[str, ...],
    ) -> RuntimeCommandResult:
        live = await self._ensure_live(session_id, external_session_id, None)
        if not args:
            if live.session_name:
                return RuntimeCommandResult(
                    command="name",
                    ok=True,
                    message=f"当前会话名称：{live.session_name}",
                    result={"sessionId": session_id},
                )
            return RuntimeCommandResult(
                command="name",
                ok=True,
                message="当前会话尚未设置名称。",
                result={"sessionId": session_id},
            )
        name = " ".join(args).strip()
        if not name:
            return RuntimeCommandResult(
                command="name",
                ok=False,
                code="pi_name_invalid",
                message="会话名称不能为空。",
            )
        try:
            await live.command({"type": "set_session_name", "name": name})
        except BUILTIN_RPC_ERRORS as exc:
            return RuntimeCommandResult(
                command="name",
                ok=False,
                code="pi_name_failed",
                message=f"设置会话名称失败：{exc}",
            )
        live.session_name = name
        await self._push_meta(live)
        return RuntimeCommandResult(
            command="name",
            ok=True,
            message=f"会话名称已设置为 {name}",
            result={"sessionId": session_id, "name": name},
        )

    async def _execute_stats_command(
        self,
        session_id: str,
        external_session_id: str | None,
    ) -> RuntimeCommandResult:
        live = await self._ensure_live(session_id, external_session_id, None)
        live.touch()
        data = await self.refresh_session_stats(live)
        if not data:
            return RuntimeCommandResult(
                command="stats",
                ok=False,
                code="pi_stats_unavailable",
                message="暂无法读取会话统计（pi 可能尚未完成首次模型调用）。",
            )
        return RuntimeCommandResult(
            command="stats",
            ok=True,
            message=_format_session_stats(data),
            result={"sessionId": session_id, "executionState": "accepted"},
        )

    async def _execute_export_command(
        self,
        session_id: str,
        external_session_id: str | None,
        args: tuple[str, ...],
    ) -> RuntimeCommandResult:
        live = await self._ensure_live(session_id, external_session_id, None)
        payload: dict[str, Any] = {"type": "export_html"}
        if args:
            output_path = args[0].strip()
            if not output_path:
                return RuntimeCommandResult(
                    command="export",
                    ok=False,
                    code="pi_export_invalid",
                    message="导出路径不能为空。",
                )
            payload["outputPath"] = output_path
        try:
            data = response_data(await live.command(payload))
        except BUILTIN_RPC_ERRORS as exc:
            return RuntimeCommandResult(
                command="export",
                ok=False,
                code="pi_export_failed",
                message=f"导出失败：{exc}",
            )
        path = data.get("path")
        if not isinstance(path, str) or not path:
            return RuntimeCommandResult(
                command="export",
                ok=False,
                code="pi_export_failed",
                message="pi 未返回导出文件路径。",
            )
        return RuntimeCommandResult(
            command="export",
            ok=True,
            message=f"已导出到 {path}",
            result={"sessionId": session_id, "executionState": "accepted", "path": path},
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
        images, notes = await self._prepare_attachments(session_id, attachments)
        workdir = self._resolve_cwd(cwd)
        live = PiLiveSession(self, session_id, cwd=workdir)
        # Bind the exact text pi will persist: the projection pairs optimistic
        # sends by comparing this text with the transcript's user message.
        prompt = _prompt_with_attachment_notes(content, notes)
        if client_message_id:
            live.client_messages.append((prompt, client_message_id))
        self._live[session_id] = live
        await live.ensure_started()
        if is_meaningful_title(title):
            await live.command({"type": "set_session_name", "name": title})
        if selections:
            await self._apply_selections(live, selections)
        await live.send_prompt(prompt, images=images)
        if client_message_id:
            await self._persist_client_bindings(live)
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
        images, notes = await self._prepare_attachments(session_id, attachments)
        live = await self._ensure_live(session_id, external_session_id, cwd)
        prompt = _prompt_with_attachment_notes(content, notes)
        logger.info(
            "pi turn start session_id=%s client_message_id=%s",
            session_id,
            client_message_id,
        )
        if client_message_id:
            live.client_messages.append((prompt, client_message_id))
            await self._persist_client_bindings(live)
        if selections:
            await self._apply_selections(live, selections)
        behavior = "followUp" if (live.is_streaming or live.is_compacting) else None
        await live.send_prompt(
            prompt,
            streaming_behavior=behavior,
            images=images,
        )
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
        images, notes = await self._prepare_attachments(session_id, attachments)
        live = await self._ensure_live(session_id, external_session_id, None)
        prompt = _prompt_with_attachment_notes(content, notes)
        if client_message_id:
            live.client_messages.append((prompt, client_message_id))
            await self._persist_client_bindings(live)
        if live.is_streaming:
            await live.send_prompt(prompt, streaming_behavior="steer", images=images)
            return RuntimeOperationResult(result={"sessionId": session_id, "steered": True})
        await live.send_prompt(prompt, images=images)
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
        # A receipt from a previous interrupt belongs to that state push, not
        # to this call; dropping it keeps a failed clear below from returning
        # the stale queue as if it were the result of this interrupt.
        live.cleared_queue = None
        if not (live.is_streaming or live.is_compacting):
            return RuntimeOperationResult(result={"sessionId": session_id, "noop": True})
        # Mirror the TUI's Esc (docs/rpc-commands.md): drain the queue first so
        # abort does not keep running the queued messages, and hand the returned
        # text back to the client so it can restore the editor. A failed clear
        # must never block the abort.
        try:
            data = response_data(
                await live.command(
                    {"type": "clear_queue"}, timeout=max(self.request_timeout, 120.0)
                )
            )
            cleared: dict[str, list[str]] = {}
            for key in ("steering", "followUp"):
                items = data.get(key)
                cleared[key] = (
                    [item for item in items if isinstance(item, str)]
                    if isinstance(items, list)
                    else []
                )
            live.cleared_queue = cleared
        except Exception:  # a failed clear must never block the abort
            logger.warning(
                "pi clear_queue failed for session %s; aborting anyway",
                session_id,
                exc_info=True,
            )
        await live.command({"type": "abort"}, timeout=max(self.request_timeout, 120.0))
        await self._push_state(live, force=True)
        result: dict[str, Any] = {"sessionId": session_id}
        if live.cleared_queue is not None:
            result["clearedQueue"] = live.cleared_queue
        return RuntimeOperationResult(result=result)

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
        # The client may have raced pi's own timeout; drop the stale entry
        # (and close its notice) before looking the requested one up.
        if await self._expire_pending_interactions(live):
            await self._push_state(live, force=True)
        pending = live.pending_ui.get(request_id)
        if pending is None:
            return RuntimeOperationResult(
                ok=False,
                code="pi_interaction_expired",
                message="该交互已处理或已失效。",
            )
        payload = pending.response_payload(action_id, input_data)
        # The process dies only between ``ensure_started`` and now (or the
        # session was reclaimed); a hard assert would kill the critical RPC
        # channel, so report the interaction as expired instead.
        if live.process is None or not live.process.alive:
            return RuntimeOperationResult(
                ok=False,
                code="pi_interaction_expired",
                message="该交互已失效（会话进程不在运行）。",
            )
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
        if event_type == "message_start":
            message = record.get("message")
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                live.begin_stream_message()
            return
        if event_type == "message_update":
            # Only text/thinking deltas matter here; tool calls are owned by
            # the tool_execution_* events.
            assistant_event = record.get("assistantMessageEvent")
            if isinstance(assistant_event, Mapping):
                live.apply_stream_delta(assistant_event)
            return
        if event_type == "message_end":
            message = record.get("message")
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                live.message_count += 1
                await live.finish_stream_message(message)
            return
        if event_type == "tool_execution_start":
            call_id = record.get("toolCallId")
            name = record.get("toolName")
            if isinstance(call_id, str) and isinstance(name, str):
                live.start_tool_call(call_id, name, record.get("args"))
            return
        if event_type == "tool_execution_update":
            call_id = record.get("toolCallId")
            name = record.get("toolName")
            if isinstance(call_id, str) and isinstance(name, str):
                live.update_tool_call(
                    call_id, name, record.get("args"), record.get("partialResult")
                )
            return
        if event_type == "tool_execution_end":
            call_id = record.get("toolCallId")
            name = record.get("toolName")
            if isinstance(call_id, str) and isinstance(name, str):
                await live.finish_tool_call(
                    call_id,
                    name,
                    record.get("result"),
                    record.get("isError") is True,
                )
            return
        if event_type == "agent_start":
            # The previous settle closed the stream gate; this turn may stream
            # again.
            live.stream_settled = False
            live.status_reason = None
            live.is_streaming = True
            await self._push_state(live)
            return
        if event_type == "agent_settled":
            # pi applies a dialog's timeout on its own side without notifying
            # the client, so expire those entries before publishing state.
            await self._expire_pending_interactions(live)
            await self._resolve_open_notifications(live)
            live.is_streaming = False
            live.is_compacting = False
            live.status_reason = None
            # The interrupt receipt was delivered by the state push that
            # carried it; later pushes must not keep re-sending the stale
            # queue contents forever.
            live.cleared_queue = None
            # The full sync below replaces every temporary stream item; settle
            # the stream generation first so the in-flight upsert lands ahead
            # of the replace and late ``message_end`` events cannot upsert
            # another one behind it.
            await live.settle_stream()
            # Refresh before publishing so the pushed state carries current
            # token, cost, and context-usage metadata.
            await self.refresh_session_stats(live)
            await self._push_state(live, force=True)
            await self._publish_timeline(live)
            await self._push_meta(live)
            # pi marks an aborted run in the event payload; the server-side
            # turn outcome whitelist is {completed, interrupted, cancelled,
            # failed}, so map it explicitly.
            outcome = "interrupted" if record.get("aborted") else "completed"
            try:
                await self.host.session_turn_ended(
                    session_id=live.platform_id,
                    runtime=RUNTIME,
                    external_session_id=live.external_id,
                    outcome=outcome,
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
        if event_type == "queue_update":
            steering = record.get("steering")
            live.steering_queue = (
                [item for item in steering if isinstance(item, str)]
                if isinstance(steering, list)
                else []
            )
            followup = record.get("followUp")
            live.followup_queue = (
                [item for item in followup if isinstance(item, str)]
                if isinstance(followup, list)
                else []
            )
            live.pending_message_count = len(live.steering_queue) + len(live.followup_queue)
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
        await live.settle_stream()
        # The process that could have produced a cleared-queue receipt is gone;
        # a later state push from a replacement must not replay it.
        live.cleared_queue = None
        if self._stopping:
            return
        live.is_streaming = False
        live.is_compacting = False
        for pending in list(live.pending_ui.values()):
            await self.host.notice_upsert(pending.as_notice(live.platform_id, status="resolved"))
        live.pending_ui.clear()
        await self._resolve_open_notifications(live)
        if self._live.get(live.platform_id) is not live:
            # The session was reclaimed or replaced while this process was
            # shutting down; a new live may already have pushed "running", and
            # an idle update from this dead process would overwrite it until
            # that session's next state push.
            logger.info("ignoring exit state for superseded pi session %s", live.platform_id)
            return
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
        # Best effort: a full projection drops any temporary stream item the
        # exited process left behind (the session file may not exist yet).
        await self._publish_timeline(live)

    async def _handle_ui_request(self, live: PiLiveSession, record: Mapping[str, Any]) -> None:
        method = record.get("method")
        if method in DIALOG_METHODS:
            pending = PendingInteraction(record)
            live.pending_ui[pending.request_id] = pending
            await self.host.notice_upsert(pending.as_notice(live.platform_id))
            await self._push_state(live, force=True)
            return
        if method == "setStatus":
            status_key = record.get("statusKey")
            if not isinstance(status_key, str) or not status_key:
                return
            status_text = record.get("statusText")
            if status_text is None:
                live.extension_status.pop(status_key, None)
            elif isinstance(status_text, str):
                live.extension_status[status_key] = status_text
            else:
                logger.debug("ignoring non-string setStatus text for %r", status_key)
                return
            await self._push_state(live)
            return
        if method == "notify":
            message = record.get("message")
            if not isinstance(message, str) or not message:
                return
            notify_type = record.get("notifyType")
            severity = notify_type if notify_type in ("info", "warning", "error") else "info"
            request_id = str(record.get("id"))
            notice = SessionNotice(
                notice_id=f"pi-notify-{request_id}",
                session_id=live.platform_id,
                runtime=RUNTIME,
                type="notification",
                title=message,
                severity=severity,  # type: ignore[arg-type]
                status="open",
                source={"runtime": RUNTIME, "component": "pi.extension-ui"},
            )
            live.open_notifications[notice.notice_id] = notice
            await self.host.notice_upsert(notice)
            return
        logger.debug("ignoring pi extension UI method %r", method)

    async def _expire_pending_interactions(self, live: PiLiveSession) -> bool:
        """Close the notices of dialogs pi already auto-resolved.

        pi enforces a dialog request's ``timeout`` itself and emits no event
        when it fires (docs/rpc-extension-ui.md), so nothing else removes the
        entry: without this the session stays in ``waiting_approval``, its
        notice stays open, and the idle reclaim loop can never close the pi
        process.
        """

        expired = live.expire_pending_ui()
        for pending in expired:
            try:
                await self.host.notice_upsert(pending.as_notice(live.platform_id, status="expired"))
            except Exception:  # a failed notice must not block the cleanup
                logger.exception(
                    "failed to publish expired interaction session=%s",
                    live.platform_id,
                )
        return bool(expired)

    async def _resolve_open_notifications(self, live: PiLiveSession) -> None:
        """Close the transient ``notify`` toasts left open by a finished run.

        pi never sends a resolve event for fire-and-forget notifications, so
        without this the platform keeps showing them for the session's whole
        life. Failures only log: settle and process exit must not fail because
        a toast could not be closed.
        """

        if not live.open_notifications:
            return
        notices = list(live.open_notifications.values())
        live.open_notifications.clear()
        for notice in notices:
            try:
                await self.host.notice_upsert(replace(notice, status="resolved"))
            except Exception:
                logger.exception(
                    "failed to resolve notify session=%s notice=%s",
                    live.platform_id,
                    notice.notice_id,
                )

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

    async def _prepare_attachments(
        self,
        session_id: str,
        attachments: tuple[RuntimeAttachment, ...],
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[tuple[str, str], ...]]:
        """Turn attachments into Pi image content and saved-file notes.

        Pi accepts images as base64 ``ImageContent`` entries on ``prompt`` and
        ``steer`` commands, so those are passed through and never written to
        disk. Other attachments are downloaded into the connector's per-session
        attachment directory and returned as ``(name, absolute path)`` notes so
        the prompt can point Pi at them. A failing or oversized attachment is
        skipped on its own and never blocks the rest of the turn.
        """

        if not attachments:
            return (), ()
        images: list[Mapping[str, Any]] = []
        notes: list[tuple[str, str]] = []
        for attachment in attachments:
            media_type = (attachment.media_type or "").strip().lower()
            try:
                downloaded = await self.host.attachment_download(session_id, attachment.file_id)
            except Exception:  # skipped attachments must not block the turn
                logger.exception(
                    "pi attachment download failed file_id=%s",
                    attachment.file_id,
                )
                continue
            if len(downloaded.content) > MAX_ATTACHMENT_BYTES:
                logger.warning(
                    "pi runtime skipped oversized attachment file_id=%s size=%d",
                    attachment.file_id,
                    len(downloaded.content),
                )
                continue
            if media_type.startswith("image/"):
                resolved_media_type = (downloaded.media_type or media_type).strip().lower()
                images.append(
                    {
                        "type": "image",
                        "data": base64.b64encode(downloaded.content).decode("ascii"),
                        "mimeType": resolved_media_type,
                    }
                )
                continue
            name = attachment.name or downloaded.name or attachment.file_id
            target = attachment_target(session_id, attachment.file_id, name)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(downloaded.content)
            except OSError:
                logger.exception(
                    "pi attachment save failed file_id=%s path=%s",
                    attachment.file_id,
                    target,
                )
                continue
            notes.append((name, str(target)))
        return tuple(images), tuple(notes)

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
        logger.info(
            "pi state push session_id=%s status=%s force=%s",
            state.session_id,
            state.status,
            force,
        )
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
