"""Independent audit of a continuity root, using only the log and the object store.

Checks: hash chain, strict fold, every integration re-derivable by re-running
the merge on its recorded inputs, every head backed by a passing evaluation
of that exact tree, every referenced object present and uncorrupted.
"""
from __future__ import annotations

from pathlib import Path

from .log import Log, verify_chain
from .merge import merge_trees
from .state import fold
from .store import Store


def audit(root: Path, merge_algo: str = "myers") -> dict:
    root = Path(root)
    store = Store(root / ".continuity")
    log = Log(root / ".continuity" / "log.jsonl", fsync=False)
    verify_chain(log.events)
    st = fold(log.events, store.EMPTY)

    proposals = {}
    for ev in log.events:
        d = ev["data"]
        if ev["kind"] == "session.propose":
            proposals[d["session"]] = d["result"]
        elif ev["kind"] == "external":
            proposals[d["delta"]] = d["result"]

    passing = {c for c, (ok, _) in st.evals.items() if ok}
    replayed = 0
    for ev in log.events:
        if ev["kind"] != "integrate":
            continue
        x = st.deltas[ev["data"]["delta"]]
        assert proposals[x.id] == x.result
        tree, conflicts = merge_trees(store, x.base, ev["data"]["from"], x.result, merge_algo)
        if tree != ev["data"]["to"]:
            raise AssertionError(f"integration of {x.id} does not re-derive (seq {ev['seq']})")
        if tree not in passing:
            raise AssertionError(f"head {tree[:12]} has no passing evaluation")
        store.files(tree)  # every blob present and hash-valid
        replayed += 1

    return {"events": len(log.events), "integrations_replayed": replayed,
            "head": st.head, "deltas": len(st.deltas)}
