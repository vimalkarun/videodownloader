from types import SimpleNamespace

import pytest

from app.bot import is_authorized, wipe_work_dir
from app.config import ConfigError, Settings


def test_from_env_parses_lists_and_defaults(tmp_path):
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_USER_IDS": "1, 2;3", "ALLOWED_CHAT_IDS": "-100"})
    assert s.allowed_user_ids == {1, 2, 3} and s.allowed_chat_ids == {-100}
    assert s.max_concurrent_jobs == 2 and s.job_timeout_seconds == 300
    assert s.delete_status_messages is False and not s.uses_local_api


@pytest.mark.parametrize("env", [
    {"ALLOWED_USER_IDS": "1"},
    {"TELEGRAM_BOT_TOKEN": "t"},
    {"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_USER_IDS": "abc"},
    {"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_USER_IDS": "1", "INSTAGRAM_COOKIES_FILE": "/nope/cookies.txt"},
])
def test_from_env_rejects_bad_config(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


SETTINGS = Settings(bot_token="t", allowed_user_ids=frozenset({42}), allowed_chat_ids=frozenset({-100}))


def _update(chat_type, chat_id, user_id):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(type=chat_type, id=chat_id),
        effective_user=SimpleNamespace(id=user_id) if user_id is not None else None,
    )


@pytest.mark.parametrize("chat_type,chat_id,user_id,ok", [
    ("private", 42, 42, True),
    ("private", 7, 7, False),
    ("group", -5, 42, False),          # allowed user, chat not allowed
    ("supergroup", -100, 42, True),
    ("supergroup", -100, 7, False),    # allowed chat, stranger
    ("channel", -100, None, True),
    ("channel", -200, None, False),
])
def test_authorization(chat_type, chat_id, user_id, ok):
    assert is_authorized(_update(chat_type, chat_id, user_id), SETTINGS) is ok


def test_wipe_work_dir(tmp_path):
    (tmp_path / "job-abc").mkdir()
    (tmp_path / "job-abc" / "video.mp4").write_bytes(b"x")
    (tmp_path / "job-stray.part").write_bytes(b"x")
    (tmp_path / "unrelated.txt").write_bytes(b"keep me")
    assert wipe_work_dir(tmp_path) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["unrelated.txt"]
