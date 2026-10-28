"""Minimal inotify (Linux) via ctypes, driven by the asyncio event loop.

Used to observe which peer-view files an agent actually opens, whatever tool
it used (a built-in read tool, `cat` in a shell, a test runner importing a
module). inotify is inode-based, so it also sees opens made through a
read-only bind mount inside a sandbox. Not available on macOS: there the
runner falls back to ACP-level signals only.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import os
import struct
import sys
from typing import Callable

IN_OPEN = 0x00000020
IN_IGNORED = 0x00008000
IN_Q_OVERFLOW = 0x00004000
IN_ISDIR = 0x40000000
_EVENT = struct.Struct("iIII")

available = sys.platform.startswith("linux")
_libc = None
if available:
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    except OSError:
        available = False


class Inotify:
    def __init__(self, on_open: Callable[[str], None]):
        if not available:
            raise OSError("inotify is not available on this platform")
        fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.fd = fd
        self.on_open = on_open
        self.dirs: dict[int, str] = {}
        self.overflowed = False
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(fd, self._ready)

    def watch(self, path: str) -> None:
        wd = _libc.inotify_add_watch(self.fd, os.fsencode(path), IN_OPEN)
        if wd < 0:
            raise OSError(ctypes.get_errno(), f"inotify_add_watch failed for {path}")
        self.dirs[wd] = path

    def watch_tree(self, top: str) -> None:
        for dirpath, _, _ in os.walk(top):
            self.watch(dirpath)

    def moved(self, old: str, new: str) -> None:
        """Watches follow inodes; keep our path labels in step after a rename."""
        for wd, p in self.dirs.items():
            if p == old or p.startswith(old + os.sep):
                self.dirs[wd] = new + p[len(old):]

    def _ready(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 1 << 16)
            except BlockingIOError:
                return
            if not data:
                return
            self._parse(data)

    def _parse(self, data: bytes) -> None:
        i = 0
        while i + _EVENT.size <= len(data):
            wd, mask, _, n = _EVENT.unpack_from(data, i)
            name = data[i + _EVENT.size:i + _EVENT.size + n].split(b"\0", 1)[0]
            i += _EVENT.size + n
            if mask & IN_Q_OVERFLOW:
                self.overflowed = True   # events were dropped; caller must not rely on completeness
                continue
            if mask & IN_IGNORED:
                self.dirs.pop(wd, None)
                continue
            if mask & IN_OPEN and not mask & IN_ISDIR and wd in self.dirs:
                self.on_open(os.path.join(self.dirs[wd], os.fsdecode(name)))

    def drain(self) -> None:
        """Process anything already queued (call before closing)."""
        self._ready()

    def close(self) -> None:
        try:
            self._loop.remove_reader(self.fd)
        finally:
            os.close(self.fd)
