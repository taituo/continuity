"""ACP client side for one disposable session (written against Kiro CLI V3).

Protocol facts this relies on (see docs/kiro-v3.md for sources):
- launch: `kiro-cli acp --agent-engine=v3 --auth-method=cli`; no --agent,
  --model or --trust-* flags in V3 ACP mode
- newline-delimited JSON-RPC 2.0 on stdio
- `session/new` takes cwd, mcpServers, additionalDirectories, _meta.kiro
  {modeId, modelId, effortLevel}; mcpServers is always [] here (MCP not used)
- a turn is complete only when the `session/prompt` request returns
  (V3 sends no TurnEnd; a `turn_end` kind must not be treated as completion)
- `session/request_permission` offers allow_once / allow_always /
  reject_once / reject_always with context in `_meta.kiro.consent`
- updates replayed by session/load carry `_meta.kiro.replay` (ignored)
- unknown agent->client requests get an explicit error
For robustness against CLI v2 it also accepts the `session/notification`
method name and PascalCase update kinds.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .jsonrpc import Connection, PeerExited, RpcError
from .policy import Fence, choose_option

PROTOCOL_VERSION = 1
CLIENT_INFO = {"name": "continuity", "version": "0.2.0"}
RESOURCE_NOT_FOUND = -32002
AUTH_REQUIRED = -32000


class AgentSetupError(Exception):
    pass


def norm_kind(kind: str) -> str:
    """'ToolCallUpdate' -> 'tool_call_update'; snake_case passes through."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", kind).lower() if kind and kind[0].isupper() else kind


@dataclass
class TurnResult:
    stop_reason: str              # ACP stopReason, or timeout | crashed | error
    final_message: str = ""
    tool_calls: dict = field(default_factory=dict)
    denials: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    info: dict = field(default_factory=dict)      # last session_info_update per _meta.kiro.kind
    unknown_requests: list = field(default_factory=list)
    error: str = ""
    agent_stop_reason: str = ""   # what the agent itself answered, when it answered

    def summary(self) -> dict:
        return {"stop_reason": self.stop_reason, "agent_stop_reason": self.agent_stop_reason,
                "tool_calls": len(self.tool_calls), "denials": self.denials[:20],
                "failures": self.failures[:20], "info": self.info,
                "unknown_requests": self.unknown_requests[:20], "error": self.error[:500]}


