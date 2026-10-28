"""Property tests for line matching and diff3 (seeded, stdlib only)."""
import random
import tempfile
import unittest
from pathlib import Path

from continuity.merge import diff3, matches, merge_trees
from continuity.store import Store

N = 2000


def lcs_len(a, b):
    D = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            D[i][j] = D[i + 1][j + 1] + 1 if a[i] == b[j] else max(D[i + 1][j], D[i][j + 1])
    return D[0][0]


def mutate(r, lines, tag):
    out = list(lines)
    for _ in range(r.randint(0, 4)):
        op = r.random()
        if op < 0.4 and out:
            out[r.randrange(len(out))] = f"{tag}{r.randint(0, 9)}"
        elif op < 0.7:
            out.insert(r.randint(0, len(out)), f"{tag}{r.randint(0, 9)}")
        elif out:
            del out[r.randrange(len(out))]
    return out


class Matching(unittest.TestCase):
    def test_myers_is_optimal_and_monotone(self):
        r = random.Random(7)
        for _ in range(N):
            a = [r.choice("abcd") for _ in range(r.randint(0, 20))]
            b = [r.choice("abcd") for _ in range(r.randint(0, 20))]
            m = matches(a, b)
            self.assertTrue(all(a[i] == b[j] for i, j in m))
            self.assertTrue(all(p[0] < q[0] and p[1] < q[1] for p, q in zip(m, m[1:])))
            self.assertEqual(len(m), lcs_len(a, b), (a, b))


class Diff3Laws(unittest.TestCase):
    def cases(self, seed):
        r = random.Random(seed)
        for _ in range(N):
            o = [r.choice("abcdefgh") for _ in range(r.randint(0, 15))]
            yield r, o, mutate(r, o, "A"), mutate(r, o, "B")

    def test_one_sided_and_identical(self):
        for r, o, a, b in self.cases(1):
            self.assertEqual(diff3(o, a, o)[0], a)
            self.assertEqual(diff3(o, o, b)[0], b)
            self.assertEqual(diff3(o, a, a)[0], a)

    def test_symmetric(self):
        for r, o, a, b in self.cases(2):
            ab, ba = diff3(o, a, b)[0], diff3(o, b, a)[0]
            self.assertEqual(ab is None, ba is None, (o, a, b))
            if ab is not None:
                self.assertEqual(ab, ba, (o, a, b))

    def test_clean_merge_keeps_both_sides(self):
        """Every line either side introduced survives a clean merge."""
        for r, o, a, b in self.cases(3):
            m = diff3(o, a, b)[0]
            if m is None:
                continue
            for side in (a, b):
                for x in side:
                    if x not in o:
                        self.assertIn(x, m, (o, a, b, m))

    def test_disjoint_edits_compose(self):
        r = random.Random(4)
        for _ in range(N):
            n = r.randint(6, 30)
            o = [f"l{k}" for k in range(n)]
            i = r.randrange(0, n // 2 - 1)
            j = r.randrange(n // 2 + 1, n)
            a, b = list(o), list(o)
            a[i], b[j] = "A", "B"
            want = list(o)
            want[i], want[j] = "A", "B"
            self.assertEqual(diff3(o, a, b)[0], want)

    def test_overlapping_different_edits_conflict(self):
        o = ["x", "y", "z"]
        merged, conflicts = diff3(o, ["x", "A", "z"], ["x", "B", "z"])
        self.assertIsNone(merged)
        self.assertEqual(conflicts, [{"base": [1, 2], "ours": [1, 2], "theirs": [1, 2]}])


class Trees(unittest.TestCase):
    def setUp(self):
        self.s = Store(Path(tempfile.mkdtemp()))

    def t(self, **files):
        return self.s.tree_from_files({k.replace("_", "/"): v for k, v in files.items()})

    def test_delete_vs_modify_conflicts(self):
        base = self.t(a_x=b"1\n")
        tree, c = merge_trees(self.s, base, self.t(), self.t(a_x=b"2\n"))
        self.assertIsNone(tree)
        self.assertEqual(c[0]["kind"], "delete/modify")

    def test_independent_files_and_same_add(self):
        base = self.t(a=b"1\n")
        ours = self.t(a=b"1\n", b=b"new\n", c=b"same\n")
        theirs = self.t(a=b"2\n", c=b"same\n")
        tree, c = merge_trees(self.s, base, ours, theirs)
        self.assertEqual(c, [])
        self.assertEqual(self.s.files(tree), {"a": b"2\n", "b": b"new\n", "c": b"same\n"})

    def test_binary_both_changed_conflicts(self):
        base = self.t(bin=b"\xff\x00")
        tree, c = merge_trees(self.s, base, self.t(bin=b"\xff\x01"), self.t(bin=b"\xff\x02"))
        self.assertIsNone(tree)
        self.assertEqual(c[0]["kind"], "binary")


if __name__ == "__main__":
    unittest.main()
