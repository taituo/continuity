"""Three-way merge of trees, with a line-level diff3 for text files.

Conflicts are returned as data (first-class records), never written into the
tree as markers: a conflicted merge produces no tree at all, so a head can
never contain an unresolved conflict.

Line matching uses Myers' O(ND) diff (the algorithm git uses by default), with
difflib as a fallback for very large edit distances.
"""
from __future__ import annotations

import difflib

from .store import Store

MYERS_MAX_D = 1500


# --------------------------------------------------------------------------
# line matching
# --------------------------------------------------------------------------
def _myers_core(A: list[int], B: list[int]) -> list[tuple[int, int]] | None:
    N, M = len(A), len(B)
    V = {1: 0}
    trace = []
    for d in range(N + M + 1):
        if d > MYERS_MAX_D:
            return None
        trace.append(V.copy())
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and V[k - 1] < V[k + 1]):
                x = V[k + 1]
            else:
                x = V[k - 1] + 1
            y = x - k
            while x < N and y < M and A[x] == B[y]:
                x += 1
                y += 1
            V[k] = x
            if x >= N and y >= M:
                return _backtrack(trace, N, M)
    return []


def _backtrack(trace, N, M):
    x, y = N, M
    out = []
    for d in range(len(trace) - 1, -1, -1):
        v = trace[d]
        k = x - y
        if k == -d or (k != d and v[k - 1] < v[k + 1]):
            pk = k + 1
        else:
            pk = k - 1
        px = v[pk]
        py = px - pk
        while x > px and y > py:
            out.append((x - 1, y - 1))
            x -= 1
            y -= 1
        if d > 0:
            x, y = px, py
    out.reverse()
    return out


def _compact(lines: list[str], chg: list[bool]) -> None:
    """Slide each group of changed lines up, then down as far as it goes
    (git's xdl_change_compact without the indent heuristic). Sliding a group
    over an equal line only changes *which* copy is marked changed, so the
    unchanged subsequence and hence the matching stay valid."""
    n, s = len(lines), 0
    while s < n:
        if not chg[s]:
            s += 1
            continue
        e = s
        while e < n and chg[e]:
            e += 1
        while s > 0 and not chg[s - 1] and lines[s - 1] == lines[e - 1]:
            s, e = s - 1, e - 1
            chg[s], chg[e] = True, False
            while s > 0 and chg[s - 1]:
                s -= 1
        while e < n and not chg[e] and lines[s] == lines[e]:
            chg[s], chg[e] = False, True
            s, e = s + 1, e + 1
            while e < n and chg[e]:
                e += 1
        s = e


def matches(a: list[str], b: list[str], algo: str = "myers",
            compact: bool = True) -> list[tuple[int, int]]:
    """Monotone list of (i, j) with a[i] == b[j]."""
    m = _raw_matches(a, b, algo)
    if not compact:
        return m
    ca, cb = [True] * len(a), [True] * len(b)
    for i, j in m:
        ca[i] = cb[j] = False
    _compact(a, ca)
    _compact(b, cb)
    ua = [i for i in range(len(a)) if not ca[i]]
    ub = [j for j in range(len(b)) if not cb[j]]
    assert len(ua) == len(ub)
    return list(zip(ua, ub))


def _raw_matches(a: list[str], b: list[str], algo: str) -> list[tuple[int, int]]:
    n, m = len(a), len(b)
    pre = 0
    while pre < n and pre < m and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and suf < m - pre and a[n - 1 - suf] == b[m - 1 - suf]:
        suf += 1
    A, B = a[pre:n - suf], b[pre:m - suf]
    core = None
    if algo == "myers":
        ids: dict[str, int] = {}
        core = _myers_core([ids.setdefault(s, len(ids)) for s in A],
                           [ids.setdefault(s, len(ids)) for s in B])
    if core is None:
        sm = difflib.SequenceMatcher(None, A, B, autojunk=False)
        core = [(i + t, j + t) for i, j, size in sm.get_matching_blocks() for t in range(size)]
    return ([(t, t) for t in range(pre)]
            + [(i + pre, j + pre) for i, j in core]
            + [(n - suf + t, m - suf + t) for t in range(suf)])


# --------------------------------------------------------------------------
# diff3 (Khanna, Kunal & Pierce, "A formal investigation of diff3", 2007)
# --------------------------------------------------------------------------
COMPACT = True


def diff3(o: list[str], a: list[str], b: list[str], algo: str = "myers"):
    """Return (merged_lines, conflicts). merged_lines is None on conflict."""
    ma = dict(matches(o, a, algo, COMPACT))
    mb = dict(matches(o, b, algo, COMPACT))
    out: list[str] = []
    conflicts = []
    io = ia = ib = 0
    while True:
        j = io
        while j < len(o) and not (j in ma and j in mb and ma[j] >= ia and mb[j] >= ib):
            j += 1
        ja, jb = (len(a), len(b)) if j == len(o) else (ma[j], mb[j])
        co, ca, cb = o[io:j], a[ia:ja], b[ib:jb]
        if ca == co:
            out += cb
        elif cb == co or ca == cb:
            out += ca
        else:
            conflicts.append({"base": [io, j], "ours": [ia, ja], "theirs": [ib, jb]})
        if j == len(o):
            break
        while j < len(o) and ma.get(j) == ja and mb.get(j) == jb:
            out.append(o[j])
            j += 1
            ja += 1
            jb += 1
        io, ia, ib = j, ja, jb
    return (None if conflicts else out), conflicts


def merge_text(base: bytes | None, ours: bytes, theirs: bytes, algo: str = "myers"):
    try:
        o = (base or b"").decode().splitlines(keepends=True)
        a = ours.decode().splitlines(keepends=True)
        b = theirs.decode().splitlines(keepends=True)
    except UnicodeDecodeError:
        return None, [{"kind": "binary"}]
    merged, regions = diff3(o, a, b, algo)
    if merged is None:
        return None, [{"kind": "content", **r} for r in regions]
    return "".join(merged).encode(), []


# --------------------------------------------------------------------------
# trees
# --------------------------------------------------------------------------
def changed_paths(store: Store, base: str, other: str) -> set[str]:
    b, o = store.tree(base), store.tree(other)
    return {p for p in b.keys() | o.keys() if b.get(p) != o.get(p)}


def merge_trees(store: Store, base: str, ours: str, theirs: str, algo: str = "myers"):
    """Three-way merge. Returns (tree_hash | None, conflicts)."""
    if ours == theirs or theirs == base:
        return ours, []
    if ours == base:
        return theirs, []
    B, O, T = store.tree(base), store.tree(ours), store.tree(theirs)
    out: dict[str, str] = {}
    conflicts = []
    for p in sorted(B.keys() | O.keys() | T.keys()):
        b, o, t = B.get(p), O.get(p), T.get(p)
        if o == t:
            r = o
        elif o == b:
            r = t
        elif t == b:
            r = o
        elif o is None or t is None:
            conflicts.append({"path": p, "kind": "delete/modify"})
            continue
        else:
            data, regions = merge_text(store.get(b) if b else None, store.get(o), store.get(t), algo)
            if data is None:
                conflicts += [{"path": p, **r} for r in regions]
                continue
            r = store.put(data)
        if r is not None:
            out[p] = r
    if conflicts:
        return None, conflicts
    return store.put_tree(out), []
