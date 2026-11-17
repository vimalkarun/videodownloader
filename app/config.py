"""Configuration loaded from environment variables.

Every setting comes from the environment (normally a `.env` file passed in by
docker-compose). Nothing is hardcoded and nothing is written to disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MB = 1024 * 1024

# Telegram's cloud Bot API accepts uploads up to 50 MB; a self-hosted
# telegram-bot-api server accepts up to 2000 MB.
CLOUD_UPLOAD_LIMIT = 50 * MB
LOCAL_UPLOAD_LIMIT = 2000 * MB

DEFAULT_REDDIT_USER_AGENT = "linux:private-tg-video-bot:v1.0 (personal single-user bot)"


class ConfigError(ValueError):
    """Raised when a required setting is missing or malformed."""


def _int_set(raw: str | None, name: str) -> frozenset[int]:
    if not raw:
        return frozenset()
    out: set[int] = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"{name} must be a comma-separated list of integers, got {part!r}") from exc
    return frozenset(out)


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(raw: str | None, default: int, name: str, minimum: int = 1) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _optional_file(raw: str | None, name: str) -> Path | None:
    if raw is None or raw.strip() == "":
        return None
    path = Path(raw.strip())
    if not path.is_file():
        raise ConfigError(f"{name} points to {path}, which does not exist or is not a file")
    return path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int] = frozenset()
    telegram_api_url: str | None = None
    instagram_cookies_file: Path | None = None
    reddit_cookies_file: Path | None = None
    youtube_cookies_file: Path | None = None
    x_cookies_file: Path | None = None
    max_concurrent_jobs: int = 2
    job_timeout_seconds: int = 300
    delete_status_messages: bool = False
    work_dir: Path = field(default_factory=lambda: Path("/tmp/tgvideobot"))
    reddit_user_agent: str = DEFAULT_REDDIT_USER_AGENT
    shutdown_grace_seconds: int = 30
    log_level: str = "INFO"

    @property
    def uses_local_api(self) -> bool:
        return bool(self.telegram_api_url)

    @property
    def upload_limit_bytes(self) -> int:
        return LOCAL_UPLOAD_LIMIT if self.uses_local_api else CLOUD_UPLOAD_LIMIT

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required")

        users = _int_set(env.get("ALLOWED_USER_IDS"), "ALLOWED_USER_IDS")
        if not users:
            # Refuse to start rather than run a bot that answers nobody (or,
            # worse, a future bug that answers everybody).
            raise ConfigError("ALLOWED_USER_IDS must contain at least one Telegram user ID")

        api_url = env.get("TELEGRAM_API_URL", "").strip().rstrip("/") or None

        return cls(
            bot_token=token,
            allowed_user_ids=users,
            allowed_chat_ids=_int_set(env.get("ALLOWED_CHAT_IDS"), "ALLOWED_CHAT_IDS"),
            telegram_api_url=api_url,
            instagram_cookies_file=_optional_file(env.get("INSTAGRAM_COOKIES_FILE"), "INSTAGRAM_COOKIES_FILE"),
            reddit_cookies_file=_optional_file(env.get("REDDIT_COOKIES_FILE"), "REDDIT_COOKIES_FILE"),
            youtube_cookies_file=_optional_file(env.get("YOUTUBE_COOKIES_FILE"), "YOUTUBE_COOKIES_FILE"),
            x_cookies_file=_optional_file(env.get("X_COOKIES_FILE"), "X_COOKIES_FILE"),
            max_concurrent_jobs=_int(env.get("MAX_CONCURRENT_JOBS"), 2, "MAX_CONCURRENT_JOBS"),
            job_timeout_seconds=_int(env.get("JOB_TIMEOUT_SECONDS"), 300, "JOB_TIMEOUT_SECONDS", minimum=10),
            delete_status_messages=_bool(env.get("DELETE_STATUS_MESSAGES"), False),
            work_dir=Path(env.get("WORK_DIR", "").strip() or "/tmp/tgvideobot"),
            reddit_user_agent=env.get("REDDIT_USER_AGENT", "").strip() or DEFAULT_REDDIT_USER_AGENT,
            shutdown_grace_seconds=_int(env.get("SHUTDOWN_GRACE_SECONDS"), 30, "SHUTDOWN_GRACE_SECONDS", minimum=0),
            log_level=(env.get("LOG_LEVEL", "").strip() or "INFO").upper(),
        )
