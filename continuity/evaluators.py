"""Real evaluators. The holdout directory lives outside every tree: it is
copied into the candidate only inside the evaluator's private temp dir, so no
worktree, peer view or log entry ever contains it."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class CommandEvaluator:
    def __init__(self, cmd: list[str], holdout: Path | None = None, timeout: int = 300):
        self.cmd, self.holdout, self.timeout = cmd, holdout, timeout

    def __call__(self, files: dict[str, bytes]) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="continuity-eval-") as d:
            root = Path(d)
            for p, data in files.items():
                f = root / p
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)
            if self.holdout:
                shutil.copytree(self.holdout, root / "_holdout", dirs_exist_ok=True)
            try:
                r = subprocess.run(self.cmd, cwd=root, capture_output=True, text=True,
                                   timeout=self.timeout)
            except subprocess.TimeoutExpired:
                return False, "timeout"
            return r.returncode == 0, (r.stdout + r.stderr)[-2000:]
