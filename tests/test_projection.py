from __future__ import annotations

from pi_aa.projection import project_session, tool_result_changes


def entries() -> list[dict]:
    return [
        {
            "type": "message",
            "id": "u1",
            "parentId": None,
            "timestamp": "t",
            "message": {"role": "user", "content": "你好", "timestamp": 1},
        },
        {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "timestamp": "t",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "先看文件"},
                    {
                        "type": "toolCall",
                        "id": "call_1",
                        "name": "bash",
                        "arguments": {"command": "ls -la"},
                    },
                    {"type": "text", "text": "看完了"},
                ],
                "model": "test-model",
                "provider": "test",
                "stopReason": "toolUse",
                "timestamp": 2,
            },
        },
        {
            "type": "message",
            "id": "r1",
            "parentId": "a1",
            "timestamp": "t",
            "message": {
                "role": "toolResult",
                "toolCallId": "call_1",
                "toolName": "bash",
                "content": [{"type": "text", "text": "total 0\n"}],
                "details": {"exitCode": 0},
                "isError": False,
                "timestamp": 3,
            },
        },
    ]


def test_projection_shapes() -> None:
    items = project_session(entries(), session_id="sess-1", external_session_id="/tmp/s.jsonl")
    types = [item.type for item in items]
    assert types == [
        "turn.start",
        "message",
        "system",
        "tool",
        "message",
        "turn.end",
    ]
    user_item = items[1]
    assert user_item.role == "user"
    assert user_item.content["text"] == "你好"
    reasoning = items[2]
    assert reasoning.content == {"kind": "reasoning", "text": "先看文件"}
    tool = items[3]
    assert tool.status == "done"
    assert tool.content["kind"] == "command"
    assert tool.content["command"] == "ls -la"
    assert tool.content["output"] == "total 0\n"
    assert tool.content["exitCode"] == 0
    assistant = items[4]
    assert assistant.content["kind"] == "markdown"
    assert assistant.content["text"] == "看完了"


def test_projection_ids_and_order_are_stable() -> None:
    first = project_session(entries(), session_id="sess-1", external_session_id="/tmp/s.jsonl")
    second = project_session(entries(), session_id="sess-1", external_session_id="/tmp/s.jsonl")
    assert [item.id for item in first] == [item.id for item in second]
    assert [item.order_seq for item in first] == list(range(1, len(first) + 1))
    for item in first:
        assert item.session_id == "sess-1"
        assert item.content_hash.startswith("sha256:")


def test_client_message_id_attaches_to_user_message() -> None:
    """The platform dedupes optimistic sends by this metadata id."""

    items = project_session(
        entries(),
        session_id="s",
        external_session_id="/tmp/s.jsonl",
        client_messages=[("你好", "cm-1")],
    )
    user_item = next(item for item in items if item.role == "user")
    assert user_item.source["clientMessageId"] == "cm-1"


def test_client_message_id_lookup_is_idempotent() -> None:
    """Repeated projections must still find the id; the sync re-projects."""

    records = [
        {
            "type": "message",
            "id": "u1",
            "parentId": None,
            "timestamp": "t",
            "message": {"role": "user", "content": "继续", "timestamp": 1},
        },
        {
            "type": "message",
            "id": "u2",
            "parentId": "u1",
            "timestamp": "t",
            "message": {"role": "user", "content": "继续", "timestamp": 2},
        },
    ]
    client_messages = [("继续", "cm-1"), ("继续", "cm-2")]
    first = project_session(
        records,
        session_id="s",
        external_session_id="/tmp/s.jsonl",
        client_messages=client_messages,
    )
    second = project_session(
        records,
        session_id="s",
        external_session_id="/tmp/s.jsonl",
        client_messages=client_messages,
    )
    pick = lambda items: [
        item.source.get("clientMessageId") for item in items if item.role == "user"
    ]
    assert pick(first) == pick(second), "a second projection must not lose ids"
    # Identical texts take the most recent id (the in-flight optimistic send).
    assert pick(first) == ["cm-2", "cm-2"]


def test_compaction_marker() -> None:
    records = [
        {
            "type": "compaction",
            "id": "c1",
            "parentId": None,
            "timestamp": "t",
            "summary": "earlier context",
            "firstKeptEntryId": "c1",
        },
    ]
    items = project_session(records, session_id="s", external_session_id="/tmp/s.jsonl")
    assert len(items) == 1
    assert items[0].type == "marker"
    assert items[0].content["kind"] == "compact"


