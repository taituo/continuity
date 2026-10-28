"""Do the invariants have teeth? Inject known bugs and check that the
simulation or the unit/property tests catch every one of them."""
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from continuity import context, evaluators, hlc, inotify, merge, sim
from continuity import store as store_mod
from continuity.acp import client as acp_client
from continuity.acp import jsonrpc
from continuity.acp.policy import Fence
from continuity.orchestrator import Orchestrator
from continuity.runner import Runner

orig_step = Orchestrator.step
orig_peek = Orchestrator.peek
orig_diff3 = merge.diff3


def m_eval_proposal_not_candidate(self):
    """Evaluates what the agent proposed but integrates the merged candidate."""
    real = self.evaluator
    self.check_drift()
    x = self._pick()
    if x is not None:
        self.evaluator = lambda files: real(self.store.files(x.result))
    try:
        return orig_step(self)
    finally:
        self.evaluator = real


def m_silent_peek(self, sid, peer):
    """Same copy, but the look is not logged."""
    p = self.state.sessions[peer]
    tree = self.state.deltas[peer].result if p.status == "proposed" else (p.checkpoint or p.base)
    view = self.root / "views" / sid / peer
    if view.exists():
        shutil.rmtree(view)
    self.store.materialize(tree, view)
    return view


def m_shared_view(self, sid, peer):
    """Logged, but the view is a live symlink into the peer's worktree."""
    if self.state.sessions[peer].status != "active":
        return orig_peek(self, sid, peer)
    p = self.state.sessions[peer]
    self._emit("session.observe", session=sid, peer=peer, tree=p.checkpoint or p.base)
    view = self.root / "views" / sid / peer
    view.parent.mkdir(parents=True, exist_ok=True)
    if view.is_symlink() or view.exists():
        view.unlink() if view.is_symlink() else shutil.rmtree(view)
    view.symlink_to(self.workdir(peer))
    return view


def m_wall_clock(self):
    self.l, self.c = self.physical(), 0
    return (self.l, self.c)


def m_diff3_prefer_ours(o, a, b, algo="myers"):
    merged, _ = orig_diff3(o, a, b, algo)
    return (merged if merged is not None else a), []


MUTANTS = {
    "protected paths not enforced": mock.patch.object(Orchestrator, "_touches_protected", lambda s, x: []),
    "evaluation skipped": mock.patch.object(sim, "evaluator", lambda files: (True, "skipped")),
    "drift detection off": mock.patch.object(Orchestrator, "check_drift", lambda s: None),
    "dependents never flagged": mock.patch.object(Orchestrator, "_flag_dependents", lambda s, d, r: None),
    "evaluates proposal, not merged head": mock.patch.object(Orchestrator, "step", m_eval_proposal_not_candidate),
    "peek not logged": mock.patch.object(Orchestrator, "peek", m_silent_peek),
    "peer view shares worktree": mock.patch.object(Orchestrator, "peek", m_shared_view),
    "wall clock instead of HLC": mock.patch.object(hlc.HLC, "now", m_wall_clock),
    "diff3 silently keeps ours": mock.patch.object(merge, "diff3", m_diff3_prefer_ours),
}


def m_leaky_eval(self, files):
    """Reports the hidden checks' output instead of withholding it."""
    ok, report = orig_eval_call(self, files)
    if "details withheld" in report:
        import tempfile, shutil, pathlib  # noqa: E401
        with tempfile.TemporaryDirectory() as d:
            for p, data in files.items():
                f = pathlib.Path(d) / p
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)
            shutil.copytree(self.holdout, pathlib.Path(d) / "_holdout")
            _, out = self._run(self.holdout_cmd, d)
        return False, out
    return ok, report


def m_choose_persistent(options, allow):
    for o in options:
        if allow and o.get("kind") == "allow_always":
            return o["optionId"], ""
    return orig_choose(options, allow)


async def m_answer_unknown_silently(self, rid, method, params):
    if method not in self.requests:
        return
    return await orig_answer(self, rid, method, params)


orig_eval_call = evaluators.CommandEvaluator.__call__
orig_choose = acp_client.choose_option
orig_answer = jsonrpc.Connection._answer

# The runner/ACP layer is not exercised by the simulation; these are judged
# by the end-to-end scenarios (real agent processes speaking ACP).
ADAPTER_MUTANTS = {
    "fence allows every write": mock.patch.object(Fence, "check_write", lambda s, paths: (True, "")),
    "persistent allow chosen": mock.patch.object(acp_client, "choose_option", m_choose_persistent),
    "inotify observation off": mock.patch.object(inotify, "available", False),
    "hidden check output leaked": mock.patch.object(evaluators.CommandEvaluator, "__call__", m_leaky_eval),
    "no history in the prompt": mock.patch.object(context, "history_lines", lambda o, sid: []),
    "no checkpoints during a turn": mock.patch.object(Runner, "_schedule_checkpoint", lambda s, sid: None),
    "orphans not recovered on restart": mock.patch.object(Orchestrator, "orphan_active", lambda s, why="": []),
    "finished goals not superseded": mock.patch.object(Runner, "_stop_superseded", lambda s: None),
    "unknown requests left unanswered": mock.patch.object(jsonrpc.Connection, "_answer", m_answer_unknown_silently),
    "build droppings captured": mock.patch.object(store_mod, "ignored", lambda rel, patterns=(): False),
}


def killed_by_sim(seeds=40, ticks=500):
    for seed in range(seeds):
        w = None
        try:
            w = sim.run(seed, ticks=ticks)
            sim.check(w)
        except Exception as e:  # noqa: BLE001 - any failure counts as a kill
            return f"sim seed {seed}: {type(e).__name__}: {str(e)[:60]}"
        finally:
            if w is not None:
                sim.cleanup(w)
    return None


def killed_by_unit_tests(name: str):
    """Run the test suite in a fresh interpreter with the mutant applied there.
    In-process runs are unsound: a test module imported while one mutant is
    active keeps the mutant bound (`from x import f`) for every later run."""
    code = ("import sys, unittest; sys.path.insert(0, '.'); import mutants; "
            f"p = {{**mutants.MUTANTS, **mutants.ADAPTER_MUTANTS}}[{name!r}]; p.start(); "
            "res = unittest.TextTestRunner(stream=open('/dev/null', 'w'), verbosity=0)"
            ".run(unittest.defaultTestLoader.discover('tests')); "
            "bad = res.failures or res.errors; "
            "print(bad[0][0].id().split('.')[-1] if bad else '')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=1800)
    first = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 and not first:
        return f"test run crashed: {r.stderr.strip().splitlines()[-1:]}"
    return f"unit test {first}" if first else None


if __name__ == "__main__":
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1)       # kill -USR1 <pid> dumps every thread's stack
    only = sys.argv[1] if len(sys.argv) > 1 else "all"
    plan = []
    if only in ("all", "core"):
        plan += [(n, p, True) for n, p in MUTANTS.items()]
    if only in ("all", "adapter"):
        plan += [(n, p, False) for n, p in ADAPTER_MUTANTS.items()]
    alive = 0
    for name, patch, use_sim in plan:
        t0 = time.time()
        with patch:
            verdict = (use_sim and killed_by_sim()) or killed_by_unit_tests(name)
        alive += verdict is None
        print(f"{'KILLED' if verdict else 'ALIVE '}  {name:37s} {verdict or ''}  ({time.time() - t0:.0f}s)",
              flush=True)
    print(f"{len(plan) - alive}/{len(plan)} mutants killed")
    sys.exit(1 if alive else 0)
