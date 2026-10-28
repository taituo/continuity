"""End-to-end use cases: runner + real agent processes speaking ACP (the fake
V3 agent) + real tests + hidden holdout. Each test is one way the system is
meant to be used; docs/scenarios.md tells the same stories in prose."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from scenario_kit import FAKE, PY, TEST_HINT, World

from continuity.audit import audit
from continuity.runner import Runner

INSERT_SUB = ["def mul(a, b):", "def sub(a, b):\n    return a - b\n\n\ndef mul(a, b):"]
APPEND_POW = "\n\ndef power(a, b):\n    return a ** b\n"
RUN_TESTS = {"do": "shell", "cmd": TEST_HINT}


class Scenario(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.o = self.w.o

    def tearDown(self):
        self.assertFalse(self.w.allow_always_used(), "a persistent allow was chosen")
        audit(self.w.root)                     # every scenario leaves an auditable log
        self.w.cleanup()

    def assertHeadGreen(self):
        files = self.o.store.files(self.o.state.head)
        ok, report = self.w.evaluator(files)
        self.assertTrue(ok, report)
        self.assertFalse([p for p in files if "__pycache__" in p], "build droppings in head")


class ParallelWork(Scenario):
    def test_two_features_in_one_file_at_the_same_time(self):
        """Two goals, two agents, same file, same starting point: both land."""
        g_sub = self.o.open_goal("add sub()")
        g_pow = self.o.open_goal("add power()")
        self.w.write_script({
            "add sub()": [[{"do": "write", "path": "calc.py", "replace": INSERT_SUB}, RUN_TESTS,
                           {"do": "say", "text": "Added sub() after add()."}]],
            "add power()": [[{"do": "write", "path": "calc.py", "append": APPEND_POW}, RUN_TESTS,
                             {"do": "say", "text": "Added power() at the end."}]]})
        r = self.w.run(parallel=2)
        self.assertEqual(sorted(r["goals_done"]), sorted(["g0", g_sub, g_pow]))
        a, b = self.w.sessions_for(g_sub)[0], self.w.sessions_for(g_pow)[0]
        self.assertEqual(a.base, b.base, "both started from the same head")
        self.assertIn("def sub", self.w.head_file())
        self.assertIn("def power", self.w.head_file())
        self.assertEqual(self.o.state.deltas[a.id].intent, "Added sub() after add().")
        self.assertHeadGreen()

    def test_durable_goal_is_worked_through_its_children(self):
        """A long-lived goal ('maintain calc') is a container: agents work on
        its children, never on it, and it is never 'done'."""
        root = self.o.open_goal("maintain calc", durable=True)
        g1 = self.o.open_goal("add sub()", parent=root)
        self.w.write_script({"add sub()": [[{"do": "expect", "text": "(part of g"},
                                            {"do": "write", "path": "calc.py", "replace": INSERT_SUB}]]})
        r = self.w.run()
        self.assertIn(g1, r["goals_done"])
        self.assertFalse(self.o.state.goals[root].done)
        self.assertEqual(self.w.sessions_for(root), [])


class Variants(Scenario):
    def test_first_good_variant_wins_and_the_rest_are_stopped(self):
        """Run competing attempts at one goal. The first green one lands; the
        slow one is cancelled instead of burning budget."""
        g = self.o.open_goal("add sub()")
        self.w.write_script({"add sub()": [
            [{"do": "write", "path": "calc.py", "replace": INSERT_SUB}, {"do": "say", "text": "fast"}],
            [{"do": "sleep", "s": 20}, {"do": "write", "path": "calc.py", "append": APPEND_POW}]]})
        t0 = time.time()
        r = self.w.run(variants=2, cancel_grace=2)
        self.assertLess(time.time() - t0, 10)
        # which process got which script depends on start order, so identify by outcome
        fast = [s for s in self.w.sessions_for(g) if s.id in self.o.state.deltas]
        slow = [s for s in self.w.sessions_for(g) if s.id not in self.o.state.deltas]
        self.assertEqual([self.o.state.deltas[s.id].status for s in fast], ["integrated"])
        self.assertEqual([(s.status, self.w.turn_note(s.id)["stop_reason"]) for s in slow],
                         [("lost", "superseded")])
        self.assertIn(g, r["goals_done"])

    def test_late_variant_is_superseded_not_merged(self):
        """Both variants finish. Whichever is decided second is not merged on
        top: it was stopped or rejected as superseded."""
        g = self.o.open_goal("add sub()")
        self.w.write_script({"add sub()": [
            [{"do": "write", "path": "calc.py", "replace": INSERT_SUB}, {"do": "say", "text": "a"}],
            [{"do": "write", "path": "calc.py", "append": APPEND_POW}, {"do": "say", "text": "b"}]]})
        self.w.run(variants=2)
        outcomes = []
        for s in self.w.sessions_for(g):
            d = self.o.state.deltas.get(s.id)
            outcomes.append((d.status, d.reason) if d else ("lost", self.w.turn_note(s.id)["stop_reason"]))
        self.assertEqual(sorted(outcomes)[0][0], "integrated")
        self.assertIn(sorted(outcomes)[1], [("rejected", "superseded"), ("lost", "superseded")])
        self.assertEqual(self.w.head_file().count("def "), 4, "exactly one variant landed")


class Conflicts(Scenario):
    def test_conflict_becomes_a_resolution_goal_with_full_context(self):
        """Two agents change the same line. The second does not block anything:
        it becomes a resolution goal, and the resolver is told what collided."""
        g_hint = self.o.open_goal("add type hints to mul")
        g_names = self.o.open_goal("rename mul parameters to x and y")
        self.w.write_script({
            "add type hints to mul": [[
                {"do": "write", "path": "calc.py",
                 "replace": ["def mul(a, b):", "def mul(a: float, b: float) -> float:"]},
                {"do": "say", "text": "Typed mul."}]],
            "rename mul parameters": [[
                {"do": "sleep", "s": 0.4},
                {"do": "write", "path": "calc.py",
                 "replace": ["def mul(a, b):\n    return a * b", "def mul(x, y):\n    return x * y"]},
                {"do": "say", "text": "Renamed mul's parameters to x and y."}]],
            "resolve conflict of": [[
                {"do": "expect", "text": ["This goal resolves a conflict", "conflicting files: calc.py",
                                          "its intent: Renamed mul's parameters to x and y.",
                                          "(for: add type hints to mul), whose intent was: Typed mul.",
                                          ".diff"]},
                {"do": "read_glob", "pattern": "conflict/*.diff"},
                {"do": "write", "path": "calc.py",
                 "replace": ["def mul(a: float, b: float) -> float:\n    return a * b",
                             "def mul(x: float, y: float) -> float:\n    return x * y"]},
                RUN_TESTS, {"do": "say", "text": "Kept the type hints and applied the rename."}]]})
        r = self.w.run(parallel=2)
        self.assertEqual(self.o.state.deltas[self.w.sessions_for(g_names)[0].id].status, "conflict")
        resolution = [g for g in self.o.state.goals.values() if g.resolves][0]
        self.assertEqual(resolution.parent, g_names)
        self.assertIn(resolution.id, r["goals_done"])
        self.assertIn(g_names, r["goals_done"], "resolving the conflict finishes the original goal")
        self.assertIn("def mul(x: float, y: float) -> float:", self.w.head_file())
        self.assertHeadGreen()


class Continuity(Scenario):
    def test_crashed_agent_is_continued_by_the_next_one(self):
        """The agent dies mid-work. Its last checkpoint stays in the log; the
        next agent is told what happened and starts from that work."""
        g = self.o.open_goal("add mod()")
        self.w.write_script({"add mod()": [
            [{"do": "write", "path": "calc.py", "append": "\n\ndef mod(a, b):\n    return a % b\n"},
             {"do": "sleep", "s": 0.2}, {"do": "crash"}],
            [{"do": "expect", "text": ["did not finish (crashed)", "it had changed calc.py", "earlier attempt"]},
             {"do": "read_glob", "pattern": "attempts/*/*/calc.py", "into": "calc.py"},
             RUN_TESTS, {"do": "say", "text": "Finished the previous attempt's mod()."}]]})
        r = self.w.run()
        first, second = self.w.sessions_for(g)
        self.assertEqual(first.status, "lost")
        self.assertEqual(self.w.turn_note(first.id)["stop_reason"], "crashed")
        self.assertIn(g, r["goals_done"])
        self.assertIn("def mod", self.w.head_file())
        obs = [e for e in self.w.events("session.observe") if e["session"] == second.id]
        self.assertEqual([e["peer"] for e in obs], [first.id])
        self.assertIn(obs[0]["via"], ("acp-tool", "inotify"))   # whichever signal arrives first
        self.assertEqual(self.o.state.deltas[second.id].flags, [], "continuing your own goal is not a risk")
        self.assertHeadGreen()

    def test_partial_work_lands_and_the_goal_stays_open(self):
        """An agent does the most valuable piece and says what remains. The piece
        is integrated, the goal stays open, the next agent continues."""
        g = self.o.open_goal("add sub() and power()")
        self.w.write_script({"add sub() and power()": [
            [{"do": "write", "path": "calc.py", "replace": INSERT_SUB},
             {"do": "say", "text": "Added sub().\nREMAINING: power()"}],
            [{"do": "expect", "text": ["integrated (partial", "REMAINING: power()"]},
             {"do": "write", "path": "calc.py", "append": APPEND_POW},
             {"do": "say", "text": "Added power(); the goal is complete."}]]})
        r = self.w.run()
        first, second = self.w.sessions_for(g)
        self.assertTrue(self.o.state.deltas[first.id].partial)
        self.assertEqual(self.o.state.deltas[first.id].status, "integrated")
        self.assertEqual(second.base, self.o.state.heads[-2][1], "second attempt starts from the partial work")
        self.assertIn(g, r["goals_done"])
        self.assertIn("def sub", self.w.head_file())
        self.assertIn("def power", self.w.head_file())

    def test_max_turn_requests_counts_as_partial(self):
        g = self.o.open_goal("add sub()", budget=1)
        self.w.write_script({"add sub()": [[{"do": "write", "path": "calc.py", "replace": INSERT_SUB},
                                            {"do": "stop", "reason": "max_turn_requests"}]]})
        self.w.run()
        s = self.w.sessions_for(g)[0]
        self.assertTrue(self.o.state.deltas[s.id].partial)
        self.assertEqual(self.o.state.deltas[s.id].status, "integrated")
        self.assertFalse(self.o.state.goals[g].done)

    def test_runner_killed_mid_session_resumes_cleanly(self):
        """kill -9 the runner while an agent works. The next runner records the
        orphaned session, keeps its checkpoint, and retries the goal."""
        g = self.o.open_goal("add mod()")
        self.w.write_script({"add mod()": [
            [{"do": "write", "path": "calc.py", "append": "\n\ndef mod(a, b):\n    return a % b\n"},
             {"do": "hang", "ignore_cancel": True, "ignore_eof": True}],
            [{"do": "expect", "text": "orphaned by a runner restart"},
             {"do": "read_glob", "pattern": "attempts/*/*/calc.py", "into": "calc.py"},
             {"do": "say", "text": "Picked up the orphaned work."}]]})
        env = dict(os.environ, FAKE_AGENT_SCRIPT=str(self.w.script), PYTHONPATH=str(Path(__file__).parents[1]))
        cmd = [PY, "-m", "continuity", "run", str(self.w.root), "--agent", " ".join(FAKE),
               "--check", " ".join(self.w.evaluator.cmd[-1:]), "--protected", "tests/", "--no-fsync"]
        p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 20
        while time.time() < deadline:
            o = self.w.orchestrator()
            s = [x for x in o.state.sessions.values() if x.goal == g]
            if s and s[0].checkpoint:
                break
            time.sleep(0.1)
        agents = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
        self.assertTrue(agents, "the runner had an agent child")
        p.send_signal(signal.SIGKILL)
        p.wait()
        p.stderr.close()
        time.sleep(0.3)
        for pid in agents:                                  # die-with-parent took them down
            self.assertFalse(Path(f"/proc/{pid}").exists() and "fake_agent" in
                             Path(f"/proc/{pid}/cmdline").read_text(errors="ignore"), pid)
        self.o = self.w.o = self.w.orchestrator()          # fresh process view of the log
        self.assertEqual(self.w.sessions_for(g)[0].status, "active")
        r = self.w.run()
        first, second = self.w.sessions_for(g)
        self.assertEqual(r["orphaned_on_start"], [first.id])
        self.assertEqual(first.status, "lost")
        self.assertIn(g, r["goals_done"])
        self.assertIn("def mod", self.w.head_file())


class Judgement(Scenario):
    def test_overfitting_is_caught_by_hidden_checks_without_leaking_them(self):
        """Visible tests pass, hidden behaviour breaks: rejected. The retry is
        told hidden checks failed, but not what they are."""
        g = self.o.open_goal("simplify div")
        self.w.write_script({"simplify div": [
            [{"do": "write", "path": "calc.py",
              "replace": ["return None if b == 0 else a / b", "return a / b"]},
             RUN_TESTS, {"do": "say", "text": "div is now a plain division."}],
            [{"do": "expect", "text": "hidden checks failed (details withheld)"},
             {"do": "expect_absent", "text": ["test_div_by_zero_is_none", "assertIsNone"]},
             {"do": "write", "path": "calc.py",
              "replace": ["return None if b == 0 else a / b", "return a / b if b else None"]},
             {"do": "say", "text": "Shorter div that keeps the zero case."}]]})
        r = self.w.run()
        first, second = self.w.sessions_for(g)
        self.assertEqual((self.o.state.deltas[first.id].status, self.o.state.deltas[first.id].reason),
                         ("rejected", "eval_failed"))
        self.assertIn(g, r["goals_done"])
        self.assertIn("return a / b if b else None", self.w.head_file())
        self.assertNotIn(b"test_div_by_zero", (self.w.root / ".continuity" / "log.jsonl").read_bytes())
        self.assertHeadGreen()

    def test_editing_the_tests_is_refused_twice(self):
        """Write to tests/ via the file tool: refused at permission time. Via
        the shell: reaches the worktree, rejected at integration."""
        g = self.o.open_goal("make mul return a sum", budget=1)
        self.w.write_script({"make mul return a sum": [[
            {"do": "write", "path": "calc.py", "replace": ["return a * b", "return a + b"]},
            {"do": "write", "path": "tests/test_calc.py", "replace": ["mul(2, 3), 6", "mul(2, 3), 5"]},
            {"do": "shell", "cmd": "sed -i 's/mul(2, 3), 6/mul(2, 3), 5/' tests/test_calc.py"},
            RUN_TESTS, {"do": "say", "text": "mul now sums; tests adjusted."}]]})
        self.w.run()
        s = self.w.sessions_for(g)[0]
        denials = self.w.turn_note(s.id)["denials"]
        self.assertEqual([d["capability"] for d in denials], ["fs_write"])
        self.assertIn("protected path", denials[0]["reason"])
        self.assertEqual(self.w.turn_note(s.id)["failures"], [{"tool": "Write tests/test_calc.py", "reason": "denied"}])
        self.assertEqual((self.o.state.deltas[s.id].status, self.o.state.deltas[s.id].reason),
                         ("rejected", "protected:tests/test_calc.py"))
        self.assertIn("return a * b", self.w.head_file())

    def test_budget_bounds_a_hopeless_goal(self):
        g = self.o.open_goal("make div faster", budget=2)
        self.w.write_script({"make div faster": [[
            {"do": "write", "path": "calc.py", "replace": ["return None if b == 0 else a / b", "return 0"]}]]})
        r = self.w.run()
        self.assertEqual(len(self.w.sessions_for(g)), 2)
        self.assertIn(g, r["goals_open"])
        self.assertEqual(r["fatal"], None)


class SharedWorld(Scenario):
    def test_human_edit_during_a_run_is_captured_and_kept(self):
        """Someone edits main/ while an agent works. The edit becomes an
        external delta, is evaluated, lands first; the agent's work merges on top."""
        g = self.o.open_goal("document add")
        self.w.write_script({"document add": [[
            {"do": "sleep", "s": 0.6},
            {"do": "write", "path": "calc.py",
             "replace": ["def add(a, b):\n", 'def add(a, b):\n    """Sum of a and b."""\n']},
            {"do": "say", "text": "Documented add."}]]})

        async def scenario():
            runner = Runner(self.o, self.w.config())
            task = asyncio.create_task(runner.run())
            await asyncio.sleep(0.25)
            f = self.w.root / "main" / "calc.py"
            f.write_text(f.read_text() + "\n\ndef neg(a):\n    return -a\n")
            return await task
        r = asyncio.run(scenario())
        ext = [x for x in self.o.state.deltas.values() if x.origin == "external"]
        self.assertEqual([(x.status) for x in ext], ["integrated"])
        self.assertIn(g, r["goals_done"])
        self.assertIn("def neg", self.w.head_file())
        self.assertIn('"""Sum of a and b."""', self.w.head_file())
        self.assertEqual((self.w.root / "main" / "calc.py").read_text(), self.w.head_file())

    def test_goals_added_from_a_shell_while_it_runs(self):
        """`continuity goal` from another process while the runner works: the
        log takes both writers without forking, and the runner picks the goal up."""
        self.o.open_goal("add sub()")
        self.w.write_script({
            "add sub()": [[{"do": "sleep", "s": 1.0},
                           {"do": "write", "path": "calc.py", "replace": INSERT_SUB}]],
            "add power()": [[{"do": "write", "path": "calc.py", "append": APPEND_POW}]]})

        async def scenario():
            task = asyncio.create_task(Runner(self.o, self.w.config()).run())
            await asyncio.sleep(0.3)
            p = await asyncio.create_subprocess_exec(
                PY, "-m", "continuity", "goal", str(self.w.root), "add power()",
                env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1])),
                stdout=asyncio.subprocess.PIPE)
            out, _ = await p.communicate()
            return out.decode().strip(), await task
        gid, r = asyncio.run(scenario())
        self.assertIn(gid, r["goals_done"])
        self.assertIn("def power", self.w.head_file())
        self.assertIn("def sub", self.w.head_file())

    def test_building_on_unfinished_peer_work_is_recorded_and_flagged(self):
        """B reads A's in-progress snapshot with plain `cat` (no ACP signal at
        all). inotify records the read. B lands first; A is later rejected; B
        is flagged so a reviewer knows it built on failed work."""
        g_a = self.o.open_goal("rework add")
        g_b = self.o.open_goal("add sum3 helper")
        self.w.write_script({
            "rework add": [[
                {"do": "write", "path": "calc.py", "replace": ["return a + b", "return a + b + 1  # WIP"]},
                {"do": "sleep", "s": 1.5}, {"do": "say", "text": "Reworked add."}]],
            "add sum3 helper": [[
                {"do": "peek_shell", "file": "calc.py", "wait": 10},
                {"do": "write", "path": "helpers.py",
                 "content": "from calc import add\n\n\ndef sum3(a, b, c):\n    return add(add(a, b), c)\n"},
                {"do": "say", "text": "Added sum3 on top of add."}]]})
        self.w.run(parallel=2, default_budget=1)
        a, b = self.w.sessions_for(g_a)[0], self.w.sessions_for(g_b)[0]
        obs = [(e["peer"], e["via"]) for e in self.w.events("session.observe") if e["session"] == b.id]
        self.assertIn((a.id, "inotify"), obs)
        db = self.o.state.deltas[b.id]
        self.assertEqual(db.status, "integrated")
        self.assertEqual(self.o.state.deltas[a.id].status, "rejected")
        self.assertIn(("dependency_unfinished", a.id), db.flags)
        self.assertIn(("dependency_rejected", a.id), db.flags)

    def test_agent_cannot_touch_other_sessions_or_the_system(self):
        """Writes outside the worktree and reads of other worktrees, the log,
        or the holdout are refused; the agent keeps working."""
        g = self.o.open_goal("probe the fence")
        root = str(self.w.root)
        self.w.write_script({"probe the fence": [[
            {"do": "write", "path": f"{root}/main/calc.py", "content": "hacked\n"},
            {"do": "write", "path": "../escape.py", "content": "x = 1\n"},
            {"do": "read", "path": f"{root}/.continuity/log.jsonl"},
            {"do": "read", "path": f"{self.w.holdout}/test_hidden.py"},
            {"do": "read", "path": "/etc/hostname"},
            {"do": "read", "path": f"{root}/.continuity/log.jsonl", "client": True},
            {"do": "write", "path": "calc.py", "append": "\n# fenced\n", "client": True},
            {"do": "say", "text": "Probed."}]]})
        self.w.run(default_budget=1)
        s = self.w.sessions_for(g)[0]
        note = self.w.turn_note(s.id)
        reasons = [d["reason"] for d in note["denials"]]
        self.assertEqual(len(reasons), 5, reasons)
        self.assertEqual(sum("outside the session worktree" in x for x in reasons), 2)
        self.assertEqual(sum("another session's or the system's" in x for x in reasons), 3)
        self.assertNotIn("hacked", (self.w.root / "main" / "calc.py").read_text())
        self.assertFalse((self.w.root / "work" / "escape.py").exists())
        self.assertIn("# fenced", self.w.head_file())


