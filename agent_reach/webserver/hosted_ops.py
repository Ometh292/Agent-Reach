# -*- coding: utf-8 -*-
"""The operations a hosted deployment may expose.

Deliberately far smaller than the local console's set. The console runs on the
operator's own machine and may install software and write credentials; a public
deployment must do neither. Nothing here installs, configures, writes to disk,
or reads a credential store.

What remains is read-only research: search, fetch a page, list a feed, pull
YouTube subtitles, and — only when the operator supplies a key — transcribe.

Platforms needing a signed-in session (Reddit, Twitter/X, Facebook, Instagram,
小红书, 雪球, LinkedIn) are absent by design, and the status endpoint says so
in plain words rather than pretending they are merely unconfigured.

Everything here calls the real Agent Reach channel implementations. Nothing is
mocked or synthesised.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional

from agent_reach.utils.process import utf8_subprocess_env
from agent_reach.utils.text import scrub_url_credentials
from agent_reach.webserver import exa

#: Caps that bound a single operation's cost.
MAX_RESULT_LINES = 4000
SEARCH_TIMEOUT = 180
YT_TIMEOUT = 120
STATUS_CACHE_SECONDS = 60


class HostedError(RuntimeError):
    """A user-facing failure, shown verbatim in the UI."""


class NeedsConnection(HostedError):
    """The caller must connect their own account for this platform first."""

    def __init__(self, platform: str, label: str):
        super().__init__(
            "Connect your " + label + " account to use this. It is held for "
            "this session only and is never stored."
        )
        self.platform = platform


@dataclass(frozen=True)
class Context:
    """Who is asking, and how to reach their session-only credentials.

    Passed explicitly rather than through a global or a thread local, because
    a credential reaching the wrong request is precisely the failure this
    design exists to prevent.
    """

    user_id: str = ""
    credentials: Optional[Callable[[str], Optional[Dict[str, str]]]] = None

    def require(self, platform: str, label: str) -> Dict[str, str]:
        values = self.credentials(platform) if self.credentials else None
        if not values:
            raise NeedsConnection(platform, label)
        return values


# --------------------------------------------------------------------------- #
# validation — every browser-supplied value passes through here
# --------------------------------------------------------------------------- #

def _text(params: dict, key: str, max_len: int = 500) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HostedError(f"Please provide {key}.")
    value = value.strip()
    if len(value) > max_len:
        raise HostedError(f"That {key} is too long (limit {max_len} characters).")
    if any(ord(character) < 0x20 for character in value):
        raise HostedError(f"That {key} contains characters that are not allowed.")
    return value


def _count(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(params.get(key, default))))
    except (TypeError, ValueError):
        return default


def _public_url(params: dict, key: str = "url") -> str:
    """Public HTTP(S) only — the SSRF boundary.

    The server sits inside a hosting provider's network where a link-local
    address reaches the instance metadata service, so a URL supplied by an
    untrusted caller is never fetched without this check. Reuses the same
    validator the channels use rather than a second, weaker copy.
    """
    from agent_reach.utils.url import normalize_public_http_url

    raw = _text(params, key, max_len=2048)
    try:
        return normalize_public_http_url(raw)
    except ValueError:
        raise HostedError(
            "Only public web addresses are allowed. Local and internal "
            "addresses are blocked."
        ) from None


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

def _search_exa(query: str, limit: int) -> Iterator[str]:
    try:
        for line in exa.search(query, num_results=limit).splitlines():
            yield line
    except exa.ExaError as error:
        raise HostedError(str(error)) from error


def _search_github(query: str, limit: int) -> Iterator[str]:
    """GitHub REST search, called directly — no gh CLI needed in the image.

    An optional server-side token raises the limit from 60/hour to 5000/hour.
    It is the operator's token; it is never sent to the browser.
    """
    import requests

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "agent-reach"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "stars", "per_page": str(limit)},
            headers=headers, timeout=30,
        )
    except requests.RequestException as exc:
        raise HostedError(f"Could not reach GitHub: {exc}") from exc

    if response.status_code == 403:
        raise HostedError("GitHub's rate limit has been reached. It resets within the hour.")
    if response.status_code >= 400:
        raise HostedError(f"GitHub returned an error ({response.status_code}).")

    for item in response.json().get("items", [])[:limit]:
        yield json.dumps({
            "name": item.get("full_name"),
            "stars": item.get("stargazers_count"),
            "language": item.get("language"),
            "description": item.get("description"),
            "url": item.get("html_url"),
        }, ensure_ascii=False)


def _search_v2ex(query: str, limit: int) -> Iterator[str]:
    from agent_reach.channels.v2ex import V2EXChannel

    try:
        results = V2EXChannel().search(query, limit=limit)
    except Exception as exc:
        raise HostedError(f"V2EX search failed: {scrub_url_credentials(exc)}") from exc
    for item in results:
        yield json.dumps(item, ensure_ascii=False)


def _stream_process(argv, timeout, env_extra=None):
    """Run argv and yield its output lines.

    argv is always a list, never a shell string. Credentials go through the
    environment, never through arguments, so they cannot show up in a process
    listing on the host.
    """
    env = utf8_subprocess_env()
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", env=env,
        )
    except OSError as exc:
        raise HostedError("Could not start that tool: " + str(exc)) from exc

    produced = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = scrub_url_credentials(line.rstrip("\n"))
            produced += len(line)
            if produced > 512 * 1024:
                proc.kill()
                yield "... output truncated"
                break
            yield line
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise HostedError("That took too long and was stopped.") from None
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode not in (0, None):
        raise HostedError(
            "That platform returned an error. Your connected session may have "
            "expired - try reconnecting the account."
        )


def _search_twitter(query, limit, ctx):
    """Search X with the caller's own session, held in memory only."""
    creds = ctx.require("twitter", "Twitter / X")
    twitter = shutil.which("twitter")
    if not twitter:
        raise HostedError("Twitter support is not installed on this deployment.")
    return _stream_process(
        [twitter, "search", query, "-n", str(limit)], SEARCH_TIMEOUT,
        {"TWITTER_AUTH_TOKEN": creds["auth_token"], "TWITTER_CT0": creds["ct0"]},
    )


