from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from connector.runtime_protocol import (
    CAPABILITY_RUNTIME_ATTACHMENT,
    RuntimeAttachment,
    RuntimeAttachmentContent,
)

from pi_aa.runtime import PiLiveSession
from tests.conftest import FakeHost
from tests.test_runtime import make_runtime, write_messages_session


@pytest.mark.parametrize("limit", [None, 3])
async def test_snapshot_read_does_not_acknowledge_delivery(fake_pi, tmp_path, fake_host, limit):
    path = tmp_path / "sessions" / "snapshot.jsonl"
    write_messages_session(path, 20)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    session = (await runtime.list_sessions())[0]
    await runtime.get_session_snapshot(session.session_id, str(path), limit=limit)
    assert (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"]
    prepared = await runtime.prepare_session_timeline_sync(session.session_id, str(path))
    assert prepared.snapshot.complete
    await prepared.commit()
    assert not (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"]


@pytest.mark.parametrize("failure", ["publish", "checkpoint", None])
async def test_settle_acknowledges_only_successful_delivery(
    fake_pi, tmp_path, fake_host, monkeypatch, failure
):
    path = tmp_path / "sessions" / "settle.jsonl"
    write_messages_session(path, 3)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    session = (await runtime.list_sessions())[0]
    live = PiLiveSession(runtime, session.session_id, cwd=str(tmp_path), session_path=str(path))

    async def fail(*args, **kwargs):
        raise OSError("backend unavailable")

    with monkeypatch.context() as patch:
        if failure:
            patch.setattr(
                fake_host, "timeline_sync" if failure == "publish" else "sync_state_write", fail
            )
        await runtime._publish_timeline(live)
    assert (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"] == bool(
        failure
    )
    if failure:
        # A failed settle remains eligible for a full scanner retry.
        prepared = await runtime.prepare_session_timeline_sync(session.session_id, str(path))
        assert prepared.snapshot.complete
        await fake_host.timeline_sync(
            session.session_id, "pi", prepared.snapshot.items, complete=True
        )
        await prepared.commit()
        assert not (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"]


async def test_file_append_during_publish_remains_unsynced(
    fake_pi, tmp_path, fake_host, monkeypatch
):
    path = tmp_path / "sessions" / "growing.jsonl"
    write_messages_session(path, 3)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    session = (await runtime.list_sessions())[0]
    live = PiLiveSession(runtime, session.session_id, cwd=str(tmp_path), session_path=str(path))

    async def append_during_send(*args, **kwargs):
        await FakeHost.timeline_sync(fake_host, *args, **kwargs)
        write_messages_session(path, 4)

    monkeypatch.setattr(fake_host, "timeline_sync", append_during_send)
    await runtime._publish_timeline(live)
    assert (await runtime.list_sessions())[0].metadata["sync"]["requires_timeline_sync"]
    prepared = await runtime.prepare_session_timeline_sync(session.session_id, str(path))
    assert prepared.snapshot is not None
    assert any(item.content.get("text") == "message 3" for item in prepared.snapshot.items)


async def test_tool_completion_updates_same_length_timeline(fake_pi, tmp_path, fake_host):
    path = tmp_path / "sessions" / "tool.jsonl"
    write_messages_session(path, 20)
    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "message",
                    "id": "assistant",
                    "parentId": "ent00019",
                    "message": {
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": "call1",
                                "name": "bash",
                                "arguments": {"command": "echo hi"},
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    first = await runtime.prepare_session_timeline_sync("session", str(path))
    assert next(item for item in first.snapshot.items if item.type == "tool").status == "running"
    await first.commit()
    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "message",
                    "id": "result",
                    "parentId": "assistant",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "call1",
                        "toolName": "bash",
                        "content": [{"type": "text", "text": "hi"}],
                        "isError": False,
                    },
                }
            )
            + "\n"
        )
    prepared = await runtime.prepare_session_timeline_sync("session", str(path))
    assert prepared.snapshot is not None
    assert not prepared.snapshot.complete
    assert len(prepared.snapshot.items) < len(first.snapshot.items)
    tool = next(item for item in prepared.snapshot.items if item.type == "tool")
    assert tool.status == "done"
    assert tool.content["output"] == "hi"
    await prepared.commit()
    assert (await runtime.prepare_session_timeline_sync("session", str(path))).snapshot is None


