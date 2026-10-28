"""Deterministic simulation: fake agents, no LLM, one seed = one schedule.

Fake agents edit real worktrees, peek at peers and copy their lines, crash,
cheat (edit the protected spec), write code that fails visible or holdout
checks, and scribble into read-only peer views. A "human" edits main/
directly. The physical clock sometimes jumps backwards and the orchestrator
is killed and rebuilt from disk at random points. Every line an agent writes
carries a unique token, so provenance of anything in head can be traced.

`run(seed)` returns the world; `check(world)` asserts all invariants.
"""
from __future__ import annotations

import random
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .audit import audit
from .log import Log, verify_chain
from .orchestrator import Orchestrator
from .state import fold

SPEC = "tests/spec.txt"
HOLDOUT_MAX_LINES = 40          # hidden rule: never in the tree, agents can't see it
TOKEN = re.compile(r"\b(s\d+|human\d+):\d+\b")


def evaluator(files: dict[str, bytes]) -> tuple[bool, str]:
    """Visible rules come from the (protected) spec in the tree; holdout is hidden."""
    forbid = [l.split(":", 1)[1].strip() for l in files.get(SPEC, b"").decode().splitlines()
              if l.startswith("forbid:")]
    for p, data in sorted(files.items()):
        if not p.startswith("src/"):
            continue
        text = data.decode()
        for w in forbid:
            if w in text:
                return False, f"visible: {p} contains {w}"
        if text.count("\n") > HOLDOUT_MAX_LINES:
            return False, f"holdout: {p} too long"
    return True, "ok"


REFERENCE_EVALUATOR = evaluator   # check() uses this binding, never the patched one


def initial_files(n_files=4, n_lines=12) -> dict[str, bytes]:
    files = {f"src/f{i}.txt": "".join(f"f{i} base {k}\n" for k in range(n_lines)).encode()
             for i in range(n_files)}
    files[SPEC] = b"forbid: BAD\n"
    return files


@dataclass
class Agent:
    sid: str
    goal: str
    model: dict                 # path -> list[str]: what the worktree must contain
    actions_left: int
    crashed: bool = False
    n: int = 0
    behaviour: str = "normal"   # normal | bad | long | cheat | grow


@dataclass
class World:
    seed: int
    root: Path
    orch: Orchestrator
    rng: random.Random
    t: int = 0
    agents: dict = field(default_factory=dict)
    human_tokens: list = field(default_factory=list)
    restarts: int = 0
    backward_jumps: int = 0
    scribbles: int = 0
    copies: int = 0


def _clock(world_ref):
    return lambda: world_ref[0].t


def _new_orch(root, world_ref):
    return Orchestrator(root, evaluator, protected=("tests/",), physical=_clock(world_ref),
                        lease_us=3000, fsync=False)


def _write(world: World, a: Agent):
    wd = world.orch.workdir(a.sid)
    for p, lines in a.model.items():
        f = wd / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("".join(lines))


def _propose(world: World, a: Agent, intent: str):
    """Propose, then assert isolation: the delta is exactly what this agent wrote."""
    o = world.orch
    o.propose(a.sid, intent)
    got = o.store.files(o.state.deltas[a.sid].result)
    want = {p: "".join(ls).encode() for p, ls in a.model.items()}
    assert got == want, f"isolation broken for {a.sid}"


def _act(world: World, a: Agent):
    r, o = world.rng, world.orch
    st = o.state.sessions[a.sid]
    if st.status != "active":          # lease expired: the agent was killed
        world.agents.pop(a.sid)
        return
    if a.crashed:
        return
    roll = r.random()
    srcs = sorted(p for p in a.model if p.startswith("src/"))
    if a.actions_left <= 0:
        _propose(world, a, f"{a.behaviour} work on {a.goal}")
        world.agents.pop(a.sid)
        return
    a.actions_left -= 1
    if roll < 0.04:
        a.crashed = True                # never proposes; reaped by lease
        return
    if roll < 0.20:
        peers = o.active_peers(a.sid)
        if peers:
            peer = r.choice(peers)
            view = o.peek(a.sid, peer)
            cands = []
            for f in sorted(view.rglob("*.txt")):
                for line in f.read_text().splitlines(keepends=True):
                    m = TOKEN.search(line)
                    if m and not m.group(1) == a.sid:
                        cands.append(line)
            if cands:
                p = r.choice(srcs)
                a.model[p].insert(r.randint(0, len(a.model[p])), r.choice(cands))
                world.copies += 1
            if r.random() < 0.3:        # try to scribble on the peer's view
                for f in sorted(view.rglob("*.txt"))[:1]:
                    try:
                        f.write_text("SCRIBBLE\n")   # root ignores 0444; must still be harmless
                        world.scribbles += 1
                    except PermissionError:
                        pass
            _write(world, a)
            return
    if roll < 0.30:
        o.checkpoint(a.sid)
        return
    p = r.choice(srcs)
    lines = a.model[p]
    a.n += 1
    tok = f"{a.sid}:{a.n}"
    if a.behaviour == "cheat" and r.random() < 0.5:
        a.model[SPEC] = ["forbid: NOTHING\n"]
        lines.insert(r.randint(0, len(lines)), f"BAD {tok}\n")
    elif a.behaviour == "bad" and r.random() < 0.5:
        lines.insert(r.randint(0, len(lines)), f"BAD {tok}\n")
    elif a.behaviour == "grow" and not a.n > 1:
        g = sorted(p for p in a.model if p.startswith("src/"))[0]   # growers crowd one file
        at = r.randint(0, len(a.model[g]))                          # anywhere, so merges stay clean
        a.model[g][at:at] = [f"grow {tok}.{k}\n" for k in range(9)]
    elif a.behaviour == "long" and r.random() < 0.5:
        for k in range(15):
            lines.append(f"pad {tok}.{k}\n")
    else:
        op = r.random()
        if op < 0.5 and lines:
            lines[r.randrange(len(lines))] = f"edit {tok}\n"
        elif op < 0.8:
            lines.insert(r.randint(0, len(lines)), f"add {tok}\n")
        elif lines:
            del lines[r.randrange(len(lines))]
    _write(world, a)


