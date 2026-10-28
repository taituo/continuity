"""Hybrid Logical Clock (Kulkarni, Demirbas et al. 2014).

Timestamps are (l, c): l tracks the largest physical time seen, c breaks ties.
They are strictly monotone even when the physical clock jumps backwards, and
l never drifts more than the max clock skew from physical time. Only the
orchestrator issues them; agents and VMs never stamp their own events.
"""
from __future__ import annotations

import time
from typing import Callable


class HLC:
    def __init__(self, physical: Callable[[], int] | None = None, state=(0, 0)):
        self.physical = physical or (lambda: time.time_ns() // 1000)  # µs
        self.l, self.c = state

    def now(self) -> tuple[int, int]:
        """Local or send event."""
        pt = self.physical()
        l2 = max(self.l, pt)
        self.c = self.c + 1 if l2 == self.l else 0
        self.l = l2
        return (self.l, self.c)

    def recv(self, m: tuple[int, int]) -> tuple[int, int]:
        """Merge a timestamp from another orchestrator (federation)."""
        ml, mc = m
        pt = self.physical()
        l2 = max(self.l, ml, pt)
        if l2 == self.l == ml:
            c2 = max(self.c, mc) + 1
        elif l2 == self.l:
            c2 = self.c + 1
        elif l2 == ml:
            c2 = mc + 1
        else:
            c2 = 0
        self.l, self.c = l2, c2
        return (self.l, self.c)
