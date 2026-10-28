"""Build a session's prompt from the log.

A disposable agent knows nothing except what this prompt tells it, so the
log has to carry continuity: the goal and its ancestry, the rules it will be
judged by, what earlier attempts did and why they failed, and, for a
resolution goal, exactly what conflicted. Hidden-check output is never
included (the evaluator already withholds it).
"""
from __future__ import annotations

from dataclasses import dataclass

from .merge import changed_paths

MAX_HISTORY = 5
REPORT_TAIL = 800


@dataclass
class PromptConfig:
    test_hint: str = ""          # e.g. "python3 -m unittest -q"
    extra_rules: tuple = ()


def _log_index(o):
    evals, conflicts = {}, {}
    for ev in o.log.events:
        d = ev["data"]
        if ev["kind"] == "eval":
            evals[d["delta"]] = d
        elif ev["kind"] == "conflict":
            conflicts[d["delta"]] = d
    return evals, conflicts


def _stop_reason(session) -> str:
    for kind, data in reversed(session.notes):
        if kind == "turn":
            return data.get("stop_reason", "?")
        if kind == "orphaned":
            return "orphaned by a runner restart"
    return "lease expired"


def history_lines(o, sid: str) -> list[str]:
    st = o.state
    s = st.sessions[sid]
    evals, conflicts = _log_index(o)
    prev = [x for x, ss in st.sessions.items() if ss.goal == s.goal and x != sid
            and ss.status != "active"]
    prev.sort(key=lambda x: st.sessions[x].started_l, reverse=True)
    out = []
    for x in prev[:MAX_HISTORY]:
        ss = st.sessions[x]
        if ss.status == "lost":
            tree = ss.checkpoint or ss.base
            files = sorted(changed_paths(o.store, ss.base, tree))
            out.append(f"- {x}: did not finish ({_stop_reason(ss)})"
                       + (f"; it had changed {', '.join(files)}" if files else "; it changed nothing"))
            continue
        d = st.deltas[x]
        head = f"- {x}: {d.status}" + (" (partial: part of the goal was left)" if d.partial else "")
        if d.status == "rejected":
            head += f" ({d.reason})"
        elif d.status == "conflict":
            head += f" with concurrent work in {', '.join(d.reason)}"
        out.append(head)
        if d.intent:
            out.append(f"  its intent: {d.intent}")
        if d.status == "rejected" and x in evals:
            rep = evals[x]["report"].strip()
            if rep:
                rep = rep[-REPORT_TAIL:]
                out.append("  evaluator said:\n" + "\n".join("    " + ln for ln in rep.splitlines()))
    return out


def resolution_lines(o, sid: str) -> list[str]:
    st = o.state
    g = st.goals[st.sessions[sid].goal]
    if not g.resolves:
        return []
    x = st.deltas[g.resolves]
    _, conflicts = _log_index(o)
    c = conflicts.get(x.id, {})
    origin = st.goals[x.goal].text if x.goal else "an external edit"
    paths = set(c.get("paths", []))
    out = [f"This goal resolves a conflict. Change {x.id} (for: {origin}) could not be merged:",
           f"- conflicting files: {', '.join(sorted(paths)) or '?'}"]
    if x.intent:
        out.append(f"- its intent: {x.intent}")
    # which integrated changes it collided with: those landed between its base
    # and the head it was merged against, touching the same files
    trees = [t for _, t, _ in st.heads]
    lo = trees.index(x.base) + 1 if x.base in trees else 0
    hi = trees.index(c["against"]) + 1 if c.get("against") in trees else len(trees)
    for _, _, did in st.heads[lo:hi]:
        y = st.deltas[did]
        if paths & changed_paths(o.store, y.base, y.result):
            what = st.goals[y.goal].text if y.goal else "an external edit"
            out.append(f"- it collided with {y.id} (for: {what})"
                       + (f", whose intent was: {y.intent}" if y.intent else ""))
    out.append(f"Re-apply what {x.id} meant to do on top of your workspace, which already contains "
               "the concurrent change it collided with. Keep both intents.")
    return out


def build_prompt(o, sid: str, views, cfg: PromptConfig) -> str:
    st = o.state
    s = st.sessions[sid]
    g = st.goals[s.goal]
    ancestry, p = [], g.parent
    while p:
        ancestry.append(st.goals[p])
        p = st.goals[p].parent
    L = ["You are one disposable worker in an autonomous development system. Your session",
         "ends when this turn ends; only the files you leave in your workspace count.", "",
         f"Goal {g.id}: {g.text}"]
    L += [f"  (part of {a.id}: {a.text})" for a in ancestry]
    L += ["", f"Workspace (your private copy of the current integrated state): {o.workdir(sid)}", "",
          "Rules:",
          "- Change files only inside the workspace. Anything else is refused."]
    if o.protected and not g.privileged:
        L.append(f"- Protected paths ({', '.join(o.protected)}) cannot be changed by you; "
                 "a change touching them is rejected.")
    L.append("- Your change is merged with other sessions' work and the merged result is "
             "evaluated. Run the checks yourself before you finish."
             + (f" Visible checks: `{cfg.test_hint}`" if cfg.test_hint else ""))
    L.append("- Hidden checks also run. Their details are never shown.")
    L.append("- A small, correct, useful part of the goal beats a large broken change. If part "
             "of the goal remains, end with a line `REMAINING: <what is left>`; the goal then "
             "stays open and the next session continues from your work.")
    L.append(f"- Other sessions' unfinished work is readable (not writable) under {views.dir}; "
             "start from PEERS.md there. Reading it is recorded.")
    L += [f"- {r}" for r in cfg.extra_rules]
    L.append("- Finish with 1-3 sentences: what you changed and why. That text is stored as your intent.")
    hist = history_lines(o, sid)
    if hist:
        L += ["", "Earlier attempts at this goal (newest first):"] + hist
    res = resolution_lines(o, sid)
    if res:
        L += [""] + res
    if views.context_lines:
        L += ["", "Read-only material for this goal:"] + views.context_lines
    return "\n".join(L) + "\n"