def _snapshot_state(st):
    return (st.head, list(st.heads), list(st.queue), sorted(st.evals.items()),
            sorted((k, v.status, v.reason, list(v.flags), v.result) for k, v in st.deltas.items()),
            sorted((k, v.status, v.checkpoint) for k, v in st.sessions.items()),
            sorted((k, v.spent) for k, v in st.goals.items()), st.counter)


def run(seed: int, ticks: int = 400, max_agents: int = 5, workdir: Path | None = None) -> World:
    rng = random.Random(seed)
    root = Path(workdir or tempfile.mkdtemp(prefix=f"continuity-sim-{seed}-"))
    ref = [None]
    holder = World(seed, root, None, rng)  # type: ignore[arg-type]
    ref[0] = holder
    holder.orch = _new_orch(root, ref)
    o = holder.orch
    # seed the repo through the front door: a privileged bootstrap goal
    boot = o.open_goal("bootstrap", privileged=True)
    sid = o.start_session(boot)
    for p, d in initial_files().items():
        f = o.workdir(sid) / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(d)
    o.propose(sid, "bootstrap")
    o.run_queue()
    root_goal = o.open_goal("keep the repo improving")
    goals = [o.open_goal(f"subgoal {k}", parent=root_goal) for k in range(3)]
    goals.append(o.open_goal("budgeted subgoal", parent=root_goal, budget=4))

    for _ in range(ticks):
        w = holder
        w.t += rng.randint(1, 20)
        if rng.random() < 0.02:
            w.t = max(0, w.t - rng.randint(50, 1500))
            w.backward_jumps += 1
        roll = rng.random()
        o = w.orch
        if roll < 0.15 and len(w.agents) < max_agents:
            open_goals = goals + [g for g, x in sorted(o.state.goals.items()) if x.resolves]
            g = rng.choice(open_goals)
            gx = o.state.goals[g]
            if gx.budget is not None and gx.spent >= gx.budget:
                continue
            sid = o.start_session(g)
            model = {p: d.decode().splitlines(keepends=True)
                     for p, d in o.store.files(o.state.head).items()}
            beh = rng.choices(["normal", "bad", "long", "cheat", "grow"], [70, 8, 6, 6, 10])[0]
            w.agents[sid] = Agent(sid, g, model, rng.randint(2, 10), behaviour=beh)
        elif roll < 0.70 and w.agents:
            _act(w, w.agents[rng.choice(sorted(w.agents))])
        elif roll < 0.85:
            o.step()
        elif roll < 0.89:
            f = o.main / f"src/f{rng.randrange(4)}.txt"
            lines = f.read_text().splitlines(keepends=True)
            tok = f"human{len(w.human_tokens)}:1"
            word = "BAD " if rng.random() < 0.2 else "human "
            lines.insert(rng.randint(0, len(lines)), f"{word}{tok}\n")
            f.write_text("".join(lines))
            w.human_tokens.append(tok)
        elif roll < 0.92:
            o.reap()
        elif roll < 0.95:
            before = _snapshot_state(o.state)
            w.orch = _new_orch(root, ref)          # kill -9 and restart from disk
            assert _snapshot_state(w.orch.state) == before, "restart changed state"
            w.restarts += 1

    # wind down: live agents finish, crashed ones get reaped, queue drains
    w, o = holder, holder.orch
    for sid in sorted(w.agents):
        a = w.agents[sid]
        if o.state.sessions[sid].status != "active":
            continue
        if a.crashed:
            o.lose(sid)
        else:
            _propose(w, a, f"{a.behaviour} work on {a.goal}")
    w.agents.clear()
    o.run_queue()
    for _ in range(3):   # dependents flagged late may need another pass
        o.run_queue()
    return w


