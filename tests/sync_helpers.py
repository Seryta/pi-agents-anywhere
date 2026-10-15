"""Observe scanner delivery effects across internal replacement and SDK delta paths."""

from dataclasses import replace

from connector.runtime_protocol import RuntimeTimelineSnapshot


async def prepare_and_capture_sync(runtime, session_id, external_session_id):
    before = len(runtime.host.timeline_syncs)
    prepared = await runtime.prepare_session_timeline_sync(session_id, external_session_id)
    if prepared.snapshot is None and len(runtime.host.timeline_syncs) > before:
        sent = runtime.host.timeline_syncs[-1]
        # This snapshot is only an observation of the already published
        # effect. Production complete replacements never escape prepare.
        prepared = replace(
            prepared,
            snapshot=RuntimeTimelineSnapshot(
                session_id=sent["session_id"],
                runtime=sent["runtime"],
                external_session_id=sent["external_session_id"],
                items=sent["items"],
                complete=sent["complete"],
            ),
        )
    return prepared
