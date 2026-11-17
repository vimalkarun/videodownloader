# Private Telegram video downloader bot

A private, single-user Telegram bot. Send it (or share to it) a link from
**Instagram, X/Twitter, YouTube (incl. Shorts) or Reddit** and it replies with
the video. Telegram is the only interface and the only storage: each video is
downloaded into a RAM-backed temp folder, uploaded, and deleted immediately.

> For personal use: save content you own or have permission to save, and
> respect each platform's terms of service.

- Long polling: no webhook, domain, HTTPS certificate or open port needed
- Zero persistence: no database, no media volumes; temp files live on tmpfs and are
  removed in a `finally` block on success, error, timeout, `/cancel` and shutdown;
  leftovers from a crash are wiped at startup
- Only answers allow-listed Telegram user IDs; everyone else is silently ignored
- Telegram-friendly output: prefers H.264/AAC MP4, otherwise remuxes/transcodes with ffmpeg
- Handles the 50 MB Bot API limit: picks a smaller format, else re-encodes to fit,
  else explains why it can't. With a self-hosted Bot API server the limit is 2 GB.
- Carousels, galleries and multi-video tweets are sent as albums (≤10) or one by one

## Project layout

```
app/
  main.py            entry point, logging setup
  config.py          settings from environment variables
  url_resolver.py    URL extraction, validation, normalisation, short-link expansion
  downloader.py      yt-dlp (as a library): format selection, size handling, error mapping
  media.py           ffmpeg/ffprobe: Reddit merge, Telegram compatibility, compression, thumbnails
  telegram_sender.py send_video / send_media_group, captions, retry
  job_queue.py       asyncio queue, bounded concurrency, cancel, graceful shutdown
  bot.py             handlers, authorization, status messages, per-job pipeline
scripts/update-ytdlp.sh
tests/
Dockerfile, docker-compose.yml, .env.example
```

## 1. Create the bot with @BotFather

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. Copy the token (`123456789:AA...`) and keep it secret: it's `TELEGRAM_BOT_TOKEN`.
4. Optional: `/setprivacy` → *Enable* (default) keeps the bot from reading group
   chatter. In groups it then only sees commands and replies to it. Disable it only if
   you want to drop plain links into an allowed group.

You don't need to register commands: the bot calls `set_my_commands` at startup
(`/start`, `/help`, `/status`, `/cancel`).

## 2. Find your Telegram user ID

Message **@userinfobot** (or @RawDataBot); it replies with your numeric ID, e.g.
`111111111`. That goes in `ALLOWED_USER_IDS`. For a group or channel, add the bot,
forward a message from it to @RawDataBot, and note the chat ID (`-100…`) for
`ALLOWED_CHAT_IDS`.

## 3. Configure `.env`

```bash
cp .env.example .env
```

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | Token from @BotFather |
| `ALLOWED_USER_IDS` | required | Comma-separated user IDs allowed to use the bot |
| `ALLOWED_CHAT_IDS` | empty | Groups/channels where the bot works (group senders must also be allowed users) |
| `TELEGRAM_API_URL` | empty | Self-hosted Bot API server URL → 2 GB limit, no compression |
| `INSTAGRAM_COOKIES_FILE` | empty | Path *inside the container* to a cookies.txt |
| `REDDIT_COOKIES_FILE` | empty | Same, for NSFW/age-gated/quarantined Reddit posts |
| `YOUTUBE_COOKIES_FILE`, `X_COOKIES_FILE` | empty | Optional extras for age-restricted / sensitive posts |
| `MAX_CONCURRENT_JOBS` | `2` | Parallel downloads |
| `JOB_TIMEOUT_SECONDS` | `300` | Per-job timeout (download + processing + upload) |
| `DELETE_STATUS_MESSAGES` | `false` | Delete the status message after success instead of editing it to "✅ Done" |
| `REDDIT_USER_AGENT` | descriptive UA | User-Agent for Reddit requests |
| `SHUTDOWN_GRACE_SECONDS` | `30` | How long in-flight jobs may finish on shutdown |
| `LOG_LEVEL` | `INFO` | |
| `TMPFS_SIZE` | `1g` | docker-compose only: size of the RAM-backed `/tmp` |

Tokens are read only from the environment; `.env` and `cookies/` are git-ignored.

## 4. Cookies (Instagram / Reddit), optional

Instagram often requires a logged-in session, even for public Reels. Reddit needs one
for NSFW, age-gated and quarantined posts. **Use secondary/throwaway accounts**: an
account used for automated downloads can get rate-limited or banned, and a cookies
file is a full login to that account.

1. In a desktop browser, open a private window and log in to the secondary account.
2. Export cookies in **Netscape format** with a "cookies.txt" extension (for example
   *Get cookies.txt LOCALLY*), or with yt-dlp:
   `yt-dlp --cookies-from-browser firefox --cookies instagram.txt "https://www.instagram.com/"`.
3. Save them as `cookies/instagram.txt` / `cookies/reddit.txt` and close the private
   window without logging out. Logging out invalidates the session.
4. In `docker-compose.yml`, uncomment the read-only mount:
   ```yaml
   volumes:
     - ./cookies:/cookies:ro
   ```
5. In `.env`:
   ```
   INSTAGRAM_COOKIES_FILE=/cookies/instagram.txt
   REDDIT_COOKIES_FILE=/cookies/reddit.txt
   ```

yt-dlp normally writes refreshed cookies back to its cookie file. The bot gives yt-dlp a
throwaway copy inside each job's temp folder, so your mounted file is never modified.
For Reddit NSFW posts, also enable "Show mature content" on that account.

## 5. Deploy