# --------------------------------------------------------------------------
# invariants
# --------------------------------------------------------------------------
def check(w: World) -> dict:
    o, st, store = w.orch, w.orch.state, w.orch.store
    events = Log(w.root / ".continuity" / "log.jsonl", fsync=False).events

    # I1/I6: chain intact, HLC strictly monotone (verify_chain checks both)
    verify_chain(events)
    # I2: state is exactly fold(log from disk)
    assert _snapshot_state(fold(events, store.EMPTY)) == _snapshot_state(st), "replay mismatch"
    # I3: every integration re-derives from its recorded inputs
    rep = audit(w.root)
    # I4: green mainline, re-checked without trusting the logged verdicts
    for _, tree, _ in st.heads:
        ok, why = REFERENCE_EVALUATOR(store.files(tree))
        assert ok, f"red head {tree[:12]}: {why}"
    # I5: isolation. Each proposal equals exactly what its agent wrote.
    #     (checked live in run via models; here: no scribble ever reached a tree)
    for x in st.deltas.values():
        assert b"SCRIBBLE" not in b"".join(store.files(x.result).values()), f"{x.id} saw a scribble"
    # I7: protected spec never changed by a non-privileged delta
    spec0 = None
    for _, tree, did in st.heads:
        spec = store.files(tree).get(SPEC)
        if spec0 is None:
            spec0 = spec
        x = st.deltas[did]
        if x.origin == "agent" and not st.goals[x.goal].privileged:
            assert spec == spec0, f"{did} changed protected spec"
    # I8: every human edit was captured as an external delta
    ext_text = b"".join(b"".join(store.files(x.result).values())
                        for x in st.deltas.values() if x.origin == "external")
    for tok in w.human_tokens:
        assert tok.encode() in ext_text, f"human edit {tok} not captured"
    # I9: integrated deltas that looked at failed/lost work are flagged
    for x in st.deltas.values():
        if x.status != "integrated":
            continue
        for dep in x.observed:
            ds = st.sessions[dep].status
            dd = st.deltas.get(dep)
            bad = "dependency_lost" if ds == "lost" else (
                f"dependency_{dd.status}" if dd and dd.status in ("rejected", "conflict") else None)
            # continuing your own goal, or the delta you exist to resolve, is expected
            expected = (st.sessions[dep].goal == x.goal
                        or (x.goal is not None and st.goals[x.goal].resolves == dep))
            if bad and not expected:
                assert (bad, dep) in x.flags, f"{x.id} missing flag {bad} for {dep}"
    # I10: provenance. Every token in any head comes from an integrated delta,
    #      or from work some integrated delta was recorded as observing.
    closure = set()
    integrated = {x.id for x in st.deltas.values() if x.status == "integrated"}
    stack = list(integrated)
    while stack:
        sid = stack.pop()
        if sid in closure:
            continue
        closure.add(sid)
        if sid in st.sessions:
            stack += list(st.sessions[sid].observed)
    ext_ok = b"".join(b"".join(store.files(x.result).values()) for x in st.deltas.values()
                      if x.origin == "external" and x.status == "integrated")
    for _, tree, _ in st.heads:
        for data in store.files(tree).values():
            for m in TOKEN.finditer(data.decode()):
                who = m.group(1)
                if who.startswith("human"):
                    assert m.group(0).encode() in ext_ok, \
                        f"human token {m.group(0)} without integrated external delta"
                else:
                    assert who in closure, f"token {m.group(0)} has no recorded provenance"
    # I11: liveness. Nothing left hanging.
    assert not st.queue, "queue not drained"
    assert all(s.status != "active" for s in st.sessions.values()), "active sessions left"
    assert all(x.status != "queued" for x in st.deltas.values())
    # budgets respected
    for g in st.goals.values():
        assert g.budget is None or g.spent <= g.budget
    assert (o.main.exists() and store.snapshot(o.main) == st.head), "main/ does not mirror head"

    statuses = {}
    for x in st.deltas.values():
        key = x.status if x.status != "rejected" else "rejected:" + x.reason.split(":")[0]
        statuses[key] = statuses.get(key, 0) + 1
    return {"seed": w.seed, "events": len(events), "heads": len(st.heads),
            "integrations_replayed": rep["integrations_replayed"], "outcomes": statuses,
            "flags": sum(len(x.flags) for x in st.deltas.values()),
            "observations": sum(len(x.observed) for x in st.deltas.values()),
            "copies": w.copies, "scribbles": w.scribbles, "restarts": w.restarts,
            "backward_jumps": w.backward_jumps, "human_edits": len(w.human_tokens)}


def cleanup(w: World):
    for f in w.root.rglob("*"):
        if f.is_dir():
            f.chmod(0o755)
    shutil.rmtree(w.root, ignore_errors=True)
