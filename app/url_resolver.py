"""URL extraction, validation, normalisation and short-link resolution.

Pure functions (``extract_urls``, ``classify``, ``normalize``) have no I/O and
are unit tested. ``resolve`` may make HTTP requests (redirect-following only,
response bodies are never read) to expand share links that yt-dlp can't
handle directly.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx


class Platform(enum.StrEnum):
    INSTAGRAM = "Instagram"
    X = "X"
    YOUTUBE = "YouTube"
    REDDIT = "Reddit"


SUPPORTED_SITES_TEXT = "Instagram, X (Twitter), YouTube (incl. Shorts) and Reddit"

# host -> platform. Hosts are matched exactly (after lower-casing and
# stripping a trailing dot) so look-alikes such as "instagram.com.evil.io" or
# "notreddit.com" are rejected.
_HOSTS: dict[str, Platform] = {}
for _h in ("instagram.com", "www.instagram.com", "m.instagram.com"):
    _HOSTS[_h] = Platform.INSTAGRAM
for _h in ("x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"):
    _HOSTS[_h] = Platform.X
for _h in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"):
    _HOSTS[_h] = Platform.YOUTUBE
for _h in ("reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com",
           "m.reddit.com", "redd.it", "v.redd.it"):
    _HOSTS[_h] = Platform.REDDIT

# Anything that looks like a link. Telegram also tells us about links via
# message entities (handled in bot.py); this regex catches links in plain
# text and captions, including bare "youtu.be/xyz" without a scheme.
_URL_RE = re.compile(
    r"""(?ix)
    (?:https?://|(?<![\w@.])(?=(?:www\.|m\.|old\.|new\.|mobile\.)?
        (?:instagram\.com|x\.com|twitter\.com|youtube\.com|youtu\.be|reddit\.com|redd\.it|v\.redd\.it)/))
    [^\s<>"'`]+
    """
)
_TRAILING_PUNCT = ".,;:!?)]}>'\"»”’…"

_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class ResolvedUrl:
    original: str
    url: str  # what we hand to yt-dlp
    platform: Platform


class UnsupportedUrl(ValueError):
    pass


def _strip_trailing(url: str) -> str:
    # Strip punctuation that is almost certainly sentence punctuation, but
    # keep a closing paren if the URL also contains an opening one.
    while url and url[-1] in _TRAILING_PUNCT:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def extract_urls(text: str | None, extra: list[str] | None = None) -> list[str]:
    """Return unique URLs found in ``text`` (plus ``extra``), in order.

    ``extra`` is for URLs Telegram reports via message entities (e.g. hidden
    text links) that aren't visible in the raw text.
    """
    found: list[str] = []
    for m in _URL_RE.finditer(text or ""):
        found.append(m.group(0))
    found.extend(extra or [])

    seen: set[str] = set()
    out: list[str] = []
    for raw in found:
        url = _strip_trailing(raw.strip())
        if not url:
            continue
        if not re.match(r"(?i)^https?://", url):
            url = "https://" + url
        key = url.lower()
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out


def _host(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def classify(url: str) -> Platform | None:
    """Return the platform for a supported URL, or ``None``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    return _HOSTS.get(_host(url))


def _drop_query(url: str, keep: tuple[str, ...] = ()) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k in keep]
    return urlunsplit((p.scheme or "https", p.netloc, p.path, urlencode(q), ""))


def normalize(url: str, platform: Platform) -> str:
    """Canonicalise a URL so yt-dlp downloads exactly one item.

    * YouTube: playlist / index / tracking params are removed; Shorts,
      youtu.be, /live/ and /embed/ links become a plain watch URL.
    * Instagram / X: tracking query strings (igsh, s, t, ...) are dropped.
    * Reddit: query and fragment are dropped.
    """
    p = urlsplit(url)
    host = _host(url)

    if platform is Platform.YOUTUBE:
        video_id: str | None = None
        path_parts = [s for s in p.path.split("/") if s]
        if host == "youtu.be" and path_parts:
            video_id = path_parts[0]
        elif path_parts and path_parts[0] in {"shorts", "live", "embed", "v", "e"} and len(path_parts) > 1:
            video_id = path_parts[1]
        else:
            video_id = dict(parse_qsl(p.query)).get("v")
        if video_id and _YT_ID_RE.match(video_id):
            t = dict(parse_qsl(p.query)).get("t")
            q = {"v": video_id}
            if t:
                q["t"] = t
            return "https://www.youtube.com/watch?" + urlencode(q)
        # Not a single-video URL (channel, bare playlist, ...): let yt-dlp
        # decide, but still never pass list= so we never expand a playlist.
        return _drop_query(url, keep=("v",))

    if platform is Platform.INSTAGRAM:
        return _drop_query(url)

    if platform is Platform.X:
        return _drop_query(url)

    if platform is Platform.REDDIT:
        return _drop_query(url)

    return url