#: Serialises Xueqiu calls. That channel keeps its cookie jar in module-level
#: state, so without this lock one user's session would serve another user's
#: request - a cross-user credential leak.
_xueqiu_lock = threading.Lock()


def _search_xueqiu(query, limit, ctx):
    creds = ctx.require("xueqiu", "Xueqiu")
    cookie = "; ".join(k + "=" + v for k, v in creds.items())
    return _xueqiu_call(lambda channel: channel.search_stock(query, limit=limit), cookie)


def _xueqiu_call(action, cookie):
    """Run one Xueqiu call with only this caller's cookie loaded.

    The jar is cleared before and after, under a lock, so a credential can
    never survive into the next request.
    """
    from agent_reach.channels import xueqiu as xq
    from agent_reach.channels.xueqiu import XueqiuChannel

    with _xueqiu_lock:
        xq._cookie_jar.clear()
        xq._cookies_initialized = False
        try:
            xq._inject_cookie_string(cookie)
            xq._cookies_initialized = True
            try:
                results = action(XueqiuChannel())
            except Exception as exc:
                raise HostedError(
                    "Xueqiu request failed: " + scrub_url_credentials(exc)
                ) from exc
        finally:
            xq._cookie_jar.clear()
            xq._cookies_initialized = False

    for item in results:
        yield json.dumps(item, ensure_ascii=False)


#: Sources that need nothing at all from the caller.
SEARCH_PLATFORMS: Dict[str, tuple] = {
    "exa":    ("Web search", _search_exa),
    "github": ("GitHub", _search_github),
    "v2ex":   ("V2EX", _search_v2ex),
}

#: Sources that need the caller's own session, supplied for this session only
#: and never written anywhere. See session_credentials.py.
CONNECTED_PLATFORMS: Dict[str, tuple] = {
    "twitter": ("Twitter / X", _search_twitter),
    "xueqiu":  ("Xueqiu", _search_xueqiu),
}

ALL_SEARCH_LABELS = dict(
    [(k, v[0]) for k, v in SEARCH_PLATFORMS.items()]
    + [(k, v[0]) for k, v in CONNECTED_PLATFORMS.items()]
)


