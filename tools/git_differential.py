"""Differential test: continuity's diff3 vs `git merge-file` on real merge history.

For every merge commit with a single merge base, every text file modified on
both sides is merged twice: by us and by git. Reports agreement on clean vs
conflict, byte-identical results, and how often a clean result equals what
the humans actually committed.

usage: python3 tools/git_differential.py <bare-repo> [max_merges] [algo]
"""
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from continuity import merge  # noqa: E402
from continuity.merge import merge_text  # noqa: E402


def git(repo, *args, inp=None):
    return subprocess.run(["git", "-C", repo, *args], input=inp, capture_output=True,
                          check=True).stdout


class Blobs:
    def __init__(self, repo):
        self.p = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def __call__(self, sha):
        self.p.stdin.write(sha.encode() + b"\n")
        self.p.stdin.flush()
        header = self.p.stdout.readline().split()
        data = self.p.stdout.read(int(header[2]))
        self.p.stdout.read(1)
        return data


def changes(repo, a, b):
    out = {}
    for line in git(repo, "diff-tree", "-r", "--no-renames", a, b).decode().splitlines():
        meta, path = line.split("\t", 1)
        _, _, old, new, status = meta.split()
        if status == "M":
            out[path] = (old, new)
    return out


def git_merge_file(ours, base, theirs):
    with tempfile.TemporaryDirectory() as d:
        o, b, t = (Path(d) / n for n in ("o", "b", "t"))
        o.write_bytes(ours), b.write_bytes(base), t.write_bytes(theirs)
        r = subprocess.run(["git", "merge-file", "-p", str(o), str(b), str(t)], capture_output=True)
        if r.returncode < 0 or r.returncode > 127:
            return "error", None
        return ("conflict" if r.returncode else "clean"), r.stdout


def main(repo, limit=10_000, algo="myers"):
    blob = Blobs(repo)
    lines = git(repo, "rev-list", "--merges", "--parents", "HEAD").decode().splitlines()
    stats, examples = Counter(), {}
    for line in lines[:limit]:
        parts = line.split()
        if len(parts) != 3:
            stats["octopus_skipped"] += 1
            continue
        m, p1, p2 = parts
        bases = git(repo, "merge-base", "--all", p1, p2).decode().split()
        if len(bases) != 1:
            stats["criss_cross_skipped"] += 1
            continue
        stats["merges"] += 1
        c1, c2 = changes(repo, bases[0], p1), changes(repo, bases[0], p2)
        for path in sorted(c1.keys() & c2.keys()):
            (bo, n1), (_, n2) = c1[path], c2[path]
            if n1 == n2:
                continue
            base, ours, theirs = blob(bo), blob(n1), blob(n2)
            if b"\0" in base + ours + theirs:
                stats["binary_skipped"] += 1
                continue
            stats["files"] += 1
            g_kind, g_out = git_merge_file(ours, base, theirs)
            j_out, j_conf = merge_text(base, ours, theirs, algo)
            j_kind = "conflict" if j_out is None else "clean"
            if g_kind == j_kind == "clean":
                key = "both_clean_identical" if g_out == j_out else "both_clean_DIFFERENT"
            elif g_kind == j_kind:
                key = "both_conflict"
            else:
                key = f"continuity_{j_kind}_git_{g_kind}"
            stats[key] += 1
            if j_kind == "clean":
                try:
                    committed = blob(git(repo, "rev-parse", f"{m}:{path}").decode().strip())
                    stats["clean_equals_committed" if committed == j_out else "clean_differs_from_committed"] += 1
                except subprocess.CalledProcessError:
                    stats["clean_path_gone_in_commit"] += 1
            if key not in ("both_clean_identical", "both_conflict"):
                try:
                    committed = blob(git(repo, "rev-parse", f"{m}:{path}").decode().strip())
                except subprocess.CalledProcessError:
                    committed = None
                stats[f"  {key}: git==committed"] += g_out == committed
                stats[f"  {key}: continuity==committed"] += j_out == committed
            examples.setdefault(key, (m[:10], path))
    return stats, examples


if __name__ == "__main__":
    repo = sys.argv[1]
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 10_000
    algo = sys.argv[3] if len(sys.argv) > 3 else "myers"
    merge.COMPACT = "nocompact" not in sys.argv
    stats, ex = main(repo, lim, algo)
    for k, v in sorted(stats.items()):
        print(f"{k:32s} {v:6d}   e.g. {ex.get(k, '')}")
