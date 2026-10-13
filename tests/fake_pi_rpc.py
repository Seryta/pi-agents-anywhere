#!/usr/bin/env python3
"""Deterministic fake ``pi --mode rpc`` used by the test suite.

Behaviour is controlled by environment variables:

- ``PI_FAKE_SESSION_FILE``: session JSONL path (defaults to ``fake-session.jsonl``
  inside ``PI_FAKE_SESSIONS_DIR`` or the current directory).
- ``PI_FAKE_UI``: when set to ``confirm`` or ``select``, the first prompt emits an
  extension UI dialog and waits for ``extension_ui_response`` before settling.
- ``PI_FAKE_SESSIONS``: number of synthetic sessions to report for inventory
  (not used yet; sessions are discovered from files).
- ``PI_FAKE_BUSY_FIRST_PROMPT``: when ``1``, the first prompt flips the session
  to streaming and never settles; a later prompt with
  ``streamingBehavior == "followUp"`` is queued (reported via ``queue_update``),
  and unblocks the busy run: it settles, then the queued texts run as normal
  turns.
- ``PI_FAKE_CLEAR_QUEUE_DATA``: JSON text of the data block the ``clear_queue``
  response returns (``{"steering": [...], "followUp": [...]}``); defaults to
  the live queue contents.
- ``PI_FAKE_CLEAR_QUEUE_FAIL``: when ``1``, ``clear_queue`` answers
  ``success: false``.
- ``PI_FAKE_COMMANDS_FILE``: when set, every received command is appended as
  one JSON line, in arrival order (used to assert command sequences).
- ``PI_FAKE_CONTEXT_PERCENT``: percent reported in the ``get_session_stats``
  ``contextUsage`` block (default ``30``; ``none``, ``missing`` or an empty
  value omits the block entirely).
- The ``compact``, ``get_session_stats`` and ``export_html`` commands respond
  with the structures used by the connector's builtin command tests:
  ``compact`` echoes the received ``customInstructions`` through the command
  log and returns ``tokensBefore``/``estimatedTokensAfter``; ``export_html``
  returns ``outputPath`` (or ``<cwd>/session-export.html``).
- ``PI_FAKE_STREAM``: when ``1``, the assistant reply streams as several
  thinking/text deltas with tiny pauses (so the throttled flush can push a
  running snapshot) before the final ``message_end``.
- ``PI_FAKE_TOOLS``: when ``1``, a bash execution (start/update/end with
  ``details.exitCode``) and an edit execution (``details.patch``) are emitted
  after the streamed reply.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

VERSION = os.environ.get("PI_FAKE_VERSION", "9.9.9-fake")

# Queued text pi reports via ``queue_update`` events; the events carry the
# complete queues, so callers replace these wholesale.
QUEUE: dict[str, list[str]] = {"steering": [], "followUp": []}
_BUSY_FIRST_PROMPT = os.environ.get("PI_FAKE_BUSY_FIRST_PROMPT") == "1"
_STREAM = os.environ.get("PI_FAKE_STREAM") == "1"
_TOOLS = os.environ.get("PI_FAKE_TOOLS") == "1"
# Small enough to keep the suite fast, large enough for the runtime's 300ms
# flush loop to observe the running snapshot before the terminal event.
_STREAM_PAUSE_SECONDS = 0.05
_THINKING_TEXT = "check the question"

STATE: dict[str, object] = {
    "model": {
        "id": "test-model",
        "name": "Test Model",
        "provider": "test",
        "reasoning": True,
        "contextWindow": 128_000,
    },
    "thinkingLevel": "medium",
    "isStreaming": False,
    "isCompacting": False,
    "sessionFile": None,
    "sessionId": "00000000-0000-0000-0000-000000000001",
    "sessionName": None,
    "messageCount": 0,
    "waitingUi": False,
    "uiIssued": False,
    "firstPromptConsumed": False,
}


def emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def session_path() -> Path:
    configured = STATE["sessionFile"]
    if not isinstance(configured, str):
        configured = os.environ.get("PI_FAKE_SESSION_FILE")
        if not configured:
            base = os.environ.get("PI_FAKE_SESSIONS_DIR", ".")
            configured = str(Path(base) / "fake-session.jsonl")
        STATE["sessionFile"] = configured
    return Path(configured)


def last_entry_id() -> str | None:
    path = session_path()
    if not path.is_file():
        return None
    last: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        entry_id = record.get("id")
        if isinstance(entry_id, str):
            last = entry_id
    return last


def append(record: dict) -> None:
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def ensure_header() -> None:
    path = session_path()
    if not path.is_file():
        append(
            {
                "type": "session",
                "version": 3,
                "id": STATE["sessionId"],
                "timestamp": "2026-01-01T00:00:00.000Z",
                "cwd": os.getcwd(),
            }
        )


def state_data() -> dict:
    return {
        "model": STATE["model"],
        "thinkingLevel": STATE["thinkingLevel"],
        "isStreaming": STATE["isStreaming"],
        "isCompacting": STATE["isCompacting"],
        "sessionFile": str(session_path()),
        "sessionId": STATE["sessionId"],
        "sessionName": STATE["sessionName"],
        "messageCount": STATE["messageCount"],
        "pendingMessageCount": len(QUEUE["steering"]) + len(QUEUE["followUp"]),
    }


def settle_after_ui() -> None:
    settle_current_turn()


def queue_snapshot() -> dict[str, list[str]]:
    return {"steering": list(QUEUE["steering"]), "followUp": list(QUEUE["followUp"])}


def emit_queue_update() -> None:
    """Emit the full current queue, as pi's ``queue_update`` contract does."""

    snapshot = queue_snapshot()
    emit(
        {
            "type": "queue_update",
            "steering": snapshot["steering"],
            "followUp": snapshot["followUp"],
        }
    )


