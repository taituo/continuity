"""State = fold(log). Pure, deterministic, and strict.

The orchestrator applies events through the same `apply` it would use when
replaying from disk, so there is no hidden state: a restarted orchestrator,
an auditor, or a second machine all get the identical State from the log.
Invalid transitions raise, so a forged or reordered log is rejected even if
its hash chain was recomputed.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class FoldError(Exception):
    pass


@dataclass
class Goal:
    id: str
    text: str
    parent: str | None
    privileged: bool = False
    budget: int | None = None
    spent: int = 0
    resolves: str | None = None


@dataclass
class Session:
    id: str
    goal: str
    base: str
    status: str = "active"            # active | proposed | lost
    checkpoint: str | None = None
    observed: dict = field(default_factory=dict)  # peer session -> [trees seen]
    started_l: int = 0


@dataclass
class Delta:
    id: str
    origin: str                       # agent | external
    goal: str | None
    base: str
    result: str
    intent: str = ""
    observed: list = field(default_factory=list)
    status: str = "queued"            # queued | integrated | rejected | conflict
    reason: str = ""
    flags: list = field(default_factory=list)  # [(reason, dep_session)]
    annotations: dict = field(default_factory=dict)  # by -> intent


@dataclass
class State:
    empty: str
    head: str = ""
    heads: list = field(default_factory=list)     # [(event_seq, tree, delta)]
    goals: dict = field(default_factory=dict)
    sessions: dict = field(default_factory=dict)
    deltas: dict = field(default_factory=dict)
    queue: list = field(default_factory=list)
    evals: dict = field(default_factory=dict)     # candidate tree -> (ok, report)
    counter: int = 0

    def __post_init__(self):
        if not self.head:
            self.head = self.empty

    # ------------------------------------------------------------------
    def apply(self, ev: dict) -> None:
        k, d = ev["kind"], ev["data"]
        fn = getattr(self, "_" + k.replace(".", "_"), None)
        if fn is None:
            raise FoldError(f"unknown event kind {k}")
        fn(d, ev)
        self.counter = ev["seq"] + 1

    def _need(self, cond, msg):
        if not cond:
            raise FoldError(msg)

    def _goal_open(self, d, ev):
        self._need(d["goal"] not in self.goals, "duplicate goal")
        self._need(d["parent"] is None or d["parent"] in self.goals, "unknown parent goal")
        self.goals[d["goal"]] = Goal(d["goal"], d["text"], d["parent"], d.get("privileged", False),
                                     d.get("budget"), 0, d.get("resolves"))

    def _session_start(self, d, ev):
        self._need(d["session"] not in self.sessions, "duplicate session")
        g = self.goals.get(d["goal"])
        self._need(g is not None, "unknown goal")
        self._need(d["base"] == self.head, "session must start from current head")
        self._need(g.budget is None or g.spent < g.budget, "goal budget exhausted")
        g.spent += 1
        self.sessions[d["session"]] = Session(d["session"], d["goal"], d["base"], started_l=ev["hlc"][0])

    def _active(self, sid):
        s = self.sessions.get(sid)
        self._need(s is not None and s.status == "active", f"session {sid} not active")
        return s

    def _session_checkpoint(self, d, ev):
        self._active(d["session"]).checkpoint = d["tree"]

    def _session_observe(self, d, ev):
        s = self._active(d["session"])
        self._need(d["peer"] in self.sessions and d["peer"] != d["session"], "bad peer")
        s.observed.setdefault(d["peer"], []).append(d["tree"])

    def _session_propose(self, d, ev):
        s = self._active(d["session"])
        s.status = "proposed"
        self.deltas[s.id] = Delta(s.id, "agent", s.goal, s.base, d["result"], d.get("intent", ""),
                                  sorted(s.observed))
        self.queue.append(s.id)

    def _session_lost(self, d, ev):
        self._active(d["session"]).status = "lost"

    def _external(self, d, ev):
        self._need(d["delta"] not in self.deltas, "duplicate delta")
        self._need(d["base"] == self.head, "external delta must be measured against head")
        self.deltas[d["delta"]] = Delta(d["delta"], "external", None, d["base"], d["result"])
        self.queue.insert(0, d["delta"])

    def _queued(self, did):
        x = self.deltas.get(did)
        self._need(x is not None and x.status == "queued", f"delta {did} not queued")
        return x

    def _eval(self, d, ev):
        self._queued(d["delta"])
        self.evals[d["candidate"]] = (d["ok"], d["report"])

    def _integrate(self, d, ev):
        x = self._queued(d["delta"])
        self._need(d["from"] == self.head, "integrate must build on current head")
        ok = self.evals.get(d["to"])
        self._need(ok is not None and ok[0], "integrated tree has no passing evaluation")
        x.status = "integrated"
        self.queue.remove(x.id)
        self.head = d["to"]
        self.heads.append((ev["seq"], d["to"], x.id))

    def _reject(self, d, ev):
        x = self._queued(d["delta"])
        x.status, x.reason = "rejected", d["reason"]
        self.queue.remove(x.id)

    def _conflict(self, d, ev):
        x = self._queued(d["delta"])
        self._need(d["against"] == self.head, "conflict must be against current head")
        x.status, x.reason = "conflict", d["paths"]
        self.queue.remove(x.id)

    def _flag(self, d, ev):
        x = self.deltas.get(d["delta"])
        self._need(x is not None, "flag on unknown delta")
        x.flags.append((d["reason"], d["dep"]))

    def _annotate(self, d, ev):
        x = self.deltas.get(d["delta"])
        self._need(x is not None, "annotation on unknown delta")
        x.annotations[d["by"]] = d["intent"]


def fold(events: list[dict], empty_tree: str) -> State:
    st = State(empty=empty_tree)
    for ev in events:
        st.apply(ev)
    return st