def op_search(params: dict, ctx=None) -> Iterator[str]:
    """Validate eagerly, then return the stream.

    A generator body does not run until it is first advanced, so validating
    inside one meant a bad request still created a job and spent a rate-limit
    token before failing. Everything below raises before any work starts.
    """
    platform = params.get("platform")
    query = _text(params, "query")
    limit = _count(params, "limit", 10, 1, 25)
    if platform in SEARCH_PLATFORMS:
        return SEARCH_PLATFORMS[platform][1](query, limit)
    if platform in CONNECTED_PLATFORMS:
        return CONNECTED_PLATFORMS[platform][1](query, limit, ctx or Context())
    raise HostedError("Choose one of the available sources.")


# --------------------------------------------------------------------------- #
# read / browse
# --------------------------------------------------------------------------- #

def op_read(params: dict, ctx=None) -> Iterator[str]:
    """Read a page as clean text, routed by whichever channel claims the URL."""
    return _read_stream(_public_url(params))


def _read_stream(url: str) -> Iterator[str]:
    from agent_reach.channels.rss import RSSChannel
    from agent_reach.channels.v2ex import V2EXChannel
    from agent_reach.channels.web import WebChannel

    if V2EXChannel().can_handle(url) and "/t/" in url:
        topic = url.rstrip("/").split("/t/")[-1].split("?")[0].split("#")[0]
        if topic.isdigit():
            try:
                yield json.dumps(V2EXChannel().get_topic(int(topic)),
                                 ensure_ascii=False, indent=2)
                return
            except Exception as exc:
                raise HostedError(
                    f"Could not read that V2EX thread: {scrub_url_credentials(exc)}"
                ) from exc

    if RSSChannel().can_handle(url):
        import feedparser

        feed = feedparser.parse(url)
        if getattr(feed, "entries", None):
            for entry in feed.entries[:40]:
                yield json.dumps({
                    "title": getattr(entry, "title", ""),
                    "link": getattr(entry, "link", ""),
                    "published": getattr(entry, "published", ""),
                }, ensure_ascii=False)
            return

    try:
        text = WebChannel().read(url)
    except Exception as exc:
        raise HostedError(f"Could not read that page: {scrub_url_credentials(exc)}") from exc
    for line in text.splitlines():
        yield line


def op_browse(params: dict, ctx=None) -> Iterator[str]:
    feed = params.get("feed")
    if feed != "v2ex_hot":
        raise HostedError("Choose one of the available feeds.")
    return _browse_stream(_count(params, "limit", 20, 1, 50))


