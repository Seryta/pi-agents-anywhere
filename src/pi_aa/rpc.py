"""Async client for ``pi --mode rpc``.

Pi speaks strict JSONL on stdin/stdout: one JSON object per line, LF
terminated. Commands sent to stdin may carry an ``id``; the matching
``response`` record repeats it. Every other record is an asynchronous session
event (or an extension UI request) pushed by Pi.

The reader must not use ``readline``-style splitting on Unicode separators:
JSON strings may legally contain U+2028/U+2029. This module reads raw bytes
and splits on LF only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
EVENT_DRAIN_TIMEOUT_SECONDS = 2.0

EventListener = Callable[[Mapping[str, Any]], Awaitable[None]]
ExitListener = Callable[[int | None], Awaitable[None]]


class PiRpcError(Exception):
    """Base error for the Pi RPC transport."""


class PiRpcProcessExited(PiRpcError):
    """The Pi process is gone and the command cannot be delivered."""


class PiRpcRequestFailed(PiRpcError):
    """Pi answered a command with ``success: false``."""

    def __init__(self, command: str | None, message: str) -> None:
        super().__init__(message)
        self.command = command


class PiRpcTimeout(PiRpcError):
    """Pi did not answer a command within the timeout."""


class PiRpcProcess:
    """Owns one long-lived ``pi --mode rpc`` subprocess."""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        on_event: EventListener | None = None,
        on_exit: ExitListener | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        stderr_path: str | None = None,
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._on_event = on_event
        self._on_exit = on_exit
        self._request_timeout = request_timeout
        self._stderr_path = stderr_path

        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future[Mapping[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_file: Any = None
        self._exit_code: int | None = None
        self._exited = asyncio.Event()
        self._closing = False

    @property
    def argv(self) -> list[str]:
        return list(self._argv)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None and not self._closing

    @property
    def returncode(self) -> int | None:
        if self._process is None:
            return self._exit_code
        return self._process.returncode

    async def start(self) -> None:
        if self._process is not None:
            raise PiRpcError("Pi RPC process already started")
        env = dict(os.environ)
        if self._env is not None:
            env.update(self._env)
        logger.debug("starting pi rpc: %s (cwd=%s)", " ".join(self._argv), self._cwd)
        stderr_target: Any = asyncio.subprocess.PIPE
        if self._stderr_path:
            stderr_target = open(self._stderr_path, "ab")  # noqa: ASYNC230, SIM115 - owned until close
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_target,
            cwd=self._cwd,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_stdout(), name="pi-rpc-reader")
        if stderr_target is asyncio.subprocess.PIPE:
            self._stderr_task = asyncio.create_task(self._read_stderr(), name="pi-rpc-stderr")
        else:
            self._stderr_file = stderr_target

    async def request(
        self,
        command: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        """Send one command and await its ``response`` record."""

        process = self._process
        if process is None or process.stdin is None:
            raise PiRpcProcessExited("Pi RPC process is not running")
        if not self.alive:
            raise PiRpcProcessExited(f"Pi RPC process exited with code {self.returncode}")

        request_id = str(command.get("id") or uuid.uuid4().hex)
        payload = {**command, "id": request_id}
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Mapping[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            process.stdin.write(encoded.encode("utf-8") + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(request_id, None)
            raise PiRpcProcessExited("Pi RPC stdin closed") from exc

        effective_timeout = self._request_timeout if timeout is None else timeout
        try:
            return await asyncio.wait_for(future, timeout=effective_timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise PiRpcTimeout(
                f"Pi did not answer {payload.get('type')!r} within {effective_timeout}s"
            ) from exc
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise

    async def notify(self, command: Mapping[str, Any]) -> None:
        """Send a command without waiting for its response.

        Used for UI responses that must not serialize behind other commands.
        """

        process = self._process
        if process is None or process.stdin is None or not self.alive:
            raise PiRpcProcessExited("Pi RPC process is not running")
        payload = {**command}
        payload.setdefault("id", uuid.uuid4().hex)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            process.stdin.write(encoded.encode("utf-8") + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise PiRpcProcessExited("Pi RPC stdin closed") from exc

    async def wait_exited(self) -> int | None:
        await self._exited.wait()
        return self.returncode

    async def close(self, *, timeout: float = 5.0) -> None:
        """Stop the process, escalating from stdin-close to SIGTERM/SIGKILL."""

        self._closing = True
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            if process.stdin is not None and not process.stdin.is_closing():
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except TimeoutError:
                logger.warning("pi rpc process %s did not exit, terminating", process.pid)
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        self._exit_code = process.returncode
        self._exited.set()
        await self._drain_tasks()
        self._fail_pending(PiRpcProcessExited("Pi RPC process stopped"))

    async def _drain_tasks(self) -> None:
        for task in (self._reader_task, self._stderr_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._stderr_file is not None:
            with contextlib.suppress(Exception):
                self._stderr_file.close()

    def _fail_pending(self, error: Exception) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream = self._process.stdout
        buffer = bytearray()
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    raw = bytes(buffer[:newline])
                    del buffer[: newline + 1]
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                    if not raw.strip():
                        continue
                    self._dispatch_line(raw)
        finally:
            if buffer.strip():
                self._dispatch_line(bytes(buffer))
            exit_code = self._process.returncode if self._process is not None else None
            self._exit_code = exit_code
            self._exited.set()
            self._fail_pending(PiRpcProcessExited(f"Pi RPC process exited with code {exit_code}"))
            if self._on_exit is not None:
                with contextlib.suppress(Exception):
                    await self._on_exit(exit_code)

    def _dispatch_line(self, raw: bytes) -> None:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("pi rpc: dropping unparseable record (%d bytes)", len(raw))
            return
        if not isinstance(record, Mapping):
            logger.warning("pi rpc: dropping non-object record")
            return
        if record.get("type") == "response":
            request_id = record.get("id")
            if isinstance(request_id, str) and request_id in self._pending:
                future = self._pending.pop(request_id)
                if not future.done():
                    if record.get("success") is False:
                        future.set_exception(
                            PiRpcRequestFailed(
                                str(record.get("command") or ""),
                                str(record.get("error") or "pi rejected the command"),
                            )
                        )
                    else:
                        future.set_result(record)
                return
            logger.debug("pi rpc: response without pending request id=%r", request_id)
            return
        if self._on_event is not None:
            asyncio.get_running_loop().create_task(self._safe_event(record))

    async def _safe_event(self, record: Mapping[str, Any]) -> None:
        assert self._on_event is not None
        try:
            await self._on_event(record)
        except Exception:
            logger.exception("pi rpc event handler failed for type=%r", record.get("type"))

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        stream = self._process.stderr
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug("pi stderr: %s", text)


def response_data(response: Mapping[str, Any]) -> Mapping[str, Any]:
    """Extract the ``data`` mapping from a successful response record."""

    data = response.get("data")
    if isinstance(data, Mapping):
        return data
    return {}


def concatenate_text(blocks: Any) -> str:
    """Join the text blocks of a Pi message content value."""

    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Mapping) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)
