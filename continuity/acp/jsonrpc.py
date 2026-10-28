"""Newline-delimited JSON-RPC 2.0 over a subprocess's stdio (ACP transport).

Both sides can send requests. Requests from the agent are dispatched to
registered handlers; an unknown method gets an explicit -32601 error instead
of silence, so the agent is never left waiting (Kiro's ACP guidance). Lines
that are not JSON are logged and skipped.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import signal
import sys
from typing import Any, Awaitable, Callable

log = logging.getLogger("continuity.acp")

METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
MAX_LINE = 64 * 1024 * 1024


def _die_with_parent() -> None:
    """Linux: the agent gets SIGKILL if the runner dies (even kill -9), so no
    orphaned agent keeps writing after its session was recorded as lost."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)   # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.data = code, message, data


class PeerExited(Exception):
    def __init__(self, returncode):
        super().__init__(f"agent process exited ({returncode})")
        self.returncode = returncode


Handler = Callable[[dict], Awaitable[Any]]


class Connection:
    def __init__(self, proc: asyncio.subprocess.Process, stderr_path: str | None = None):
        self.proc = proc
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.requests: dict[str, Handler] = {}
        self.notifications: dict[str, Handler] = {}
        self.unknown_requests: list[str] = []
        self.garbage_lines = 0
        self._closed = asyncio.get_running_loop().create_future()
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr = asyncio.create_task(self._drain_stderr(stderr_path))
        self._handlers: set[asyncio.Task] = set()

    @classmethod
    async def spawn(cls, argv: list[str], cwd: str, env: dict | None = None,
                    stderr_path: str | None = None) -> "Connection":
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=MAX_LINE,
            start_new_session=True,   # own process group: kill reaches its children
            preexec_fn=_die_with_parent if sys.platform.startswith("linux") else None)
        return cls(proc, stderr_path)

    # -- outgoing -------------------------------------------------------
    async def _send(self, msg: dict) -> None:
        if self.proc.stdin is None or self.proc.stdin.is_closing():
            raise PeerExited(self.proc.returncode)
        self.proc.stdin.write(json.dumps(msg, separators=(",", ":")).encode() + b"\n")
        try:
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise PeerExited(self.proc.returncode) from None

    async def request(self, method: str, params: dict) -> Any:
        self._next += 1
        rid = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            done, _ = await asyncio.wait({fut, self._closed}, return_when=asyncio.FIRST_COMPLETED)
            if fut in done:
                return fut.result()
            raise PeerExited(self.proc.returncode)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- incoming -------------------------------------------------------
    async def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except ValueError:  # a single line beyond MAX_LINE
                    log.warning("oversized line from agent dropped")
                    continue
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self.garbage_lines += 1
                    log.debug("non-JSON line from agent: %r", line[:200])
                    continue
                if not isinstance(msg, dict):
                    self.garbage_lines += 1
                    continue
                self._dispatch(msg)
            await self.proc.wait()          # EOF: the agent is exiting
        finally:
            # Never wait for the process here: if this task is cancelled while
            # the agent is still alive, waiting would hang the caller's cleanup.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(PeerExited(self.proc.returncode))
            if not self._closed.done():
                self._closed.set_result(self.proc.returncode)

    def _dispatch(self, msg: dict) -> None:
        if "method" not in msg:
            fut = self._pending.get(msg.get("id"))
            if fut is None or fut.done():
                return
            if "error" in msg:
                e = msg["error"] or {}
                fut.set_exception(RpcError(e.get("code", INTERNAL_ERROR), e.get("message", ""), e.get("data")))
            else:
                fut.set_result(msg.get("result"))
            return
        method, params = msg["method"], msg.get("params") or {}
        if "id" in msg:
            t = asyncio.create_task(self._answer(msg["id"], method, params))
        else:
            h = self.notifications.get(method)
            if h is None:
                return  # unknown notifications are ignored, as ACP requires
            t = asyncio.create_task(self._safe(h, method, params))
        self._handlers.add(t)
        t.add_done_callback(self._handlers.discard)

    async def _safe(self, h: Handler, method: str, params: dict) -> None:
        try:
            await h(params)
        except Exception:  # noqa: BLE001 - a bad notification must not kill the reader
            log.exception("notification handler for %s failed", method)

    async def _answer(self, rid, method: str, params: dict) -> None:
        h = self.requests.get(method)
        if h is None:
            self.unknown_requests.append(method)
            reply = {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": METHOD_NOT_FOUND, "message": f"method not supported: {method}"}}
        else:
            try:
                reply = {"jsonrpc": "2.0", "id": rid, "result": await h(params)}
            except RpcError as e:
                reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.message}}
            except Exception as e:  # noqa: BLE001
                log.exception("request handler for %s failed", method)
                reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": INTERNAL_ERROR, "message": str(e)}}
        try:
            await self._send(reply)
        except PeerExited:
            pass

    async def _drain_stderr(self, path: str | None) -> None:
        assert self.proc.stderr is not None
        out = open(path, "ab") if path else None
        try:
            while chunk := await self.proc.stderr.read(65536):
                if out:
                    out.write(chunk)
                    out.flush()
        finally:
            if out:
                out.close()

    # -- lifecycle ------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def wait_closed(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(asyncio.shield(self._closed), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self, grace: float = 2.0) -> int | None:
        """Close stdin, then SIGTERM, then SIGKILL the whole process group."""
        if self.alive and self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        for sig, wait in ((None, grace), (15, grace), (9, 5.0)):
            if not self.alive:
                break
            if sig is not None:
                try:
                    os.killpg(self.proc.pid, sig)
                except ProcessLookupError:
                    break
            await self.wait_closed(wait)
        for t in (self._reader, self._stderr, *self._handlers):
            if not t.done():
                t.cancel()
        await asyncio.gather(self._reader, self._stderr, *self._handlers, return_exceptions=True)
        return self.proc.returncode
