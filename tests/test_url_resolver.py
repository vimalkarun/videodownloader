import httpx
import pytest

from app.url_resolver import (
    Platform,
    UnsupportedUrl,
    classify,
    extract_urls,
    needs_network_resolution,
    normalize,
    resolve,
    resolve_offline,
)

UA = "test-agent"


# -- extraction --------------------------------------------------------------

def test_extracts_multiple_urls_from_shared_text():
    text = ("Check this out! https://www.instagram.com/reel/C1abcDEF/?igsh=xyz and also "
            "https://x.com/user/status/1234567890?s=20.\nShared via app")
    assert extract_urls(text) == [
        "https://www.instagram.com/reel/C1abcDEF/?igsh=xyz",
        "https://x.com/user/status/1234567890?s=20",
    ]


def test_strips_trailing_punctuation_and_parens():
    assert extract_urls("(see https://youtu.be/dQw4w9WgXcQ).") == ["https://youtu.be/dQw4w9WgXcQ"]
    assert extract_urls("“https://redd.it/abc123”") == ["https://redd.it/abc123"]


def test_bare_domain_without_scheme():
    assert extract_urls("youtu.be/dQw4w9WgXcQ please") == ["https://youtu.be/dQw4w9WgXcQ"]


def test_ignores_email_like_text_and_dedupes():
    assert extract_urls("mail me@youtube.com") == []
    assert extract_urls("https://youtu.be/a https://youtu.be/a", ["https://youtu.be/a"]) == ["https://youtu.be/a"]


def test_extra_entity_urls_are_included():
    assert extract_urls("tap here", ["https://www.reddit.com/r/a/comments/xyz/t/"]) == [
        "https://www.reddit.com/r/a/comments/xyz/t/"
    ]


def test_none_text():
    assert extract_urls(None) == []


# -- classification ----------------------------------------------------------

@pytest.mark.parametrize("url,platform", [
    ("https://instagram.com/p/abc/", Platform.INSTAGRAM),
    ("https://www.instagram.com/reel/abc/", Platform.INSTAGRAM),
    ("https://x.com/u/status/1", Platform.X),
    ("https://twitter.com/u/status/1", Platform.X),
    ("https://mobile.twitter.com/u/status/1", Platform.X),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE),
    ("https://youtube.com/shorts/dQw4w9WgXcQ", Platform.YOUTUBE),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE),
    ("https://youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE),
    ("https://reddit.com/r/a/comments/b/c/", Platform.REDDIT),
    ("https://www.reddit.com/r/a/comments/b/c/", Platform.REDDIT),
    ("https://old.reddit.com/r/a/comments/b/c/", Platform.REDDIT),
    ("https://new.reddit.com/r/a/comments/b/c/", Platform.REDDIT),
    ("https://redd.it/abc123", Platform.REDDIT),
    ("https://v.redd.it/abc123", Platform.REDDIT),
    ("HTTPS://WWW.REDDIT.COM/r/a/comments/b/", Platform.REDDIT),
])
def test_supported(url, platform):
    assert classify(url) is platform


@pytest.mark.parametrize("url", [
    "https://vimeo.com/123",
    "https://instagram.com.evil.example/p/abc",
    "https://notreddit.com/r/a",
    "https://youtube.com@evil.example/watch?v=x",
    "ftp://youtube.com/watch?v=x",
    "https://i.redd.it/image.jpg",
    "javascript:alert(1)",
])
def test_unsupported(url):
    assert classify(url) is None


# -- normalisation -----------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=4", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc&list=PL1", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://youtube.com/shorts/dQw4w9WgXcQ?feature=share", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?t=42", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42"),
])
def test_youtube_single_video(url, expected):
    assert normalize(url, Platform.YOUTUBE) == expected


def test_youtube_playlist_only_url_drops_list():
    assert "list=" not in normalize("https://www.youtube.com/playlist?list=PL1", Platform.YOUTUBE)


def test_tracking_params_dropped():
    assert normalize("https://www.instagram.com/reel/C1/?igsh=abc", Platform.INSTAGRAM) == "https://www.instagram.com/reel/C1/"
    assert normalize("https://x.com/u/status/1?s=20&t=abc", Platform.X) == "https://x.com/u/status/1"


# -- Reddit short / share links ---------------------------------------------

def test_redd_it_resolves_offline():
    assert resolve_offline("https://redd.it/1abcde", Platform.REDDIT) == "https://www.reddit.com/comments/1abcde/"


def test_which_links_need_network():
    assert needs_network_resolution("https://www.reddit.com/r/videos/s/AbC123xyz", Platform.REDDIT)
    assert needs_network_resolution("https://v.redd.it/abc123", Platform.REDDIT)
    assert not needs_network_resolution("https://www.reddit.com/r/videos/comments/abc/x/", Platform.REDDIT)
    assert needs_network_resolution("https://www.instagram.com/share/reel/BAabc", Platform.INSTAGRAM)


