"""Do the invariants have teeth? Inject known bugs and check that the
simulation or the unit/property tests catch every one of them."""
import shutil
import sys
import unittest
from unittest import mock

from continuity import hlc, merge, sim
from continuity.orchestrator import Orchestrator

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


def killed_by_unit_tests():
    suite = unittest.defaultTestLoader.discover("tests")
    with open("/dev/null", "w") as null:
        res = unittest.TextTestRunner(stream=null, verbosity=0).run(suite)
    bad = res.failures or res.errors
    return f"unit test {bad[0][0].id().split('.')[-1]}" if bad else None


if __name__ == "__main__":
    alive = 0
    for name, patch in MUTANTS.items():
        with patch:
            verdict = killed_by_sim() or killed_by_unit_tests()
        alive += verdict is None
        print(f"{'KILLED' if verdict else 'ALIVE '}  {name:37s} {verdict or ''}", flush=True)
    print(f"{len(MUTANTS) - alive}/{len(MUTANTS)} mutants killed")
    sys.exit(1 if alive else 0)
