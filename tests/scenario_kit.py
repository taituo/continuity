"""Shared setup for end-to-end scenarios: a small real Python project, real
tests, a hidden holdout, the orchestrator, and the runner driving the fake
V3 ACP agent as a separate process."""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

from continuity.evaluators import CommandEvaluator
from continuity.orchestrator import Orchestrator
from continuity.runner import Runner, RunnerConfig
from continuity.context import PromptConfig
from continuity.testing import fake_agent

PY = sys.executable
FAKE = [PY, fake_agent.__file__]

CALC = textwrap.dedent('''\
    def add(a, b):
        return a + b


    def mul(a, b):
        return a * b


    def div(a, b):
        return None if b == 0 else a / b
''')

TESTS = textwrap.dedent('''\
    import unittest
    from calc import add, mul, div


    class T(unittest.TestCase):
        def test_add(self):
            self.assertEqual(add(2, 3), 5)

        def test_mul(self):
            self.assertEqual(mul(2, 3), 6)

        def test_div(self):
            self.assertEqual(div(6, 3), 2)
''')

HIDDEN = textwrap.dedent('''\
    import unittest
    from calc import div


    class Hidden(unittest.TestCase):
        def test_div_by_zero_is_none(self):
            self.assertIsNone(div(1, 0))
''')

VISIBLE_CMD = ["sh", "-c", f"PYTHONPATH=. {PY} -m unittest discover -s tests -q"]
HIDDEN_CMD = ["sh", "-c", f"PYTHONPATH=. {PY} -m unittest discover -s _holdout -q"]
TEST_HINT = f"PYTHONPATH=. {PY} -m unittest discover -s tests -q"


class World:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="continuity-scn-"))
        self.root = self.tmp / "root"
        self.holdout = self.tmp / "holdout"
        self.holdout.mkdir()
        (self.holdout / "test_hidden.py").write_text(HIDDEN)
        self.script = self.tmp / "script.json"
        self.evaluator = CommandEvaluator(VISIBLE_CMD, holdout=self.holdout, holdout_cmd=HIDDEN_CMD)
        self.o = self.orchestrator()
        boot = self.o.open_goal("bootstrap", privileged=True)
        s = self.o.start_session(boot)
        (self.o.workdir(s) / "calc.py").write_text(CALC)
        (self.o.workdir(s) / "tests").mkdir()
        (self.o.workdir(s) / "tests" / "test_calc.py").write_text(TESTS)
        self.o.propose(s, "bootstrap")
        assert self.o.run_queue()[-1]["outcome"] == "integrated"

    def orchestrator(self) -> Orchestrator:
        return Orchestrator(self.root, self.evaluator, protected=("tests/",), fsync=False)

    def write_script(self, goals: dict, default=None) -> None:
        self.script.write_text(json.dumps({"goals": goals, "default": default or []}))

    def config(self, **kw) -> RunnerConfig:
        env = dict(__import__("os").environ, FAKE_AGENT_SCRIPT=str(self.script))
        base = dict(agent_argv=FAKE, parallel=3, turn_timeout=30, setup_timeout=10, cancel_grace=2,
                    deny_read=(str(self.holdout),), env=env, prompt=PromptConfig(test_hint=TEST_HINT))
        base.update(kw)
        return RunnerConfig(**base)

    def run(self, **kw) -> dict:
        return asyncio.run(Runner(self.o, self.config(**kw)).run())

    # -- inspection ---------------------------------------------------------
    def head_file(self, path="calc.py") -> str:
        return self.o.store.files(self.o.state.head)[path].decode()

    def sessions_for(self, goal: str) -> list:
        return sorted((s for s in self.o.state.sessions.values() if s.goal == goal),
                      key=lambda s: int(s.id[1:]))

    def turn_note(self, sid: str) -> dict:
        return [d for k, d in self.o.state.sessions[sid].notes if k == "turn"][-1]

    def events(self, kind: str) -> list:
        return [e["data"] for e in self.o.log.events if e["kind"] == kind]

    def allow_always_used(self) -> bool:
        return Path(str(self.script) + ".ALLOW_ALWAYS").exists()

    def cleanup(self) -> None:
        for f in self.tmp.rglob("*"):
            if f.is_dir() and not f.is_symlink():
                f.chmod(0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)
