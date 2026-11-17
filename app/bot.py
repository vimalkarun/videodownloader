"""Telegram handlers, authorization, status messages and the per-job pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
import time
from pathlib import Path

import httpx
from telegram import BotCommand, Message, MessageEntity, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .config import Settings
from .downloader import USER_MESSAGES, Downloader, DownloadError, ErrorKind
from .job_queue import DownloadQueue, Job, JobState, QueueClosed
from .telegram_sender import UPLOAD_TIMEOUT, TelegramSender
from .url_resolver import SUPPORTED_SITES_TEXT, UnsupportedUrl, classify, extract_urls, resolve

log = logging.getLogger(__name__)

MAX_LINKS_PER_MESSAGE = 10
STATUS_EDIT_INTERVAL = 3.0  # seconds between progress edits (Telegram flood limits)

COMMANDS = [
    BotCommand("start", "Usage and supported platforms"),
    BotCommand("help", "Tips for sharing links from each app"),
    BotCommand("status", "Queue length and active jobs"),
    BotCommand("cancel", "Cancel my queued and active jobs"),
]

START_TEXT = (
    "👋 Send me a link (or share one from the app) and I'll send the video back here.\n\n"
    f"Supported: {SUPPORTED_SITES_TEXT}.\n"
    "Several links in one message become separate jobs. Carousels, galleries and "
    "multi-video posts arrive as an album.\n\n"
    "Nothing is stored on the server: each file is deleted right after it's uploaded.\n\n"
    "/status · /cancel · /help"
)

HELP_TEXT = (
    "Tips\n\n"
    "• Instagram: on a Reel or post tap ➤ Share → Copy link (or share straight to this bot). "
    "Private accounts and some Reels need cookies — see the README.\n"
    "• X / Twitter: Share → Copy link, or Share via… → Telegram → this bot. "
    "x.com and twitter.com both work; posts with several videos come back as an album.\n"
    "• YouTube: Share → Copy link. Shorts and youtu.be links work; playlist parts of a link are ignored "
    "and only that one video is downloaded.\n"
    "• Reddit: Share → Copy link. Short links (redd.it) and share links (/s/…) are expanded automatically. "
    "Galleries with videos come back as an album. Posts that only link elsewhere (YouTube, Streamable, …) "
    "are downloaded from that site if possible.\n\n"
    "Videos over 50 MB are sent at lower quality or re-compressed to fit Telegram's limit. "
    "Use /cancel to stop your jobs."
)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def is_authorized(update: Update, settings: Settings) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return False
    if chat.type == ChatType.PRIVATE:
        return user is not None and user.id in settings.allowed_user_ids
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return (chat.id in settings.allowed_chat_ids
                and user is not None and user.id in settings.allowed_user_ids)
    if chat.type == ChatType.CHANNEL:
        # Channel posts have no sender user; the channel itself must be allowed.
        return chat.id in settings.allowed_chat_ids
    return False


JOB_DIR_PREFIX = "job-"


def wipe_work_dir(work_dir: Path) -> int:
    """Delete per-job temp folders left over from a crash.

    Only ``job-*`` entries (the ones this bot creates) are touched, so a
    misconfigured ``WORK_DIR=/tmp`` can't wipe unrelated files.
    Returns the number of entries removed."""
    work_dir.mkdir(parents=True, exist_ok=True)
    removed = 0
    for child in work_dir.iterdir():
        if not child.name.startswith(JOB_DIR_PREFIX):
            continue
        with contextlib.suppress(FileNotFoundError):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Status message (throttled edits)
# ---------------------------------------------------------------------------

class StatusMessage:
    def __init__(self, app: Application, job: Job):
        self.app = app
        self.job = job
        self._latest: str | None = None
        self._shown: str | None = None
        self._last_edit = 0.0
        self._flush_task: asyncio.Task[None] | None = None

    def push(self, text: str) -> None:
        """Schedule a (throttled) edit to ``text``. Safe to call often."""
        self._latest = text
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        wait = STATUS_EDIT_INTERVAL - (time.monotonic() - self._last_edit)
        if wait > 0:
            await asyncio.sleep(wait)
        if self._latest is not None:
            await self._edit(self._latest)

    async def set(self, text: str) -> None:
        """Edit immediately (used for state transitions and the final text)."""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._latest = text
        await self._edit(text)

    async def _edit(self, text: str) -> None:
        if self.job.status_message_id is None or text == self._shown:
            return
        self._last_edit = time.monotonic()
        try:
            await self.app.bot.edit_message_text(
                chat_id=self.job.chat_id, message_id=self.job.status_message_id, text=text
            )
            self._shown = text
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.debug("status edit failed: %s", exc)
        except TelegramError as exc:
            log.debug("status edit failed: %s", type(exc).__name__)

    async def delete(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        if self.job.status_message_id is None:
            return
        with contextlib.suppress(TelegramError):
            await self.app.bot.delete_message(self.job.chat_id, self.job.status_message_id)


def _downloading_text(progress: str) -> tuple[JobState, str]:
    """Map the downloader's progress strings to a state and status text."""
    progress = progress.removesuffix("downloading").strip()
    if progress.endswith("processing"):
        return JobState.PROCESSING, f"⚙️ Processing… {progress.removesuffix('processing').strip()}".strip()
    return JobState.DOWNLOADING, f"⬇️ Downloading… {progress}".strip()


