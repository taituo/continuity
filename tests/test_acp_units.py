"""Unit tests for the ACP adapter pieces: permission policy, option choice,
JSON-RPC transport, inotify observation, sandbox argv, ignore rules."""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from continuity import inotify
from continuity.acp.client import norm_kind
from continuity.acp.jsonrpc import METHOD_NOT_FOUND, Connection
from continuity.acp.policy import Fence, choose_option
from continuity.sandbox import Bwrap
from continuity.store import Store, ignored


def perm(capability=None, resource=None, kind=None, locations=None, raw=None):
    p = {"sessionId": "x", "toolCall": {"toolCallId": "t"}, "options": []}
    if capability:
        p["_meta"] = {"kiro": {"consent": {"capability": capability, "resource": resource}}}
    if kind:
        p["toolCall"]["kind"] = kind
    if locations:
        p["toolCall"]["locations"] = [{"path": x} for x in locations]
    if raw:
        p["toolCall"]["rawInput"] = raw
    return p


class Policy(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        for d in ("work/s5", "work/s50", "views/s5", "views/s50", ".continuity", "main"):
            (self.root / d).mkdir(parents=True)
        self.hold = self.root.parent / (self.root.name + "-hold")
        self.f = Fence(self.root / "work/s5", self.root / "views/s5", self.root, ("tests/",),
                       deny_read=(str(self.hold),))

    def d(self, **kw):
        return self.f.decide(perm(**kw))

    def test_writes_only_inside_own_worktree(self):
        w = str(self.root / "work/s5")
        self.assertTrue(self.d(capability="fs_write", resource=f"{w}/calc.py").allow)
        self.assertTrue(self.d(capability="fs_write", resource="src/new.py").allow)   # relative
        self.assertFalse(self.d(capability="fs_write", resource=str(self.root / "work/s50/x.py")).allow)
        self.assertFalse(self.d(capability="fs_write", resource=f"{w}/../s50/x.py").allow)
        self.assertFalse(self.d(capability="fs_write", resource=str(self.root / "main/calc.py")).allow)
        self.assertFalse(self.d(capability="fs_write", resource="tests/test_x.py").allow)
        self.assertFalse(self.d(capability="fs_write").allow, "unknown target is refused")

    def test_symlink_out_of_the_worktree_is_resolved(self):
        os.symlink(self.root / "main", self.root / "work/s5/link")
        self.assertFalse(self.d(capability="fs_write", resource="link/calc.py").allow)

    def test_privileged_goal_may_touch_protected(self):
        f = Fence(self.root / "work/s5", self.root / "views/s5", self.root, ("tests/",), privileged=True)
        self.assertTrue(f.decide(perm(capability="fs_write", resource="tests/t.py")).allow)

    def test_reads(self):
        r = self.root
        self.assertTrue(self.d(capability="fs_read", resource="/etc/hostname").allow)
        self.assertFalse(self.d(capability="fs_read", resource=str(r / ".continuity/log.jsonl")).allow)
        self.assertFalse(self.d(capability="fs_read", resource=str(r / "work/s50/a.py")).allow)
        self.assertFalse(self.d(capability="fs_read", resource=str(r / "views/s50/PEERS.md")).allow)
        self.assertFalse(self.d(capability="fs_read", resource=str(self.hold / "t.py")).allow)
        own = self.d(capability="fs_read", resource=str(r / "views/s5/peers/s7/abc/calc.py"))
        self.assertTrue(own.allow)
        self.assertEqual(len(own.observed), 1)

    def test_classification_without_kiro_meta(self):
        w = str(self.root / "work/s5")
        self.assertTrue(self.f.decide(perm(kind="edit", locations=[f"{w}/a.py"])).allow)
        self.assertFalse(self.f.decide(perm(kind="edit", raw={"file_path": "/etc/passwd"})).allow)
        self.assertEqual(self.f.decide(perm(kind="execute", raw={"command": "ls"})).capability, "shell")

    def test_shell_guard_is_exact_about_own_paths(self):
        r = str(self.root)
        self.assertTrue(self.d(capability="shell", resource=f"cat {r}/work/s5/a.py").allow)
        self.assertTrue(self.d(capability="shell", resource=f"ls {r}/views/s5/peers").allow)
        self.assertFalse(self.d(capability="shell", resource=f"cat {r}/work/s50/a.py").allow)
        self.assertFalse(self.d(capability="shell", resource=f"cat {r}/.continuity/log.jsonl").allow)

    def test_other_capabilities(self):
        self.assertFalse(self.d(capability="mcp", resource="srv/tool").allow)
        self.assertFalse(self.d(capability="subagent", resource="x").allow)
        self.assertTrue(self.d(capability="web_fetch", resource="https://x").allow)


class Options(unittest.TestCase):
    ALL = [{"optionId": "a1", "kind": "allow_once"}, {"optionId": "aa", "kind": "allow_always"},
           {"optionId": "r1", "kind": "reject_once"}, {"optionId": "ra", "kind": "reject_always"}]

    def test_one_shot_only(self):
        self.assertEqual(choose_option(self.ALL, True)[0], "a1")
        self.assertEqual(choose_option(self.ALL, False)[0], "r1")

    def test_never_persist_an_allow(self):
        opts = [o for o in self.ALL if o["kind"] != "allow_once"]
        oid, note = choose_option(opts, True)
        self.assertEqual(oid, "r1")
        self.assertIn("persisting", note)

    def test_nothing_to_choose_means_cancelled(self):
        self.assertIsNone(choose_option([], True)[0])

    def test_pascal_case_kinds(self):
        self.assertEqual(norm_kind("ToolCallUpdate"), "tool_call_update")
        self.assertEqual(norm_kind("agent_message_chunk"), "agent_message_chunk")


ECHO_AGENT = r'''
import json, sys
print("garbage line", flush=True)
print(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "_kiro/unknown", "params": {}}), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    if m.get("id") == 7:
        print(json.dumps({"jsonrpc": "2.0", "method": "seen", "params": {"error": m.get("error")}}), flush=True)
    elif "method" in m:
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"echo": m["params"]}}), flush=True)
'''


class Transport(unittest.TestCase):
    def test_unknown_request_gets_explicit_error_and_garbage_is_skipped(self):
        async def go():
            seen = asyncio.get_running_loop().create_future()
            conn = await Connection.spawn([sys.executable, "-c", ECHO_AGENT], cwd=".")
            conn.notifications["seen"] = lambda p: seen.set_result(p) or asyncio.sleep(0)
            try:
                res = await conn.request("ping", {"x": 1})
                err = await asyncio.wait_for(seen, 5)
            finally:
                await conn.close()
            return res, err, conn.garbage_lines, conn.unknown_requests
        res, err, garbage, unknown = asyncio.run(go())
        self.assertEqual(res, {"echo": {"x": 1}})
        self.assertEqual(err["error"]["code"], METHOD_NOT_FOUND)
        self.assertEqual(garbage, 1)
        self.assertEqual(unknown, ["_kiro/unknown"])

    def test_dead_agent_fails_pending_requests(self):
        async def go():
            conn = await Connection.spawn([sys.executable, "-c", "import sys; sys.stdin.readline()"], cwd=".")
            try:
                await asyncio.wait_for(conn.request("x", {}), 5)
            except Exception as e:  # noqa: BLE001
                return type(e).__name__
            finally:
                await conn.close()
        self.assertEqual(asyncio.run(go()), "PeerExited")


@unittest.skipUnless(inotify.available, "inotify is Linux-only")
class TransportCleanup(unittest.TestCase):
    def test_cancelling_with_a_live_agent_does_not_hang(self):
        """asyncio.run cancels leftover tasks on exit; the reader must not wait
        for an agent that is still alive."""
        async def go():
            conn = await Connection.spawn([sys.executable, "-c", "import time; time.sleep(30)"], cwd=".")
            self.pid = conn.proc.pid
            await asyncio.sleep(0.1)
        t0 = time.time()
        asyncio.run(go())
        self.assertLess(time.time() - t0, 5)
        os.kill(self.pid, 9)


class Inotify(unittest.TestCase):
    def test_open_is_seen_including_after_rename_and_from_a_child_process(self):
        d = Path(tempfile.mkdtemp())
        (d / ".tmp" / "sub").mkdir(parents=True)
        (d / ".tmp" / "sub" / "f.txt").write_text("x")

        async def go():
            seen = []
            w = inotify.Inotify(seen.append)
            w.watch_tree(str(d / ".tmp"))
            os.rename(d / ".tmp", d / "final")
            w.moved(str(d / ".tmp"), str(d / "final"))
            p = await asyncio.create_subprocess_exec("cat", str(d / "final" / "sub" / "f.txt"),
                                                     stdout=asyncio.subprocess.DEVNULL)
            await p.wait()
            await asyncio.sleep(0.05)
            w.drain()
            w.close()
            return seen
        self.assertEqual(asyncio.run(go()), [str(d / "final" / "sub" / "f.txt")])


class SandboxArgv(unittest.TestCase):
    def test_root_hidden_then_only_this_session_exposed(self):
        b = Bwrap(rw=("/var",), ro_hide=("/opt/holdout",))
        a = b.wrap(["kiro-cli", "acp"], "/srv/c", "/srv/c/work/s5", "/srv/c/views/s5")
        i_root = a.index("--tmpfs", a.index("/tmp"))
        self.assertEqual(a[i_root + 1], "/srv/c")
        self.assertLess(i_root, a.index("/srv/c/work/s5"), "binds come after the hiding tmpfs")
        self.assertEqual(a[a.index("--bind") + 1:a.index("--bind") + 3], ["/srv/c/work/s5"] * 2)
        self.assertIn("--ro-bind", a[a.index("/srv/c/work/s5"):])
        self.assertIn("/opt/holdout", a)
        self.assertEqual(a[-3:], ["--", "kiro-cli", "acp"])
        self.assertIn("--die-with-parent", a)


class Ignore(unittest.TestCase):
    def test_build_droppings_never_enter_a_tree(self):
        self.assertTrue(ignored("pkg/__pycache__/m.cpython-313.pyc"))
        self.assertTrue(ignored("a/.git/config"))
        self.assertFalse(ignored("src/git_helpers.py"))
        d = Path(tempfile.mkdtemp())
        (d / "__pycache__").mkdir()
        (d / "__pycache__" / "x.pyc").write_bytes(b"\0")
        (d / "a.py").write_text("x")
        os.symlink("/etc/passwd", d / "leak")
        s = Store(Path(tempfile.mkdtemp()))
        self.assertEqual(sorted(s.tree(s.snapshot(d))), ["a.py"])


if __name__ == "__main__":
    unittest.main()