### Small VPS or home machine (Docker Compose)

```bash
git clone https://github.com/<you>/videodownloader.git && cd videodownloader
cp .env.example .env    # then edit it
docker compose up -d --build
docker compose logs -f bot
```

Send `/start` to your bot. 1 vCPU / 1 GB RAM is enough for the 50 MB mode. Re-encoding
oversized videos is the only CPU-heavy part.

### Raspberry Pi

Use a **64-bit** OS (Raspberry Pi OS 64-bit / Ubuntu arm64). The image and its
dependencies, including the Deno runtime yt-dlp needs for YouTube, ship arm64 builds.
Install Docker (`curl -fsSL https://get.docker.com | sh`), then follow the Compose steps.
A Pi 4/5 handles downloads easily. Re-encoding a long video over 50 MB is slow, so
consider `MAX_CONCURRENT_JOBS=1` and a higher `JOB_TIMEOUT_SECONDS`.

### Railway

1. New Project → Deploy from GitHub repo. Railway detects the `Dockerfile`.
2. Add the `.env` variables under **Variables**. Don't add a volume.
3. No domain or port is needed: the bot only makes outbound connections.

Railway doesn't support Compose `tmpfs`. `/tmp` is the container's own ephemeral
filesystem, wiped on every redeploy, and the bot still deletes each job's folder right
away. Railway has no secret-file mounts, so cookies-dependent platforms work better on
Render or a VPS.

### Render

1. New → **Background Worker** (no HTTP port needed) → connect the repo, runtime **Docker**.
2. Add environment variables. For cookies, use **Secret Files**: they appear under
   `/etc/secrets/<name>`, so set e.g. `INSTAGRAM_COOKIES_FILE=/etc/secrets/instagram.txt`.

Background workers are a paid Render instance type.

### Self-hosted Bot API server (2 GB uploads), optional

1. Get `api_id` / `api_hash` at <https://my.telegram.org> → API development tools,
   and add `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` to `.env`.
2. Log the bot out of the cloud API once (required before switching servers):
   `curl https://api.telegram.org/bot<TOKEN>/logOut`
3. Set `TELEGRAM_API_URL=http://telegram-bot-api:8081` and raise `TMPFS_SIZE` (e.g. `6g`).
4. Uncomment the `telegram-bot-api` service and the `depends_on` line in
   `docker-compose.yml`, then `docker compose up -d --build`.

The bot uploads to the local server over HTTP, the same way it uploads to the cloud
API, so the two containers share no files. The server buffers uploads in its own tmpfs.
Large tmpfs mounts use RAM (or swap) only while files exist.

## 6. Updating yt-dlp

Platforms change often. When downloads from one site start failing, update yt-dlp
first:

```bash
scripts/update-ytdlp.sh            # quick: pip install -U yt-dlp inside the running container, restart
scripts/update-ytdlp.sh --rebuild  # durable: rebuild the image with the latest yt-dlp
```

The quick update lasts until the container is recreated. Rebuilding (or any
`docker compose up --build`) always installs the latest release, because
`requirements.txt` sets no upper bound on yt-dlp. Without the script:
`docker compose exec -u root bot pip install -U "yt-dlp[default,curl-cffi,deno]"`.

## Usage

- Send or share one or more links; each becomes its own job, with a status message
  replying to your message: *Downloading… (queued)* → *Downloading… 42%* →
  *Processing…* → *Uploading…* → *✅ Done* (or an error).
- `/status`: waiting and active jobs. `/cancel`: cancel your queued and running jobs.
- `/help`: how to share links from each app.

Error messages cover: private/deleted posts, login required, unsupported links, posts
without video, rate limiting, files too large, and timeouts.

### Platform notes

- **YouTube**: `watch`, `shorts/`, `live/`, `embed/` and `youtu.be` links are
  normalised to a single watch URL. `list=`/`index=` are dropped, so playlists are
  never expanded.
- **X/Twitter**: `x.com`, `twitter.com` and `mobile.` hosts. Multi-video posts are sent
  as an album. Only the linked post is fetched, not the rest of its thread.
- **Instagram**: Reels, posts, carousels (videos only; images are skipped) and
  `instagram.com/share/…` links.
- **Reddit**: `redd.it/<id>` is expanded offline. `/r/<sub>/s/<code>` share links and
  `v.redd.it/<id>` are expanded by following redirects (only `Location` headers are
  read). Reddit-hosted video downloads as separate video and audio files that the bot
  merges with ffmpeg; silent videos and GIF-style posts work too. Galleries with videos
  become albums. Posts that link to another host (YouTube, Streamable, Imgur, …) go
  through yt-dlp's extractor for that site.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt     # ffmpeg must be on PATH for the media tests
pytest
ruff check app tests
```

The tests run offline. They cover URL extraction/validation (including Reddit
short/share/`v.redd.it` links), the Reddit split-stream download and audio+video merge
(with and without audio, using local `file://` streams generated by ffmpeg), size
estimation, format picking, the compression plan, real re-encoding under a limit, the
job queue (concurrency, cancel, shutdown), authorization, and the pipeline's temp-dir
cleanup on success, error, timeout and cancel.

Run locally without Docker: `set -a; . ./.env; set +a; python -m app.main`. Set
`WORK_DIR` to a scratch folder if `/tmp/tgvideobot` isn't suitable. At startup the bot
deletes leftover `job-*` folders in `WORK_DIR` and leaves everything else alone.

## Logging and privacy

Logs contain only job ID, platform, outcome, size and duration. No URLs, titles, file
contents or tokens: yt-dlp's own logger is silenced, and `httpx` request logging (which
would include the bot token in URLs) is turned down to warnings.
