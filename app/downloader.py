"""Download videos with yt-dlp (used as a library) into a per-job temp dir.

Flow for one URL:

1. ``extract`` (in a worker thread): yt-dlp resolves the post into one or
   more video entries (carousels, galleries and multi-video tweets become
   several entries) and picks formats, without downloading anything.
2. For each entry we estimate the download size. If it's over Telegram's
   upload limit we pick a smaller format ourselves (``pick_formats``) and,
   if the video is so long it can't fit at watchable quality even after
   re-encoding, fail fast *before* downloading.
3. Download the entry (again in a thread). Reddit downloads video and audio
   as separate files that we merge with ffmpeg ourselves.
4. Post-process: ensure H.264/AAC MP4, compress if still too big, make a
   thumbnail.

Every file lives under the ``workdir`` the caller owns and deletes.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from . import media
from .config import Settings
from .url_resolver import Platform, ResolvedUrl

log = logging.getLogger(__name__)

MAX_ITEMS_PER_POST = 20

# Prefer streams Telegram plays inline (H.264 video + AAC audio in MP4) so
# no re-encode is needed; fall back to anything, which media.py then fixes.
PREFERRED_FORMAT = (
    "bv*[vcodec^=avc1][ext=mp4]+ba[acodec^=mp4a]"
    "/b[ext=mp4][vcodec^=avc1][acodec^=mp4a]"
    "/bv*[vcodec^=avc1]+ba"
    "/bv*+ba/b/bv*"
)
# Reddit: fetch the best video and best audio as two separate files (the ","
# operator) and merge them ourselves; when the post has no audio track the
# "ba" half simply selects nothing instead of failing.
REDDIT_FORMAT = "bv*[vcodec^=avc1]/bv*/b,ba[acodec^=mp4a]/ba"

# Fallback when we can't estimate sizes from metadata (spec: best[filesize<50M]).
def size_capped_format(limit: int) -> str:
    mb = max(1, int(limit / 1024 / 1024 * 0.97))
    return f"b[filesize<{mb}M]/b[filesize_approx<{mb}M]/bv*[filesize<{mb}M]+ba/w/wv*+wa"


class ErrorKind(enum.Enum):
    PRIVATE = "private"
    LOGIN = "login"
    UNSUPPORTED = "unsupported"
    NO_VIDEO = "no_video"
    TOO_LARGE = "too_large"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    RATE_LIMIT = "rate_limit"
    GENERIC = "generic"


USER_MESSAGES = {
    ErrorKind.PRIVATE: "🔒 This post is private, deleted or unavailable.",
    ErrorKind.LOGIN: "🔑 This platform requires login for this post. Add a cookies file (see README).",
    ErrorKind.UNSUPPORTED: "🚫 This link isn't supported (or the post links to a site yt-dlp can't download).",
    ErrorKind.NO_VIDEO: "🖼 No video found in this post.",
    ErrorKind.TOO_LARGE: "📦 The video is too large to send via Telegram.",
    ErrorKind.TIMEOUT: "⏱ Timed out. Try again later or raise JOB_TIMEOUT_SECONDS.",
    ErrorKind.CANCELLED: "✖️ Cancelled.",
    ErrorKind.RATE_LIMIT: "🐢 The platform is rate-limiting this server. Try again later or add cookies.",
    ErrorKind.GENERIC: "⚠️ Download failed.",
}


class DownloadError(Exception):
    def __init__(self, kind: ErrorKind, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind.value}: {detail}")

    @property
    def user_message(self) -> str:
        msg = USER_MESSAGES[self.kind]
        detail = clean_error_detail(self.detail)
        if self.kind in (ErrorKind.TOO_LARGE, ErrorKind.GENERIC) and detail:
            msg += f"\n{detail[:300]}"
        return msg


# Ordered: first match wins.
_ERROR_PATTERNS: list[tuple[ErrorKind, re.Pattern[str]]] = [
    (ErrorKind.NO_VIDEO, re.compile(
        r"no video (could be )?found|no media found|there's no video|does not contain (a )?video|"
        r"no video formats found|this post does not contain|no formats found|only images", re.I)),
    (ErrorKind.RATE_LIMIT, re.compile(r"rate.?limit|too many requests|http error 429", re.I)),
    (ErrorKind.LOGIN, re.compile(
        r"login|log in|sign in|cookies|authentication|age.?restrict|confirm your age|"
        r"inappropriate for some users|nsfw|quarantined|account .*required", re.I)),
    (ErrorKind.PRIVATE, re.compile(
        r"private|not available|unavailable|removed|deleted|does not exist|doesn't exist|"
        r"not found|http error 404|http error 403|suspended|copyright", re.I)),
    (ErrorKind.UNSUPPORTED, re.compile(r"unsupported url|no suitable extractor", re.I)),
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def clean_error_detail(message: str) -> str:
    """Strip yt-dlp boilerplate ("ERROR: ", "please report this issue ...")."""
    message = _ANSI_RE.sub("", message or "").strip()
    message = re.sub(r"^ERROR:\s*", "", message)
    message = re.split(r";\s*please report this issue", message, maxsplit=1, flags=re.I)[0]
    message = re.split(r"\s*\(caused by ", message, maxsplit=1)[0]
    return message.strip()


def classify_error(message: str) -> ErrorKind:
    message = _ANSI_RE.sub("", message or "")
    for kind, pattern in _ERROR_PATTERNS:
        if pattern.search(message):
            return kind
    return ErrorKind.GENERIC


# ---------------------------------------------------------------------------
# Size estimation / format picking (pure, unit tested)
# ---------------------------------------------------------------------------

def estimate_format_size(fmt: dict[str, Any], duration: float | None) -> int | None:
    for key in ("filesize", "filesize_approx"):
        if fmt.get(key):
            return int(fmt[key])
    tbr = fmt.get("tbr") or ((fmt.get("vbr") or 0) + (fmt.get("abr") or 0)) or None
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None


def estimate_entry_size(entry: dict[str, Any], separate: bool = False) -> int | None:
    """Estimate the size of the formats yt-dlp selected for ``entry``.

    With ``separate`` (Reddit's "video,audio" selection) yt-dlp doesn't
    record a combined selection, so estimate best video + best audio.
    """
    duration = entry.get("duration")
    if separate:
        formats = entry.get("formats") or []
        videos = [f for f in formats if _has_video(f)]
        audios = [f for f in formats if _has_audio(f) and f.get("vcodec") == "none"]
        if not videos:
            return None
        best_v = max(videos, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        sizes = [estimate_format_size(best_v, duration)]
        if audios:
            sizes.append(estimate_format_size(max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0),
                                              duration))
        return None if any(x is None for x in sizes) else sum(x for x in sizes if x is not None)
    requested = entry.get("requested_formats")
    if requested:
        sizes = [estimate_format_size(f, duration) for f in requested]
        if any(s is None for s in sizes):
            return None
        return sum(s for s in sizes if s is not None)
    return estimate_format_size(entry, duration)


def _has_video(f: dict[str, Any]) -> bool:
    return f.get("vcodec") not in (None, "none") or (f.get("vcodec") is None and f.get("acodec") is None
                                                       and f.get("ext") in ("mp4", "webm", "mov", "mkv"))


def _has_audio(f: dict[str, Any]) -> bool:
    return f.get("acodec") not in (None, "none")


def _is_avc(f: dict[str, Any]) -> bool:
    return str(f.get("vcodec") or "").startswith(("avc", "h264"))


def pick_formats(formats: list[dict[str, Any]], duration: float | None, limit: int,
                 separate: bool = False, safety: float = 0.97) -> str | None:
    """Pick the best-quality format (or video+audio pair) estimated to fit
    under ``limit`` bytes. Returns a yt-dlp format spec, or ``None`` if no
    candidate with a known size fits.

    Preference: H.264 (inline-playable, no re-encode) > higher resolution >
    higher bitrate. ``separate`` makes the result use "," instead of "+"
    (download both, merge ourselves — used for Reddit).
    """
    budget = limit * safety
    audio_only = [f for f in formats if _has_audio(f) and f.get("vcodec") == "none"]
    sized_audio = [(estimate_format_size(a, duration), a) for a in audio_only]
    sized_audio = [(s, a) for s, a in sized_audio if s is not None]
    # Smallest AAC first is a fine trade: audio is a tiny slice of the budget.
    sized_audio.sort(key=lambda sa: (not str(sa[1].get("acodec", "")).startswith("mp4a"), sa[0]))
    audio = sized_audio[0] if sized_audio else None

    candidates: list[tuple[tuple, str, int]] = []
    for f in formats:
        if not _has_video(f) or not f.get("format_id"):
            continue
        size = estimate_format_size(f, duration)
        if size is None:
            continue
        if _has_audio(f):
            spec, total = f["format_id"], size
        elif audio is not None:
            joiner = "," if separate else "+"
            spec, total = f"{f['format_id']}{joiner}{audio[1]['format_id']}", size + audio[0]
        elif not audio_only:
            spec, total = f["format_id"], size  # genuinely silent video
        else:
            continue
        if total > budget:
            continue
        rank = (_is_avc(f), f.get("height") or 0, f.get("tbr") or 0, -total)
        candidates.append((rank, spec, total))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class DownloadedVideo:
    path: Path
    title: str
    duration: float
    width: int | None
    height: int | None
    thumbnail: Path | None = None

    @property
    def size(self) -> int:
        return self.path.stat().st_size


@dataclass
class DownloadResult:
    platform: Platform
    title: str
    source_url: str
    videos: list[DownloadedVideo] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


ProgressFn = Callable[[str], None]


class _SilentLogger:
    """yt-dlp logger that drops everything: errors still reach us as
    exceptions, and we never want URLs / titles in the container logs."""

    def debug(self, msg: str) -> None: ...
    def info(self, msg: str) -> None: ...
    def warning(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...


def _fmt_bytes(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class Downloader:
    def __init__(self, settings: Settings):
        self.settings = settings

    # -- yt-dlp option building --------------------------------------------

    def _cookie_source(self, platform: Platform) -> Path | None:
        return {
            Platform.INSTAGRAM: self.settings.instagram_cookies_file,
            Platform.REDDIT: self.settings.reddit_cookies_file,
            Platform.YOUTUBE: self.settings.youtube_cookies_file,
            Platform.X: self.settings.x_cookies_file,
        }.get(platform)

    def _base_opts(self, platform: Platform, workdir: Path, cancel: threading.Event,
                   progress: ProgressFn) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _SilentLogger(),
            "noplaylist": True,          # YouTube: never expand list=
            "cachedir": False,           # nothing in ~/.cache
            "paths": {"home": str(workdir), "temp": str(workdir)},
            "restrictfilenames": True,
            "merge_output_format": "mp4",
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 4,
            "overwrites": True,
            "writethumbnail": False,
            "writesubtitles": False,
            "check_formats": False,
            # Hard ceiling so a surprise 4K hour-long video can't fill tmpfs.
            "max_filesize": self.settings.upload_limit_bytes * (1 if self.settings.uses_local_api else 8),
        }
        if platform is Platform.REDDIT:
            opts["http_headers"] = {"User-Agent": self.settings.reddit_user_agent}

        src = self._cookie_source(platform)
        if src is not None:
            # yt-dlp *writes back* to its cookiefile on exit. Give it a
            # private copy inside the job dir so the read-only mounted
            # original is never touched and the copy dies with the job.
            dst = workdir / "cookies.txt"
            shutil.copyfile(src, dst)
            opts["cookiefile"] = str(dst)

        last = {"t": 0.0}

        def hook(d: dict[str, Any]) -> None:
            if cancel.is_set():
                raise DownloadCancelled()
            if d.get("status") != "downloading":
                return
            now = time.monotonic()
            if now - last["t"] < 1.0:
                return
            last["t"] = now
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(f"{min(100, done * 100 / total):.0f}% of {_fmt_bytes(total)}")
            else:
                progress(_fmt_bytes(done))

        def pp_hook(d: dict[str, Any]) -> None:
            if cancel.is_set():
                raise DownloadCancelled()

        opts["progress_hooks"] = [hook]
        opts["postprocessor_hooks"] = [pp_hook]
        return opts

    # -- thread helpers -------------------------------------------------------

    @staticmethod
    async def _in_thread(fn: Callable[[], Any], cancel: threading.Event) -> Any:
        """Run blocking yt-dlp code in a thread, cooperatively cancellable.

        A thread can't be killed, so on cancellation we set ``cancel`` (the
        progress hook raises at the next chunk) and wait briefly for the
        thread to stop writing before the caller deletes the temp dir.
        """
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, fn)
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            cancel.set()
            with contextlib.suppress(Exception):
                await asyncio.wait({fut}, timeout=30)
            if fut.done() and not fut.cancelled():
                fut.exception()  # mark retrieved
            raise

    # -- main entry point -----------------------------------------------------

    async def download(self, target: ResolvedUrl, workdir: Path, *,
                       progress: ProgressFn, cancel: threading.Event) -> DownloadResult:
        loop = asyncio.get_running_loop()

        def threadsafe_progress(text: str) -> None:
            loop.call_soon_threadsafe(progress, text)

        opts = self._base_opts(target.platform, workdir, cancel, threadsafe_progress)
        separate = target.platform is Platform.REDDIT
        opts["format"] = REDDIT_FORMAT if separate else PREFERRED_FORMAT

        def extract() -> dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target.url, download=False)
                return ydl.sanitize_info(info) if info else {}

        progress("fetching info")
        try:
            info = await self._in_thread(extract, cancel)
        except DownloadCancelled as exc:
            raise DownloadError(ErrorKind.CANCELLED) from exc
        except yt_dlp.utils.YoutubeDLError as exc:
            raise DownloadError(classify_error(str(exc)), str(exc)) from exc

        entries = self._video_entries(info)
        if not entries:
            raise DownloadError(ErrorKind.NO_VIDEO)

        result = DownloadResult(
            platform=target.platform,
            title=_title(info) or _title(entries[0]) or "",
            source_url=target.original,
        )
        if len(entries) > MAX_ITEMS_PER_POST:
            result.notes.append(f"Post has {len(entries)} videos; sending the first {MAX_ITEMS_PER_POST}.")
            entries = entries[:MAX_ITEMS_PER_POST]

        errors: list[DownloadError] = []
        for idx, entry in enumerate(entries, start=1):
            prefix = f"[{idx}/{len(entries)}] " if len(entries) > 1 else ""

            def entry_progress(text: str, _p: str = prefix) -> None:
                progress(_p + text)

            try:
                video = await self._download_entry(entry, idx, workdir, opts, separate, entry_progress, cancel)
                result.videos.append(video)
            except DownloadError as exc:
                if exc.kind is ErrorKind.CANCELLED or len(entries) == 1:
                    raise
                errors.append(exc)
                result.notes.append(f"Item {idx} skipped: {USER_MESSAGES[exc.kind]}")

        if not result.videos:
            raise errors[0] if errors else DownloadError(ErrorKind.NO_VIDEO)
        return result

    @staticmethod
    def _video_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
        if not info:
            return []
        if info.get("_type") in ("playlist", "multi_video"):
            raw = [e for e in (info.get("entries") or []) if e]
        else:
            raw = [info]
        out = []
        for e in raw:
            fmts = e.get("formats") or ([e] if e.get("url") else [])
            if any(_has_video(f) for f in fmts):
                out.append(e)
        return out

    async def _download_entry(self, entry: dict[str, Any], idx: int, workdir: Path,
                              base_opts: dict[str, Any], separate: bool,
                              progress: ProgressFn, cancel: threading.Event) -> DownloadedVideo:
        limit = self.settings.upload_limit_bytes
        duration = float(entry.get("duration") or 0)
        fmt = base_opts["format"]

        estimate = estimate_entry_size(entry, separate)
        if estimate is not None and estimate > limit:
            picked = pick_formats(entry.get("formats") or [], duration or None, limit, separate=separate)
            if picked:
                fmt = picked
            elif self.settings.uses_local_api:
                raise DownloadError(ErrorKind.TOO_LARGE,
                                    f"About {_fmt_bytes(estimate)}; the limit is {_fmt_bytes(limit)}.")
            else:
                # Nothing fits as-is: fail now if even re-encoding can't make
                # it fit, otherwise take the smallest and compress later.
                if duration:
                    try:
                        media.plan_compression(duration, limit, has_audio=True)
                    except media.TooLargeError as exc:
                        raise DownloadError(ErrorKind.TOO_LARGE, str(exc)) from exc
                fmt = size_capped_format(limit)

        opts = dict(base_opts)
        opts["format"] = fmt
        opts["outtmpl"] = {"default": f"{idx:02d}-%(id).40B-%(format_id).30B.%(ext)s"}

        def run() -> dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.process_ie_result(dict(entry), download=True)

        progress("downloading")
        try:
            done = await self._in_thread(run, cancel)
        except DownloadCancelled as exc:
            raise DownloadError(ErrorKind.CANCELLED) from exc
        except yt_dlp.utils.YoutubeDLError as exc:
            msg = str(exc)
            if "max_filesize" in msg or "larger than max-filesize" in msg.lower():
                raise DownloadError(ErrorKind.TOO_LARGE, "The source file is far above the upload limit.") from exc
            raise DownloadError(classify_error(msg), msg) from exc
        if cancel.is_set():
            raise DownloadError(ErrorKind.CANCELLED)

        files = [Path(d["filepath"]) for d in (done.get("requested_downloads") or []) if d.get("filepath")]
        files = [f for f in files if f.exists()]
        if not files:
            raise DownloadError(ErrorKind.NO_VIDEO, "yt-dlp produced no file")

        progress("processing")
        path = await self._postprocess(files, idx, workdir, separate, limit)
        info = await media.probe(path)
        thumb = await media.make_thumbnail(path, workdir / f"{idx:02d}-thumb.jpg", info.duration)
        return DownloadedVideo(
            path=path,
            title=_title(entry) or "",
            duration=info.duration or duration,
            width=info.width,
            height=info.height,
            thumbnail=thumb,
        )

    async def _postprocess(self, files: list[Path], idx: int, workdir: Path,
                           separate: bool, limit: int) -> Path:
        try:
            if separate or len(files) > 1:
                # Reddit-style split streams: find the video file and the
                # audio file and merge. No audio file -> video only.
                probes = [(f, await media.probe(f)) for f in files]
                video = next((f for f, p in probes if p.has_video), None)
                audio = next((f for f, p in probes if p.has_audio and not p.has_video), None)
                if video is None:
                    raise DownloadError(ErrorKind.NO_VIDEO)
                if audio is None and len(files) == 1:
                    path = video
                else:
                    path = await media.merge_audio_video(video, audio, workdir / f"{idx:02d}-merged.mp4")
            else:
                path = files[0]

            info = await media.probe(path)
            if not info.has_video:
                raise DownloadError(ErrorKind.NO_VIDEO)
            path = await media.make_telegram_compatible(path, workdir / f"{idx:02d}-tg.mp4", info)

            size = path.stat().st_size
            if size > limit:
                if self.settings.uses_local_api:
                    raise DownloadError(ErrorKind.TOO_LARGE,
                                        f"{_fmt_bytes(size)}; the limit is {_fmt_bytes(limit)}.")
                info = await media.probe(path)
                log.info("compressing item=%d size=%d limit=%d duration=%.0f", idx, size, limit, info.duration)
                path = await media.compress_to_limit(path, workdir / f"{idx:02d}-small.mp4", info, limit)
            return path
        except media.TooLargeError as exc:
            raise DownloadError(ErrorKind.TOO_LARGE, str(exc)) from exc
        except media.MediaError as exc:
            raise DownloadError(ErrorKind.GENERIC, f"Video processing failed: {exc}") from exc


def _title(d: dict[str, Any]) -> str:
    for key in ("title", "fulltitle", "description"):
        val = d.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().splitlines()[0][:200]
    return ""