def _browse_stream(limit: int) -> Iterator[str]:
    from agent_reach.channels.v2ex import V2EXChannel

    try:
        items = V2EXChannel().get_hot_topics(limit=limit)
    except Exception as exc:
        raise HostedError(f"Could not load that feed: {scrub_url_credentials(exc)}") from exc
    for item in items:
        yield json.dumps(item, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# youtube
# --------------------------------------------------------------------------- #

def _yt_dlp_path() -> str:
    found = shutil.which("yt-dlp")
    if not found:
        raise HostedError("Video reading is not available on this deployment.")
    return found


def op_youtube(params: dict, ctx=None) -> Iterator[str]:
    """Video details plus subtitles, via the same yt-dlp the channel uses."""
    from agent_reach.channels.youtube import YouTubeChannel

    url = _public_url(params)
    if not YouTubeChannel().can_handle(url):
        raise HostedError("That is not a YouTube address.")
    _yt_dlp_path()          # fail now, not once the job has started
    return _youtube_stream(url)


def _youtube_stream(url: str) -> Iterator[str]:
    argv = [
        _yt_dlp_path(),
        "--skip-download", "--no-warnings", "--no-playlist",
        "--write-auto-sub", "--write-sub", "--sub-format", "vtt",
        "--print", "%(title)s\n%(uploader)s\n%(duration)s seconds\n%(webpage_url)s",
        "--", url,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, encoding="utf-8", errors="replace",
            timeout=YT_TIMEOUT, env=utf8_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        raise HostedError("YouTube took too long to respond. Try again.") from None
    except OSError as exc:
        raise HostedError(f"Could not read that video: {exc}") from exc

    if proc.returncode != 0:
        detail = scrub_url_credentials((proc.stderr or "").strip())[:300]
        if "Sign in to confirm" in detail or "bot" in detail.lower():
            raise HostedError(
                "YouTube blocked this request. Hosted servers are often "
                "rate-limited by YouTube; try again later."
            )
        raise HostedError(f"Could not read that video: {detail or 'unknown error'}")

    for line in (proc.stdout or "").splitlines():
        yield scrub_url_credentials(line)


# --------------------------------------------------------------------------- #
# transcription — only when the operator has opted in
# --------------------------------------------------------------------------- #

def transcription_enabled() -> bool:
    """Audio costs money per request, so it stays off unless a key is supplied."""
    has_key = bool(os.environ.get("GROQ_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    return has_key and bool(shutil.which("ffmpeg")) and bool(shutil.which("yt-dlp"))


def op_transcribe(params: dict, ctx=None) -> Iterator[str]:
    if not transcription_enabled():
        raise HostedError("Transcription is not enabled on this deployment.")
    return _transcribe_stream(_public_url(params))


def _transcribe_stream(url: str) -> Iterator[str]:
    from agent_reach.config import Config
    from agent_reach.transcribe import TranscribeError, transcribe

    yield "Downloading audio…"
    yield "This can take a few minutes for a long recording."
    try:
        text = transcribe(url, provider="auto", config=Config(read_only=True))
    except TranscribeError as exc:
        # scrub_url_credentials also strips any api_key= that reached a message.
        raise HostedError(scrub_url_credentials(exc)) from exc
    except Exception as exc:                    # noqa: BLE001 — never leak internals
        raise HostedError("Transcription failed. Please try a different link.") from exc
    yield ""
    for line in text.splitlines():
        yield line


# --------------------------------------------------------------------------- #
# channel status — real probes, honest labels
# --------------------------------------------------------------------------- #

#: state -> meaning shown in the UI.
#:   available   — works right now
#:   degraded    — works, but unreliable from a datacenter IP
#:   disabled    — could work, operator has not enabled it
#:   needs_account — needs the end user's own login; not offered here
#:   unavailable — cannot work in a hosted environment at all
_STATIC_CHANNELS = [
    # Could run on a server with the user's own credentials. Not offered here
    # because that means this service holding their login — see docs/hosted-app.md.
    ("twitter", "Twitter / X", "needs_account",
     "Possible on a server: twitter-cli works from exported X cookies. Not "
     "offered here, because it would mean this service storing your X session — "
     "which is equivalent to your password. The desktop tool does this on your "
     "own machine instead."),
    ("reddit", "Reddit", "needs_account",
     "Possible on a server: rdt-cli works from a saved Reddit session cookie. "
     "Not offered here, because it would mean storing your Reddit login on a "
     "shared server."),
    ("xiaohongshu", "小红书", "needs_account",
     "Possible on a server: the xiaohongshu-mcp service works from an exported "
     "cookie. Not offered here, because it would mean storing your 小红书 login "
     "on a shared server."),
    ("xueqiu", "雪球 Xueqiu", "needs_account",
     "Possible on a server with your Xueqiu login cookie. Not offered here for "
     "the same reason."),
    ("linkedin", "LinkedIn", "needs_account",
     "Possible on a server with a signed-in LinkedIn session. Not offered here "
     "for the same reason — and LinkedIn suspends accounts used this way."),

    # No server-capable backend exists at all: reading these needs a desktop
    # browser session, and storing a credential does not change that.
    ("facebook", "Facebook", "unavailable",
     "Agent Reach reads Facebook only through a signed-in desktop Chrome "
     "window, which a server does not have. Meta's official API could be built "
     "instead, but that needs a Business account and app review — a separate "
     "project, not a setting."),
    ("instagram", "Instagram", "unavailable",
     "Agent Reach reads Instagram only through a signed-in desktop Chrome "
     "window, which a server does not have. Meta's official API could be built "
     "instead, but that needs a Business account and app review — a separate "
     "project, not a setting."),
]

_status_cache: Optional[tuple] = None
_status_lock = threading.Lock()


def _probe_exa() -> tuple:
    try:
        exa.search("connectivity check", num_results=1)
        return "available", "Working."
    except Exception as exc:
        return "degraded", f"Search is not responding right now: {scrub_url_credentials(exc)}"[:200]


def _probe_v2ex() -> tuple:
    from agent_reach.channels.v2ex import V2EXChannel

    status, message = V2EXChannel().check()
    return ("available", "Working.") if status == "ok" else ("degraded", message[:200])


def _probe_github() -> tuple:
    import requests

    headers = {"User-Agent": "agent-reach"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get("https://api.github.com/rate_limit",
                                headers=headers, timeout=15)
    except requests.RequestException:
        return "degraded", "GitHub is not responding right now."
    if response.status_code >= 400:
        return "degraded", f"GitHub returned {response.status_code}."
    remaining = (response.json().get("resources", {}).get("search", {})
                 .get("remaining"))
    suffix = f" {remaining} searches left this hour." if remaining is not None else ""
    return "available", ("Working." + suffix)


def _probe_youtube() -> tuple:
    if not shutil.which("yt-dlp"):
        return "disabled", "Video reading is not installed on this deployment."
    return ("degraded",
            "Available, but YouTube often rate-limits requests from hosted "
            "servers, so it can fail intermittently.")


def compute_status() -> List[dict]:
    """Probe what can be probed; never claim more than was actually checked."""
    channels: List[dict] = [
        {"id": "web", "label": "Web pages", "state": "available",
         "detail": "Read any public page as clean text."},
        {"id": "rss", "label": "RSS / Atom", "state": "available",
         "detail": "Read any public feed."},
    ]

    for channel_id, label, probe in (
        ("exa", "Web search", _probe_exa),
        ("github", "GitHub", _probe_github),
        ("v2ex", "V2EX", _probe_v2ex),
        ("youtube", "YouTube", _probe_youtube),
    ):
        state, detail = probe()
        channels.append({"id": channel_id, "label": label,
                         "state": state, "detail": detail})

    if transcription_enabled():
        channels.append({"id": "transcribe", "label": "Transcription",
                         "state": "available",
                         "detail": "Turn a video or podcast into text."})
    else:
        missing = []
        if not (os.environ.get("GROQ_API_KEY") or os.environ.get("OPENAI_API_KEY")):
            missing.append("a transcription key")
        if not shutil.which("ffmpeg"):
            missing.append("ffmpeg")
        channels.append({
            "id": "transcribe", "label": "Transcription", "state": "disabled",
            "detail": "Not enabled on this deployment"
                      + (f" (missing {', '.join(missing)})." if missing else "."),
        })

    for channel_id, label, state, detail in _STATIC_CHANNELS:
        channels.append({"id": channel_id, "label": label,
                         "state": state, "detail": detail})
    return channels


def op_status(params: dict, ctx=None) -> dict:
    """Channel availability, cached briefly so the page is cheap to refresh."""
    global _status_cache
    with _status_lock:
        if _status_cache and _status_cache[0] > time.time():
            return {"channels": _status_cache[1], "cached": True}

    channels = compute_status()
    with _status_lock:
        _status_cache = (time.time() + STATUS_CACHE_SECONDS, channels)
    return {"channels": channels, "cached": False}


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

#: name -> (handler, streams_output?)
OPERATIONS: Dict[str, tuple] = {
    "search":     (op_search, True),
    "read":       (op_read, True),
    "browse":     (op_browse, True),
    "youtube":    (op_youtube, True),
    "transcribe": (op_transcribe, True),
    "status":     (op_status, False),
}


def available_operations() -> List[str]:
    names = [name for name in OPERATIONS if name != "transcribe"]
    if transcription_enabled():
        names.append("transcribe")
    return names


def get_operation(name: str) -> tuple:
    if name not in OPERATIONS:
        raise HostedError("That action is not available.")
    if name == "transcribe" and not transcription_enabled():
        raise HostedError("Transcription is not enabled on this deployment.")
    return OPERATIONS[name]


__all__ = [
    "HostedError", "OPERATIONS", "SEARCH_PLATFORMS", "available_operations",
    "compute_status", "get_operation", "transcription_enabled",
]