def long_transcript() -> list[dict]:
    """Four turns of user/assistant/tool traffic to slice from many angles."""

    records: list[dict] = []
    parent: str | None = None

    def add(entry: dict, entry_id: str) -> None:
        nonlocal parent
        entry["id"] = entry_id
        entry["parentId"] = parent
        entry["timestamp"] = "t"
        records.append(entry)
        parent = entry_id

    for turn in range(4):
        add(
            {
                "type": "message",
                "message": {"role": "user", "content": f"问题 {turn}", "timestamp": turn},
            },
            f"u{turn}",
        )
        add(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "timestamp": turn,
                    "content": [
                        {"type": "text", "text": f"先看 {turn}"},
                        {
                            "type": "toolCall",
                            "id": f"call_{turn}",
                            "name": "bash",
                            "arguments": {"command": f"ls {turn}"},
                        },
                    ],
                },
            },
            f"a{turn}",
        )
        add(
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": f"call_{turn}",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": f"out {turn}"}],
                    "details": {"exitCode": 0},
                    "isError": False,
                    "timestamp": turn,
                },
            },
            f"r{turn}",
        )
        add(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "timestamp": turn,
                    "content": [{"type": "text", "text": f"完成 {turn}"}],
                },
            },
            f"b{turn}",
        )
    return records


def test_windowed_projection_matches_full_tail() -> None:
    """A capped projection must equal the tail of the full one, item for item."""

    records = long_transcript()
    full = project_session(records, session_id="s", external_session_id="/tmp/s.jsonl")
    assert len(full) > 12
    for limit in (1, 2, 3, 5, 8, 13, len(full), len(full) + 5):
        windowed = project_session(
            records,
            session_id="s",
            external_session_id="/tmp/s.jsonl",
            max_items=limit,
        )
        assert windowed == full[-limit:], f"limit={limit}"


def test_windowed_projection_drops_tool_result_for_dropped_call() -> None:
    """A call that left the window must not resurface as a standalone result."""

    records: list[dict] = [
        {
            "type": "message",
            "id": "u1",
            "parentId": None,
            "timestamp": "t",
            "message": {"role": "user", "content": "开始", "timestamp": 1},
        },
        {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "timestamp": "t",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "timestamp": 2,
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call_late",
                        "name": "bash",
                        "arguments": {"command": "sleep 1"},
                    }
                ],
            },
        },
    ]
    parent = "a1"
    for index in range(6):
        records.append(
            {
                "type": "message",
                "id": f"fill{index}",
                "parentId": parent,
                "timestamp": "t",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "timestamp": 10 + index,
                    "content": [{"type": "text", "text": f"填充 {index}"}],
                },
            }
        )
        parent = f"fill{index}"
    records.append(
        {
            "type": "message",
            "id": "r1",
            "parentId": parent,
            "timestamp": "t",
            "message": {
                "role": "toolResult",
                "toolCallId": "call_late",
                "toolName": "bash",
                "content": [{"type": "text", "text": "done"}],
                "details": {"exitCode": 0},
                "isError": False,
                "timestamp": 99,
            },
        }
    )

    full = project_session(records, session_id="s", external_session_id="/tmp/s.jsonl")
    limit = 4
    assert all(
        not (item.type == "tool" and item.content.get("command") == "sleep 1")
        for item in full[-limit:]
    ), "test setup: the call must be outside the retained tail"
    windowed = project_session(
        records,
        session_id="s",
        external_session_id="/tmp/s.jsonl",
        max_items=limit,
    )
    assert windowed == full[-limit:]
    assert not any(item.content.get("kind") == "tool_result" for item in windowed)


