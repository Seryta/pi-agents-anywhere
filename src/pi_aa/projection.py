"""Projection of Pi transcripts into the platform timeline contract.

The projection is intentionally pure: it reads parsed Pi session entries (or
live message objects) and produces ``RuntimeTimelineItem`` values. No process,
no I/O, no connector state.

Item identities follow the same scheme the DSH bridge uses: a digest over
``external_session_id``, item kind, and a native business id, so repeated
projections of the same transcript produce identical ids.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from connector.runtime_protocol.models import RuntimeTimelineItem
from connector.runtime_protocol.timeline import TimelineSource, timeline_content_hash

RUNTIME = "pi"

# ``pi.appendEntry`` type used by ``examples/extensions/btw.ts`` for headless
# side-question exchanges. Custom entries stay out of the LLM context; the
# payload is ``{question, answer}`` or ``{question, error}``. Keep this value
# in sync with the extension's ``BTW_ENTRY_TYPE``.
BTW_CUSTOM_TYPE = "pi.btw"

IMAGE_PLACEHOLDER = "[图片暂不支持跨设备预览]"

_TOOL_KINDS: dict[str, str] = {
    "bash": "command",
    "pwsh": "command",
    "edit": "file_change",
    "write": "file_change",
    "web_search": "web_search",
    "websearch": "web_search",
    "fetch": "web_search",
    "mcp": "mcp",
}


def item_id(external_session_id: str, kind: str, business_id: str) -> str:
    digest = hashlib.sha256(f"{external_session_id}\0{kind}\0{business_id}".encode()).hexdigest()
    return f"pi_{digest}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_text(content: Any) -> str:
    """Join text blocks, replacing image blocks with a placeholder."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block.get("type") == "image":
            parts.append(IMAGE_PLACEHOLDER)
    return "\n".join(part for part in parts if part)


def tool_content(name: str, arguments: Any) -> dict[str, Any]:
    """Map a Pi tool call to a platform tool content mapping."""

    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = arguments
    else:
        parsed = arguments
    args = parsed if isinstance(parsed, Mapping) else {}
    kind = _TOOL_KINDS.get(name, "tool_call")
    if name.startswith(("mcp__", "mcp:")):
        kind = "mcp"
    content: dict[str, Any] = {
        "kind": kind,
        "title": name,
        "toolName": name,
        "input": parsed if parsed != {} else args,
    }
    if kind == "command":
        command = args.get("command")
        if isinstance(command, str):
            content["command"] = command
    elif kind == "file_change":
        path = args.get("path") or args.get("file_path") or args.get("filePath")
        if isinstance(path, str):
            content["path"] = path
    elif kind == "web_search":
        query = args.get("query") or args.get("q")
        if isinstance(query, str):
            content["query"] = query
    elif kind == "mcp":
        content["name"] = name
    return content


def tool_exit_code(details: Any) -> int | None:
    if isinstance(details, Mapping):
        value = details.get("exitCode")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def tool_result_changes(content: Mapping[str, Any], details: Any) -> list[dict[str, Any]] | None:
    """Build the platform ``changes`` array for a completed file-change call.

    The platform renders ``diff`` entries as a diff panel when the text is a
    unified diff, which Pi provides in ``details.patch``; ``details.diff`` is
    the looser fallback. The path comes from the retained tool call input.
    """

    if not isinstance(details, Mapping):
        return None
    diff = _as_text(details.get("patch")) or _as_text(details.get("diff"))
    if diff is None:
        return None
    arguments = content.get("input")
    args = arguments if isinstance(arguments, Mapping) else {}
    path: str | None = None
    for key in ("path", "file_path", "filePath"):
        path = _as_text(args.get(key))
        if path is not None:
            break
    if path is None:
        return None
    return [{"path": path, "action": "modify", "diff": diff}]


