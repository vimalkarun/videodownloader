import shutil
import subprocess
from pathlib import Path

import pytest

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    """Synthetic Reddit-style DASH pieces: a video-only and an audio-only file,
    plus a VP9/Opus WebM that needs converting for Telegram."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("media")
    _ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-t", "3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "video_only.mp4"))
    _ff("-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:a", "aac", str(d / "audio_only.m4a"))
    _ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-f", "lavfi", "-i", "sine", "-t", "2",
        "-c:v", "libvpx-vp9", "-b:v", "200k", "-c:a", "libopus", "-shortest", str(d / "vp9.webm"))
    # High-bitrate 10 s clip for compression tests (~2.5 MB).
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i", "sine", "-t", "10",
        "-c:v", "libx264", "-b:v", "2M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "big.mp4"))
    return d
