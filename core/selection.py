from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .model import Song

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .platform import BaseMusicPlayer


@dataclass(slots=True)
class PendingMusicSelection:
    """One user's active song-selection session."""

    selection_id: str
    selection_key: str
    event: AstrMessageEvent
    player: BaseMusicPlayer
    songs: list[Song]
    expires_at: float
    notify_on_timeout: bool = False
    display_mode: str | None = None
    timeout_task: asyncio.Task[None] | None = None


class MusicSelectionManager:
    """Keep pending selections isolated by conversation and sender."""

    def __init__(
        self,
        on_expire: Callable[[PendingMusicSelection], Awaitable[None]] | None = None,
    ) -> None:
        self._contexts: dict[str, PendingMusicSelection] = {}
        self._context_ids: dict[str, str] = {}
        self._expired_keys: dict[str, float] = {}
        self._timeout_tasks: set[asyncio.Task[None]] = set()
        self._on_expire = on_expire

    def create(
        self,
        *,
        selection_key: str,
        event: AstrMessageEvent,
        player: BaseMusicPlayer,
        songs: list[Song],
        timeout: float,
        notify_on_timeout: bool = False,
        start_timeout: bool = True,
    ) -> PendingMusicSelection:
        self.clear_by_key(selection_key)
        self._expired_keys.pop(selection_key, None)

        timeout = max(float(timeout), 0.1)
        context = PendingMusicSelection(
            selection_id=uuid.uuid4().hex,
            selection_key=selection_key,
            event=event,
            player=player,
            songs=songs,
            expires_at=(
                time.monotonic() + timeout if start_timeout else float("inf")
            ),
            notify_on_timeout=notify_on_timeout,
        )
        self._contexts[context.selection_id] = context
        self._context_ids[selection_key] = context.selection_id
        if start_timeout:
            context.timeout_task = self._schedule_timeout(
                context.selection_id, timeout
            )
        return context

    def start_timeout(self, selection_id: str, timeout: float) -> bool:
        context = self._contexts.get(selection_id)
        if context is None:
            return False
        task = context.timeout_task
        if task is not None and not task.done():
            task.cancel()
        timeout = max(float(timeout), 0.1)
        context.expires_at = time.monotonic() + timeout
        context.timeout_task = self._schedule_timeout(
            context.selection_id, timeout
        )
        return True

    def lookup_by_key(
        self, selection_key: str
    ) -> tuple[str, PendingMusicSelection | None]:
        self._prune_expired_keys()
        selection_id = self._context_ids.get(selection_key)
        if selection_id is None:
            if selection_key in self._expired_keys:
                return "expired", None
            return "not_found", None
        return self.lookup_by_id(selection_id)

    def lookup_by_id(
        self, selection_id: str
    ) -> tuple[str, PendingMusicSelection | None]:
        context = self._contexts.get(selection_id)
        if context is None:
            return "not_found", None
        if context.expires_at <= time.monotonic():
            self._remove(context, mark_expired=True)
            return "expired", None
        return "ok", context

    def claim_by_key(
        self, selection_key: str, index: int
    ) -> tuple[str, PendingMusicSelection | None]:
        status, context = self.lookup_by_key(selection_key)
        if status != "ok" or context is None:
            return status, None
        return self._claim(context, index)

    def claim_by_id(
        self, selection_id: str, index: int
    ) -> tuple[str, PendingMusicSelection | None]:
        status, context = self.lookup_by_id(selection_id)
        if status != "ok" or context is None:
            return status, None
        return self._claim(context, index)

    def clear_by_key(self, selection_key: str) -> None:
        selection_id = self._context_ids.get(selection_key)
        if selection_id is None:
            return
        context = self._contexts.get(selection_id)
        if context is not None:
            self._remove(context)

    def clear_by_id(self, selection_id: str) -> None:
        context = self._contexts.get(selection_id)
        if context is not None:
            self._remove(context)

    async def close(self) -> None:
        tasks = list(self._timeout_tasks)
        for context in list(self._contexts.values()):
            self._remove(context)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._timeout_tasks.clear()
        self._expired_keys.clear()

    def _claim(
        self, context: PendingMusicSelection, index: int
    ) -> tuple[str, PendingMusicSelection | None]:
        if index < 1 or index > len(context.songs):
            return "invalid_index", context
        self._remove(context)
        return "ok", context

    async def _expire_after(self, selection_id: str, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return

        context = self._contexts.get(selection_id)
        if context is None:
            return
        self._remove(context, mark_expired=True)
        if context.notify_on_timeout and self._on_expire is not None:
            await self._on_expire(context)

    def _schedule_timeout(
        self, selection_id: str, timeout: float
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(self._expire_after(selection_id, timeout))
        self._timeout_tasks.add(task)
        task.add_done_callback(self._timeout_tasks.discard)
        return task

    def _remove(
        self, context: PendingMusicSelection, *, mark_expired: bool = False
    ) -> None:
        self._contexts.pop(context.selection_id, None)
        if self._context_ids.get(context.selection_key) == context.selection_id:
            self._context_ids.pop(context.selection_key, None)

        task = context.timeout_task
        current_task = asyncio.current_task()
        if task is not None and task is not current_task and not task.done():
            task.cancel()
        context.timeout_task = None

        if mark_expired:
            # Retain only a short tombstone so a late reply can be explained to
            # the LLM without keeping Song/player/event objects alive.
            self._expired_keys[context.selection_key] = time.monotonic() + 300
        else:
            self._expired_keys.pop(context.selection_key, None)

    def _prune_expired_keys(self) -> None:
        now = time.monotonic()
        for key, expires_at in list(self._expired_keys.items()):
            if expires_at <= now:
                self._expired_keys.pop(key, None)