def test_windowed_projection_keeps_turn_end_status() -> None:
    """Turn completion status may come from an assistant item outside the window."""

    records: list[dict] = [
        {
            "type": "message",
            "id": "u1",
            "parentId": None,
            "timestamp": "t",
            "message": {"role": "user", "content": "第一步", "timestamp": 1},
        },
        {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "timestamp": "t",
            "message": {
                "role": "assistant",
                "stopReason": "aborted",
                "timestamp": 2,
                "content": [{"type": "text", "text": "被中断"}],
            },
        },
    ]
    parent = "a1"
    for index in range(8):
        records.append(
            {
                "type": "custom_message",
                "id": f"fill{index}",
                "parentId": parent,
                "timestamp": "t",
                "content": f"填充 {index}",
                "display": True,
            }
        )
        parent = f"fill{index}"
    records.append(
        {
            "type": "message",
            "id": "u2",
            "parentId": parent,
            "timestamp": "t",
            "message": {"role": "user", "content": "第二步", "timestamp": 99},
        }
    )
    records.append(
        {
            "type": "message",
            "id": "a2",
            "parentId": "u2",
            "timestamp": "t",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "timestamp": 100,
                "content": [{"type": "text", "text": "继续"}],
            },
        }
    )

    full = project_session(records, session_id="s", external_session_id="/tmp/s.jsonl")
    ends = [item for item in full if item.type == "turn.end"]
    assert ends and ends[0].status == "interrupted"

    limit = 5
    assert any(item.type == "turn.end" and item.status == "interrupted" for item in full[-limit:])
    windowed = project_session(
        records,
        session_id="s",
        external_session_id="/tmp/s.jsonl",
        max_items=limit,
    )
    assert windowed == full[-limit:]


def edit_turn(arguments: dict, details: dict) -> list[dict]:
    """An assistant edit call followed by its tool result."""

    return [
        {
            "type": "message",
            "id": "u1",
            "parentId": None,
            "timestamp": "t",
            "message": {"role": "user", "content": "改文件", "timestamp": 1},
        },
        {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "timestamp": "t",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "timestamp": 2,
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call_edit",
                        "name": "edit",
                        "arguments": arguments,
                    }
                ],
            },
        },
        {
            "type": "message",
            "id": "r1",
            "parentId": "a1",
            "timestamp": "t",
            "message": {
                "role": "toolResult",
                "toolCallId": "call_edit",
                "toolName": "edit",
                "content": [{"type": "text", "text": "edited"}],
                "details": details,
                "isError": False,
                "timestamp": 3,
            },
        },
    ]


def only_tool_item(records: list[dict]):
    items = project_session(records, session_id="s", external_session_id="/tmp/s.jsonl")
    (tool,) = [item for item in items if item.type == "tool"]
    return tool


def test_edit_tool_result_projects_patch_into_changes() -> None:
    """The platform renders this as a diff panel, so the patch must survive."""

    patch = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    tool = only_tool_item(
        edit_turn(
            {"path": "foo.py", "oldText": "old", "newText": "new"},
            # patch is the unified diff; it wins over the looser diff detail.
            {"patch": patch, "diff": "1 old\n1 new\n"},
        )
    )
    assert tool.content["kind"] == "file_change"
    assert tool.content["changes"] == [{"path": "foo.py", "action": "modify", "diff": patch}]


def test_edit_tool_result_falls_back_to_diff_detail() -> None:
    diff = "@@ -1 +1 @@\n-old\n+new\n"
    tool = only_tool_item(
        edit_turn(
            {"file_path": "src/bar.py", "oldText": "old", "newText": "new"},
            {"diff": diff},
        )
    )
    assert tool.content["changes"] == [{"path": "src/bar.py", "action": "modify", "diff": diff}]


def test_edit_tool_result_without_diff_has_no_changes() -> None:
    tool = only_tool_item(edit_turn({"filePath": "baz.py", "oldText": "o", "newText": "n"}, {}))
    assert "changes" not in tool.content


def test_non_file_change_tool_result_has_no_changes() -> None:
    """Only file_change items carry changes, even when details contain a diff."""

    records = edit_turn({"file_path": "x.py", "oldText": "o", "newText": "n"}, {"patch": "d"})
    # Swap the recorded edit call for a bash call; the result keeps the patch.
    records[1]["message"]["content"][0]["name"] = "bash"
    records[1]["message"]["content"][0]["arguments"] = {"command": "true"}
    tool = only_tool_item(records)
    assert tool.content["kind"] == "command"
    assert "changes" not in tool.content


def test_tool_result_changes_needs_a_path() -> None:
    """A diff without a file path in the tool call input cannot be projected."""

    assert tool_result_changes({"input": {"oldText": "old"}}, {"patch": "d"}) is None
