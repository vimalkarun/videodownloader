import asyncio

from app.job_queue import DownloadQueue, Job, JobState, QueueClosed
from app.url_resolver import Platform


def _job(user=1, chat=1) -> Job:
    return Job(url="https://youtu.be/x", platform=Platform.YOUTUBE, chat_id=chat, message_id=1, user_id=user)


async def test_respects_concurrency_limit():
    running, peak = 0, 0
    release = asyncio.Event()

    async def runner(job):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        job.state = JobState.DONE

    q = DownloadQueue(runner, concurrency=2)
    q.start()
    jobs = [_job() for _ in range(5)]
    positions = [q.submit(j) for j in jobs]
    assert positions[:2] == [0, 0] and positions[2:] == [1, 2, 3]
    await asyncio.sleep(0.05)
    assert peak == 2 and len(q.active) == 2 and len(q.pending) == 3
    release.set()
    await asyncio.sleep(0.05)
    assert all(j.state is JobState.DONE for j in jobs)
    await q.shutdown(0)


async def test_cancel_queued_and_active_for_user_only():
    started = asyncio.Event()
    notified = []

    async def runner(job):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            raise

    async def on_cancel(job):
        notified.append(job)

    q = DownloadQueue(runner, concurrency=1, on_queued_cancel=on_cancel)
    q.start()
    mine_active, mine_queued, other = _job(user=1), _job(user=1), _job(user=2)
    for j in (mine_active, mine_queued, other):
        q.submit(j)
    await started.wait()

    assert await q.cancel_for(1, 1) == (1, 1)
    await asyncio.sleep(0.05)
    assert mine_active.state is JobState.CANCELLED and mine_active.cancel_event.is_set()
    assert mine_queued.state is JobState.CANCELLED and notified == [mine_queued]
    assert other.state is not JobState.CANCELLED
    await q.shutdown(0)


async def test_shutdown_waits_then_cancels_and_rejects_new_jobs():
    finished = []

    async def runner(job):
        try:
            await asyncio.sleep(job.message_id)
            finished.append(job)
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            raise

    q = DownloadQueue(runner, concurrency=2)
    q.start()
    quick, slow, waiting = _job(), _job(), _job()
    quick.message_id, slow.message_id = 0, 30
    for j in (quick, slow, waiting):
        q.submit(j)
    await asyncio.sleep(0.01)
    await q.shutdown(grace_seconds=0.2)
    assert finished == [quick]
    assert slow.state is JobState.CANCELLED and waiting.state is JobState.CANCELLED
    try:
        q.submit(_job())
    except QueueClosed:
        pass
    else:
        raise AssertionError("submit after shutdown should fail")


async def test_runner_crash_does_not_kill_worker():
    done = []

    async def runner(job):
        if job.message_id == 1:
            raise RuntimeError("boom")
        done.append(job)

    q = DownloadQueue(runner, concurrency=1)
    q.start()
    bad, good = _job(), _job()
    good.message_id = 2
    q.submit(bad)
    q.submit(good)
    await asyncio.sleep(0.05)
    assert done == [good]
    await q.shutdown(0)