class Failures(Scenario):
    def test_timeout_cancel_and_unresponsive_agent(self):
        """A stuck agent gets session/cancel; one that ignores it is killed.
        Both sessions are lost, nothing hangs, the goals stay open."""
        g1 = self.o.open_goal("refactor everything", budget=1)
        g2 = self.o.open_goal("rewrite everything", budget=1)
        self.w.write_script({"refactor everything": [[{"do": "hang"}]],
                             "rewrite everything": [[{"do": "hang", "ignore_cancel": True}]]})
        t0 = time.time()
        r = self.w.run(turn_timeout=1.0, cancel_grace=1.0)
        self.assertLess(time.time() - t0, 15)
        n1 = self.w.turn_note(self.w.sessions_for(g1)[0].id)
        n2 = self.w.turn_note(self.w.sessions_for(g2)[0].id)
        self.assertEqual((n1["stop_reason"], n1["agent_stop_reason"]), ("timeout", "cancelled"))
        self.assertEqual((n2["stop_reason"], n2["agent_stop_reason"]), ("timeout", ""))
        self.assertEqual(sorted(r["goals_open"]), sorted([g1, g2]))

    def test_two_runners_cannot_share_a_root(self):
        """The log has one writer. A second runner on the same root refuses to start."""
        from continuity.runner import RunnerBusy
        g = self.o.open_goal("add sub()", budget=1)
        self.w.write_script({"add sub()": [[{"do": "sleep", "s": 1.0}, {"do": "say", "text": "slow"}]]})

        async def both():
            first = asyncio.create_task(Runner(self.o, self.w.config()).run())
            await asyncio.sleep(0.3)
            with self.assertRaises(RunnerBusy):
                await Runner(self.w.orchestrator(), self.w.config()).run()
            return await first
        r = asyncio.run(both())
        self.assertEqual(r["sessions_started"], 1)
        self.assertEqual(len(self.w.sessions_for(g)), 1)

    def test_refusal_is_lost_not_proposed(self):
        g = self.o.open_goal("delete the tests", budget=1)
        self.w.write_script({"delete the tests": [[{"do": "stop", "reason": "refusal"}]]})
        self.w.run()
        s = self.w.sessions_for(g)[0]
        self.assertEqual(s.status, "lost")
        self.assertEqual(self.w.turn_note(s.id)["stop_reason"], "refusal")

    def test_agent_that_cannot_start_stops_the_run(self):
        """A setup failure (here: the agent binary is missing; with Kiro, a
        missing login) stops the runner instead of burning every budget."""
        self.o.open_goal("add sub()")
        self.o.open_goal("add power()")
        r = self.w.run(agent_argv=["/nonexistent/kiro-cli", "acp"])
        self.assertIsNotNone(r["fatal"])
        self.assertEqual(r["sessions_started"], 2)