def _client(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    seen_agents = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_agents.append(request.headers.get("user-agent"))
        return routes.get(str(request.url), httpx.Response(403))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"User-Agent": UA})
    client.seen_agents = seen_agents  # type: ignore[attr-defined]
    return client


async def test_reddit_share_link_followed_to_post():
    client = _client({
        "https://www.reddit.com/r/videos/s/AbC123xyz": httpx.Response(
            301, headers={"location": "https://www.reddit.com/r/videos/comments/1xyz/funny_cat/?share_id=q"}),
    })
    r = await resolve("https://www.reddit.com/r/videos/s/AbC123xyz", user_agent=UA, client=client)
    assert r.platform is Platform.REDDIT
    assert r.url == "https://www.reddit.com/r/videos/comments/1xyz/funny_cat/"
    assert client.seen_agents == [UA]


async def test_reddit_redirect_chain_and_relative_location():
    client = _client({
        "https://reddit.com/r/videos/s/AbC": httpx.Response(302, headers={"location": "https://www.reddit.com/r/videos/s/AbC"}),
        "https://www.reddit.com/r/videos/s/AbC": httpx.Response(301, headers={"location": "/r/videos/comments/9q/t/"}),
    })
    r = await resolve("https://reddit.com/r/videos/s/AbC", user_agent=UA, client=client)
    assert r.url == "https://www.reddit.com/r/videos/comments/9q/t/"


async def test_redd_it_needs_no_network():
    client = _client({})
    r = await resolve("https://redd.it/1abcde", user_agent=UA, client=client)
    assert r.url == "https://www.reddit.com/comments/1abcde/"
    assert client.seen_agents == []


async def test_v_redd_it_resolves_to_post():
    client = _client({
        "https://v.redd.it/abc123": httpx.Response(
            302, headers={"location": "https://www.reddit.com/r/aww/comments/zz1/doggo/"}),
    })
    r = await resolve("https://v.redd.it/abc123", user_agent=UA, client=client)
    assert r.url == "https://www.reddit.com/r/aww/comments/zz1/doggo/"


async def test_v_redd_it_falls_back_to_dash_manifest():
    r = await resolve("https://v.redd.it/abc123", user_agent=UA, client=_client({}))
    assert r.url == "https://v.redd.it/abc123/DASHPlaylist.mpd"


async def test_unresolvable_reddit_share_link_is_unsupported():
    with pytest.raises(UnsupportedUrl):
        await resolve("https://www.reddit.com/r/videos/s/dead", user_agent=UA, client=_client({}))


async def test_instagram_share_link():
    client = _client({
        "https://www.instagram.com/share/reel/BAxyz": httpx.Response(
            302, headers={"location": "https://www.instagram.com/reel/C9abc/?igsh=1"}),
    })
    r = await resolve("https://www.instagram.com/share/reel/BAxyz", user_agent=UA, client=client)
    assert r.url == "https://www.instagram.com/reel/C9abc/"


async def test_unsupported_raises():
    with pytest.raises(UnsupportedUrl):
        await resolve("https://vimeo.com/1", user_agent=UA, client=_client({}))


# -- Telegram message parsing --------------------------------------------------

def test_urls_from_telegram_message_text_caption_and_hidden_links():
    import datetime as dt

    from telegram import Chat, Message, MessageEntity

    from app.bot import urls_from_message

    chat = Chat(1, Chat.PRIVATE)
    now = dt.datetime.now(dt.UTC)
    text = "look at this and https://youtu.be/dQw4w9WgXcQ"
    msg = Message(1, now, chat, text=text, entities=[
        MessageEntity(MessageEntity.TEXT_LINK, 0, 4, url="https://www.reddit.com/r/a/comments/xyz/t/"),
        MessageEntity(MessageEntity.URL, text.index("https"), len("https://youtu.be/dQw4w9WgXcQ")),
    ])
    assert urls_from_message(msg) == [
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.reddit.com/r/a/comments/xyz/t/",
    ]

    shared_video = Message(2, now, chat, caption="via https://x.com/u/status/123?s=46")
    assert urls_from_message(shared_video) == ["https://x.com/u/status/123?s=46"]


def test_error_detail_cleanup():
    from app.downloader import clean_error_detail

    raw = ("ERROR: [youtube] x: Unable to download API page: boom (caused by ProxyError('x')); "
           "please report this issue on https://github.com/yt-dlp/yt-dlp/issues")
    assert clean_error_detail(raw) == "[youtube] x: Unable to download API page: boom"
