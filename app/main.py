"""Entry point: ``python -m app.main``."""

from __future__ import annotations

import logging
import sys

from telegram import Update

from .config import ConfigError, Settings


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # httpx logs every request URL at INFO, and Bot API URLs contain the bot
    # token. Keep these (and other chatty libraries) quiet.
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level)

    from .bot import VideoBot  # imported after logging is configured

    bot = VideoBot(settings)
    # Long polling: no webhook, no public URL, no open port.
    # run_polling handles SIGINT/SIGTERM and runs post_stop (which drains the
    # queue and cleans temp files) before exiting.
    bot.app.run_polling(
        allowed_updates=[Update.MESSAGE, Update.CHANNEL_POST],
        drop_pending_updates=False,
        # Keep retrying if the network isn't up yet (e.g. a Pi still booting).
        bootstrap_retries=-1,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