class Protocol(Scenario):
    def test_v2_style_and_noisy_agent_still_works(self):
        """PascalCase update kinds on `session/notification`, a non-JSON line,
        an unknown request to the client, client-side fs writes, and usage
        info: all handled; the work lands."""
        g = self.o.open_goal("add sub()")
        self.w.write_script({"add sub()": [[
            {"do": "pascal"}, {"do": "noise"},
            {"do": "ask_client", "method": "_kiro/auth/getAccessToken"},
            {"do": "write", "path": "calc.py", "replace": INSERT_SUB, "client": True},
            {"do": "info", "credits": 2.5},
            {"do": "say", "text": "Added sub()."}]]})
        r = self.w.run()
        s = self.w.sessions_for(g)[0]
        note = self.w.turn_note(s.id)
        self.assertEqual(note["unknown_requests"], ["_kiro/auth/getAccessToken"])
        self.assertIn("turn_completion", note["info"])
        self.assertEqual(self.o.state.deltas[s.id].intent, "Added sub().")
        self.assertIn(g, r["goals_done"])

    def test_no_mcp_and_views_passed_as_additional_directory(self):
        """The fake agent refuses any session/new with MCP servers, so every
        scenario passing proves mcpServers is always []."""
        g = self.o.open_goal("add sub()")
        self.w.write_script({"add sub()": [[{"do": "expect", "text": "PEERS.md"},
                                            {"do": "write", "path": "calc.py", "replace": INSERT_SUB}]]})
        r = self.w.run(additional_dirs=False)
        self.assertIn(g, r["goals_done"])


if __name__ == "__main__":
    unittest.main()
