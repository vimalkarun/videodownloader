import pytest

from app import media
from app.config import CLOUD_UPLOAD_LIMIT, LOCAL_UPLOAD_LIMIT, MB, Settings
from app.downloader import (
    ErrorKind,
    classify_error,
    estimate_entry_size,
    estimate_format_size,
    pick_formats,
    size_capped_format,
)
from app.telegram_sender import build_caption, chunk_for_media_groups

from .conftest import requires_ffmpeg

# -- limits ------------------------------------------------------------------

def test_cloud_vs_local_limit():
    base = dict(bot_token="x", allowed_user_ids=frozenset({1}))
    assert Settings(**base).upload_limit_bytes == CLOUD_UPLOAD_LIMIT == 50 * MB
    local = Settings(**base, telegram_api_url="http://telegram-bot-api:8081")
    assert local.uses_local_api and local.upload_limit_bytes == LOCAL_UPLOAD_LIMIT


# -- compression plan ----------------------------------------------------------

def test_plan_fits_under_limit():
    plan = media.plan_compression(duration=600, limit_bytes=50 * MB, has_audio=True)
    total_bits = (plan.video_kbps + plan.audio_kbps) * 1000 * 600
    assert total_bits / 8 <= 50 * MB
    assert plan.video_kbps > 400
    assert 32 <= plan.audio_kbps <= 128


def test_plan_no_audio_gives_all_bitrate_to_video():
    with_a = media.plan_compression(300, 50 * MB, True)
    without = media.plan_compression(300, 50 * MB, False)
    assert without.audio_kbps == 0
    assert without.video_kbps == with_a.video_kbps + with_a.audio_kbps


def test_plan_lowers_resolution_for_low_bitrates():
    assert media.plan_compression(1800, 50 * MB, True, source_height=1080).max_height == 360
    assert media.plan_compression(60, 50 * MB, True, source_height=1080).max_height is None
    # never upscale: 240p source keeps its size
    assert media.plan_compression(1800, 50 * MB, True, source_height=240).max_height is None


def test_plan_impossible_raises():
    with pytest.raises(media.TooLargeError):
        media.plan_compression(duration=4 * 3600, limit_bytes=50 * MB, has_audio=True)
    with pytest.raises(media.TooLargeError):
        media.plan_compression(duration=0, limit_bytes=50 * MB, has_audio=True)


# -- size estimation / format picking ----------------------------------------

def test_estimates():
    assert estimate_format_size({"filesize": 123}, 10) == 123
    assert estimate_format_size({"filesize_approx": 456}, 10) == 456
    assert estimate_format_size({"tbr": 800}, 10) == 1_000_000
    assert estimate_format_size({}, 10) is None
    entry = {"duration": 10, "requested_formats": [{"filesize": 100}, {"tbr": 8}]}
    assert estimate_entry_size(entry) == 100 + 10_000