@pytest.mark.parametrize("append", [False, True])
async def test_replaced_tail_ids_require_removing_old_branch(fake_pi, tmp_path, fake_host, append):
    path = tmp_path / "sessions" / "branch.jsonl"
    write_messages_session(path, 20)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    first = await runtime.prepare_session_timeline_sync("session", str(path))
    await first.commit()
    if append:
        write_messages_session(path, 21)
    path.write_text(path.read_text().replace('"ent00019"', '"branch-19"'))
    prepared = await runtime.prepare_session_timeline_sync("session", str(path))
    assert prepared.snapshot.complete
    old_ids = {item.id for item in first.snapshot.items}
    new_ids = {item.id for item in prepared.snapshot.items}
    assert old_ids - new_ids


@pytest.mark.parametrize("index,complete", [(3, True), (-1, False)])
@pytest.mark.parametrize("field", ["source", "metadata"])
async def test_reconciliation_fields_sync_even_with_same_content_hash(
    fake_pi, tmp_path, fake_host, monkeypatch, index, complete, field
):
    path = tmp_path / "sessions" / "binding.jsonl"
    write_messages_session(path, 20)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    first = await runtime.prepare_session_timeline_sync("session", str(path))
    await first.commit()
    project = runtime._project_session_items
    previous = first.snapshot.items[index]

    async def project_reconciled(*args, **kwargs):
        items = list(await project(*args, **kwargs))
        items[index] = replace(items[index], **{field: {"clientMessageId": "optimistic-message"}})
        return tuple(items)

    monkeypatch.setattr(runtime, "_project_session_items", project_reconciled)
    prepared = await runtime.prepare_session_timeline_sync("session", str(path))
    assert prepared.snapshot is not None
    assert prepared.snapshot.complete is complete
    reconciled = next(item for item in prepared.snapshot.items if item.id == previous.id)
    assert reconciled.content_hash == previous.content_hash
    assert getattr(reconciled, field)["clientMessageId"] == "optimistic-message"


async def test_attachment_capabilities_allow_files_on_new_and_existing_sessions(
    fake_pi, tmp_path, fake_host
):
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    for capabilities in (
        await runtime.get_runtime_capabilities(),
        await runtime.get_session_capabilities("session"),
    ):
        attachment = next(
            item
            for item in capabilities.capabilities
            if item.capability_id == CAPABILITY_RUNTIME_ATTACHMENT
        )
        assert attachment.supported and attachment.available and attachment.allowed
        # The official composer and server interpret a present list as the
        # exhaustive allowlist. Omitting it permits both images and documents.
        assert "allowedMimeTypes" not in attachment.metadata


@pytest.mark.parametrize(
    "media_type,name",
    [
        ("application/pdf", "document.pdf"),
        ("text/plain", "notes.txt"),
        ("image/svg+xml", "drawing.svg"),
        ("image/bmp", "drawing.bmp"),
        ("image/png", "drawing.png"),
        ("IMAGE/JPEG; charset=binary", "drawing.jpg"),
    ],
)
async def test_advertised_attachments_reach_pi_in_supported_form(
    fake_pi, tmp_path, fake_host, monkeypatch, media_type, name
):
    monkeypatch.setenv("AGENT_CONNECTOR_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    content = b"uploaded attachment content"

    async def download(*args):
        return RuntimeAttachmentContent(
            file_id="upload", name=name, media_type=media_type, content=content
        )

    monkeypatch.setattr(fake_host, "attachment_download", download)
    runtime = make_runtime(fake_pi, tmp_path, fake_host)
    images, notes = await runtime._prepare_attachments(
        "session", (RuntimeAttachment(file_id="upload", name=name, media_type=media_type),)
    )
    if name.endswith((".png", ".jpg")):
        assert len(images) == 1
        assert images[0]["mimeType"] in {"image/png", "image/jpeg"}
        assert notes == ()
        assert not (tmp_path / "attachments").exists()
    else:
        assert images == ()
        assert len(notes) == 1
        assert notes[0][0] == name
        assert Path(notes[0][1]).read_bytes() == content
