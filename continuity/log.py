"""Append-only, hash-chained event log: the single source of truth.

Each line is canonical JSON {seq, hlc, prev, kind, data, id} where
id = sha256(canonical JSON of everything but id) and prev = id of the
previous event. Changing, dropping, reordering or inserting any event
breaks the chain.

Several processes may append (a runner, and `continuity goal` from a shell
while it runs). Appends happen under an exclusive flock on a side lock file,
after catching up with whatever other writers appended (`sync`), so the
chain never forks. Only complete, newline-terminated lines count: a torn
final fragment (crash mid-append) is truncated on open, under the lock, so
an in-progress append by another process is never mistaken for one. A
corrupt complete line is an error, never silently dropped.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .store import canon, sha

GENESIS = "0" * 64


class ChainError(Exception):
    pass


def event_id(ev: dict) -> str:
    return sha(canon({k: v for k, v in ev.items() if k != "id"}))


def _check_link(ev: dict, i: int, prev: str, last_hlc) -> None:
    if ev.get("seq") != i:
        raise ChainError(f"event {i}: seq {ev.get('seq')} out of order")
    if ev.get("prev") != prev:
        raise ChainError(f"event {i}: prev link broken")
    if event_id(ev) != ev.get("id"):
        raise ChainError(f"event {i}: content does not match its id")
    if last_hlc is not None and tuple(ev["hlc"]) <= last_hlc:
        raise ChainError(f"event {i}: HLC {tuple(ev['hlc'])} not after {last_hlc}")


def verify_chain(events: list[dict], anchor: str | None = None) -> None:
    """Checks links, ids, seq and HLC order. Dropping events off the *end*
    leaves a valid shorter chain, so pass `anchor` (a head id published
    elsewhere: a remote, a tag, a second orchestrator) to detect truncation."""
    if anchor is not None and (events[-1]["id"] if events else GENESIS) != anchor:
        if not any(ev.get("id") == anchor for ev in events):
            raise ChainError("log does not reach the anchored head (truncated or forked)")
    prev, last_hlc = GENESIS, None
    for i, ev in enumerate(events):
        _check_link(ev, i, prev, last_hlc)
        prev, last_hlc = ev["id"], tuple(ev["hlc"])


def _parse(chunk: bytes, offset: int) -> list[dict]:
    out = []
    for n, line in enumerate(chunk.split(b"\n")[:-1]):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            raise ChainError(f"corrupt line {n} after byte {offset}") from None
        if not isinstance(ev, dict):
            raise ChainError(f"corrupt line {n} after byte {offset}")
        out.append(ev)
    return out


class Log:
    def __init__(self, path: Path, fsync: bool = True):
        self.path = Path(path)
        self.fsync = fsync
        self.events: list[dict] = []
        self._size = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with self.locked():
                self._load()

    @contextmanager
    def locked(self):
        with open(self.path.with_name(self.path.name + ".lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _load(self) -> None:
        raw = self.path.read_bytes()
        complete = raw[:raw.rfind(b"\n") + 1]
        if len(complete) < len(raw):              # torn final fragment
            with open(self.path, "r+b") as f:
                f.truncate(len(complete))
        events = _parse(complete, 0)
        verify_chain(events)
        self.events, self._size = events, len(complete)

    def sync(self) -> list[dict]:
        """Pick up complete events other processes appended since we last
        looked. Call under `locked()` before appending."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size <= self._size:
            return []
        with open(self.path, "rb") as f:
            f.seek(self._size)
            data = f.read(size - self._size)
        complete = data[:data.rfind(b"\n") + 1]
        new = _parse(complete, self._size)
        last = tuple(self.events[-1]["hlc"]) if self.events else None
        for ev in new:
            _check_link(ev, len(self.events), self.head_id, last)
            self.events.append(ev)
            last = tuple(ev["hlc"])
        self._size += len(complete)
        return new

    @property
    def head_id(self) -> str:
        return self.events[-1]["id"] if self.events else GENESIS

    def prepare(self, kind: str, data: dict, hlc: tuple[int, int]) -> dict:
        ev = {"seq": len(self.events), "hlc": list(hlc), "prev": self.head_id,
              "kind": kind, "data": data}
        ev["id"] = event_id(ev)
        return ev

    def append(self, kind: str, data: dict, hlc: tuple[int, int]) -> dict:
        with self.locked():
            self.sync()
            return self.commit(self.prepare(kind, data, hlc))

    def commit(self, ev: dict) -> dict:
        """Write one prepared event. Callers that may race other writers hold
        `locked()` and have called `sync()` since preparing."""
        assert ev["seq"] == len(self.events) and ev["prev"] == self.head_id
        line = canon(ev) + b"\n"
        with open(self.path, "ab") as f:
            f.write(line)
            f.flush()
            if self.fsync:
                os.fsync(f.fileno())
        self.events.append(ev)
        self._size += len(line)
        return ev
