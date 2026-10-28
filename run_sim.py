"""Run many seeded schedules and aggregate. Usage: python3 run_sim.py [n_seeds] [ticks]"""
import sys
import time
from collections import Counter

from continuity import sim

n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 600
tot = Counter()
outcomes = Counter()
t0 = time.time()
for seed in range(n):
    w = sim.run(seed, ticks=ticks)
    try:
        r = sim.check(w)
    except AssertionError as e:
        print(f"FAIL seed={seed}: {e}  (root kept at {w.root})")
        sys.exit(1)
    for k, v in r.items():
        if isinstance(v, int) and k != "seed":
            tot[k] += v
    outcomes.update(r["outcomes"])
    sim.cleanup(w)
print(f"{n} schedules x {ticks} ticks, all invariants held ({time.time() - t0:.1f}s)")
print(" ", dict(tot))
print("  outcomes:", dict(outcomes))
