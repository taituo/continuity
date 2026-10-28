"""git as an external oracle for the ambiguous case: both sides insert at the
same spot among duplicate (blank) lines. git merge-file and continuity must agree
on clean vs conflict and, when clean, on the exact bytes."""
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from continuity.merge import diff3


def git_merge(o, a, b):
    with tempfile.TemporaryDirectory() as d:
        paths = [Path(d) / n for n in "oab"]
        for p, lines in zip(paths, (o, a, b)):
            p.write_text("".join(lines))
        r = subprocess.run(["git", "merge-file", "-p", str(paths[1]), str(paths[0]), str(paths[2])],
                           capture_output=True, text=True)
        return ("conflict" if r.returncode else "clean"), r.stdout


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitOracle(unittest.TestCase):
    def test_same_spot_insertions_match_git(self):
        r = random.Random(5)

        def block(tag):
            b = [f"{tag}{k}\n" for k in range(r.randint(1, 3))]
            if r.random() < .5:
                b = ["\n"] + b
            if r.random() < .5:
                b = b + ["\n"]
            return b
        for _ in range(400):
            o = [r.choice(["x\n", "y\n", "\n", "\n"]) for _ in range(r.randint(1, 12))]
            i = r.randint(0, len(o))
            a, b = o[:i] + block("A") + o[i:], o[:i] + block("B") + o[i:]
            gk, gout = git_merge(o, a, b)
            merged, _ = diff3(o, a, b)
            self.assertEqual(gk, "clean" if merged is not None else "conflict", (o, a, b))
            if merged is not None:
                self.assertEqual(gout, "".join(merged))


if __name__ == "__main__":
    unittest.main()
