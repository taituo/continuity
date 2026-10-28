"""Real evaluators.

The holdout directory lives outside every tree: it is copied into the
candidate only inside the evaluator's private temp dir, so no worktree, peer
view or log entry ever contains it. With a separate `holdout_cmd`, holdout
output is also withheld from the report, because reports are fed back to the
next attempt's prompt and would otherwise leak the hidden checks.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

HOLDOUT_DIR = "_holdout"


class CommandEvaluator:
    def __init__(self, cmd: list[str], holdout: Path | None = None, timeout: int = 300,
                 holdout_cmd: list[str] | None = None):
        self.cmd, self.holdout, self.timeout, self.holdout_cmd = cmd, holdout, timeout, holdout_cmd

    def _run(self, cmd, cwd) -> tuple[bool, str]:
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        return r.returncode == 0, (r.stdout + r.stderr)[-2000:]

    def __call__(self, files: dict[str, bytes]) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="continuity-eval-") as d:
            root = Path(d)
            for p, data in files.items():
                f = root / p
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)
            if self.holdout_cmd is None:
                if self.holdout:
                    shutil.copytree(self.holdout, root / HOLDOUT_DIR, dirs_exist_ok=True)
                return self._run(self.cmd, root)
            ok, report = self._run(self.cmd, root)
            if not ok:
                return False, "visible checks failed:\n" + report
            if self.holdout:
                shutil.copytree(self.holdout, root / HOLDOUT_DIR, dirs_exist_ok=True)
            hok, _ = self._run(self.holdout_cmd, root)
            if not hok:
                return False, "visible checks passed; hidden checks failed (details withheld)"
            return True, "visible and hidden checks passed"
