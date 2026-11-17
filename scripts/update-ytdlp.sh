#!/usr/bin/env sh
# Update yt-dlp for the running bot.
#
#   scripts/update-ytdlp.sh            # quick: upgrade inside the running container + restart
#   scripts/update-ytdlp.sh --rebuild  # durable: rebuild the image (pulls latest yt-dlp) + recreate
#
# The quick path survives `docker compose restart` but not a container
# re-create (`up --build`, `down`/`up`); use --rebuild for a permanent update.
set -eu
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--rebuild" ]; then
    docker compose build --pull --no-cache bot
    docker compose up -d bot
else
    docker compose exec -u root bot pip install --no-cache-dir -U "yt-dlp[default,curl-cffi,deno]"
    docker compose restart bot
fi
docker compose exec bot python -c "import yt_dlp; print('yt-dlp', yt_dlp.version.__version__)"
