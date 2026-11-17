"""Upload downloaded videos back to the chat.

* 1 video            -> send_video
* 2..10 videos       -> send_media_group (split into several groups if the
                        combined size would exceed one upload request's limit)
* more than 10       -> one send_video per item
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import IO, Any

from telegram import Bot, InputMediaVideo, Message, ReplyParameters
from telegram.error import NetworkError, RetryAfter, TimedOut

from .downloader import DownloadedVideo, DownloadResult

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
MEDIA_GROUP_MAX = 10
UPLOAD_TIMEOUT = 600  # seconds; a 50 MB upload on a slow home uplink takes a while



def build_caption(platform: str, title: str, url: str, index: int | None = None,
                  total: int | None = None) -> str:
    """``<platform> · <title>`` + ``(i/n)`` + newline + URL, trimmed to
    Telegram's 1024-char caption limit (the URL is never truncated)."""
    head = platform
    if index is not None and total and total > 1:
        head += f" ({index}/{total})"
    title = " ".join((title or "").split())
    url_part = "\n" + url
    if title:
        room = CAPTION_LIMIT - len(head) - len(" · ") - len(url_part)
        if room <= 1:
            title = ""
        elif len(title) > room:
            title = title[: room - 1].rstrip() + "…"
    caption = f"{head} · {title}{url_part}" if title else f"{head}{url_part}"
    return caption[:CAPTION_LIMIT]


def chunk_for_media_groups(videos: list[DownloadedVideo], max_bytes: int | None,
                           max_items: int = MEDIA_GROUP_MAX) -> list[list[DownloadedVideo]]:
    """Greedily split videos into groups of <= ``max_items`` whose combined
    size stays <= ``max_bytes`` (if given)."""
    groups: list[list[DownloadedVideo]] = []
    current: list[DownloadedVideo] = []
    current_size = 0
    for v in videos:
        size = v.size
        too_many = len(current) >= max_items
        too_big = max_bytes is not None and current and current_size + size > max_bytes
        if current and (too_many or too_big):
            groups.append(current)
            current, current_size = [], 0
        current.append(v)
        current_size += size
    if current:
        groups.append(current)
    return groups


async def with_retry[T](action: Callable[[], Awaitable[T]], what: str = "upload") -> T:
    """Run ``action``; retry once on a transient Telegram/network error.

    ``action`` must open its files itself so the retry re-reads them from
    the start.
    """
    try:
        return await action()
    except RetryAfter as exc:
        delay = exc.retry_after
        delay = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
        log.warning("%s rate limited, retrying in %.0fs", what, delay)
        await asyncio.sleep(min(delay, 60) + 1)
    except (TimedOut, NetworkError) as exc:
        log.warning("%s transient error (%s), retrying once", what, type(exc).__name__)
        await asyncio.sleep(3)
    return await action()


class _Files:
    """Opens files for one upload attempt and always closes them."""

    def __init__(self) -> None:
        self._stack = contextlib.ExitStack()

    def open(self, path: Path | None) -> IO[bytes] | None:
        if path is None or not path.exists():
            return None
        return self._stack.enter_context(path.open("rb"))

    def __enter__(self) -> _Files:
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stack.close()


class TelegramSender:
    def __init__(self, bot: Bot, upload_limit_bytes: int, per_request_limit: int | None):
        self.bot = bot
        self.upload_limit_bytes = upload_limit_bytes
        # Cloud Bot API: keep each multipart request within the 50 MB limit.
        self.per_request_limit = per_request_limit

    async def send(self, chat_id: int, reply_to: int, result: DownloadResult,
                   on_progress: Callable[[str], None] | None = None) -> list[Message]:
        videos = result.videos
        total = len(videos)
        reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        sent: list[Message] = []

        def caption_for(i: int, v: DownloadedVideo) -> str:
            title = (v.title or result.title) if total > 1 else (result.title or v.title)
            return build_caption(result.platform.value, title, result.source_url, i, total)

        if total == 1:
            sent.append(await self._send_one(chat_id, reply, videos[0], caption_for(1, videos[0])))
        elif total <= MEDIA_GROUP_MAX:
            groups = chunk_for_media_groups(videos, self.per_request_limit)
            index = 0
            for group in groups:
                if on_progress:
                    on_progress(f"{index + 1}–{index + len(group)} of {total}")
                if len(group) == 1:
                    index += 1
                    sent.append(await self._send_one(chat_id, reply, group[0], caption_for(index, group[0])))
                    continue
                start = index
                sent.extend(await self._send_group(chat_id, reply, group, start, caption_for))
                index += len(group)
        else:
            for i, v in enumerate(videos, start=1):
                if on_progress:
                    on_progress(f"{i} of {total}")
                sent.append(await self._send_one(chat_id, reply, v, caption_for(i, v)))
        return sent

    async def _send_one(self, chat_id: int, reply: ReplyParameters, v: DownloadedVideo,
                        caption: str) -> Message:
        async def action() -> Message:
            with _Files() as files:
                return await self.bot.send_video(
                    chat_id=chat_id,
                    video=files.open(v.path),
                    caption=caption,
                    duration=int(round(v.duration)) or None,
                    width=v.width,
                    height=v.height,
                    thumbnail=files.open(v.thumbnail),
                    supports_streaming=True,
                    reply_parameters=reply,
                    read_timeout=UPLOAD_TIMEOUT,
                    write_timeout=UPLOAD_TIMEOUT,
                )

        return await with_retry(action, "send_video")

    async def _send_group(self, chat_id: int, reply: ReplyParameters, group: list[DownloadedVideo],
                          start: int, caption_for: Callable[[int, DownloadedVideo], str]) -> list[Message]:
        async def action() -> list[Message]:
            with _Files() as files:
                media = [
                    InputMediaVideo(
                        media=files.open(v.path),
                        # Telegram shows the first item's caption for the whole album.
                        caption=caption_for(start + i + 1, v) if i == 0 else None,
                        duration=int(round(v.duration)) or None,
                        width=v.width,
                        height=v.height,
                        thumbnail=files.open(v.thumbnail),
                        supports_streaming=True,
                    )
                    for i, v in enumerate(group)
                ]
                return list(await self.bot.send_media_group(
                    chat_id=chat_id,
                    media=media,
                    reply_parameters=reply,
                    read_timeout=UPLOAD_TIMEOUT,
                    write_timeout=UPLOAD_TIMEOUT,
                ))

        return await with_retry(action, "send_media_group")

