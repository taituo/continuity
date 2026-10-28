"""Per-session read-only views of other work, and recording what gets read.

Layout under views/<sid>/ (outside the worktree, so never part of a delta):

  PEERS.md                       index: who is doing what, where to look
  peers/<peer>/<tree12>/...      immutable snapshot of a peer's checkpoint
  attempts/<sid>/<tree12>/...    earlier attempts at the same goal
  conflict/<delta>/<tree12>/...  the conflicted delta a resolution goal must re-apply
  conflict/<delta>.diff          what that delta changed relative to its base

Each snapshot holds only the files that work changed relative to its own
base (and DELETED.txt for removals): the rest is the base, which the agent
already has. Cost is proportional to the change, not to the repository.

Snapshots are immutable and named by tree hash, so every recorded
observation names the exact tree that was read. A new checkpoint of a peer
appears as a new directory; old ones stay. A directory is materialised under
a temporary name, watched, then renamed into place, so no open can slip in
before the watch exists.
"""
from __future__ import annotations

import difflib
import os
import shutil
from pathlib import Path
from typing import Callable

from . import inotify
from .merge import changed_paths

MAX_ATTEMPTS_SHOWN = 3


class SessionViews:
    def __init__(self, orch, sid: str, on_observe: Callable[[str, str, str], None]):
        self.o, self.sid = orch, sid
        self.dir = orch.root / "views" / sid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.on_observe = on_observe
        self.index: dict[str, tuple[str, str]] = {}   # real dir -> (session, tree)
        self.published: set[tuple[str, str, str]] = set()
        self.context_lines: list[str] = []
        self.watcher = None
        if inotify.available:
            try:
                self.watcher = inotify.Inotify(self._opened)
            except OSError:
                self.watcher = None

    # -- materialisation -------------------------------------------------
    def _publish(self, area: str, owner: str, name: str, tree: str, base: str) -> Path:
        """Materialise only what `tree` changed relative to `base` (everything
        else is identical to the base, which the agent already has). Keeps the
        cost per snapshot proportional to the change, not to the repo."""
        key = (area, name, tree)
        dest = self.dir / area / name / tree[:12]
        if key in self.published:
            return dest
        tmp = self.dir / area / name / f".tmp-{tree[:12]}"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        files = self.o.store.tree(tree)
        changed = sorted(changed_paths(self.o.store, base, tree))
        for p in changed:
            if p in files:
                f = tmp / p
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(self.o.store.get(files[p]))
                f.chmod(0o444)
        deleted = [p for p in changed if p not in files]
        if deleted:
            (tmp / "DELETED.txt").write_text("".join(p + "\n" for p in deleted))
        if self.watcher:
            self.watcher.watch_tree(str(tmp))
        os.rename(tmp, dest)
        if self.watcher:
            self.watcher.moved(str(tmp), str(dest))
        self.index[os.path.realpath(dest)] = (owner, tree)
        self.published.add(key)
        return dest

    def peers(self) -> list[str]:
        st = self.o.state
        out = []
        for x, s in sorted(st.sessions.items()):
            if x == self.sid:
                continue
            if s.status == "active" and s.checkpoint and s.checkpoint != s.base:
                out.append(x)
            elif s.status == "proposed" and st.deltas[x].status == "queued":
                out.append(x)
        return out

    def setup_context(self) -> list[str]:
        """Earlier attempts at this goal and, for a resolution goal, the
        conflicted delta. Returns prompt lines describing them."""
        st, o = self.o.state, self.o
        s = st.sessions[self.sid]
        g = st.goals[s.goal]
        lines = []
        prev = [x for x, ss in st.sessions.items() if ss.goal == s.goal and x != self.sid
                and (ss.status == "lost" or (ss.status == "proposed"
                                             and st.deltas[x].status in ("rejected", "conflict")))]
        prev.sort(key=lambda x: st.sessions[x].started_l, reverse=True)
        for x in prev[:MAX_ATTEMPTS_SHOWN]:
            ss = st.sessions[x]
            tree = st.deltas[x].result if x in st.deltas else (ss.checkpoint or ss.base)
            if tree == ss.base:
                continue
            d = self._publish("attempts", x, x, tree, ss.base)
            lines.append(f"- earlier attempt {x}: the files it had changed are in {d}")
        if g.resolves:
            x = st.deltas[g.resolves]
            d = self._publish("conflict", x.id, x.id, x.result, x.base)
            diff = self.dir / "conflict" / f"{x.id}.diff"
            diff.write_text(unified_diff(o.store, x.base, x.result))
            diff.chmod(0o444)
            lines.append(f"- the conflicted change {x.id} as it was proposed: {d}")
            lines.append(f"- what {x.id} changed relative to its own base: {diff}")
        self.context_lines = lines
        return lines

    def refresh(self) -> list[str]:
        """Publish every peer's current visible tree and rewrite PEERS.md."""
        st = self.o.state
        rows = []
        for p in self.peers():
            tree = self.o.visible_tree(p)
            ps = st.sessions[p]
            d = self._publish("peers", p, p, tree, ps.base)
            g = st.goals[ps.goal]
            status = "proposed, waiting for integration" if ps.status == "proposed" else "in progress"
            changed = sorted(changed_paths(self.o.store, ps.base, tree))
            intent = st.deltas[p].intent if p in st.deltas else ""
            rows.append(f"## {p}: {g.text}\n- status: {status}\n- latest snapshot: {d}\n"
                        f"- files changed: {', '.join(changed) or '(none)'}\n"
                        + (f"- stated intent: {intent}\n" if intent else ""))
        body = ("# Work in progress by other sessions (read-only)\n\n"
                "Each snapshot holds only the files that session changed (plus DELETED.txt\n"
                "if it removed any). Snapshots are immutable; a newer one appears as a new\n"
                "directory.\n"
                "Reading them is recorded. If you build on work that is later rejected,\n"
                "your change is flagged for review.\n\n"
                + ("\n".join(rows) if rows else "No other session has anything to show right now.\n"))
        if self.context_lines:
            body += "\n# Context for this goal\n\n" + "\n".join(self.context_lines) + "\n"
        tmp = self.dir / ".PEERS.md.tmp"
        tmp.write_text(body)
        os.replace(tmp, self.dir / "PEERS.md")
        return self.peers()

    # -- observation -------------------------------------------------------
    def owner_of(self, path: str) -> tuple[str, str] | None:
        real = os.path.realpath(path)
        for d, who in self.index.items():
            if real == d or real.startswith(d + os.sep):
                return who
        return None

    def _opened(self, path: str) -> None:
        self.touch(path, "inotify")

    def touch(self, path: str, via: str) -> None:
        who = self.owner_of(path)
        if who:
            self.on_observe(who[0], who[1], via)

    def close(self) -> None:
        if self.watcher:
            self.watcher.drain()
            self.watcher.close()
            self.watcher = None


def unified_diff(store, base: str, result: str) -> str:
    out = []
    a, b = store.tree(base), store.tree(result)
    for p in sorted(changed_paths(store, base, result)):
        old = store.get(a[p]).decode(errors="replace").splitlines(keepends=True) if p in a else []
        new = store.get(b[p]).decode(errors="replace").splitlines(keepends=True) if p in b else []
        out += difflib.unified_diff(old, new, f"a/{p}", f"b/{p}")
    return "".join(out)
