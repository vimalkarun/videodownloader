"""ffmpeg / ffprobe helpers: probing, Telegram-compatibility fixes, merging,
size-targeted compression and thumbnails.

All subprocesses are asyncio subprocesses so a cancelled job kills ffmpeg
immediately instead of leaving it writing into a directory we're deleting.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path

# Telegram plays H.264/AAC MP4 inline on every client.
COMPATIBLE_VCODECS = {"h264"}
COMPATIBLE_ACODECS = {"aac", "mp3"}

# Leave headroom for container overhead and the multipart request.
SIZE_SAFETY = 0.94
MIN_VIDEO_KBPS = 120   # below this the result is unwatchable -> give up
MAX_AUDIO_KBPS = 128
MIN_AUDIO_KBPS = 32


class MediaError(RuntimeError):
    pass


class TooLargeError(MediaError):
    def __init__(self, size: int, limit: int, reason: str = ""):
        self.size = size
        self.limit = limit
        super().__init__(reason or f"{size} bytes exceeds limit of {limit} bytes")


@dataclass(frozen=True)
class ProbeResult:
    duration: float
    width: int | None
    height: int | None
    vcodec: str | None
    acodec: str | None
    format_name: str

    @property
    def has_video(self) -> bool:
        return self.vcodec is not None

    @property
    def has_audio(self) -> bool:
        return self.acodec is not None

    @property
    def is_mp4(self) -> bool:
        return "mp4" in self.format_name.split(",")

    @property
    def telegram_compatible(self) -> bool:
        return (
            self.is_mp4
            and self.vcodec in COMPATIBLE_VCODECS
            and (self.acodec is None or self.acodec in COMPATIBLE_ACODECS)
        )


async def _run(*args: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    return proc.returncode or 0, out, err


async def probe(path: Path) -> ProbeResult:
    code, out, err = await _run(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)
    )
    if code != 0:
        raise MediaError(f"ffprobe failed: {err.decode(errors='replace')[-300:]}")
    data = json.loads(out or b"{}")
    vstream = next(
        (s for s in data.get("streams", [])
         if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    astream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    duration = 0.0
    for candidate in (fmt.get("duration"), (vstream or {}).get("duration"), (astream or {}).get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    return ProbeResult(
        duration=duration,
        width=(vstream or {}).get("width"),
        height=(vstream or {}).get("height"),
        vcodec=(vstream or {}).get("codec_name"),
        acodec=(astream or {}).get("codec_name"),
        format_name=fmt.get("format_name", ""),
    )


async def _ffmpeg(*args: str) -> None:
    code, _, err = await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args)
    if code != 0:
        raise MediaError(f"ffmpeg failed: {err.decode(errors='replace')[-500:]}")


async def merge_audio_video(video: Path, audio: Path | None, out: Path) -> Path:
    """Mux a video-only and an audio-only stream (Reddit DASH) into one MP4.

    If ``audio`` is ``None`` or contains no audio stream (Reddit GIF-style
    posts) the video is remuxed on its own instead of failing.
    """
    inputs = ["-i", str(video)]
    maps = ["-map", "0:v:0"]
    if audio is not None and audio.exists():
        info = await probe(audio)
        if info.has_audio:
            inputs += ["-i", str(audio)]
            maps += ["-map", "1:a:0"]
    vinfo = await probe(video)
    vcodec = ["-c:v", "copy"] if vinfo.vcodec in COMPATIBLE_VCODECS else _x264_args()
    acodec = ["-c:a", "aac", "-b:a", "128k"] if len(maps) > 2 else []
    await _ffmpeg(*inputs, *maps, *vcodec, *acodec, "-movflags", "+faststart", "-shortest", str(out))
    return out


def _x264_args(preset: str = "veryfast", crf: int = 23) -> list[str]:
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-profile:v", "high"]


async def make_telegram_compatible(src: Path, out: Path, info: ProbeResult) -> Path:
    """Remux / transcode so Telegram can play the file inline.

    Copies streams that are already H.264/AAC and only re-encodes what isn't,
    so the common case (yt-dlp gave us H.264+AAC in a WebM/MKV) is a fast
    remux.
    """
    if not info.has_video:
        raise MediaError("no video stream")
    if info.telegram_compatible:
        return src
    vcodec = ["-c:v", "copy"] if info.vcodec in COMPATIBLE_VCODECS else _x264_args()
    if info.has_audio:
        acodec = ["-c:a", "copy"] if info.acodec in COMPATIBLE_ACODECS else ["-c:a", "aac", "-b:a", "128k"]
        amap = ["-map", "0:a:0"]
    else:
        acodec, amap = [], []
    await _ffmpeg("-i", str(src), "-map", "0:v:0", *amap, *vcodec, *acodec, "-movflags", "+faststart", str(out))
    return out


@dataclass(frozen=True)
class CompressionPlan:
    video_kbps: int
    audio_kbps: int
    max_height: int | None


def plan_compression(duration: float, limit_bytes: int, has_audio: bool,
                     source_height: int | None = None, safety: float = SIZE_SAFETY) -> CompressionPlan:
    """Work out bitrates so ``duration`` seconds fit in ``limit_bytes``.

    Raises ``TooLargeError`` if the resulting video bitrate would be below
    ``MIN_VIDEO_KBPS`` (i.e. the video is too long to fit at any watchable
    quality).
    """
    if duration <= 0:
        raise TooLargeError(0, limit_bytes, "unknown duration; cannot compute a target bitrate")
    total_kbps = math.floor(limit_bytes * 8 * safety / duration / 1000)
    if has_audio:
        audio_kbps = max(MIN_AUDIO_KBPS, min(MAX_AUDIO_KBPS, total_kbps // 8))
    else:
        audio_kbps = 0
    video_kbps = total_kbps - audio_kbps
    if video_kbps < MIN_VIDEO_KBPS:
        raise TooLargeError(
            int(limit_bytes), limit_bytes,
            f"a {duration / 60:.0f}-minute video can't fit in {limit_bytes / 1024 / 1024:.0f} MB "
            f"at watchable quality (would need {video_kbps} kbps)",
        )
    # Lower the resolution along with the bitrate so it doesn't turn to mush.
    if video_kbps < 400:
        max_h = 360
    elif video_kbps < 800:
        max_h = 480
    elif video_kbps < 1800:
        max_h = 720
    else:
        max_h = None
    if source_height and max_h and source_height <= max_h:
        max_h = None
    return CompressionPlan(video_kbps=video_kbps, audio_kbps=audio_kbps, max_height=max_h)


async def compress_to_limit(src: Path, out: Path, info: ProbeResult, limit_bytes: int) -> Path:
    """Re-encode ``src`` to H.264/AAC under ``limit_bytes``.

    Single-pass ABR with a VBV cap; if the first attempt overshoots (rare),
    retry once with a proportionally lower target.
    """
    safety = SIZE_SAFETY
    for _attempt in range(2):
        plan = plan_compression(info.duration, limit_bytes, info.has_audio, info.height, safety=safety)
        vf = ["-vf", f"scale=-2:'min({plan.max_height},ih)'"] if plan.max_height else []
        audio = (["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{plan.audio_kbps}k", "-ac", "2"]
                 if info.has_audio else [])
        await _ffmpeg(
            "-i", str(src), "-map", "0:v:0", *audio, *vf,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-b:v", f"{plan.video_kbps}k", "-maxrate", f"{int(plan.video_kbps * 1.25)}k",
            "-bufsize", f"{plan.video_kbps * 2}k",
            "-movflags", "+faststart", str(out),
        )
        size = out.stat().st_size
        if size <= limit_bytes:
            return out
        safety *= (limit_bytes / size) * 0.95
    raise TooLargeError(out.stat().st_size, limit_bytes, "could not compress below the upload limit")


async def make_thumbnail(src: Path, out: Path, duration: float) -> Path | None:
    """Grab a small JPEG frame for Telegram's preview. Best effort."""
    at = min(1.0, max(duration / 3, 0.0))
    try:
        await _ffmpeg("-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
                      "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "5", str(out))
    except MediaError:
        return None
    if not out.exists() or out.stat().st_size > 200 * 1024:
        return None
    return out