def log_command(command: dict) -> None:
    """Append one line per received command for sequence assertions."""

    path = os.environ.get("PI_FAKE_COMMANDS_FILE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(command, ensure_ascii=False) + "\n")


def first_prompt_stays_busy() -> bool:
    """Busy-first mode keeps the first prompt streaming without settling."""

    if not _BUSY_FIRST_PROMPT or STATE["firstPromptConsumed"]:
        return False
    STATE["firstPromptConsumed"] = True
    return True


def settle_current_turn() -> None:
    STATE["isStreaming"] = False
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled"})


def drain_queued_turns() -> None:
    """Settle the busy run, then take queued texts as ordinary turns."""

    settle_current_turn()
    for kind in ("followUp", "steering"):
        while QUEUE[kind]:
            text = QUEUE[kind].pop(0)
            emit_queue_update()
            handle_prompt({"message": text})


def issue_ui_if_needed() -> bool:
    method = os.environ.get("PI_FAKE_UI")
    if not method or STATE["uiIssued"] or method not in ("confirm", "select", "input"):
        return False
    STATE["uiIssued"] = True
    STATE["waitingUi"] = True
    request: dict = {
        "type": "extension_ui_request",
        "id": "ui-request-1",
        "method": method,
        "title": "Fake dialog",
    }
    if method == "confirm":
        request["message"] = "Proceed?"
    if method == "select":
        request["options"] = ["alpha", "beta"]
    emit(request)
    return True


def _reply_chunks(reply: str) -> list[str]:
    """Split a reply into three deltas for streaming mode."""

    size = max(1, len(reply) // 3)
    chunks = [reply[:size], reply[size : 2 * size], reply[2 * size :]]
    return [chunk for chunk in chunks if chunk]


def emit_tool_activity() -> None:
    """Emit one bash and one edit execution through their full lifecycle."""

    bash_args = {"command": "echo hi"}
    emit(
        {
            "type": "tool_execution_start",
            "toolCallId": "call-bash",
            "toolName": "bash",
            "args": bash_args,
        }
    )
    time.sleep(_STREAM_PAUSE_SECONDS)
    emit(
        {
            "type": "tool_execution_update",
            "toolCallId": "call-bash",
            "toolName": "bash",
            "args": bash_args,
            "partialResult": {
                "content": [{"type": "text", "text": "partial output"}],
                "details": {},
            },
        }
    )
    time.sleep(_STREAM_PAUSE_SECONDS)
    emit(
        {
            "type": "tool_execution_end",
            "toolCallId": "call-bash",
            "toolName": "bash",
            "result": {
                "content": [{"type": "text", "text": "final output"}],
                "details": {"exitCode": 0},
            },
            "isError": False,
            "durationMs": 12,
        }
    )
    edit_args = {"path": "/tmp/example.txt"}
    emit(
        {
            "type": "tool_execution_start",
            "toolCallId": "call-edit",
            "toolName": "edit",
            "args": edit_args,
        }
    )
    time.sleep(_STREAM_PAUSE_SECONDS)
    emit(
        {
            "type": "tool_execution_end",
            "toolCallId": "call-edit",
            "toolName": "edit",
            "result": {
                "content": [{"type": "text", "text": "File updated"}],
                "details": {"patch": "-old\n+new", "exitCode": 0},
            },
            "isError": False,
            "durationMs": 3,
        }
    )


def handle_prompt_command(command: dict) -> None:
    """Queue follow-up/steering texts, or run a plain prompt as a turn."""

    behavior = command.get("streamingBehavior")
    text = command.get("message", "")
    if behavior == "followUp":
        QUEUE["followUp"].append(str(text))
        emit_queue_update()
        # Busy-first mode: the queued message unblocks the first run; the
        # draining turns then answer every queued text.
        if _BUSY_FIRST_PROMPT:
            drain_queued_turns()
        return
    if behavior == "steer":
        QUEUE["steering"].append(str(text))
        emit_queue_update()
        return
    handle_prompt(command)


def handle_prompt(command: dict) -> None:
    text = command.get("message", "")
    images = command.get("images") or []
    # Pi renders image inputs as block content on the persisted user message.
    content = text if not images else [{"type": "text", "text": text}, *images]
    ensure_header()
    STATE["isStreaming"] = True
    user_id = uuid.uuid4().hex[:8]
    append(
        {
            "type": "message",
            "id": user_id,
            "parentId": last_entry_id(),
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {"role": "user", "content": content, "timestamp": 1},
        }
    )
    reply = "reply to " + str(text)
    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    emit({"type": "message_start", "message": {"role": "user", "content": content}})
    emit({"type": "message_end", "message": {"role": "user", "content": content}})
    emit(
        {
            "type": "message_start",
            "message": {"role": "assistant", "content": [], "stopReason": "pending"},
        }
    )
    if _STREAM:
        # Text first, then thinking: the first delta must open the text item so
        # the flush loop can push a running snapshot of it before message_end.
        emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_start", "contentIndex": 0},
            }
        )
        for chunk in _reply_chunks(reply):
            time.sleep(_STREAM_PAUSE_SECONDS)
            emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "contentIndex": 0,
                        "delta": chunk,
                    },
                }
            )
        emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_end",
                    "contentIndex": 0,
                    "content": reply,
                },
            }
        )
        emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 1},
            }
        )
        for chunk in _THINKING_TEXT.split(" "):
            time.sleep(_STREAM_PAUSE_SECONDS)
            emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "thinking_delta",
                        "contentIndex": 1,
                        "delta": chunk + " ",
                    },
                }
            )
        emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_end",
                    "contentIndex": 1,
                    "content": _THINKING_TEXT,
                },
            }
        )
        content_blocks: list[dict] = [
            {"type": "thinking", "thinking": _THINKING_TEXT},
            {"type": "text", "text": reply},
        ]
    else:
        emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "contentIndex": 0,
                    "delta": reply,
                },
            }
        )
        content_blocks = [{"type": "text", "text": reply}]
    assistant_id = uuid.uuid4().hex[:8]
    append(
        {
            "type": "message",
            "id": assistant_id,
            "parentId": user_id,
            "timestamp": "2026-01-01T00:00:02.000Z",
            "message": {
                "role": "assistant",
                "content": content_blocks,
                "provider": "test",
                "model": "test-model",
                "stopReason": "stop",
                "usage": {
                    "input": 1,
                    "output": 1,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 2,
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "total": 0,
                    },
                },
                "timestamp": 2,
            },
        }
    )
    STATE["messageCount"] = int(STATE["messageCount"]) + 2  # type: ignore[call-overload]
    emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": content_blocks,
                "stopReason": "stop",
            },
        }
    )
    if _TOOLS:
        emit_tool_activity()
    emit({"type": "turn_end", "message": {"role": "assistant"}, "toolResults": []})
    busy = first_prompt_stays_busy()
    if not issue_ui_if_needed() and not busy:
        settle_current_turn()


