"""End-to-end job pipeline with fake Telegram + fake downloader: checks the
zero-persistence guarantee (temp dir gone on success, failure, timeout and
cancel) and the status-message lifecycle."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bot import VideoBot
from app.config import Settings
from app.downloader import DownloadedVideo, DownloadError, DownloadResult, ErrorKind
from app.job_queue import Job, JobState
from app.url_resolver import Platform


class FakeBot:
    def __init__(self):
        self.edits: list[str] = []
        self.deleted: list[int] = []

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append(text)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


class FakeDownloader:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.workdirs: list[Path] = []

    async def download(self, target, workdir, *, progress, cancel):
        self.workdirs.append(workdir)
        f = workdir / "01-video.mp4"
        f.write_bytes(b"\0" * 1000)
        progress("50% of 1 KB")
        if self.behaviour == "error":
            raise DownloadError(ErrorKind.LOGIN, "login required")
        if self.behaviour == "hang":
            await asyncio.sleep(60)
        return DownloadResult(platform=target.platform, title="t", source_url=target.original,
                              videos=[DownloadedVideo(f, "t", 1.0, 320, 240)])


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, reply_to, result, on_progress=None):
        assert all(v.path.exists() for v in result.videos)  # still there while uploading
        self.sent.append((chat_id, reply_to, [v.path.name for v in result.videos]))


@pytest.fixture
def make_bot(tmp_path):
    def _make(behaviour="ok", **overrides):
        settings = Settings(bot_token="123:abc", allowed_user_ids=frozenset({1}), work_dir=tmp_path,
                            job_timeout_seconds=30)
        settings = replace(settings, **overrides)
        vb = VideoBot(settings)
        fake_bot = FakeBot()
        vb.app = SimpleNamespace(bot=fake_bot)
        vb.downloader = FakeDownloader(behaviour)
        vb.sender = FakeSender()
        return vb, fake_bot
    return _make


def _job():
    return Job(url="https://youtu.be/dQw4w9WgXcQ", platform=Platform.YOUTUBE, chat_id=5, message_id=10,
               user_id=1, status_message_id=11)


async def test_success_uploads_and_cleans_up(make_bot, tmp_path):
    vb, bot = make_bot()
    job = _job()
    await vb.run_job(job)
    assert job.state is JobState.DONE
    assert vb.sender.sent == [(5, 10, ["01-video.mp4"])]
    assert not vb.downloader.workdirs[0].exists()
    assert list(tmp_path.iterdir()) == []
    assert bot.edits[0] == "⬇️ Downloading…" and "⬆️ Uploading…" in bot.edits and bot.edits[-1] == "✅ Done"


async def test_delete_status_message_option(make_bot):
    vb, bot = make_bot(delete_status_messages=True)
    await vb.run_job(_job())
    assert bot.deleted == [11]


async def test_error_reports_and_cleans_up(make_bot, tmp_path):
    vb, bot = make_bot("error")
    job = _job()
    await vb.run_job(job)
    assert job.state is JobState.FAILED
    assert "login" in bot.edits[-1].lower()
    assert list(tmp_path.iterdir()) == []


async def test_timeout_reports_and_cleans_up(make_bot, tmp_path):
    vb, bot = make_bot("hang", job_timeout_seconds=10)
    vb.settings = replace(vb.settings, job_timeout_seconds=0.2)  # bypass from_env minimum
    job = _job()
    await vb.run_job(job)
    assert bot.edits[-1].startswith("⏱")
    assert job.cancel_event.is_set()
    assert list(tmp_path.iterdir()) == []


async def test_cancel_reports_and_cleans_up(make_bot, tmp_path):
    vb, bot = make_bot("hang")
    job = _job()
    task = asyncio.create_task(vb.run_job(job))
    await asyncio.sleep(0.1)
    assert any(tmp_path.iterdir())  # temp dir exists mid-download
    task.cancel()
    await asyncio.wait({task})
    assert job.state is JobState.CANCELLED
    assert bot.edits[-1].startswith("✖️")
    assert list(tmp_path.iterdir()) == []


async def test_unsupported_reddit_share_link(make_bot, monkeypatch):
    vb, bot = make_bot()

    async def boom(*a, **kw):
        from app.url_resolver import UnsupportedUrl
        raise UnsupportedUrl("x")

    monkeypatch.setattr("app.bot.resolve", boom)
    await vb.run_job(_job())
    assert bot.edits[-1].startswith("🚫")
