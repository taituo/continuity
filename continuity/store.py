"""Content-addressed object store.

Blobs are raw bytes, trees are canonical JSON {path: blob_hash}. Every object
is named by the sha256 of its bytes, so an object can never change under its
name and any copy (a worktree, a peer view) is just a re-materialisation.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path

EMPTY_TREE_BYTES = b"{}"

# Build and tool droppings that must never become part of a delta: an agent
# running the tests in its worktree would otherwise commit __pycache__.
DEFAULT_IGNORE = ("__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                  ".git", ".hg", ".DS_Store", "node_modules")


def ignored(rel: str, patterns=DEFAULT_IGNORE) -> bool:
    """True if any component of the relative path matches an ignore pattern."""
    return any(fnmatch.fnmatchcase(part, pat) for part in rel.split("/") for pat in patterns)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canon(obj) -> bytes:
    """Canonical JSON: the only serialisation that ever gets hashed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, bytes] = {}
        self.EMPTY = self.put(EMPTY_TREE_BYTES)

    # -- raw objects -------------------------------------------------------
    def _path(self, h: str) -> Path:
        return self.root / "objects" / h[:2] / h[2:]

    def put(self, data: bytes) -> str:
        h = sha(data)
        p = self._path(h)
        if not p.exists():
            p.parent.mkdir(exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)  # atomic: a crash never leaves a half object
        self._cache[h] = data
        return h

    def get(self, h: str) -> bytes:
        if h in self._cache:
            return self._cache[h]
        data = self._path(h).read_bytes()
        if sha(data) != h:
            raise ValueError(f"object {h} is corrupt")
        self._cache[h] = data
        return data

    # -- trees -------------------------------------------------------------
    def put_tree(self, files: dict[str, str]) -> str:
        return self.put(canon(dict(sorted(files.items()))))

    def tree(self, h: str) -> dict[str, str]:
        return json.loads(self.get(h))

    def files(self, h: str) -> dict[str, bytes]:
        return {p: self.get(b) for p, b in self.tree(h).items()}

    def tree_from_files(self, files: dict[str, bytes]) -> str:
        return self.put_tree({p: self.put(d) for p, d in files.items()})

    # -- working directories ----------------------------------------------
    def materialize(self, h: str, dest: Path) -> None:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for p, b in self.tree(h).items():
            f = dest / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(self.get(b))

    def snapshot(self, src: Path, ignore=DEFAULT_IGNORE) -> str:
        """Tree of every regular file under src. Symlinks are not followed,
        so a link pointing outside the worktree cannot smuggle content in."""
        src = Path(src)
        files = {}
        for dirpath, dirnames, filenames in os.walk(src):
            rel_dir = Path(dirpath).relative_to(src).as_posix()
            dirnames[:] = sorted(d for d in dirnames
                                 if not ignored(d if rel_dir == "." else f"{rel_dir}/{d}", ignore))
            for name in sorted(filenames):
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                f = Path(dirpath) / name
                if ignored(rel, ignore) or f.is_symlink() or not f.is_file():
                    continue
                files[rel] = f.read_bytes()
        return self.tree_from_files(files)
