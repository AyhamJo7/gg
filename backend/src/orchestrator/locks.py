"""Resource locking abstractions.

Serial scheduler v1 uses these for safety; the interface supports future
parallel task execution (file-level locks) without engine rewrites.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ResourceLocks:
    def __init__(self) -> None:
        self._workspace_lock = asyncio.Lock()
        self._git_lock = asyncio.Lock()
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._task_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def workspace(self) -> AsyncIterator[None]:
        async with self._workspace_lock:
            yield

    @asynccontextmanager
    async def git(self) -> AsyncIterator[None]:
        async with self._git_lock:
            yield

    @asynccontextmanager
    async def files(self, paths: list[str]) -> AsyncIterator[None]:
        locks = [self._file_locks.setdefault(p, asyncio.Lock()) for p in sorted(paths)]
        for lock in locks:
            await lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    @asynccontextmanager
    async def task(self, task_id: str) -> AsyncIterator[None]:
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            yield
