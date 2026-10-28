"""Tamper detection, torn-write recovery and HLC properties."""
import json
import random
import tempfile
import unittest
from pathlib import Path

from continuity.hlc import HLC
from continuity.log import ChainError, Log, event_id, verify_chain
from continuity.state import FoldError, fold
from continuity.store import Store, canon


def build(n=20):
    d = Path(tempfile.mkdtemp())
    log = Log(d / "log.jsonl", fsync=False)
    clock = HLC(lambda: 1000)
    for k in range(n):
        log.append("goal.open", {"goal": f"g{k}", "parent": None, "text": str(k)}, clock.now())
    return d, log


class Tamper(unittest.TestCase):
    def test_any_edit_breaks_chain(self):
        r = random.Random(0)
        _, log = build()
        for _ in range(200):
            evs = json.loads(json.dumps(log.events))
            i = r.randrange(len(evs))
            choice = r.random()
            if choice < 0.4:
                evs[i]["data"]["text"] += "x"
            elif choice < 0.6:
                del evs[min(i, len(evs) - 2)]   # tail drops: see test_truncation_needs_anchor
            elif choice < 0.8 and len(evs) > 1:
                j = (i + 1) % len(evs)
                evs[i], evs[j] = evs[j], evs[i]
            else:
                evs.insert(i, dict(evs[i]))
            with self.assertRaises(ChainError):
                verify_chain(evs)

    def test_truncation_needs_anchor(self):
        _, log = build()
        anchor = log.head_id
        cut = log.events[:-3]
        verify_chain(cut)                       # a shorter chain is self-consistent...
        with self.assertRaises(ChainError):
            verify_chain(cut, anchor=anchor)    # ...but not against a published head
        verify_chain(log.events, anchor=anchor)

    def test_rehashed_forgery_is_rejected_by_fold(self):
        """Attacker recomputes every hash, but the history itself is illegal."""
        store = Store(Path(tempfile.mkdtemp()))
        evs = [{"seq": 0, "hlc": [1, 0], "kind": "external",
                "data": {"delta": "x0", "base": store.EMPTY, "result": store.EMPTY}},
               {"seq": 1, "hlc": [2, 0], "kind": "integrate",
                "data": {"delta": "x0", "from": store.EMPTY, "to": store.EMPTY}}]
        prev = "0" * 64
        for ev in evs:
            ev["prev"] = prev
            ev["id"] = prev = event_id(ev)
        verify_chain(evs)  # chain is valid...
        with self.assertRaises(FoldError):
            fold(evs, store.EMPTY)  # ...but integrating without a passing eval is not

    def test_torn_tail_is_truncated(self):
        d, log = build(5)
        with open(d / "log.jsonl", "ab") as f:
            f.write(canon({"seq": 5})[:7])  # crash mid-append
        again = Log(d / "log.jsonl", fsync=False)
        self.assertEqual(len(again.events), 5)
        verify_chain(again.events)


class HybridClock(unittest.TestCase):
    def test_monotone_and_bounded_under_backward_jumps(self):
        r = random.Random(1)
        t = [10_000]
        c = HLC(lambda: t[0])
        last, max_seen = (0, 0), 0
        for _ in range(20_000):
            t[0] += r.randint(-5, 50) if r.random() > 0.01 else -r.randint(100, 5000)
            t[0] = max(0, t[0])
            max_seen = max(max_seen, t[0])
            ts = c.recv((t[0] + r.randint(-30, 30), r.randint(0, 3))) if r.random() < 0.1 else c.now()
            self.assertGreater(ts, last)
            # l never exceeds the largest physical time seen (+ remote skew)
            self.assertLessEqual(ts[0], max_seen + 30)
            last = ts


if __name__ == "__main__":
    unittest.main()
