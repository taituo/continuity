"""Orchestrator: the only writer of the log.

Layout under root:
  .continuity/objects/   content-addressed blobs and trees
  .continuity/log.jsonl  hash-chained event log (truth)
  main/               materialised head; humans and other systems may edit it
  work/<session>/     a session's private, writable worktree
  views/<s>/<peer>/   read-only snapshot copies of peers' work, per observer

Agents write only inside their worktree. They see other sessions' unfinished
work only through `peek`, which copies a checkpoint snapshot and logs the
observation, so soft dependencies are recorded by the system, not by agents.
Only `step` (the integrator) ever moves head.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Protocol

from .hlc import HLC
from .log import Log
from .merge import changed_paths, merge_trees
from .state import State, fold
from .store import DEFAULT_IGNORE, Store, ignored


class Evaluator(Protocol):
    def __call__(self, files: dict[str, bytes]) -> tuple[bool, str]: ...


def sync_dir(store: Store, tree: str, dest: Path, ignore=DEFAULT_IGNORE) -> None:
    """Make dest contain exactly `tree` (only touches what differs; leaves
    ignored droppings such as __pycache__ alone)."""
    dest.mkdir(parents=True, exist_ok=True)
    want = store.tree(tree)
    for f in sorted(dest.rglob("*"), reverse=True):
        rel = f.relative_to(dest).as_posix()
        if ignored(rel, ignore):
            continue
        if (f.is_file() or f.is_symlink()) and rel not in want:
            f.unlink()
        elif f.is_dir() and not f.is_symlink() and not any(f.iterdir()):
            f.rmdir()
    for p, b in want.items():
        f = dest / p
        data = store.get(b)
        if not f.is_file() or f.read_bytes() != data:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)


class Orchestrator:
    def __init__(self, root: Path, evaluator: Evaluator, protected: tuple[str, ...] = (),
                 physical: Callable[[], int] | None = None, lease_us: int = 3_600_000_000,
                 fsync: bool = True, merge_algo: str = "myers", ignore=DEFAULT_IGNORE,
                 supersede_done: bool = False):
        """supersede_done: reject a delta whose goal is already done (another
        attempt or variant got there first) instead of merging it as well."""
        self.root = Path(root)
        self.supersede_done = supersede_done
        self.ignore = tuple(ignore)
        self.store = Store(self.root / ".continuity")
        self.log = Log(self.root / ".continuity" / "log.jsonl", fsync=fsync)
        self.state: State = fold(self.log.events, self.store.EMPTY)
        last = tuple(self.log.events[-1]["hlc"]) if self.log.events else (0, 0)
        self.clock = HLC(physical, state=last)
        self.evaluator = evaluator
        self.protected = tuple(protected)
        self.lease_us = lease_us
        self.merge_algo = merge_algo
        self.main = self.root / "main"
        if not self.main.exists():
            sync_dir(self.store, self.state.head, self.main, self.ignore)

    def snapshot(self, path: Path) -> str:
        return self.store.snapshot(path, self.ignore)

    # ------------------------------------------------------------------
    def _emit(self, kind: str, **data) -> dict:
        # Validate by applying first: an invalid transition raises before
        # anything reaches disk, so the log only ever holds legal history.
        # (Every State handler checks before it mutates.)
        with self.log.locked():
            self._catch_up()
            ev = self.log.prepare(kind, data, self.clock.now())
            self.state.apply(ev)
            return self.log.commit(ev)

    def _catch_up(self) -> list[dict]:
        new = self.log.sync()
        for ev in new:
            self.state.apply(ev)
        if new:
            self.clock.recv(tuple(new[-1]["hlc"]))
        return new

    def refresh(self) -> list[dict]:
        """Apply events other processes appended (e.g. `continuity goal` from a
        shell while a runner runs)."""
        with self.log.locked():
            return self._catch_up()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}{self.state.counter}"

    # -- goals ---------------------------------------------------------
    def open_goal(self, text: str, parent: str | None = None, privileged: bool = False,
                  budget: int | None = None, resolves: str | None = None,
                  durable: bool = False) -> str:
        """durable: a long-lived container goal (maintain a service, keep
        improving X). It is never 'done' and is not worked on directly; its
        children are."""
        gid = self._new_id("g")
        data = dict(goal=gid, parent=parent, text=text, privileged=privileged,
                    budget=budget, resolves=resolves)
        if durable:
            data["durable"] = True
        self._emit("goal.open", **data)
        return gid

    # -- drift ---------------------------------------------------------
    def check_drift(self) -> str | None:
        """Anything in main/ that head does not explain is an external delta."""
        snap = self.snapshot(self.main)
        if snap == self.state.head:
            return None
        did = self._new_id("x")
        self._emit("external", delta=did, base=self.state.head, result=snap)
        sync_dir(self.store, self.state.head, self.main, self.ignore)  # main/ mirrors head again
        return did

    # -- sessions ------------------------------------------------------
    def start_session(self, goal: str) -> str:
        self.check_drift()
        sid = self._new_id("s")
        self._emit("session.start", session=sid, goal=goal, base=self.state.head)
        self.store.materialize(self.state.head, self.workdir(sid))
        return sid

    def workdir(self, sid: str) -> Path:
        return self.root / "work" / sid

    def checkpoint(self, sid: str) -> str:
        s = self.state.sessions[sid]
        tree = self.snapshot(self.workdir(sid))
        if tree != (s.checkpoint or s.base):
            self._emit("session.checkpoint", session=sid, tree=tree)
        return tree

    def visible_tree(self, peer: str) -> str:
        """What other sessions get to see of `peer`: its latest checkpoint,
        or its proposal once proposed."""
        p = self.state.sessions[peer]
        if p.status == "proposed":
            return self.state.deltas[peer].result
        return p.checkpoint or p.base

    def observe(self, sid: str, peer: str, tree: str, via: str = "peek") -> bool:
        """Record that `sid` looked at `peer`'s work as of `tree`. Idempotent."""
        s = self.state.sessions.get(sid)
        if s is None or s.status != "active" or peer == sid or peer not in self.state.sessions:
            return False
        if tree in s.observed.get(peer, []):
            return False
        self._emit("session.observe", session=sid, peer=peer, tree=tree, via=via)
        return True

    def note(self, sid: str, kind: str, data: dict) -> None:
        """Adapter-level facts about a session (stop reason, denials, usage)."""
        self._emit("session.note", session=sid, what=kind, data=data)

    def orphan_active(self, why: str = "orphaned") -> list[str]:
        """Agent processes do not survive their runner. After a restart every
        session still active in the log is dead: record that, keep its last
        checkpoint in the log for the next attempt."""
        dead = sorted(x for x, s in self.state.sessions.items() if s.status == "active")
        for x in dead:
            if self.workdir(x).exists():
                self.checkpoint(x)      # keep whatever it wrote after its last checkpoint
            self.note(x, "orphaned", {"why": why})
            self.lose(x)
        return dead

    def peek(self, sid: str, peer: str) -> Path:
        """Read-only copy of a peer's latest checkpoint; the look is logged."""
        tree = self.visible_tree(peer)
        self._emit("session.observe", session=sid, peer=peer, tree=tree)
        view = self.root / "views" / sid / peer
        if view.exists():
            shutil.rmtree(view)
        self.store.materialize(tree, view)
        for f in view.rglob("*"):
            f.chmod(0o555 if f.is_dir() else 0o444)
        return view

    def active_peers(self, sid: str) -> list[str]:
        return [x for x, s in sorted(self.state.sessions.items())
                if x != sid and s.status in ("active", "proposed")
                and (s.checkpoint or s.status == "proposed")]

    def propose(self, sid: str, intent: str = "", partial: bool = False) -> str:
        """partial: the delta is a useful piece; once integrated the goal stays
        open and the next session continues from it."""
        result = self.snapshot(self.workdir(sid))
        data = dict(session=sid, result=result, intent=intent)
        if partial:
            data["partial"] = True
        self._emit("session.propose", **data)
        self._dispose(sid)
        return sid

    def lose(self, sid: str) -> None:
        self._emit("session.lost", session=sid)
        self._dispose(sid)
        self._flag_dependents(sid, "dependency_lost")

    def reap(self) -> list[str]:
        now = self.clock.physical()
        dead = [x for x, s in sorted(self.state.sessions.items())
                if s.status == "active" and now - s.started_l > self.lease_us]
        for x in dead:
            self.lose(x)
        return dead

    def _dispose(self, sid: str) -> None:
        for d in (self.workdir(sid), self.root / "views" / sid):
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_dir():
                        f.chmod(0o755)
                shutil.rmtree(d)

    def annotate(self, delta: str, by: str, intent: str) -> None:
        self._emit("annotate", delta=delta, by=by, intent=intent)

    # -- integration ---------------------------------------------------
    def expected_dependency(self, x, dep: str) -> bool:
        """Building on an earlier attempt at the same goal, or on the delta a
        resolution goal exists to resolve, is the designed path, not a risk."""
        st = self.state
        if x.goal is None or dep not in st.sessions:
            return False
        if st.sessions[dep].goal == x.goal:
            return True
        return st.goals[x.goal].resolves == dep

    def _flag_dependents(self, dep: str, reason: str) -> None:
        for x in sorted(self.state.deltas.values(), key=lambda d: d.id):
            if (dep in x.observed and x.status == "integrated" and (reason, dep) not in x.flags
                    and not self.expected_dependency(x, dep)):
                self._emit("flag", delta=x.id, reason=reason, dep=dep)

    def _blocked(self, x) -> list[str]:
        st = self.state
        return [p for p in x.observed if p in st.deltas and st.deltas[p].status == "queued"]

    def _pick(self):
        q = self.state.queue
        if not q:
            return None
        for did in q:
            if not self._blocked(self.state.deltas[did]):
                return self.state.deltas[did]
        return self.state.deltas[q[0]]  # dependency cycle: oldest first

    def _touches_protected(self, x) -> list[str]:
        if x.origin == "external" or (x.goal and self.state.goals[x.goal].privileged):
            return []
        return sorted(p for p in changed_paths(self.store, x.base, x.result)
                      if p.startswith(self.protected))

    def step(self) -> dict | None:
        """Decide one queued delta. Returns a small report, or None if idle."""
        self.check_drift()
        x = self._pick()
        if x is None:
            return None
        st = self.state
        for dep in x.observed:
            ds = st.sessions[dep].status
            dd = st.deltas.get(dep)
            reason = None
            if ds == "active":
                reason = "dependency_unfinished"
            elif ds == "lost":
                reason = "dependency_lost"
            elif dd and dd.status in ("rejected", "conflict"):
                reason = f"dependency_{dd.status}"
            if reason and (reason, dep) not in x.flags and not self.expected_dependency(x, dep):
                self._emit("flag", delta=x.id, reason=reason, dep=dep)

        if (self.supersede_done and x.goal is not None and st.goals[x.goal].done
                and not st.goals[x.goal].durable):
            return self._decide_reject(x, "superseded")
        if x.result == x.base:
            return self._decide_reject(x, "empty")
        bad = self._touches_protected(x)
        if bad:
            return self._decide_reject(x, "protected:" + ",".join(bad))

        head = st.head
        cand, conflicts = merge_trees(self.store, x.base, head, x.result, self.merge_algo)
        if cand is None:
            paths = sorted({c["path"] for c in conflicts})
            self._emit("conflict", delta=x.id, against=head, paths=paths, detail=conflicts)
            parent = x.goal if x.goal else None
            self.open_goal(f"resolve conflict of {x.id} in {', '.join(paths)}", parent=parent,
                           resolves=x.id)
            self._flag_dependents(x.id, "dependency_conflict")
            return {"delta": x.id, "outcome": "conflict", "paths": paths}

        if cand not in st.evals:
            ok, report = self.evaluator(self.store.files(cand))
            self._emit("eval", delta=x.id, against=head, candidate=cand, ok=ok, report=report[-2000:])
        ok, report = st.evals[cand]
        if not ok:
            return self._decide_reject(x, "eval_failed")

        # Compare-and-swap: only integrate onto the exact head we evaluated.
        if self.check_drift() is not None or st.head != head:
            return {"delta": x.id, "outcome": "retry"}
        self._emit("integrate", delta=x.id, **{"from": head, "to": cand})
        sync_dir(self.store, cand, self.main, self.ignore)
        return {"delta": x.id, "outcome": "integrated"}

    def _decide_reject(self, x, reason):
        self._emit("reject", delta=x.id, reason=reason)
        self._flag_dependents(x.id, "dependency_rejected")
        return {"delta": x.id, "outcome": "rejected", "reason": reason}

    def run_queue(self, max_steps: int = 10_000) -> list[dict]:
        out = []
        for _ in range(max_steps):
            r = self.step()
            if r is None:
                break
            out.append(r)
        return out