def handle_command(command: dict) -> None:
    command_type = command.get("type")
    request_id = command.get("id")
    log_command(command)
    if command_type == "get_state":
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "get_state",
                "success": True,
                "data": state_data(),
            }
        )
    elif command_type == "prompt":
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "prompt",
                "success": True,
            }
        )
        handle_prompt_command(command)
    elif command_type == "steer":
        emit({"id": request_id, "type": "response", "command": "steer", "success": True})
    elif command_type == "clear_queue":
        if os.environ.get("PI_FAKE_CLEAR_QUEUE_FAIL") == "1":
            emit(
                {
                    "id": request_id,
                    "type": "response",
                    "command": "clear_queue",
                    "success": False,
                    "error": "fake clear_queue failure",
                }
            )
            return
        data = queue_snapshot()
        preset = os.environ.get("PI_FAKE_CLEAR_QUEUE_DATA")
        if preset:
            try:
                preset_data = json.loads(preset)
            except json.JSONDecodeError:
                preset_data = None
            if isinstance(preset_data, dict):
                preset_steering = preset_data.get("steering")
                preset_followup = preset_data.get("followUp")
                data = {
                    "steering": (
                        [item for item in preset_steering if isinstance(item, str)]
                        if isinstance(preset_steering, list)
                        else []
                    ),
                    "followUp": (
                        [item for item in preset_followup if isinstance(item, str)]
                        if isinstance(preset_followup, list)
                        else []
                    ),
                }
        QUEUE["steering"] = []
        QUEUE["followUp"] = []
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "clear_queue",
                "success": True,
                "data": data,
            }
        )
        emit_queue_update()
    elif command_type == "abort":
        STATE["isStreaming"] = False
        emit({"id": request_id, "type": "response", "command": "abort", "success": True})
        emit({"type": "agent_settled"})
    elif command_type == "set_model":
        model = dict(STATE["model"])  # type: ignore[arg-type]
        if isinstance(command.get("modelId"), str):
            model["id"] = command["modelId"]
        if isinstance(command.get("provider"), str):
            model["provider"] = command["provider"]
        STATE["model"] = model
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "set_model",
                "success": True,
                "data": model,
            }
        )
    elif command_type == "set_thinking_level":
        STATE["thinkingLevel"] = command.get("level")
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "set_thinking_level",
                "success": True,
                "data": {"level": STATE["thinkingLevel"]},
            }
        )
        emit({"type": "thinking_level_changed", "level": STATE["thinkingLevel"]})
    elif command_type == "set_session_name":
        name = command.get("name")
        STATE["sessionName"] = name
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "set_session_name",
                "success": True,
                "data": {"name": name},
            }
        )
        emit({"type": "session_info_changed", "name": name})
    elif command_type == "get_available_models":
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "get_available_models",
                "success": True,
                "data": {
                    "models": [
                        dict(STATE["model"]),  # type: ignore[arg-type]
                        {
                            "id": "other-model",
                            "name": "Other Model",
                            "provider": "test",
                        },
                        # Same model name under another provider: pi allows
                        # it, so the catalog must keep ids unique and label
                        # the titles.
                        {
                            "id": "test-model",
                            "name": "Test Model",
                            "provider": "alt",
                        },
                    ]
                },
            }
        )
    elif command_type == "get_commands":
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "get_commands",
                "success": True,
                "data": {
                    "commands": [
                        {
                            "name": "fix-tests",
                            "description": "Fix failing tests",
                            "source": "prompt",
                        }
                    ]
                },
            }
        )
    elif command_type == "compact":
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "compact",
                "success": True,
                "data": {
                    "summary": "fake compacted summary",
                    "firstKeptEntryId": last_entry_id() or "entry-0",
                    "tokensBefore": 150000,
                    "estimatedTokensAfter": 32000,
                    "usage": {
                        "input": 1,
                        "output": 1,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                },
            }
        )
    elif command_type == "get_session_stats":
        context_window = 128_000
        percent_raw = os.environ.get("PI_FAKE_CONTEXT_PERCENT", "30")
        stats: dict = {
            "tokens": {
                "input": 12000,
                "output": 8000,
                "cacheRead": 1000,
                "cacheWrite": 500,
                "total": 21500,
            },
            "cost": 0.0123,
            "userMessages": 3,
        }
        if percent_raw.strip().lower() not in ("", "none", "missing"):
            try:
                percent = float(percent_raw)
            except ValueError:
                percent = 30.0
            stats["contextUsage"] = {
                "tokens": int(context_window * percent / 100),
                "contextWindow": context_window,
                "percent": percent,
            }
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "get_session_stats",
                "success": True,
                "data": stats,
            }
        )
    elif command_type == "export_html":
        output_path = command.get("outputPath")
        if not isinstance(output_path, str) or not output_path:
            output_path = str(Path(os.getcwd()) / "session-export.html")
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "export_html",
                "success": True,
                "data": {"path": output_path},
            }
        )
    elif command_type == "extension_ui_response":
        if STATE["waitingUi"]:
            STATE["waitingUi"] = False
            settle_after_ui()
    else:
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": command_type,
                "success": False,
                "error": f"unsupported fake command {command_type!r}",
            }
        )


def run_rpc() -> int:
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            command = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(command, dict):
            continue
        handle_command(command)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        print(VERSION)
        return 0
    if "--mode" in argv and "rpc" in argv:
        return run_rpc()
    print(f"fake pi: unsupported arguments {argv!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
