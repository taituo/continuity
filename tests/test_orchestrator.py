"""Scenario tests against a real Python repo, real test runs and a holdout."""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from continuity.audit import audit
from continuity.evaluators import CommandEvaluator
from continuity.orchestrator import Orchestrator

CALC = textwrap.dedent('''\
    def add(a, b):
        return a + b


    def mul(a, b):
        return a * b


    def div(a, b):
        return a / b


    def square(x):
        return x * x
''')

IMPORTS = textwrap.dedent('''\
    import importlib, pathlib, unittest

    class I(unittest.TestCase):
        def test_every_module_imports(self):
            for p in sorted(pathlib.Path(".").glob("*.py")):
                importlib.import_module(p.stem)
''')

TESTS = textwrap.dedent('''\
    import unittest
    from calc import add, mul, div

    class T(unittest.TestCase):
        def test_add(self): self.assertEqual(add(2, 3), 5)
        def test_mul(self): self.assertEqual(mul(2, 3), 6)
        def test_div(self): self.assertEqual(div(6, 3), 2)
''')

HOLDOUT = textwrap.dedent('''\
    import unittest
    from calc import div

    class H(unittest.TestCase):
        def test_div_by_zero_is_none(self): self.assertIsNone(div(1, 0))
''')


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        hold = self.root.parent / (self.root.name + "-holdout")
        hold.mkdir()
        (hold / "test_holdout.py").write_text(HOLDOUT)
        py = sys.executable
        self.ev = CommandEvaluator(
            ["sh", "-c", f"{py} -m unittest discover -s . -q && "
                         f"PYTHONPATH=. {py} -m unittest discover -s _holdout -q"], holdout=hold)
        self.o = Orchestrator(self.root, self.ev, protected=("test_",), fsync=False)
        boot = self.o.open_goal("bootstrap", privileged=True)
        s = self.o.start_session(boot)
        (self.o.workdir(s) / "calc.py").write_text(CALC.replace(
            "return a / b", "return None if b == 0 else a / b"))
        (self.o.workdir(s) / "test_calc.py").write_text(TESTS)
        (self.o.workdir(s) / "test_imports.py").write_text(IMPORTS)
        self.o.propose(s)
        self.assertEqual(self.o.run_queue()[-1]["outcome"], "integrated")
        self.g = self.o.open_goal("improve calc")

    def edit(self, sid, old, new, path="calc.py"):
        f = self.o.workdir(sid) / path
        text = f.read_text()
        self.assertIn(old, text)
        f.write_text(text.replace(old, new))

    def test_parallel_edits_to_one_file_merge_cleanly(self):
        a, b = self.o.start_session(self.g), self.o.start_session(self.g)
        self.edit(a, "def add(a, b):\n    return a + b", "def add(a, b):\n    # sum\n    return b + a")
        self.edit(b, "def mul(a, b):\n    return a * b", "def mul(a, b):\n    # product\n    return b * a")
        self.o.propose(a), self.o.propose(b)
        out = [r["outcome"] for r in self.o.run_queue()]
        self.assertEqual(out, ["integrated", "integrated"])
        text = (self.o.main / "calc.py").read_text()
        self.assertIn("# sum", text)
        self.assertIn("# product", text)
        self.assertGreaterEqual(audit(self.root)["integrations_replayed"], 3)

    def test_same_line_conflict_becomes_a_goal_not_a_block(self):
        a, b, c = (self.o.start_session(self.g) for _ in range(3))
        self.edit(a, "return a + b", "return b + a")
        self.edit(b, "return a + b", "return (a) + (b)")
        self.edit(c, "return a * b", "return b * a")
        for s in (a, b, c):
            self.o.propose(s)
        out = {r["delta"]: r["outcome"] for r in self.o.run_queue()}
        self.assertEqual(out, {a: "integrated", b: "conflict", c: "integrated"})
        self.assertTrue(any(g.resolves == b for g in self.o.state.goals.values()))

    def test_editing_tests_to_pass_is_rejected(self):
        s = self.o.start_session(self.g)
        self.edit(s, "return a * b", "return a + b")
        self.edit(s, "assertEqual(mul(2, 3), 6)", "assertEqual(mul(2, 3), 5)", path="test_calc.py")
        self.o.propose(s)
        r = self.o.run_queue()[-1]
        self.assertEqual((r["outcome"], r["reason"]), ("rejected", "protected:test_calc.py"))

    def test_holdout_catches_what_visible_tests_miss(self):
        s = self.o.start_session(self.g)
        self.edit(s, "return None if b == 0 else a / b", "return a / b")  # visible tests still pass
        self.o.propose(s)
        r = self.o.run_queue()[-1]
        self.assertEqual((r["outcome"], r["reason"]), ("rejected", "eval_failed"))
        self.assertNotIn("_holdout", str(self.o.store.tree(self.o.state.deltas[s].result)))

    def test_human_edit_in_main_is_captured_and_evaluated(self):
        f = self.o.main / "calc.py"
        f.write_text(f.read_text() + "\n\ndef sub(a, b):\n    return a - b\n")
        r = self.o.run_queue()
        self.assertEqual(r[0]["outcome"], "integrated")
        x = self.o.state.deltas[r[0]["delta"]]
        self.assertEqual(x.origin, "external")

    def test_observation_creates_flag_when_dependency_fails(self):
        other = self.o.open_goal("document add")
        a, b = self.o.start_session(self.g), self.o.start_session(other)
        self.edit(a, "return a * b", "return a ** b")      # will fail tests
        self.o.checkpoint(a)
        view = self.o.peek(b, a)
        self.assertIn("a ** b", (view / "calc.py").read_text())
        self.edit(b, "def add(a, b):", "def add(a, b):  # saw a's work")
        self.o.propose(b)                                    # b is integrated first...
        self.assertEqual(self.o.run_queue()[-1]["outcome"], "integrated")
        self.assertIn(("dependency_unfinished", a), self.o.state.deltas[b].flags)
        self.o.propose(a)                                    # ...then a fails
        self.assertEqual(self.o.run_queue()[-1]["outcome"], "rejected")
        self.assertIn(("dependency_rejected", a), self.o.state.deltas[b].flags)

    def test_drift_between_eval_and_integrate_forces_retry(self):
        s = self.o.start_session(self.g)
        self.edit(s, "return a + b", "return b + a")
        self.o.propose(s)
        real = self.o.evaluator

        def sneaky(files):
            f = self.o.main / "calc.py"          # a human edits main/ mid-evaluation
            f.write_text(f.read_text() + "# touched\n")
            self.o.evaluator = real
            return real(files)
        self.o.evaluator = sneaky
        first = self.o.step()
        self.assertEqual(first["outcome"], "retry")
        rest = [r["outcome"] for r in self.o.run_queue()]
        self.assertEqual(rest, ["integrated", "integrated"])     # human edit, then s
        self.assertIn("# touched", (self.o.main / "calc.py").read_text())
        self.assertIn("return b + a", (self.o.main / "calc.py").read_text())

    def test_semantic_conflict_caught_on_merged_head(self):
        """Textually clean, each side green alone, broken together."""
        a, b = self.o.start_session(self.g), self.o.start_session(self.g)
        self.edit(a, "def square(x):", "def sq(x):")
        (self.o.workdir(b) / "geometry.py").write_text("from calc import square\n\n"
                                                       "def area(s):\n    return square(s)\n")
        self.assertTrue(self.ev(self.o.store.files(self.o.store.snapshot(self.o.workdir(a))))[0])
        self.assertTrue(self.ev(self.o.store.files(self.o.store.snapshot(self.o.workdir(b))))[0])
        self.o.propose(a), self.o.propose(b)
        out = [(r["outcome"], r.get("reason")) for r in self.o.run_queue()]
        self.assertEqual(out, [("integrated", None), ("rejected", "eval_failed")])
        self.assertNotIn("geometry.py", self.o.store.tree(self.o.state.head))

    def test_same_goal_dependency_is_expected_not_flagged(self):
        """A later attempt reading an earlier attempt at the same goal is the
        designed continuation path, so it is recorded but not flagged."""
        a, b = self.o.start_session(self.g), self.o.start_session(self.g)
        self.edit(a, "return a * b", "return a ** b")
        self.o.checkpoint(a)
        self.o.peek(b, a)
        self.o.lose(a)
        self.edit(b, "def add(a, b):", "def add(a, b):  # continued")
        self.o.propose(b)
        self.assertEqual(self.o.run_queue()[-1]["outcome"], "integrated")
        self.assertEqual(self.o.state.deltas[b].observed, [a])
        self.assertEqual(self.o.state.deltas[b].flags, [])

    def test_restart_mid_session_resumes(self):
        s = self.o.start_session(self.g)
        self.edit(s, "return a + b", "return b + a")
        o2 = Orchestrator(self.root, self.ev, protected=("test_",), fsync=False)  # crash+restart
        self.assertEqual(o2.state.sessions[s].status, "active")
        o2.propose(s)
        self.assertEqual(o2.run_queue()[-1]["outcome"], "integrated")


if __name__ == "__main__":
    unittest.main()
