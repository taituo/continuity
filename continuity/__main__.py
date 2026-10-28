"""Command line.

  python3 -m continuity init    ROOT --from DIR
  python3 -m continuity goal    ROOT "text" [--parent G] [--budget N] [--durable] [--privileged]
  python3 -m continuity run     ROOT --check "CMD" [--agent "CMD"] [options]
  python3 -m continuity status  ROOT
  python3 -m continuity history ROOT GOAL
  python3 -m continuity audit   ROOT
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import sys
from pathlib import Path

from .audit import audit
from .context import PromptConfig
from .evaluators import CommandEvaluator
from .orchestrator import Orchestrator
from .runner import Runner, RunnerBusy, RunnerConfig
from .sandbox import Bwrap
from .store import DEFAULT_IGNORE, ignored

KIRO_V3 = "kiro-cli acp --agent-engine=v3 --auth-method=cli"


def _accept(files):
    return True, "accepted without checks"


def _orch(root, evaluator=_accept, protected=(), fsync=True, ignore=()) -> Orchestrator:
    return Orchestrator(Path(root).resolve(), evaluator, protected=tuple(protected), fsync=fsync,
                        ignore=DEFAULT_IGNORE + tuple(ignore))


def cmd_init(a) -> int:
    root = Path(a.root).resolve()
    if (root / ".continuity").exists():
        print(f"{root} is already initialised", file=sys.stderr)
        return 1
    o = _orch(root)
    g = o.open_goal(f"import {a.src}", privileged=True)
    s = o.start_session(g)
    src = Path(a.src).resolve()
    n = 0
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src).as_posix()
        if f.is_file() and not f.is_symlink() and not ignored(rel, o.ignore):
            dest = o.workdir(s) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
            n += 1
    o.propose(s, f"import of {src}")
    o.run_queue()
    print(f"initialised {root} with {n} files; head {o.state.head[:12]}")
    return 0


def cmd_goal(a) -> int:
    o = _orch(a.root)
    gid = o.open_goal(a.text, parent=a.parent, privileged=a.privileged, budget=a.budget,
                      durable=a.durable)
    print(gid)
    return 0


def cmd_run(a) -> int:
    holdout = Path(a.holdout).resolve() if a.holdout else None
    ev = CommandEvaluator(["sh", "-c", a.check], holdout=holdout, timeout=a.check_timeout,
                          holdout_cmd=["sh", "-c", a.holdout_check] if a.holdout_check else None)
    o = _orch(a.root, ev, a.protected, fsync=not a.no_fsync, ignore=a.ignore)
    meta = {k: v for k, v in (("modeId", a.mode), ("modelId", a.model), ("effortLevel", a.effort)) if v}
    sandbox = None
    if a.sandbox:
        sandbox = Bwrap(ro_hide=(str(holdout),) if holdout else ())
        if not sandbox.available():
            print("bwrap not found on PATH; install bubblewrap or drop --sandbox", file=sys.stderr)
            return 2
    cfg = RunnerConfig(agent_argv=shlex.split(a.agent), parallel=a.parallel, variants=a.variants,
                       default_budget=a.budget, turn_timeout=a.timeout, max_sessions=a.max_sessions,
                       sandbox=sandbox, deny_read=(str(holdout),) if holdout else (),
                       allow_network=not a.no_network, kiro_meta=meta or None,
                       prompt=PromptConfig(test_hint=a.test_hint or a.check))
    runner = Runner(o, cfg)

    async def main():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, runner.request_stop)     # graceful: cancel turns, then exit
        return await runner.run()
    try:
        summary = asyncio.run(main())
    except RunnerBusy as e:
        print(str(e), file=sys.stderr)
        return 3
    print(json.dumps({k: v for k, v in summary.items() if k != "outcomes"}, indent=2))
    return 1 if summary["fatal"] else 0


def cmd_status(a) -> int:
    o = _orch(a.root)
    st = o.state
    kids: dict = {}
    for g in st.goals.values():
        kids.setdefault(g.parent, []).append(g)

    def show(g, depth):
        mark = "durable" if g.durable else ("done" if g.done else "open")
        budget = f"{g.spent}/{g.budget}" if g.budget is not None else f"{g.spent}"
        print(f"{'  ' * depth}{g.id} [{mark}, attempts {budget}] {g.text}")
        for c in kids.get(g.id, []):
            show(c, depth + 1)
    for g in kids.get(None, []):
        show(g, 0)
    active = [s for s in st.sessions.values() if s.status == "active"]
    print(f"\nhead {st.head[:12]}  integrations {len(st.heads)}  queue {st.queue}  active {[s.id for s in active]}")
    return 0


def cmd_history(a) -> int:
    o = _orch(a.root)
    st = o.state
    for s in sorted((s for s in st.sessions.values() if s.goal == a.goal), key=lambda s: int(s.id[1:])):
        d = st.deltas.get(s.id)
        turn = [x for k, x in s.notes if k == "turn"]
        line = f"{s.id}: session {s.status}"
        if turn:
            line += f", stop {turn[-1].get('stop_reason')}"
        if d:
            line += f", delta {d.status}" + (f" ({d.reason})" if d.reason else "")
            line += f", flags {d.flags}" if d.flags else ""
            line += f"\n    intent: {d.intent}" if d.intent else ""
        print(line)
    return 0


def cmd_audit(a) -> int:
    print(json.dumps(audit(Path(a.root).resolve()), indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="continuity")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init")
    s.add_argument("root")
    s.add_argument("--from", dest="src", required=True)
    s = sub.add_parser("goal")
    s.add_argument("root")
    s.add_argument("text")
    s.add_argument("--parent")
    s.add_argument("--budget", type=int)
    s.add_argument("--durable", action="store_true")
    s.add_argument("--privileged", action="store_true")
    s = sub.add_parser("run")
    s.add_argument("root")
    s.add_argument("--agent", default=KIRO_V3)
    s.add_argument("--check", required=True, help="shell command; exit 0 = visible checks pass")
    s.add_argument("--check-timeout", type=int, default=600)
    s.add_argument("--holdout", help="directory copied to _holdout/ only inside the evaluator")
    s.add_argument("--holdout-check", help="shell command for hidden checks; its output is withheld")
    s.add_argument("--protected", nargs="*", default=[])
    s.add_argument("--ignore", nargs="*", default=[],
                   help="extra name patterns never captured into a delta (e.g. agent droppings)")
    s.add_argument("--parallel", type=int, default=3)
    s.add_argument("--variants", type=int, default=1)
    s.add_argument("--budget", type=int, default=3, help="attempts per goal without its own budget")
    s.add_argument("--timeout", type=float, default=1800)
    s.add_argument("--max-sessions", type=int)
    s.add_argument("--sandbox", action="store_true", help="wrap each agent in bubblewrap")
    s.add_argument("--no-network", action="store_true", help="refuse web fetch/search tools")
    s.add_argument("--mode"), s.add_argument("--model"), s.add_argument("--effort")
    s.add_argument("--test-hint")
    s.add_argument("--no-fsync", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("root")
    s = sub.add_parser("history")
    s.add_argument("root")
    s.add_argument("goal")
    s = sub.add_parser("audit")
    s.add_argument("root")
    a = p.parse_args(argv)
    logging.basicConfig(level=os.environ.get("CONTINUITY_LOG", "WARNING"))
    return {"init": cmd_init, "goal": cmd_goal, "run": cmd_run, "status": cmd_status,
            "history": cmd_history, "audit": cmd_audit}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
