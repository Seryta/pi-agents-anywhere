"""Deterministic session projection / disk benchmark; run only in a container."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "src"),
    str(ROOT),
    os.environ.get("PI_AA_CONNECTOR_SOURCE", "/aa/connector"),
]


def doctor() -> list[str]:
    errors = []
    if not Path("/.dockerenv").exists():
        errors.append("Run with docker/run-tests.sh; a container is required.")
    try:
        import pi_aa.runtime  # noqa: F401
    except ImportError as exc:
        errors.append(f"Runtime dependencies unavailable: {exc}")
    return errors


async def measure(operation, repeats: int) -> dict:
    durations, lags = [], []
    for _ in range(repeats):
        stop = asyncio.Event()

        async def heartbeat(stop=stop):
            previous = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.001)
                now = time.perf_counter()
                lags.append(max(0.0, (now - previous - 0.001) * 1000))
                previous = now

        task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        started = time.perf_counter()
        await operation()
        durations.append((time.perf_counter() - started) * 1000)
        await asyncio.sleep(0.002)
        stop.set()
        await task
    return {
        "median_ms": round(statistics.median(durations), 3),
        "max_loop_lag_ms": round(max(lags), 3),
    }


async def benchmark(args) -> dict:
    from connector.runtime_protocol import RuntimeAttachment, RuntimeAttachmentContent

    from pi_aa.runtime import PiLiveSession
    from pi_aa.sessions import load_session_doc
    from tests.conftest import FakeHost
    from tests.test_runtime import make_runtime

    payload = b"x" * (args.attachment_mib * 1024 * 1024)

    class Host(FakeHost):
        async def attachment_download(self, session_id, file_id):
            return RuntimeAttachmentContent(
                file_id=file_id, name="payload.txt", media_type="text/plain", content=payload
            )

    with tempfile.TemporaryDirectory(prefix="pi-aa-session-benchmark-") as directory:
        root = Path(directory)
        path = root / "sessions" / "benchmark.jsonl"
        path.parent.mkdir()
        with path.open("w") as stream:
            stream.write(
                json.dumps({"type": "session", "id": "benchmark", "cwd": directory}) + "\n"
            )
            for index in range(args.entries):
                stream.write(
                    json.dumps(
                        {
                            "type": "message",
                            "id": str(index),
                            "parentId": str(index - 1) if index else None,
                            "message": {
                                "role": "assistant" if index % 2 else "user",
                                "content": [{"type": "text", "text": "x" * args.text_bytes}],
                            },
                        }
                    )
                    + "\n"
                )
        doc = load_session_doc(path)
        if doc is None:
            raise ValueError(
                "Generated session exceeds the runtime file limit; reduce entries/text bytes."
            )
        host = Host()
        runtime = make_runtime(Path("/unused-pi"), root, host)
        live = PiLiveSession(runtime, "benchmark-session", cwd=directory, session_path=str(path))
        runtime._live[live.platform_id] = live
        attachments = (
            RuntimeAttachment(file_id="benchmark", name="payload.txt", media_type="text/plain"),
        )

        async def project():
            items = await runtime._project_session_items(live.platform_id, doc)
            assert len(items) >= args.entries

        async def disk_read():
            assert await asyncio.to_thread(load_session_doc, path) is not None

        async def disk_write():
            _, notes = await runtime._prepare_attachments(live.platform_id, attachments)
            assert len(notes) == 1
            assert Path(notes[0][1]).stat().st_size == len(payload)

        async def scanner():
            prepared = await runtime.prepare_session_timeline_sync(live.platform_id, str(path))
            assert prepared is not None and prepared.commit is not None
            if prepared.snapshot is not None:
                await host.timeline_sync(
                    live.platform_id,
                    "pi",
                    prepared.snapshot.items,
                    external_session_id=str(path),
                    complete=prepared.snapshot.complete,
                )
            await prepared.commit()

        # Measure both the initial complete checkpoint and unchanged scanner
        # passes: prepare verifies the prefix, commit persists it again.
        runtime._live.clear()

        return {
            "entries": args.entries,
            "session_bytes": path.stat().st_size,
            "attachment_bytes": len(payload),
            "repeats": args.repeats,
            "projection": await measure(project, args.repeats),
            "disk_read": await measure(disk_read, args.repeats),
            "attachment_write": await measure(disk_write, args.repeats),
            "scanner_prepare_commit": await measure(scanner, args.repeats),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--entries", type=int, default=12000)
    parser.add_argument("--text-bytes", type=int, default=4096)
    parser.add_argument("--attachment-mib", type=int, default=32)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-loop-lag-ms", type=float, default=100)
    args = parser.parse_args()
    errors = doctor()
    if errors or args.doctor:
        print(json.dumps({"ok": not errors, "errors": errors}))
        return int(bool(errors))
    if min(args.entries, args.text_bytes, args.attachment_mib, args.repeats) <= 0:
        parser.error("workload sizes and repeats must be positive")
    result = asyncio.run(benchmark(args))
    result["max_loop_lag_ms"] = args.max_loop_lag_ms
    result["ok"] = all(
        result[key]["max_loop_lag_ms"] <= args.max_loop_lag_ms
        for key in ("projection", "disk_read", "attachment_write", "scanner_prepare_commit")
    )
    print(json.dumps(result))
    return int(not result["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
