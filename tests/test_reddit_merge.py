"""Reddit-hosted videos (v.redd.it) are DASH: separate video and audio
streams. These tests exercise our split download + ffmpeg merge path
offline, using local file:// "formats" in place of v.redd.it URLs."""

import threading
from pathlib import Path

import pytest
import yt_dlp

from app import media
from app.config import Settings
from app.downloader import REDDIT_FORMAT, Downloader
from app.url_resolver import Platform

from .conftest import requires_ffmpeg


def _formats(media_dir: Path, with_audio: bool) -> list[dict]:
    fmts = [
        {"format_id": "dash-480", "url": (media_dir / "video_only.mp4").as_uri(), "vcodec": "avc1.4d401e",
         "acodec": "none", "ext": "mp4", "height": 240, "tbr": 300, "protocol": "file"},
    ]
    if with_audio:
        fmts.append({"format_id": "dash-audio", "url": (media_dir / "audio_only.m4a").as_uri(), "vcodec": "none",
                     "acodec": "mp4a.40.2", "ext": "m4a", "abr": 128, "protocol": "file"})
    return fmts


def test_reddit_format_selects_separate_video_and_audio():
    ydl = yt_dlp.YoutubeDL({"quiet": True})
    fmts = [
        {"format_id": "v360", "vcodec": "avc1", "acodec": "none", "ext": "mp4", "height": 360, "url": "x"},
        {"format_id": "v720", "vcodec": "avc1", "acodec": "none", "ext": "mp4", "height": 720, "url": "x"},
        {"format_id": "audio", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "url": "x"},
    ]
    picked = ydl._select_formats(fmts, ydl.build_format_selector(REDDIT_FORMAT))
    assert [f["format_id"] for f in picked] == ["v720", "audio"]


def test_reddit_format_without_audio_does_not_fail():
    ydl = yt_dlp.YoutubeDL({"quiet": True})
    fmts = [{"format_id": "v720", "vcodec": "avc1", "acodec": "none", "ext": "mp4", "height": 720, "url": "x"}]
    picked = ydl._select_formats(fmts, ydl.build_format_selector(REDDIT_FORMAT))
    assert [f["format_id"] for f in picked] == ["v720"]


@requires_ffmpeg
async def test_merge_audio_video(media_dir, tmp_path):
    out = await media.merge_audio_video(media_dir / "video_only.mp4", media_dir / "audio_only.m4a", tmp_path / "m.mp4")
    info = await media.probe(out)
    assert info.vcodec == "h264" and info.acodec == "aac"
    assert info.telegram_compatible


@requires_ffmpeg
async def test_merge_without_audio_track(media_dir, tmp_path):
    out = await media.merge_audio_video(media_dir / "video_only.mp4", None, tmp_path / "m.mp4")
    info = await media.probe(out)
    assert info.has_video and not info.has_audio


@pytest.mark.parametrize("with_audio", [True, False])
@requires_ffmpeg
async def test_reddit_entry_download_and_merge(media_dir, tmp_path, with_audio):
    settings = Settings(bot_token="x", allowed_user_ids=frozenset({1}), work_dir=tmp_path)
    dl = Downloader(settings)
    entry = {"id": "abc123", "title": "A Reddit post", "extractor": "generic", "extractor_key": "Generic",
             "webpage_url": "https://www.reddit.com/r/t/comments/abc123/", "duration": 3,
             "formats": _formats(media_dir, with_audio)}
    cancel = threading.Event()
    opts = dl._base_opts(Platform.REDDIT, tmp_path, cancel, lambda _t: None)
    opts.update(format=REDDIT_FORMAT, enable_file_urls=True)

    video = await dl._download_entry(entry, 1, tmp_path, opts, True, lambda _t: None, cancel)

    info = await media.probe(video.path)
    assert info.vcodec == "h264"
    assert info.has_audio is with_audio
    assert info.telegram_compatible
    assert video.title == "A Reddit post"
    assert round(video.duration) == 3
    assert video.thumbnail is not None and video.thumbnail.exists()