def test_estimate_reddit_separate_streams():
    entry = {"duration": 100, "formats": [
        {"format_id": "v360", "vcodec": "avc1", "acodec": "none", "height": 360, "tbr": 500},
        {"format_id": "v1080", "vcodec": "avc1", "acodec": "none", "height": 1080, "tbr": 4000},
        {"format_id": "a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "tbr": 128},
    ]}
    assert estimate_entry_size(entry, separate=True) == (4000 + 128) * 1000 // 8 * 100


YT_FORMATS = [
    {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "filesize": 5 * MB, "abr": 128},
    {"format_id": "251", "vcodec": "none", "acodec": "opus", "filesize": 4 * MB, "abr": 130},
    {"format_id": "137", "vcodec": "avc1.640028", "acodec": "none", "height": 1080, "filesize": 120 * MB},
    {"format_id": "136", "vcodec": "avc1.4d401f", "acodec": "none", "height": 720, "filesize": 60 * MB},
    {"format_id": "135", "vcodec": "avc1.4d401e", "acodec": "none", "height": 480, "filesize": 30 * MB},
    {"format_id": "244", "vcodec": "vp9", "acodec": "none", "height": 480, "filesize": 20 * MB},
    {"format_id": "18", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "height": 360, "filesize": 25 * MB},
]


def test_pick_best_h264_that_fits():
    assert pick_formats(YT_FORMATS, 300, 50 * MB) == "135+140"


def test_pick_separate_for_reddit():
    assert pick_formats(YT_FORMATS, 300, 50 * MB, separate=True) == "135,140"


def test_pick_progressive_when_only_it_fits():
    assert pick_formats(YT_FORMATS, 300, 26 * MB) == "18"


def test_pick_none_when_nothing_fits():
    assert pick_formats(YT_FORMATS, 300, 5 * MB) is None


def test_pick_silent_video():
    fmts = [{"format_id": "v", "vcodec": "avc1", "acodec": "none", "height": 720, "filesize": 10 * MB}]
    assert pick_formats(fmts, 30, 50 * MB) == "v"


def test_size_capped_format_spec():
    assert "b[filesize<48M]" in size_capped_format(50 * MB)


# -- real compression ----------------------------------------------------------

@requires_ffmpeg
async def test_compress_to_limit(media_dir, tmp_path):
    src = media_dir / "big.mp4"
    info = await media.probe(src)
    limit = 700 * 1024  # force a ~4x reduction
    assert src.stat().st_size > limit
    out = await media.compress_to_limit(src, tmp_path / "small.mp4", info, limit)
    assert out.stat().st_size <= limit
    small = await media.probe(out)
    assert small.telegram_compatible and small.has_audio
    assert abs(small.duration - info.duration) < 1


@requires_ffmpeg
async def test_make_telegram_compatible_converts_vp9(media_dir, tmp_path):
    src = media_dir / "vp9.webm"
    info = await media.probe(src)
    assert not info.telegram_compatible
    out = await media.make_telegram_compatible(src, tmp_path / "tg.mp4", info)
    assert (await media.probe(out)).telegram_compatible


@requires_ffmpeg
async def test_compatible_file_untouched(media_dir, tmp_path):
    src = media_dir / "big.mp4"
    info = await media.probe(src)
    assert await media.make_telegram_compatible(src, tmp_path / "x.mp4", info) == src


# -- captions / grouping / errors --------------------------------------------------

def test_caption_contains_platform_title_url_and_fits():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    cap = build_caption("YouTube", "x" * 5000, url)
    assert cap.startswith("YouTube · x") and cap.endswith(url) and len(cap) <= 1024
    assert build_caption("X", "", url) == f"X\n{url}"
    assert build_caption("Reddit", "Hi", url, 2, 3).startswith("Reddit (2/3) · Hi")


class _V:
    def __init__(self, size):
        self.size = size


def test_media_group_chunking():
    vids = [_V(20 * MB) for _ in range(5)]
    groups = chunk_for_media_groups(vids, 50 * MB)
    assert [len(g) for g in groups] == [2, 2, 1]
    assert [len(g) for g in chunk_for_media_groups([_V(1)] * 10, None)] == [10]
    assert [len(g) for g in chunk_for_media_groups([_V(1)] * 3, None, max_items=2)] == [2, 1]


@pytest.mark.parametrize("msg,kind", [
    ("ERROR: [Instagram] abc: Requested content is not available, rate-limit reached or login required",
     ErrorKind.RATE_LIMIT),
    ("ERROR: [Instagram] abc: Main webpage is locked behind the login page", ErrorKind.LOGIN),
    ("ERROR: [youtube] x: Sign in to confirm your age", ErrorKind.LOGIN),
    ("ERROR: [youtube] x: Video unavailable. This video is private", ErrorKind.PRIVATE),
    ("ERROR: [twitter] 1: No video could be found in this tweet", ErrorKind.NO_VIDEO),
    ("ERROR: [Reddit] abc: No media found", ErrorKind.NO_VIDEO),
    ("ERROR: Unsupported URL: https://example.com", ErrorKind.UNSUPPORTED),
    ("ERROR: something odd", ErrorKind.GENERIC),
])
def test_classify_error(msg, kind):
    assert classify_error(msg) is kind
