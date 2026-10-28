"""Runner: drives disposable ACP agents against the orchestrator.

One asyncio process, one writer of the log. Every orchestrator call happens
under `self.lock`; evaluation runs in a worker thread while holding it, so
agents keep streaming while their checkpoints wait.

Per session:
  start_session -> views + prompt from the log -> spawn agent (optionally in
  bubblewrap) -> initialize -> session/new (cwd = worktree, mcpServers = [])
  -> session/prompt -> checkpoint after every finished tool call -> when the
  prompt returns: propose (end_turn; max_tokens / max_turn_requests as
  partial work) or lose (refusal, cancelled, timeout, crash) -> kill the
  agent -> integrate.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .acp.client import AcpSession, AgentSetupError, TurnResult
from .acp.jsonrpc import Connection, PeerExited, RpcError
from .acp.policy import Fence
from .context import PromptConfig, build_prompt
from .orchestrator import Orchestrator
from .sandbox import Bwrap
from .views import SessionViews

log = logging.getLogger("continuity.runner")

PROPOSE = {"end_turn"}
PROPOSE_PARTIAL = {"max_tokens", "max_turn_requests"}
REMAINING = re.compile(r"^\s*REMAINING:", re.M)    # the agent says part of the goal is left


class RunnerBusy(Exception):
    pass


@dataclass
class RunnerConfig:
    agent_argv: list
    parallel: int = 3
    variants: int = 1                  # concurrent attempts at the same goal
    default_budget: int = 3            # attempts per goal when the goal sets none
    turn_timeout: float = 1800.0
    setup_timeout: float = 60.0
    cancel_grace: float = 15.0
    max_sessions: int | None = None
    sandbox: Bwrap | None = None
    deny_read: tuple = ()              # e.g. the holdout directory
    allow_network: bool = True
    allow_subagents: bool = False
    additional_dirs: bool = True       # pass the views dir as additionalDirectories
    kiro_meta: dict | None = None      # {"modeId", "modelId", "effortLevel"}
    env: dict | None = None
    prompt: PromptConfig = field(default_factory=PromptConfig)
    supersede_done: bool = True        # first integrated attempt finishes a goal; stop the rest


class Runner:
    def __init__(self, orch: Orchestrator, cfg: RunnerConfig):
        self.o, self.cfg = orch, cfg
        orch.supersede_done = cfg.supersede_done
        self.lock = asyncio.Lock()
        self._stops: dict[str, asyncio.Event] = {}
        self.views: dict[str, SessionViews] = {}
        self.stop = asyncio.Event()
        self.fatal: Exception | None = None
        self.started = 0
        self.outcomes: list[dict] = []
        self._pending: dict[str, set] = {}
        self._ckpt: dict[str, asyncio.Task] = {}
        self._lockfile = None
        self.agent_dir = self.o.root / ".continuity" / "agents"
        self.agent_dir.mkdir(parents=True, exist_ok=True)

    # -- single writer ---------------------------------------------------
    def _acquire(self) -> None:
        self._lockfile = open(self.o.root / ".continuity" / "runner.lock", "w")
        try:
            fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lockfile.close()
            raise RunnerBusy("another runner owns this root") from None

    def _release(self) -> None:
        if self._lockfile:
            fcntl.flock(self._lockfile, fcntl.LOCK_UN)
            self._lockfile.close()
            self._lockfile = None

    # -- scheduling --------------------------------------------------------
    def runnable_goals(self) -> list[str]:
        st = self.o.state
        children: dict[str, list] = {}
        for g in st.goals.values():
            if g.parent:
                children.setdefault(g.parent, []).append(g)
        active: dict[str, int] = {}
        for s in st.sessions.values():
            if s.status == "active":
                active[s.goal] = active.get(s.goal, 0) + 1
        out = []
        for gid, g in st.goals.items():
            if g.done or g.durable or gid in children:
                continue          # finished, a container, or decomposed into children
            budget = g.budget if g.budget is not None else self.cfg.default_budget
            if g.spent >= budget or active.get(gid, 0) >= self.cfg.variants:
                continue
            out.append(gid)
        # resolution goals first: they unblock their parents
        out.sort(key=lambda gid: (st.goals[gid].resolves is None, int(gid[1:])))
        return out

    # -- main loop ---------------------------------------------------------
    async def run(self) -> dict:
        self._acquire()
        try:
            async with self.lock:
                orphans = self.o.orphan_active("runner restart: agent processes do not survive it")
            await self.integrate()
            tasks: set[asyncio.Task] = set()
            try:
                await self._loop(tasks)
            except asyncio.CancelledError:
                # Hard stop: kill agents, leave their sessions active in the log.
                # The next runner records them as orphaned and retries the goals.
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            await self.integrate()
            return self.summary(orphans)
        finally:
            self._release()

    async def _loop(self, tasks: set) -> None:
        while True:
            if not self.stop.is_set() and self.fatal is None:
                async with self.lock:
                    self.o.refresh()                # goals added from a shell meanwhile
                    progress = True
                    while progress:            # round-robin: one session per goal per pass
                        progress = False
                        for gid in self.runnable_goals():
                            if len(tasks) >= self.cfg.parallel or (
                                    self.cfg.max_sessions is not None
                                    and self.started >= self.cfg.max_sessions):
                                break
                            sid = self.o.start_session(gid)
                            self.started += 1
                            tasks.add(asyncio.create_task(self._session(sid), name=gid))
                            progress = True
            if not tasks:
                return
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            tasks.difference_update(done)
            for t in done:
                exc = t.exception()
                if exc is not None:
                    log.error("session task failed", exc_info=exc)
                    self.fatal = self.fatal or exc
                    self.request_stop()

    def summary(self, orphans=()) -> dict:
        st = self.o.state
        return {"sessions_started": self.started, "orphaned_on_start": list(orphans),
                "outcomes": self.outcomes,
                "goals_done": sorted(g.id for g in st.goals.values() if g.done),
                "goals_open": sorted(g.id for g in st.goals.values() if not g.done and not g.durable),
                "head": st.head, "fatal": repr(self.fatal) if self.fatal else None}

    # -- one session ---------------------------------------------------------
    async def _session(self, sid: str) -> None:
        o, cfg = self.o, self.cfg
        async with self.lock:
            s = o.state.sessions[sid]
            g = o.state.goals[s.goal]
            views = SessionViews(o, sid, lambda peer, tree, via: self._observe(sid, peer, tree, via))
            self.views[sid] = views
            views.setup_context()
            views.refresh()
            prompt = build_prompt(o, sid, views, cfg.prompt)
            workdir = o.workdir(sid)
            fence = Fence(workdir, views.dir, o.root, o.protected, g.privileged, tuple(cfg.deny_read),
                          cfg.allow_network, cfg.allow_subagents)
        argv = list(cfg.agent_argv)
        if cfg.sandbox:
            argv = cfg.sandbox.wrap(argv, str(o.root), str(workdir), str(views.dir))
        my_stop = self._stops.setdefault(sid, asyncio.Event())
        if self.stop.is_set():
            my_stop.set()
        env = dict(os.environ if cfg.env is None else cfg.env)
        env.update(CONTINUITY_SESSION=sid, CONTINUITY_VIEWS=str(views.dir))
        conn = None
        result: TurnResult
        try:
            conn = await Connection.spawn(argv, cwd=str(workdir), env=env,
                                          stderr_path=str(self.agent_dir / f"{sid}.stderr"))
            acp = AcpSession(conn, fence, on_tool_done=lambda tc: self._schedule_checkpoint(sid),
                             on_observe=lambda paths, via: [views.touch(p, via) for p in paths])
            await asyncio.wait_for(acp.initialize(), cfg.setup_timeout)
            await asyncio.wait_for(acp.new_session(workdir, [views.dir] if cfg.additional_dirs else [],
                                                   cfg.kiro_meta), cfg.setup_timeout)
            result = await acp.run_turn(prompt, cfg.turn_timeout, cfg.cancel_grace, my_stop)
        except AgentSetupError as e:
            result = TurnResult("setup_failed", error=str(e))
            self.fatal = self.fatal or e      # e.g. not logged in: stop burning budgets
        except (PeerExited, RpcError, asyncio.TimeoutError, OSError) as e:
            result = TurnResult("crashed" if conn else "spawn_failed", error=repr(e))
            if conn is None:
                self.fatal = self.fatal or e
        finally:
            if conn is not None:
                await conn.close()
        views.close()                                    # drains queued inotify events
        await self._settle(sid)
        async with self.lock:
            if sid in self._ckpt:
                self._ckpt.pop(sid).cancel()
            self._stops.pop(sid, None)
            if result.stop_reason == "stopped" and o.state.goals[s.goal].done:
                result.stop_reason = "superseded"
            o.note(sid, "turn", {**result.summary(), "prompt_chars": len(prompt)})
            intent = result.final_message[:2000]
            if result.stop_reason in PROPOSE:
                o.propose(sid, intent, partial=bool(REMAINING.search(intent)))
            elif result.stop_reason in PROPOSE_PARTIAL:
                o.propose(sid, intent, partial=True)
            else:
                if o.workdir(sid).exists():
                    o.checkpoint(sid)       # the next attempt continues from the latest state
                o.lose(sid)
            self.views.pop(sid, None)
            self.outcomes.append({"session": sid, "goal": s.goal, "stop_reason": result.stop_reason})
            self._refresh_views()
        await self.integrate()

    # -- callbacks (sync, from the event loop) --------------------------------
    def _observe(self, sid: str, peer: str, tree: str, via: str) -> None:
        async def go():
            async with self.lock:
                self.o.observe(sid, peer, tree, via)
        t = asyncio.get_running_loop().create_task(go())
        self._pending.setdefault(sid, set()).add(t)
        t.add_done_callback(self._pending[sid].discard)

    def _schedule_checkpoint(self, sid: str) -> None:
        if sid in self._ckpt and not self._ckpt[sid].done():
            return

        async def go():
            await asyncio.sleep(0.05)                     # coalesce bursts of tool updates
            async with self.lock:
                s = self.o.state.sessions.get(sid)
                if s is None or s.status != "active" or not self.o.workdir(sid).exists():
                    return
                before = s.checkpoint or s.base
                if self.o.checkpoint(sid) != before:
                    self._refresh_views(exclude=sid)
        t = asyncio.get_running_loop().create_task(go())
        self._ckpt[sid] = t
        self._pending.setdefault(sid, set()).add(t)
        t.add_done_callback(self._pending[sid].discard)

    async def _settle(self, sid: str) -> None:
        while self._pending.get(sid):
            await asyncio.gather(*list(self._pending[sid]), return_exceptions=True)
        self._pending.pop(sid, None)

    def _refresh_views(self, exclude: str | None = None) -> None:
        for sid, v in list(self.views.items()):
            if sid != exclude and self.o.state.sessions[sid].status == "active":
                v.refresh()

    # -- integration -----------------------------------------------------------
    async def integrate(self) -> list[dict]:
        out = []
        loop = asyncio.get_running_loop()
        async with self.lock:
            while True:
                r = await loop.run_in_executor(None, self.o.step)
                if r is None:
                    break
                out.append(r)
                self.outcomes.append({"integration": r})
            if out:
                self._refresh_views()
                self._stop_superseded()
        return out

    def _stop_superseded(self) -> None:
        """Another attempt finished the goal: cancel the remaining ones."""
        if not self.cfg.supersede_done:
            return
        st = self.o.state
        for sid, ev in self._stops.items():
            s = st.sessions.get(sid)
            if s and s.status == "active" and st.goals[s.goal].done:
                ev.set()

    def request_stop(self) -> None:
        """Graceful stop: no new sessions; running turns get session/cancel."""
        self.stop.set()
        for ev in self._stops.values():
            ev.set()