class TranscriptProjector:
    """Builds an ordered timeline for one Pi session."""

    def __init__(
        self,
        session_id: str,
        external_session_id: str,
        *,
        client_messages: Sequence[tuple[str, str]] = (),
        max_items: int | None = None,
    ) -> None:
        self.session_id = session_id
        self.external_session_id = external_session_id
        self._items: list[RuntimeTimelineItem] = []
        # A truncated snapshot only needs the tail, so cap the retained items
        # instead of materializing every item just to slice it afterwards.
        self._max_items = max_items if max_items and max_items > 0 else None
        self._dropped_items = 0
        self._appended_items = 0
        self._last_assistant_status: str | None = None
        self._tool_index: dict[str, int] = {}
        self._order = 0
        self._turn_counter = 0
        self._current_turn_id: str | None = None
        self._client_messages: list[tuple[str, str]] = list(client_messages)

    # -- public API ---------------------------------------------------------

    def project_entries(
        self, entries: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]]
    ) -> None:
        for entry in entries:
            self.apply_entry(entry)
        self.end_turn(status="done")

    def items(self) -> tuple[RuntimeTimelineItem, ...]:
        return tuple(self._items)

    # -- entry level --------------------------------------------------------

    def apply_entry(self, entry: Mapping[str, Any]) -> None:
        entry_type = entry.get("type")
        if entry_type == "message":
            message = entry.get("message")
            if isinstance(message, Mapping):
                self.apply_message(message, entry_id=_entry_id(entry))
        elif entry_type == "compaction":
            self._add_marker(
                key=_entry_id(entry) or "compaction",
                label="Context compacted",
                text=_as_text(entry.get("summary")),
                kind="compact",
            )
        elif entry_type == "branch_summary":
            summary = _as_text(entry.get("summary"))
            if summary:
                self._add_system_text(
                    key=_entry_id(entry) or "branch-summary",
                    text=summary,
                    kind="notice",
                )
        elif entry_type == "custom" and entry.get("customType") == BTW_CUSTOM_TYPE:
            self._apply_btw_entry(entry)
        elif entry_type == "custom_message":
            content = content_text(entry.get("content"))
            if content and entry.get("display") is not False:
                self._add_system_text(
                    key=_entry_id(entry) or "custom-message",
                    text=content,
                    kind="system",
                )

    def _apply_btw_entry(self, entry: Mapping[str, Any]) -> None:
        """Project a headless ``/btw`` exchange into the timeline.

        The extension writes the question plus the answer (or the error) via
        ``pi.appendEntry``; custom entries never enter the LLM context, so the
        side question stays out of the conversation history.
        """

        data = entry.get("data")
        if not isinstance(data, Mapping):
            return
        question = _as_text(data.get("question"))
        if question is None:
            return
        answer = _as_text(data.get("answer"))
        error = _as_text(data.get("error"))
        if error:
            text, kind = f"/btw {question}\n\n{error}", "error"
        elif answer:
            text, kind = f"/btw {question}\n\n{answer}", "system"
        else:
            return
        self._add_system_text(key=f"{_entry_id(entry) or 'btw'}:btw", text=text, kind=kind)

    # -- message level ------------------------------------------------------

    def apply_message(self, message: Mapping[str, Any], *, entry_id: str | None) -> None:
        role = message.get("role")
        if role == "user":
            self.end_turn(status=self._turn_end_status())
            self.begin_turn()
            text = content_text(message.get("content"))
            if text:
                self._add_message(
                    key=f"{entry_id or 'user'}:user",
                    role="user",
                    text=text,
                    client_message_id=self._client_message_id_for(text),
                )
        elif role == "assistant":
            self._apply_assistant(message, entry_id=entry_id)
        elif role == "toolResult":
            self._apply_tool_result(message, entry_id=entry_id)
        elif role == "bashExecution":
            self._apply_bash_execution(message, entry_id=entry_id)
        elif role == "custom":
            text = content_text(message.get("content"))
            if text and message.get("display") is not False:
                self._add_system_text(
                    key=f"{entry_id or 'custom'}:body",
                    text=text,
                    kind="system",
                )
        elif role == "branchSummary":
            summary = _as_text(message.get("summary"))
            if summary:
                self._add_system_text(
                    key=f"{entry_id or 'branch-summary'}:body",
                    text=summary,
                    kind="notice",
                )
        elif role == "compactionSummary":
            self._add_marker(
                key=f"{entry_id or 'compaction'}:marker",
                label="Context compacted",
                text=_as_text(message.get("summary")),
                kind="compact",
            )

    def _apply_assistant(self, message: Mapping[str, Any], *, entry_id: str | None) -> None:
        if self._current_turn_id is None:
            self.begin_turn()
        base = entry_id or "assistant"
        blocks = message.get("content")
        stop_reason = message.get("stopReason")
        status = "done"
        if stop_reason == "aborted":
            status = "cancelled"
        elif stop_reason == "error":
            status = "failed"

        pending_kind: str | None = None
        pending_values: list[str] = []
        counters = {"text": 0, "thinking": 0, "tool": 0}

        def flush() -> None:
            nonlocal pending_kind
            if pending_values:
                if pending_kind == "thinking":
                    counters["thinking"] += 1
                    self._add_system_text(
                        key=f"{base}:reasoning:{counters['thinking']}",
                        text="\n\n".join(pending_values),
                        kind="reasoning",
                    )
                else:
                    counters["text"] += 1
                    self._add_message(
                        key=f"{base}:text:{counters['text']}",
                        role="assistant",
                        text="".join(pending_values),
                        status=status,
                        metadata=_assistant_metadata(message),
                    )
                pending_values.clear()
            pending_kind = None

        if isinstance(blocks, list):
            for index, block in enumerate(blocks):
                if not isinstance(block, Mapping):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        if pending_kind != "text":
                            flush()
                            pending_kind = "text"
                        pending_values.append(text)
                elif block_type == "thinking":
                    thinking = block.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        if pending_kind != "thinking":
                            flush()
                            pending_kind = "thinking"
                        pending_values.append(thinking)
                elif block_type == "toolCall":
                    flush()
                    counters["tool"] += 1
                    self._add_tool_call(
                        block,
                        key=f"{base}:tool:{index}",
                    )
        flush()
        if stop_reason == "error":
            error_message = _as_text(message.get("errorMessage"))
            self._add_system_text(
                key=f"{base}:error",
                text=error_message or "The model request failed.",
                kind="error",
            )

    def _apply_tool_result(self, message: Mapping[str, Any], *, entry_id: str | None) -> None:
        call_id = _as_text(message.get("toolCallId")) or _as_text(entry_id) or "tool"
        output = content_text(message.get("content"))
        is_error = message.get("isError") is True
        if call_id not in self._tool_index:
            # A result without a recorded call (e.g. truncated history).
            content = {
                "kind": "tool_result",
                "title": _as_text(message.get("toolName")) or "tool",
                "output": output,
                "result": message.get("details"),
                "isError": is_error,
            }
            self._append(
                key=f"{call_id}:result",
                item_type="tool",
                role="tool",
                status="failed" if is_error else "done",
                content=content,
                native_type="toolResult",
            )
            return
        index = self._tool_index[call_id]
        item = self._item_at(index)
        if item is None:
            # The call already left the retained window; the update is not
            # observable in the projection output.
            return
        completed_content = dict(item.content)
        completed_content["output"] = output
        completed_content["result"] = message.get("details")
        completed_content["isError"] = is_error
        if is_error and output:
            completed_content.setdefault("error", output)
        exit_code = tool_exit_code(message.get("details"))
        if exit_code is not None:
            completed_content["exitCode"] = exit_code
        if completed_content.get("kind") == "file_change":
            changes = tool_result_changes(completed_content, message.get("details"))
            if changes is not None:
                completed_content["changes"] = changes
        self._replace(
            index,
            status="failed" if is_error else "done",
            content=completed_content,
        )

    def _apply_bash_execution(self, message: Mapping[str, Any], *, entry_id: str | None) -> None:
        exit_code = message.get("exitCode")
        cancelled = message.get("cancelled") is True
        status = (
            "cancelled"
            if cancelled
            else ("failed" if isinstance(exit_code, int) and exit_code != 0 else "done")
        )
        content = {
            "kind": "command",
            "title": "bash",
            "command": _as_text(message.get("command")),
            "output": _as_text(message.get("output")),
        }
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            content["exitCode"] = exit_code
        self._append(
            key=f"{entry_id or 'bash'}:bash",
            item_type="tool",
            role="tool",
            status=status,
            content=content,
            native_type="bashExecution",
        )

    # -- turn helpers -------------------------------------------------------

    def begin_turn(self) -> str:
        if self._current_turn_id is not None:
            return self._current_turn_id
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter}"
        self._current_turn_id = turn_id
        self._append(
            key=f"{turn_id}:start",
            item_type="turn.start",
            role="system",
            status="done",
            content={"kind": "turn_start"},
            native_type="turn",
            turn_id=turn_id,
        )
        return turn_id

    def end_turn(self, *, status: str = "done") -> None:
        if self._current_turn_id is None:
            return
        turn_id = self._current_turn_id
        content: dict[str, Any] = {"kind": "turn_end"}
        if status != "done":
            content["reason"] = status
        self._append(
            key=f"{turn_id}:end",
            item_type="turn.end",
            role="system",
            status=status,
            content=content,
            native_type="turn",
            turn_id=turn_id,
        )
        self._current_turn_id = None

    def _turn_end_status(self) -> str:
        status = self._last_assistant_status
        if status == "failed":
            return "failed"
        if status == "cancelled":
            return "interrupted"
        return "done"

    # -- item construction --------------------------------------------------

    def _client_message_id_for(self, text: str) -> str | None:
        """Pair one projected user message with its platform client message id.

        The platform deduplicates optimistic local sends by this id; without
        it the echo renders next to the local copy. The lookup must be
        idempotent: the same transcript is projected repeatedly (live push
        plus the polling sync), so consuming a pair would drop the id from
        later projections and the echo would duplicate again. Identical
        texts take the most recent id, which is the one an in-flight
        optimistic send is waiting for.
        """

        target = text.strip()
        for candidate_text, client_message_id in reversed(self._client_messages):
            if candidate_text.strip() == target:
                return client_message_id
        return None

    def _add_message(
        self,
        *,
        key: str,
        role: str,
        text: str,
        status: str = "done",
        metadata: Mapping[str, Any] | None = None,
        client_message_id: str | None = None,
    ) -> RuntimeTimelineItem:
        if role == "assistant":
            self._last_assistant_status = status
        return self._append(
            key=key,
            item_type="message",
            role=role,
            status=status,
            content={"kind": "markdown", "format": "markdown", "text": text},
            native_type="message",
            metadata=metadata,
            client_message_id=client_message_id,
        )

    def _add_system_text(
        self,
        *,
        key: str,
        text: str,
        kind: str,
    ) -> RuntimeTimelineItem:
        return self._append(
            key=key,
            item_type="system",
            role="assistant",
            status="done",
            content={"kind": kind, "text": text},
            native_type=kind,
        )

    def _add_marker(
        self,
        *,
        key: str,
        label: str,
        text: str | None,
        kind: str,
    ) -> RuntimeTimelineItem:
        content: dict[str, Any] = {"kind": kind, "label": label}
        if text:
            content["text"] = text
        return self._append(
            key=key,
            item_type="marker",
            role="system",
            status="done",
            content=content,
            native_type=kind,
        )

    def _add_tool_call(self, block: Mapping[str, Any], *, key: str) -> RuntimeTimelineItem:
        call_id = _as_text(block.get("id")) or key
        name = _as_text(block.get("name")) or "tool"
        content = tool_content(name, block.get("arguments"))
        content["callId"] = call_id
        item = self._append(
            key=f"{key}:{call_id}",
            item_type="tool",
            role="assistant",
            status="running",
            content=content,
            native_type="toolCall",
        )
        self._tool_index[call_id] = self._appended_items - 1
        return item

    def _append(
        self,
        *,
        key: str,
        item_type: str,
        role: str | None,
        status: str,
        content: Mapping[str, Any],
        native_type: str,
        turn_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        client_message_id: str | None = None,
    ) -> RuntimeTimelineItem:
        self._order += 1
        item = self._build(
            key=key,
            order_seq=self._order,
            item_type=item_type,
            role=role,
            status=status,
            content=dict(content),
            native_type=native_type,
            turn_id=turn_id if turn_id is not None else self._current_turn_id,
            metadata=metadata,
            client_message_id=client_message_id,
        )
        self._items.append(item)
        self._appended_items += 1
        if self._max_items is not None and len(self._items) > self._max_items:
            dropped = len(self._items) - self._max_items
            del self._items[:dropped]
            self._dropped_items += dropped
        return item

    def _item_at(self, index: int) -> RuntimeTimelineItem | None:
        """Look up an appended item by its absolute append position."""

        position = index - self._dropped_items
        if 0 <= position < len(self._items):
            return self._items[position]
        return None

    def _replace(
        self,
        index: int,
        *,
        status: str,
        content: Mapping[str, Any],
    ) -> None:
        previous = self._item_at(index)
        if previous is None:
            return
        updated = replace(
            previous,
            status=status,
            content=dict(content),
            content_hash=timeline_content_hash(
                item_type=previous.type,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
                role=previous.role,  # type: ignore[arg-type]
                content=dict(content),
            ),
            revision=previous.revision + 1,
        )
        self._items[index - self._dropped_items] = updated

    def _build(
        self,
        *,
        key: str,
        order_seq: int,
        item_type: str,
        role: str | None,
        status: str,
        content: Mapping[str, Any],
        native_type: str,
        turn_id: str | None,
        metadata: Mapping[str, Any] | None,
        client_message_id: str | None = None,
    ) -> RuntimeTimelineItem:
        source = TimelineSource(
            runtime=RUNTIME,
            external_session_id=self.external_session_id,
            turn_id=turn_id,
            native_item_id=key,
            native_item_type=native_type,
            client_message_id=client_message_id,
        ).to_mapping()
        return RuntimeTimelineItem(
            id=item_id(self.external_session_id, item_type, key),
            session_id=self.session_id,
            type=item_type,
            status=status,
            order_seq=order_seq,
            content_hash=timeline_content_hash(
                item_type=item_type,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
                role=role,  # type: ignore[arg-type]
                content=dict(content),
            ),
            role=role,
            turn_id=turn_id,
            content=dict(content),
            source=source,
            revision=1,
            metadata=dict(metadata or {}),
        )


def project_session(
    doc_entries: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
    *,
    session_id: str,
    external_session_id: str,
    client_messages: Sequence[tuple[str, str]] = (),
    max_items: int | None = None,
) -> tuple[RuntimeTimelineItem, ...]:
    """Project the active branch of a parsed Pi session document.

    When ``max_items`` is set the projector retains only that many trailing
    items, which yields the same tail as projecting everything and slicing.
    """

    projector = TranscriptProjector(
        session_id,
        external_session_id,
        client_messages=client_messages,
        max_items=max_items,
    )
    projector.project_entries(doc_entries)
    return projector.items()


def _entry_id(entry: Mapping[str, Any]) -> str | None:
    value = entry.get("id")
    return value if isinstance(value, str) and value else None


def _as_text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _assistant_metadata(message: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for source_key, target_key in (
        ("model", "model"),
        ("provider", "provider"),
        ("stopReason", "stopReason"),
        ("responseModel", "responseModel"),
    ):
        value = message.get(source_key)
        if isinstance(value, str) and value:
            metadata[target_key] = value
    usage = message.get("usage")
    if isinstance(usage, Mapping):
        metadata["usage"] = dict(usage)
    return metadata
