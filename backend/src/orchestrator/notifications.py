"""Notification abstraction.

v1: desktop notifications via notify-send when available (Linux/WSL best
effort). Future channels (webhook/email/Slack) implement Notifier.
"""

from __future__ import annotations

import asyncio
import shutil


class Notifier:
    async def notify(self, title: str, body: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class NullNotifier(Notifier):
    async def notify(self, title: str, body: str) -> None:
        return


class DesktopNotifier(Notifier):
    async def notify(self, title: str, body: str) -> None:
        binary = shutil.which("notify-send")
        if not binary:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, title, body[:200],
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            return  # notifications must never break orchestration


def default_notifier() -> Notifier:
    return DesktopNotifier() if shutil.which("notify-send") else NullNotifier()
