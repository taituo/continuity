"""A scriptable stand-in for `kiro-cli acp --agent-engine=v3`.

It speaks the same wire protocol as documented for Kiro CLI V3 (newline JSON-
RPC 2.0, standard ACP methods, `_meta.kiro.consent` on permission requests,
`_meta.kiro.failureReason` on failed tools, `session_info_update` with
`_meta.kiro.kind`, no TurnEnd) and behaves like an agent with built-in tools:
it asks permission, then reads and writes files itself.

Behaviour comes from a JSON script (env FAKE_AGENT_SCRIPT):

  {"goals": {"<substring of goal text>": [[attempt 1 actions], [attempt 2 actions]]},
   "default": [actions]}

Each attempt at a goal takes the next action list (attempt counter in
<script>.state.json). Actions: see ACTIONS below.
"""
from __future__ import annotations

import fcntl
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

OUT_LOCK = threading.Lock()
PASCAL = {"agent_message_chunk": "AgentMessageChunk", "tool_call": "ToolCall",
          "tool_call_update": "ToolCallUpdate"}


class Agent:
    def __init__(self, script_path: str):
        self.script_path = script_path
        self.script = json.load(open(script_path)) if script_path else {}
        self.pending: dict[int, queue.Queue] = {}
        self.inbox: queue.Queue = queue.Queue()
        self.cancelled = threading.Event()
        self.next_id = 1000
        self.client_caps: dict = {}
        self.cwd = ""
        self.extra_dirs: list = []
        self.session_id = ""
        self.pascal = False
        self.prompt = ""
        self.ignore_eof = False           # a stubborn agent that outlives its client

    # -- wire ---------------------------------------------------------------
    def send(self, msg: dict) -> None:
        with OUT_LOCK:
            sys.stdout.write(json.dumps(msg) + "\n")
            sys.stdout.flush()

    def call(self, method: str, params: dict):
        self.next_id += 1
        rid = self.next_id
        q: queue.Queue = queue.Queue()
        self.pending[rid] = q
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        msg = q.get()
        if "error" in msg:
            return None, msg["error"]
        return msg.get("result"), None

    def update(self, update_type: str, **fields) -> None:
        method = "session/update"
        if self.pascal:
            update_type, method = PASCAL.get(update_type, update_type), "session/notification"
        self.send({"jsonrpc": "2.0", "method": method,
                   "params": {"sessionId": self.session_id,
                              "update": {"sessionUpdate": update_type, **fields}}})

    def reader(self) -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if "method" not in msg:
                q = self.pending.pop(msg.get("id"), None)
                if q:
                    q.put(msg)
            elif msg["method"] == "session/cancel":
                self.cancelled.set()
            elif "id" in msg:
                self.inbox.put(msg)
        if not self.ignore_eof:
            os._exit(0)                   # client closed stdin

    # -- server loop ----------------------------------------------------------
    def serve(self) -> None:
        threading.Thread(target=self.reader, daemon=True).start()
        while True:
            msg = self.inbox.get()
            m, p, rid = msg["method"], msg.get("params") or {}, msg["id"]
            try:
                result = getattr(self, "m_" + m.replace("/", "_"))(p)
                self.send({"jsonrpc": "2.0", "id": rid, "result": result})
            except AttributeError:
                self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": m}})
            except ValueError as e:
                self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": str(e)}})

    def m_initialize(self, p):
        self.client_caps = p.get("clientCapabilities") or {}
        return {"protocolVersion": 1,
                "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True},
                                      "_meta": {"kiro": {"extensionMethods": ["_kiro/account/getUsage"]}}},
                "agentInfo": {"name": "fake-kiro", "version": "3.0.0-fake"}, "authMethods": []}

    def m_session_new(self, p):
        cwd = p.get("cwd", "")
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise ValueError("cwd must be an existing absolute path")
        if p.get("mcpServers") != []:
            raise ValueError("this environment has no MCP: mcpServers must be []")
        self.cwd, self.extra_dirs = cwd, list(p.get("additionalDirectories") or [])
        self.session_id = "fake-" + uuid.uuid4().hex[:8]
        threading.Thread(target=lambda: (time.sleep(0.01), self.update(
            "available_commands_update", availableCommands=[]), self.update(
            "config_option_update", configOptions=[])), daemon=True).start()
        return {"sessionId": self.session_id,
                "configOptions": [{"id": "model", "currentValue": "fake"}]}

    def m_session_prompt(self, p):
        if p.get("sessionId") != self.session_id:
            raise ValueError("unknown session")
        blocks = p.get("prompt")
        if not isinstance(blocks, list):
            raise ValueError("prompt must be a list of content blocks")
        self.prompt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        stop = self.run(self.pick_actions())
        return {"stopReason": stop}

    # -- script ---------------------------------------------------------------
    def pick_actions(self) -> list:
        m = re.search(r"^Goal (g\d+): (.*)$", self.prompt, re.M)
        text = m.group(2) if m else ""
        goals = self.script.get("goals", {})
        key = max((k for k in goals if k in text), key=len, default=None)
        if key is None:
            return self.script.get("default", [{"do": "say", "text": "nothing to do"}])
        state_path = self.script_path + ".state.json"
        with open(state_path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            state = json.loads(f.read() or "{}")
            n = state.get(key, 0)
            state[key] = n + 1
            f.seek(0)
            f.truncate()
            f.write(json.dumps(state))
        attempts = goals[key]
        return attempts[min(n, len(attempts) - 1)]

    def views_dir(self) -> str:
        m = re.search(r"readable \(not writable\) under (\S+);", self.prompt)
        return m.group(1) if m else ""

    def abspath(self, path: str) -> str:
        return path if os.path.isabs(path) else os.path.normpath(os.path.join(self.cwd, path))

    def permission(self, tool_id: str, title: str, capability: str, resource: str) -> bool:
        res, err = self.call("session/request_permission", {
            "sessionId": self.session_id,
            "toolCall": {"toolCallId": tool_id, "title": title},
            "options": [{"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                        {"optionId": "reject-always", "name": "Never", "kind": "reject_always"}],
            "_meta": {"kiro": {"consent": {"capability": capability, "resource": resource,
                                           "workspaceRoot": self.cwd, "persistableConsent": True}}}})
        if err or not res:
            return False
        outcome = res.get("outcome") or {}
        if outcome.get("optionId") == "allow-always":   # must never happen
            open(self.script_path + ".ALLOW_ALWAYS", "a").write(resource + "\n")
        return outcome.get("outcome") == "selected" and outcome.get("optionId") in ("allow-once", "allow-always")

    def tool(self, kind: str, title: str, capability: str, resource: str, fn, locations=(), raw=None,
             ask=True):
        tid = "t" + uuid.uuid4().hex[:8]
        self.update("tool_call", toolCallId=tid, title=title, kind=kind, status="pending",
                    locations=[{"path": p} for p in locations], rawInput=raw or {})
        if ask and not self.permission(tid, title, capability, resource):
            self.update("tool_call_update", toolCallId=tid, status="failed",
                        _meta={"kiro": {"failureReason": "denied"}})
            return None
        self.update("tool_call_update", toolCallId=tid, status="in_progress")
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001
            self.update("tool_call_update", toolCallId=tid, status="failed",
                        content=[{"type": "content", "content": {"type": "text", "text": str(e)}}],
                        _meta={"kiro": {"failureReason": "error"}})
            return None
        self.update("tool_call_update", toolCallId=tid, status="completed",
                    content=[{"type": "content", "content": {"type": "text", "text": str(out)[:2000]}}])
        return out

    def inside_workspace(self, p: str) -> bool:
        roots = [self.cwd] + self.extra_dirs
        rp = os.path.realpath(p)
        return any(rp == os.path.realpath(r) or rp.startswith(os.path.realpath(r) + os.sep) for r in roots)

    # -- actions ----------------------------------------------------------------
    def run(self, actions: list) -> str:
        for a in actions:
            if self.cancelled.is_set():
                return "cancelled"
            r = getattr(self, "a_" + a["do"])(a)
            if isinstance(r, str):
                return r
        return "end_turn"

    def a_say(self, a):
        mid = uuid.uuid4().hex[:6]
        for chunk in re.findall(r".{1,40}", a["text"], re.S):
            self.update("agent_message_chunk", messageId=mid, content={"type": "text", "text": chunk})

    def a_expect(self, a):
        missing = [t for t in ([a["text"]] if isinstance(a["text"], str) else a["text"]) if t not in self.prompt]
        if missing:
            self.a_say({"text": f"MISSING CONTEXT: {missing}"})
            return "end_turn"

    def _write(self, path: str, a: dict):
        if "content" in a:
            new = a["content"]
        else:
            old = open(path).read() if os.path.exists(path) else ""
            if "append" in a:
                new = old + a["append"]
            else:
                o, n = a["replace"]
                if o not in old:
                    raise ValueError(f"text to replace not found in {path}")
                new = old.replace(o, n, 1)
        if a.get("client") and (self.client_caps.get("fs") or {}).get("writeTextFile"):
            _, err = self.call("fs/write_text_file", {"sessionId": self.session_id, "path": path, "content": new})
            if err:
                raise PermissionError(err.get("message"))
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").write(new)
        return f"wrote {len(new)} bytes"

    def a_write(self, a):
        path = self.abspath(a["path"])
        self.tool("edit", f"Write {a['path']}", "fs_write", path, lambda: self._write(path, a),
                  locations=[path], raw={"path": path})

    def a_read(self, a):
        path = self.abspath(a["path"])
        if a.get("client"):
            res, err = self.call("fs/read_text_file", {"sessionId": self.session_id, "path": path})
            self.a_say({"text": "read failed: " + err["message"] if err else f"read {len(res['content'])} chars"})
            return
        self.tool("read", f"Read {a['path']}", "fs_read", path, lambda: open(path).read(),
                  locations=[path], raw={"path": path}, ask=not self.inside_workspace(path))

    def a_shell(self, a):
        def run():
            r = subprocess.run(a["cmd"], shell=True, cwd=self.cwd, capture_output=True, text=True,
                               timeout=a.get("timeout", 120))
            return f"exit {r.returncode}\n{r.stdout}{r.stderr}"
        self.tool("execute", f"Run: {a['cmd'][:60]}", "shell", a["cmd"], run, raw={"command": a["cmd"]})

    def a_peek_shell(self, a):
        """Wait for a peer snapshot containing `file`, then `cat` it in a shell
        (no ACP location: only inotify can see this read)."""
        views, deadline = self.views_dir(), time.time() + a.get("wait", 10)
        while time.time() < deadline and not self.cancelled.is_set():
            hits = sorted(os.path.join(dp, a["file"]) for dp, _, fs in os.walk(os.path.join(views, "peers"))
                          if a["file"] in fs and "/.tmp-" not in dp)
            if hits:
                return self.a_shell({"cmd": f"cat {hits[-1]} > /dev/null"})
            time.sleep(0.05)
        self.a_say({"text": f"no peer snapshot of {a['file']} appeared"})

    def a_expect_absent(self, a):
        present = [t for t in ([a["text"]] if isinstance(a["text"], str) else a["text"]) if t in self.prompt]
        if present:
            self.a_say({"text": f"LEAKED INTO PROMPT: {present}"})
            return "end_turn"

    def a_read_glob(self, a):
        """Read the first file matching a glob under the views dir with the read tool."""
        import glob
        hits = sorted(glob.glob(os.path.join(self.views_dir(), a["pattern"])))
        if not hits:
            self.a_say({"text": f"nothing matches {a['pattern']}"})
            return
        if "into" in a:   # continue from it: copy its content into the workspace
            content = self.tool("read", f"Read {hits[0]}", "fs_read", hits[0], lambda: open(hits[0]).read(),
                                locations=[hits[0]], raw={"path": hits[0]}, ask=not self.inside_workspace(hits[0]))
            if content is not None:
                self.a_write({"path": a["into"], "content": content})
            return
        self.a_read({"path": hits[0]})

    def a_sleep(self, a):
        self.cancelled.wait(a["s"])

    def a_crash(self, a):
        os._exit(3)

    def a_hang(self, a):
        self.ignore_eof = bool(a.get("ignore_eof"))
        if a.get("ignore_cancel"):
            while True:
                time.sleep(3600)
        self.cancelled.wait()
        return "cancelled"

    def a_stop(self, a):
        return a["reason"]

    def a_ask_client(self, a):
        _, err = self.call(a["method"], a.get("params", {}))
        self.a_say({"text": f"client answered {err['code'] if err else 'ok'}"})

    def a_noise(self, a):
        with OUT_LOCK:
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()

    def a_pascal(self, a):
        self.pascal = True

    def a_info(self, a):
        self.update("session_info_update", _meta={"kiro": {"kind": "turn_completion",
                                                           "promptTurnSummaries": [{"credits": a.get("credits", 1.5)}]}})

    def a_wait_for(self, a):
        """Block until a path (relative to the views dir's parent root) exists."""
        deadline = time.time() + a.get("wait", 10)
        while time.time() < deadline and not os.path.exists(a["path"]):
            time.sleep(0.05)


if __name__ == "__main__":
    Agent(os.environ.get("FAKE_AGENT_SCRIPT", "")).serve()
