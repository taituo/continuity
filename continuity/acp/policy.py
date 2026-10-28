"""Permission policy for disposable sessions.

The runner answers every `session/request_permission` (and every client
fs/* call) itself. A session may write only inside its own worktree, never to
protected paths unless its goal is privileged, and may not read other
sessions' worktrees, other sessions' views, the store/log, or the holdout.

Only one-shot options are ever chosen. `allow_always` would persist consent
(Kiro V3 can store it in user/workspace scope) and outlive the disposable
agent, so it is never selected.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

CAP_FROM_KIND = {"edit": "fs_write", "delete": "fs_write", "move": "fs_write",
                 "read": "fs_read", "search": "fs_read", "execute": "shell",
                 "fetch": "web_fetch"}
PATH_KEYS = ("path", "file_path", "filePath", "filepath", "target", "source", "destination",
             "old_path", "new_path", "dir", "directory")


def _within(p: str, root: str) -> bool:
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def kiro_consent(params: dict) -> dict:
    for holder in (params, params.get("toolCall") or {}):
        c = ((holder.get("_meta") or {}).get("kiro") or {}).get("consent")
        if isinstance(c, dict):
            return c
    return {}


@dataclass
class Decision:
    allow: bool
    capability: str
    resources: list
    reason: str = ""
    observed: list = field(default_factory=list)   # paths inside the session's views


@dataclass
class Fence:
    workdir: Path
    views: Path
    root: Path
    protected: tuple = ()
    privileged: bool = False
    deny_read: tuple = ()           # e.g. the holdout directory
    allow_network: bool = True
    allow_subagents: bool = False
    allow_unknown: bool = True

    def __post_init__(self):
        self._work = os.path.realpath(self.workdir)
        self._views = os.path.realpath(self.views)
        root = os.path.realpath(self.root)
        self._forbidden = [os.path.join(root, ".continuity"), os.path.join(root, "work"),
                           os.path.join(root, "views")] + [os.path.realpath(p) for p in self.deny_read]

    # -- helpers ---------------------------------------------------------
    def resolve(self, p: str, base: str | None = None) -> str:
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            p = os.path.join(base or self._work, p)
        return os.path.realpath(p)

    def in_views(self, real: str) -> bool:
        return _within(real, self._views)

    def rel_in_work(self, real: str) -> str | None:
        return os.path.relpath(real, self._work).replace(os.sep, "/") if _within(real, self._work) else None

    def check_write(self, paths: list[str]) -> tuple[bool, str]:
        if not paths:
            return False, "write without a known target"
        for real in paths:
            rel = self.rel_in_work(real)
            if rel is None:
                return False, f"write outside the session worktree: {real}"
            if not self.privileged and any(rel == p.rstrip("/") or rel.startswith(p) for p in self.protected):
                return False, f"write to protected path: {rel}"
        return True, "inside worktree"

    def check_read(self, paths: list[str]) -> tuple[bool, str, list]:
        observed = []
        for real in paths:
            if _within(real, self._work):
                continue
            if _within(real, self._views):
                observed.append(real)
                continue
            for f in self._forbidden:
                if _within(real, f):
                    return False, f"read of another session's or the system's files: {real}", []
        return True, "allowed", observed

    def check_command(self, cmd: str) -> tuple[bool, str]:
        """A cheap guard against absolute references to fenced paths. It cannot
        see through `cd ..` and friends; the OS sandbox is the real fence."""
        scrubbed = cmd
        for own in (self._views, self._work):   # own paths, exactly (not /work/s5 inside /work/s50)
            scrubbed = re.sub(re.escape(own) + r"(?=$|[/\s'\";:)])", "", scrubbed)
        for f in self._forbidden:
            if f in scrubbed:
                return False, f"command references a fenced path: {f}"
        return True, "allowed (shell is fenced by the sandbox, if enabled)"

    # -- the decision ------------------------------------------------------
    def classify(self, params: dict) -> tuple[str, list, str | None]:
        consent = kiro_consent(params)
        tc = params.get("toolCall") or {}
        cap = consent.get("capability") or CAP_FROM_KIND.get(tc.get("kind") or "", "unknown")
        res = consent.get("resource")
        resources = [res] if isinstance(res, str) else [r for r in (res or []) if isinstance(r, str)]
        if not resources:
            resources = [loc["path"] for loc in tc.get("locations") or []
                         if isinstance(loc, dict) and isinstance(loc.get("path"), str)]
            raw = tc.get("rawInput") if isinstance(tc.get("rawInput"), dict) else {}
            for k in PATH_KEYS:
                if isinstance(raw.get(k), str):
                    resources.append(raw[k])
            if cap == "shell" and isinstance(raw.get("command"), str):
                resources = [raw["command"]]
        return cap, resources, consent.get("workspaceRoot")

    def decide(self, params: dict) -> Decision:
        cap, resources, ws_root = self.classify(params)
        if cap in ("fs_write", "fs_read"):
            real = [self.resolve(r, ws_root) for r in resources]
            if cap == "fs_write":
                ok, why = self.check_write(real)
                return Decision(ok, cap, real, why)
            ok, why, obs = self.check_read(real)
            return Decision(ok, cap, real, why, obs)
        if cap == "shell":
            ok, why = self.check_command(" ".join(resources))
            return Decision(ok, cap, resources, why)
        if cap in ("web_fetch", "web_search"):
            return Decision(self.allow_network, cap, resources, "network tools " + ("allowed" if self.allow_network else "disabled"))
        if cap == "subagent":
            return Decision(self.allow_subagents, cap, resources,
                            "subagents " + ("allowed" if self.allow_subagents else "disabled: work must stay in this disposable session"))
        if cap == "mcp":
            return Decision(False, cap, resources, "MCP is not in use")
        return Decision(self.allow_unknown, cap, resources, "unclassified capability")


def choose_option(options: list, allow: bool) -> tuple[str | None, str]:
    """Pick a one-shot option. Returns (optionId or None for 'cancelled', note)."""
    by_kind = {}
    for o in options or []:
        if isinstance(o, dict) and o.get("optionId") is not None:
            by_kind.setdefault(o.get("kind"), o["optionId"])
    if allow and "allow_once" in by_kind:
        return by_kind["allow_once"], ""
    note = "" if not allow else "no one-shot allow offered; rejected to avoid persisting consent"
    for k in ("reject_once", "reject_always"):
        if k in by_kind:
            return by_kind[k], note
    return None, note or "no reject option offered"