# ---------------------------------------------------------------------------
# The bot
# ---------------------------------------------------------------------------

class VideoBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.downloader = Downloader(settings)
        self.queue = DownloadQueue(self.run_job, settings.max_concurrent_jobs,
                                   on_queued_cancel=self._notify_queued_cancel)
        self.http: httpx.AsyncClient | None = None
        self.sender: TelegramSender | None = None
        self.app: Application = self._build_application()

    # -- setup -----------------------------------------------------------------

    def _build_application(self) -> Application:
        s = self.settings
        builder = (
            ApplicationBuilder()
            .token(s.bot_token)
            .concurrent_updates(True)
            .connect_timeout(20)
            .read_timeout(30)
            .write_timeout(30)
            .media_write_timeout(UPLOAD_TIMEOUT)
            .post_init(self._post_init)
            .post_stop(self._post_stop)
            .post_shutdown(self._post_shutdown)
        )
        if s.telegram_api_url:
            # Self-hosted telegram-bot-api server: same HTTP API, 2000 MB
            # uploads. We still upload over HTTP (not local_mode file paths),
            # so the two containers don't need to share a filesystem.
            builder = builder.base_url(f"{s.telegram_api_url}/bot").base_file_url(f"{s.telegram_api_url}/file/bot")
        app = builder.build()

        # Gate everything first: unauthorized updates stop here, silently.
        app.add_handler(TypeHandler(Update, self._gate), group=-1)

        msg_filter = filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST
        app.add_handler(CommandHandler(["start"], self.cmd_start, filters=msg_filter))
        app.add_handler(CommandHandler(["help"], self.cmd_help, filters=msg_filter))
        app.add_handler(CommandHandler(["status"], self.cmd_status, filters=msg_filter))
        app.add_handler(CommandHandler(["cancel"], self.cmd_cancel, filters=msg_filter))
        app.add_handler(MessageHandler(
            msg_filter & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, self.on_message
        ))
        app.add_error_handler(self._on_error)
        return app

    async def _post_init(self, app: Application) -> None:
        removed = wipe_work_dir(self.settings.work_dir)
        if removed:
            log.info("startup: removed %d leftover temp entr%s", removed, "y" if removed == 1 else "ies")
        self.http = httpx.AsyncClient(
            headers={"User-Agent": self.settings.reddit_user_agent}, timeout=15
        )
        per_request = None if self.settings.uses_local_api else self.settings.upload_limit_bytes
        self.sender = TelegramSender(app.bot, self.settings.upload_limit_bytes, per_request)
        with contextlib.suppress(TelegramError):
            await app.bot.set_my_commands(COMMANDS)
        self.queue.start()
        log.info("bot ready: concurrency=%d timeout=%ss limit=%dMB local_api=%s",
                 self.settings.max_concurrent_jobs, self.settings.job_timeout_seconds,
                 self.settings.upload_limit_bytes // (1024 * 1024), self.settings.uses_local_api)

    async def _post_stop(self, app: Application) -> None:
        # Bot is still usable here, so cancelled jobs can update their status.
        await self.queue.shutdown(self.settings.shutdown_grace_seconds)

    async def _post_shutdown(self, app: Application) -> None:
        if self.http:
            await self.http.aclose()
        wipe_work_dir(self.settings.work_dir)

    async def _gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update, self.settings):
            raise ApplicationHandlerStop

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Type only: exception messages can contain URLs.
        log.error("handler error: %s", type(context.error).__name__)

    # -- commands --------------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(START_TEXT, disable_web_page_preview=True)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP_TEXT, disable_web_page_preview=True)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pending, active = self.queue.pending, self.queue.active
        lines = [f"📊 Queue: {len(pending)} waiting · {len(active)} active "
                 f"(max {self.settings.max_concurrent_jobs})"]
        now = time.monotonic()
        for job in active:
            elapsed = now - (job.started_at or now)
            lines.append(f"• {job.platform.value} — {job.state.value} {job.detail} ({elapsed:.0f}s)".rstrip())
        for i, job in enumerate(pending, start=1):
            lines.append(f"• #{i} {job.platform.value} — queued")
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        queued, running = await self.queue.cancel_for(user.id if user else None, update.effective_chat.id)
        if queued == running == 0:
            text = "Nothing to cancel."
        else:
            text = f"✖️ Cancelled {queued} queued and {running} active job(s)."
        await update.effective_message.reply_text(text)

    # -- links -----------------------------------------------------------------

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        urls = urls_from_message(msg)
        is_private = update.effective_chat.type == ChatType.PRIVATE

        if not urls:
            if is_private:
                await msg.reply_text(f"Send me a link from {SUPPORTED_SITES_TEXT}.")
            return

        supported = [u for u in urls if classify(u) is not None]
        unsupported = [u for u in urls if classify(u) is None]
        if unsupported and (is_private or supported):
            listed = "\n".join(f"• {u}" for u in unsupported[:5])
            await msg.reply_text(
                f"🚫 Not a supported link:\n{listed}\n\nSupported: {SUPPORTED_SITES_TEXT}.",
                disable_web_page_preview=True,
            )

        if len(supported) > MAX_LINKS_PER_MESSAGE:
            await msg.reply_text(f"Only the first {MAX_LINKS_PER_MESSAGE} links will be downloaded.")
            supported = supported[:MAX_LINKS_PER_MESSAGE]

        user = update.effective_user
        for url in supported:
            platform = classify(url)
            assert platform is not None
            job = Job(url=url, platform=platform, chat_id=msg.chat_id, message_id=msg.message_id,
                      user_id=user.id if user else None)
            waiting = len(self.queue.pending) + len(self.queue.active) - self.settings.max_concurrent_jobs + 1
            text = f"⏳ Downloading… (queued, #{waiting})" if waiting > 0 else "⏳ Downloading…"
            try:
                status = await msg.reply_text(text, do_quote=True)
                job.status_message_id = status.message_id
            except TelegramError:
                pass
            try:
                self.queue.submit(job)
            except QueueClosed:
                await self._final(StatusMessage(self.app, job), "⚠️ Bot is shutting down, send the link again later.")
                return
            log.info("job=%s platform=%s status=queued", job.id, job.platform.value)

    async def _notify_queued_cancel(self, job: Job) -> None:
        await StatusMessage(self.app, job).set(USER_MESSAGES[ErrorKind.CANCELLED])

    async def _final(self, status: StatusMessage, text: str) -> None:
        await status.set(text)

    # -- the pipeline ----------------------------------------------------------

    async def run_job(self, job: Job) -> None:
        assert self.sender is not None
        status = StatusMessage(self.app, job)
        started = time.monotonic()
        total_size = 0
        outcome = "failed"
        final_text: str | None = None

        def on_progress(text: str) -> None:
            job.state, rendered = _downloading_text(text)
            job.detail = text if job.state is JobState.DOWNLOADING else ""
            status.push(rendered)

        try:
            async with asyncio.timeout(self.settings.job_timeout_seconds):
                job.state = JobState.DOWNLOADING
                await status.set("⬇️ Downloading…")
                target = await resolve(job.url, user_agent=self.settings.reddit_user_agent, client=self.http)

                with tempfile.TemporaryDirectory(prefix=JOB_DIR_PREFIX, dir=self.settings.work_dir,
                                                 ignore_cleanup_errors=True) as tmp:
                    result = await self.downloader.download(
                        target, Path(tmp), progress=on_progress, cancel=job.cancel_event
                    )
                    total_size = sum(v.size for v in result.videos)
                    job.state, job.detail = JobState.UPLOADING, ""
                    await status.set("⬆️ Uploading…")
                    await self.sender.send(
                        job.chat_id, job.message_id, result,
                        on_progress=lambda t: status.push(f"⬆️ Uploading… {t}"),
                    )
                # <- temp dir and everything in it is gone here

            job.state = JobState.DONE
            outcome = "done"
            if result.notes:
                final_text = "✅ Done\n" + "\n".join(result.notes)
            elif self.settings.delete_status_messages:
                await status.delete()
            else:
                final_text = "✅ Done"
        except UnsupportedUrl:
            final_text = USER_MESSAGES[ErrorKind.UNSUPPORTED]
        except DownloadError as exc:
            outcome = exc.kind.value
            final_text = exc.user_message
            if exc.kind is ErrorKind.CANCELLED:
                job.state = JobState.CANCELLED
        except TimeoutError:
            outcome = "timeout"
            job.cancel_event.set()
            final_text = USER_MESSAGES[ErrorKind.TIMEOUT]
        except asyncio.CancelledError:
            # /cancel or shutdown. Report it, then finish quietly: nobody
            # awaits this task's result.
            outcome = "cancelled"
            job.state = JobState.CANCELLED
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            final_text = USER_MESSAGES[ErrorKind.CANCELLED]
        except TelegramError as exc:
            outcome = "upload_error"
            final_text = f"⚠️ Upload to Telegram failed ({type(exc).__name__}). Try again."
        except Exception as exc:  # noqa: BLE001 - last-resort reporting
            log.exception("job=%s unexpected %s", job.id, type(exc).__name__)
            final_text = USER_MESSAGES[ErrorKind.GENERIC]
        finally:
            if job.state is not JobState.DONE and job.state is not JobState.CANCELLED:
                job.state = JobState.FAILED
            log.info("job=%s platform=%s status=%s size=%d duration=%.1fs",
                     job.id, job.platform.value, outcome, total_size, time.monotonic() - started)

        if final_text:
            with contextlib.suppress(Exception):
                await asyncio.shield(status.set(final_text))


def urls_from_message(msg: Message) -> list[str]:
    """All URLs in a message's text/caption, including hidden text links."""
    text = msg.text or msg.caption or ""
    extra: list[str] = []
    for ent in (msg.entities or ()) + (msg.caption_entities or ()):
        if ent.type == MessageEntity.TEXT_LINK and ent.url:
            extra.append(ent.url)
    parsed = msg.parse_entities([MessageEntity.URL]) if msg.text else msg.parse_caption_entities([MessageEntity.URL])
    extra.extend(parsed.values())
    return extract_urls(text, extra)
