from __future__ import annotations

from connector.runtime_protocol.timeline import timeline_content_hash

from pi_aa.projection import project_session
from tests.test_projection import entries


def test_late_client_binding_changes_published_identity_without_changing_message() -> None:
    kwargs = {"session_id": "s", "external_session_id": "/sessions/history.jsonl"}
    before = project_session(entries(), **kwargs)
    after = project_session(entries(), **kwargs, client_messages=(("你好", "client-message"),))
    previous = next(item for item in before if item.role == "user")
    reconciled = next(item for item in after if item.role == "user")
    assert previous.id == reconciled.id
    assert previous.content == reconciled.content
    assert previous.source != reconciled.source
    assert reconciled.source["clientMessageId"] == "client-message"
    assert previous.content_hash != reconciled.content_hash
    repeated = project_session(entries(), **kwargs, client_messages=(("你好", "client-message"),))
    assert [item.content_hash for item in repeated] == [item.content_hash for item in after]
    assert all(item.content_hash.startswith("sha256:") for item in after)
    unbound_again = project_session(entries(), **kwargs)
    assert [item.content_hash for item in unbound_again] == [item.content_hash for item in before]
    changed_binding = project_session(
        entries(), **kwargs, client_messages=(("你好", "another-id"),)
    )
    latest = next(item for item in changed_binding if item.role == "user")
    assert latest.content_hash != reconciled.content_hash
    assert latest.id == reconciled.id
    # Reconciliation changes only the user echo, preserving other history hashes.
    assert [item.content_hash for item in before if item.role != "user"] == [
        item.content_hash for item in after if item.role != "user"
    ]


def test_tool_completion_updates_hash_and_preserves_terminal_content() -> None:
    kwargs = {"session_id": "s", "external_session_id": "/sessions/history.jsonl"}
    pending = project_session(entries()[:-1], **kwargs)
    complete = project_session(entries(), **kwargs)
    before = next(item for item in pending if item.type == "tool")
    after = next(item for item in complete if item.type == "tool")
    assert before.id == after.id
    assert before.status == "running" and after.status == "done"
    assert after.content["output"] == "total 0\n"
    assert after.content["exitCode"] == 0
    assert before.content_hash != after.content_hash
    assert after.content_hash == timeline_content_hash(
        item_type=after.type,
        status=after.status,
        role=after.role,
        content=after.content,
    )
