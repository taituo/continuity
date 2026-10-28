"""Append-only, hash-chained event log: the single source of truth.

Each line is canonical JSON {seq, hlc, prev, kind, data, id} where
id = sha256(canonical JSON of everything but id) and prev = id of the
previous event. Changing, dropping, reordering or inserting any event
breaks the chain. A torn last line (crash mid-append) is truncated on open.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .store import canon, sha

GENESIS = "0" * 64


class ChainError(Exception):
    pass


def event_id(ev: dict) -> str:
    return sha(canon({k: v for k, v in ev.items() if k != "id"}))


def verify_chain(events: list[dict], anchor: str | None = None) -> None:
    """Checks links, ids, seq and HLC order. Dropping events off the *end*
    leaves a valid shorter chain, so pass `anchor` (a head id published
    elsewhere: a remote, a tag, a second orchestrator) to detect truncation."""
    if anchor is not None and (events[-1]["id"] if events else GENESIS) != anchor:
        if not any(ev.get("id") == anchor for ev in events):
            raise ChainError("log does not reach the anchored head (truncated or forked)")
    prev, last_hlc = GENESIS, None
    for i, ev in enumerate(events):
        if ev.get("seq") != i:
            raise ChainError(f"event {i}: seq {ev.get('seq')} out of order")
        if ev.get("prev") != prev:
            raise ChainError(f"event {i}: prev link broken")
        if event_id(ev) != ev.get("id"):
            raise ChainError(f"event {i}: content does not match its id")
        hlc = tuple(ev["hlc"])
        if last_hlc is not None and hlc <= last_hlc:
            raise ChainError(f"event {i}: HLC {hlc} not after {last_hlc}")
        prev, last_hlc = ev["id"], hlc


class Log:
    def __init__(self, path: Path, fsync: bool = True):
        self.path = Path(path)
        self.fsync = fsync
        self.events: list[dict] = []
        if self.path.exists():
            self._load()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        raw = self.path.read_bytes()
        lines = raw.split(b"\n")
        good: list[dict] = []
        valid_len = 0
        for line in lines:
            if not line:
                continue
            try:
                good.append(json.loads(line))
                valid_len += len(line) + 1
            except json.JSONDecodeError:
                break  # torn tail: everything after it is discarded
        if valid_len < len(raw):
            with open(self.path, "r+b") as f:
                f.truncate(valid_len)
        verify_chain(good)
        self.events = good

    @property
    def head_id(self) -> str:
        return self.events[-1]["id"] if self.events else GENESIS

    def prepare(self, kind: str, data: dict, hlc: tuple[int, int]) -> dict:
        ev = {"seq": len(self.events), "hlc": list(hlc), "prev": self.head_id,
              "kind": kind, "data": data}
        ev["id"] = event_id(ev)
        return ev

    def append(self, kind: str, data: dict, hlc: tuple[int, int]) -> dict:
        return self.commit(self.prepare(kind, data, hlc))

    def commit(self, ev: dict) -> dict:
        assert ev["seq"] == len(self.events) and ev["prev"] == self.head_id
        line = canon(ev) + b"\n"
        with open(self.path, "ab") as f:
            f.write(line)
            f.flush()
            if self.fsync:
                os.fsync(f.fileno())
        self.events.append(ev)
        return ev