# ---------------------------------------------------------------------------
# Network resolution of short/share links
# ---------------------------------------------------------------------------

_REDDIT_SHARE_RE = re.compile(r"^/r/[^/]+/s/[A-Za-z0-9]+/?$")
_REDDIT_COMMENTS_RE = re.compile(r"/comments/[A-Za-z0-9]+")
_REDD_IT_ID_RE = re.compile(r"^/([A-Za-z0-9]{4,10})/?$")
_IG_SHARE_RE = re.compile(r"^/share/")


def needs_network_resolution(url: str, platform: Platform) -> bool:
    host = _host(url)
    path = urlsplit(url).path
    if platform is Platform.REDDIT:
        return bool(host == "v.redd.it" or _REDDIT_SHARE_RE.match(path))
    if platform is Platform.INSTAGRAM:
        return bool(_IG_SHARE_RE.match(path))
    return False


def resolve_offline(url: str, platform: Platform) -> str:
    """Resolutions that need no network: ``redd.it/<id>`` is a post ID."""
    if platform is Platform.REDDIT and _host(url) == "redd.it":
        m = _REDD_IT_ID_RE.match(urlsplit(url).path)
        if m:
            return f"https://www.reddit.com/comments/{m.group(1)}/"
    return url


async def follow_redirects(url: str, client: httpx.AsyncClient, max_hops: int = 6,
                           done: Callable[[str], bool] | None = None) -> str:
    """Follow HTTP redirects using only the ``Location`` headers.

    We deliberately do not let httpx auto-follow: Reddit often answers the
    *final* page with 403 for server IPs, but the redirect chain itself still
    tells us the canonical post URL, which is all we need. ``done(url)``
    stops early once we've reached a URL we can use.
    """
    current = url
    for _ in range(max_hops):
        if done is not None and current != url and done(current):
            return current
        resp = await client.head(current, follow_redirects=False)
        if resp.status_code in (405, 501):  # HEAD not allowed -> GET, body ignored
            resp = await client.get(current, follow_redirects=False)
        location = resp.headers.get("location")
        if not (300 <= resp.status_code < 400 and location):
            return current
        current = urljoin(current, location)
    return current


def _is_final_post_url(url: str) -> bool:
    platform = classify(url)
    if platform is Platform.REDDIT:
        return bool(_REDDIT_COMMENTS_RE.search(urlsplit(url).path))
    if platform is Platform.INSTAGRAM:
        return not _IG_SHARE_RE.match(urlsplit(url).path)
    return False


async def resolve(url: str, *, user_agent: str, client: httpx.AsyncClient | None = None) -> ResolvedUrl:
    """Validate, expand and normalise a single URL.

    Raises ``UnsupportedUrl`` for links outside the supported platforms or
    Reddit/Instagram share links that don't resolve to a post.
    """
    platform = classify(url)
    if platform is None:
        raise UnsupportedUrl(url)

    target = resolve_offline(url, platform)

    if needs_network_resolution(target, platform):
        own_client = client is None
        client = client or httpx.AsyncClient(headers={"User-Agent": user_agent}, timeout=15)
        try:
            expanded = await follow_redirects(target, client, done=_is_final_post_url)
        except httpx.HTTPError:
            expanded = target
        finally:
            if own_client:
                await client.aclose()

        if platform is Platform.REDDIT:
            if classify(expanded) is Platform.REDDIT and _REDDIT_COMMENTS_RE.search(urlsplit(expanded).path):
                target = expanded
            elif _host(target) == "v.redd.it":
                # Couldn't map the bare video link to its post (deleted post,
                # blocked IP, ...). Fall back to the DASH manifest, which
                # yt-dlp's generic extractor downloads as separate video and
                # audio streams and merges with ffmpeg.
                vid = urlsplit(target).path.strip("/").split("/")[0]
                return ResolvedUrl(url, f"https://v.redd.it/{vid}/DASHPlaylist.mpd", platform)
            else:
                raise UnsupportedUrl(url)
        else:
            target = expanded
            if classify(target) is not platform:
                raise UnsupportedUrl(url)

    return ResolvedUrl(original=url, url=normalize(target, platform), platform=platform)
