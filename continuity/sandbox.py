"""OS-level fence for an agent process (Linux, bubblewrap).

Kiro's own sandbox covers shell commands but explicitly not its built-in
file tools, which are governed by permission rules. Wrapping the whole agent
process closes that gap: inside the sandbox the continuity root is an empty
tmpfs except for this session's worktree (read-write) and its views
(read-only), so neither a file tool nor a shell command can reach another
session, the log, or the holdout, whatever the agent's permission settings.

The agent still needs its own state (login tokens, session store, logs)
writable. Which directories that is depends on the agent version, so they are
configurable; the defaults are the usual Kiro CLI locations and should be
verified on the target machine (run once, check for write errors in the
agent's stderr log under .continuity/agents/).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


def _default_rw() -> tuple:
    rw = ["~/.kiro", "~/.local/share/kiro-cli", "~/.cache", "~/.aws/sso/cache"]
    if os.environ.get("XDG_RUNTIME_DIR"):
        rw.append(os.environ["XDG_RUNTIME_DIR"])
    if os.environ.get("KIRO_HOME"):
        rw.append(os.environ["KIRO_HOME"])
    return tuple(rw)


@dataclass
class Bwrap:
    rw: tuple = _default_rw()
    ro_hide: tuple = ()          # extra paths to hide entirely (e.g. the holdout)
    share_net: bool = True       # the agent must reach its model API
    binary: str = "bwrap"

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def wrap(self, argv: list[str], root: str, workdir: str, views: str) -> list[str]:
        root, workdir, views = (os.path.realpath(p) for p in (root, workdir, views))
        a = [self.binary, "--die-with-parent", "--new-session",
             "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        if not self.share_net:
            a.append("--unshare-net")
        a += ["--tmpfs", root]                       # hide log, store, main/, other sessions
        for p in self.ro_hide:
            a += ["--tmpfs", os.path.realpath(os.path.expanduser(p))]
        a += ["--bind", workdir, workdir, "--ro-bind", views, views]
        for p in self.rw:
            p = os.path.realpath(os.path.expanduser(p))
            if os.path.exists(p) and not (p == root or p.startswith(root + os.sep)):
                a += ["--bind", p, p]
        a += ["--chdir", workdir, "--"]
        return a + list(argv)