class AcpSession:
    def __init__(self, conn: Connection, fence: Fence,
                 on_tool_done: Callable[[dict], Any] | None = None,
                 on_observe: Callable[[list, str], Any] | None = None):
        self.conn, self.fence = conn, fence
        self.on_tool_done = on_tool_done or (lambda tc: None)
        self.on_observe = on_observe or (lambda paths, via: None)
        self.agent_caps: dict = {}
        self.agent_info: dict = {}
        self.extension_methods: list = []
        self.session_id: str | None = None
        self.cancelling = False
        self._turn = TurnResult("pending")
        self._message: list[str] = []
        self._message_id = None
        conn.requests["session/request_permission"] = self._permission
        conn.requests["fs/read_text_file"] = self._fs_read
        conn.requests["fs/write_text_file"] = self._fs_write
        conn.notifications["session/update"] = self._update
        conn.notifications["session/notification"] = self._update   # CLI v2 name

    # -- setup -------------------------------------------------------------
    async def initialize(self) -> dict:
        res = await self.conn.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True},
                                   "terminal": False},
            "clientInfo": CLIENT_INFO})
        if not isinstance(res, dict) or res.get("protocolVersion") != PROTOCOL_VERSION:
            raise AgentSetupError(f"agent speaks ACP protocol {res and res.get('protocolVersion')}, "
                                  f"this client speaks {PROTOCOL_VERSION}")
        self.agent_caps = res.get("agentCapabilities") or {}
        self.agent_info = res.get("agentInfo") or {}
        kiro = ((self.agent_caps.get("_meta") or {}).get("kiro") or {})
        self.extension_methods = list(kiro.get("extensionMethods") or [])
        return res

    async def new_session(self, cwd: Path, additional_dirs: list[Path] = (),
                          kiro_meta: dict | None = None) -> str:
        params: dict = {"cwd": str(cwd), "mcpServers": []}
        if additional_dirs:
            params["additionalDirectories"] = [str(d) for d in additional_dirs]
        if kiro_meta:
            params["_meta"] = {"kiro": kiro_meta}
        try:
            res = await self.conn.request("session/new", params)
        except RpcError as e:
            if e.code == AUTH_REQUIRED or "auth" in e.message.lower():
                raise AgentSetupError("agent requires authentication: log in with the agent's own CLI "
                                      "on this machine (e.g. `kiro-cli login`), then retry") from e
            raise AgentSetupError(f"session/new failed: {e}") from e
        sid = (res or {}).get("sessionId")
        if not sid:
            raise AgentSetupError(f"session/new returned no sessionId: {res!r}")
        self.session_id = sid
        return sid

    # -- the turn ----------------------------------------------------------
    async def run_turn(self, text: str, timeout: float, cancel_grace: float = 15.0,
                       stop: asyncio.Event | None = None) -> TurnResult:
        """One prompt turn. On timeout or `stop`, send session/cancel and give
        the agent `cancel_grace` seconds to answer before giving up on it."""
        self._turn = TurnResult("pending")
        req = asyncio.create_task(self.conn.request(
            "session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]}))
        waiters = {req}
        stopper = asyncio.create_task(stop.wait()) if stop else None
        if stopper:
            waiters.add(stopper)
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if stopper:
            stopper.cancel()
        interrupted = req not in done
        stopped = bool(stop and stop.is_set() and interrupted)
        timed_out = interrupted
        if interrupted:
            self.cancelling = True
            try:
                await self.conn.notify("session/cancel", {"sessionId": self.session_id})
            except PeerExited:
                pass
            done, _ = await asyncio.wait({req}, timeout=cancel_grace)
        t = self._turn
        if req in done:
            try:
                res = req.result() or {}
                t.agent_stop_reason = str(res.get("stopReason", ""))
                t.stop_reason = t.agent_stop_reason or "end_turn"
            except PeerExited as e:
                t.stop_reason, t.error = "crashed", str(e)
            except RpcError as e:
                t.stop_reason, t.error = "error", str(e)
        else:
            req.cancel()
            await asyncio.gather(req, return_exceptions=True)
        if timed_out:
            t.stop_reason = "stopped" if stopped else "timeout"
        t.final_message = "".join(self._message).strip()
        t.unknown_requests = list(self.conn.unknown_requests)
        return t

    # -- agent -> client -----------------------------------------------------
    async def _permission(self, params: dict) -> dict:
        if self.cancelling:
            return {"outcome": {"outcome": "cancelled"}}
        d = self.fence.decide(params)
        option, note = choose_option(params.get("options") or [], d.allow)
        tc = params.get("toolCall") or {}
        if d.observed:
            self.on_observe(d.observed, "acp-permission")
        if not d.allow or note:
            self._turn.denials.append({"capability": d.capability, "resources": d.resources[:5],
                                       "reason": note or d.reason, "tool": tc.get("title", "")})
        if option is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option}}

    async def _fs_read(self, params: dict) -> dict:
        real = self.fence.resolve(str(params.get("path", "")))
        ok, why, observed = self.fence.check_read([real])
        if not ok:
            self._turn.denials.append({"capability": "fs_read", "resources": [real], "reason": why,
                                       "tool": "fs/read_text_file"})
            raise RpcError(-32001, why)
        if observed:
            self.on_observe(observed, "acp-fs")
        try:
            text = Path(real).read_text(errors="replace")
        except FileNotFoundError:
            raise RpcError(RESOURCE_NOT_FOUND, f"not found: {real}") from None
        line, limit = params.get("line"), params.get("limit")
        if line or limit:
            lines = text.splitlines(keepends=True)
            start = max(int(line or 1) - 1, 0)
            text = "".join(lines[start:start + int(limit)] if limit else lines[start:])
        return {"content": text}

    async def _fs_write(self, params: dict) -> dict:
        real = self.fence.resolve(str(params.get("path", "")))
        ok, why = self.fence.check_write([real])
        if not ok:
            self._turn.denials.append({"capability": "fs_write", "resources": [real], "reason": why,
                                       "tool": "fs/write_text_file"})
            raise RpcError(-32001, why)
        p = Path(real)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.continuity-tmp")
        tmp.write_text(str(params.get("content", "")))
        os.replace(tmp, p)
        self.on_tool_done({"toolCallId": None, "kind": "edit", "status": "completed",
                           "locations": [{"path": real}]})
        return {}

    async def _update(self, params: dict) -> None:
        u = params.get("update") or {}
        if ((u.get("_meta") or {}).get("kiro") or {}).get("replay"):
            return
        kind = norm_kind(u.get("sessionUpdate") or u.get("type") or "")
        t = self._turn
        if kind == "agent_message_chunk":
            mid = u.get("messageId")
            if mid is not None and mid != self._message_id:
                self._message, self._message_id = [], mid
            c = u.get("content") or {}
            if isinstance(c, dict) and c.get("type", "text") == "text":
                self._message.append(str(c.get("text", "")))
        elif kind in ("tool_call", "tool_call_update"):
            tid = u.get("toolCallId")
            tc = t.tool_calls.setdefault(tid, {"toolCallId": tid})
            for k in ("title", "kind", "status", "locations", "rawInput"):
                if u.get(k) is not None:
                    tc[k] = u[k]
            if kind == "tool_call":
                self._message, self._message_id = [], None   # the final message comes after tools
            locs = [loc.get("path") for loc in u.get("locations") or []
                    if isinstance(loc, dict) and isinstance(loc.get("path"), str)]
            obs = [p for p in (self.fence.resolve(x) for x in locs) if self.fence.in_views(p)]
            if obs:
                self.on_observe(obs, "acp-tool")
            if u.get("status") in ("completed", "failed"):
                if u.get("status") == "failed":
                    reason = ((u.get("_meta") or {}).get("kiro") or {}).get("failureReason")
                    t.failures.append({"tool": tc.get("title", ""), "reason": reason or "error"})
                self.on_tool_done(dict(tc))
        elif kind == "session_info_update":
            k = ((u.get("_meta") or {}).get("kiro") or {}).get("kind") or "info"
            t.info[k] = _trim(u)


def _trim(obj, depth=0):
    if depth > 4:
        return "…"
    if isinstance(obj, dict):
        return {k: _trim(v, depth + 1) for k, v in list(obj.items())[:20]}
    if isinstance(obj, list):
        return [_trim(v, depth + 1) for v in obj[:20]]
    if isinstance(obj, str) and len(obj) > 300:
        return obj[:300] + "…"
    return obj
