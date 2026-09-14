# -*- coding: utf-8 -*-
"""Allowlisted operations the local web UI may run.

Every action the browser can trigger is a named entry in ``OPERATIONS``. The
browser never sends a command line: it sends an operation name plus typed
parameters, and the handler here builds the argv itself. Nothing in this module
takes a shell string, and ``shell=True`` is never used, so a hostile request
body cannot become a command.

Long-running work (installs, transcription) is executed by
``agent_reach.webui.server`` as a background job; handlers here either return a
result directly or yield output lines as they arrive.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Dict, Iterator, List, Optional

from agent_reach.config import Config
from agent_reach.utils.process import utf8_subprocess_env
from agent_reach.utils.text import scrub_url_credentials

#: Hard ceiling on captured child output, so a runaway tool cannot exhaust RAM.
MAX_OUTPUT_CHARS = 512 * 1024

#: Per-operation wall clock limits, in seconds.
QUICK_TIMEOUT = 60
SEARCH_TIMEOUT = 180
INSTALL_TIMEOUT = 1800
TRANSCRIBE_TIMEOUT = 1800


class OperationError(RuntimeError):
    """A user-facing failure. The message is shown in the UI verbatim."""


class ToolMissing(OperationError):
    """A required upstream tool is not installed; message carries the fix."""


# --------------------------------------------------------------------------- #
# parameter validation
# --------------------------------------------------------------------------- #

def _text(params: dict, key: str, *, max_len: int = 2000, required: bool = True) -> str:
    value = params.get(key)
    if value is None or value == "":
        if required:
            raise OperationError(f"Missing required field: {key}")
        return ""
    if not isinstance(value, str):
        raise OperationError(f"{key} must be text")
    value = value.strip()
    if len(value) > max_len:
        raise OperationError(f"{key} is too long (limit {max_len} characters)")
    if any(ord(ch) < 0x20 and ch not in "\t" for ch in value):
        raise OperationError(f"{key} contains control characters")
    return value


def _count(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = params.get(key, default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise OperationError(f"{key} must be a whole number") from None
    return max(lo, min(hi, number))


def _choice(params: dict, key: str, allowed: tuple, default=None) -> str:
    value = params.get(key, default)
    if value not in allowed:
        raise OperationError(
            f"{key} must be one of: {', '.join(str(a) for a in allowed)}"
        )
    return value


def _public_url(params: dict, key: str = "url") -> str:
    """Validate a URL with the same rules the channels use."""
    from agent_reach.utils.url import normalize_public_http_url

    raw = _text(params, key, max_len=2048)
    try:
        return normalize_public_http_url(raw)
    except ValueError:
        raise OperationError(
            "Only public http(s) URLs are allowed."
        ) from None


# --------------------------------------------------------------------------- #
# subprocess helpers
# --------------------------------------------------------------------------- #

def _resolve(command: str, install_hint: str) -> str:
    """Locate a tool, including shims a running process has not picked up."""
    from agent_reach.cli import _find_installed_tool

    found = _find_installed_tool(command)
    if not found:
        raise ToolMissing(f"`{command}` is not installed. {install_hint}")
    return found


def _stream(argv: List[str], timeout: int, env_extra: Optional[dict] = None) -> Iterator[str]:
    """Run argv and yield output lines as they are produced.

    argv is always a list — no shell, no interpolation. Output is scrubbed of
    URL credentials before it leaves this process, because a configured proxy
    can otherwise be echoed back by an upstream tool.
    """
    env = utf8_subprocess_env()
    if env_extra:
        env.update(env_extra)

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=os.path.expanduser("~"),
        )
    except FileNotFoundError:
        raise OperationError(f"`{argv[0]}` could not be started.") from None
    except OSError as exc:
        raise OperationError(f"Could not start `{argv[0]}`: {exc}") from exc

    produced = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = scrub_url_credentials(line.rstrip("\n"))
            produced += len(line)
            if produced > MAX_OUTPUT_CHARS:
                proc.kill()
                yield "… output truncated (limit reached)"
                break
            yield line
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise OperationError(f"`{argv[0]}` timed out after {timeout}s") from None
    finally:
        if proc.poll() is None:
            proc.kill()

    if proc.returncode not in (0, None):
        raise OperationError(f"`{argv[0]}` exited with code {proc.returncode}")


def _capture(argv: List[str], timeout: int, env_extra: Optional[dict] = None) -> str:
    return "\n".join(_stream(argv, timeout, env_extra))


# --------------------------------------------------------------------------- #
# status / config
# --------------------------------------------------------------------------- #

def op_status(params: dict) -> dict:
    """Full doctor report, already scrubbed by the doctor module."""
    from agent_reach.doctor import check_all

    results = check_all(Config(read_only=True))
    active = sum(1 for r in results.values() if r["status"] == "ok")
    return {"channels": results, "active": active, "total": len(results)}


def op_config_get(params: dict) -> dict:
    """Configured settings, with every secret masked by Config.to_dict()."""
    config = Config(read_only=True)
    return {
        "values": config.to_dict(),
        "features": config.get_configured_features(),
        "path": str(config.config_path),
    }


#: configure key -> the config field(s) it writes.
_CONFIGURABLE = {
    "github-token": "github_token",
    "groq-key": "groq_api_key",
    "openai-key": "openai_api_key",
    "proxy": "proxy",
    "youtube-cookies": "youtube_cookies_from",
}


def op_config_set(params: dict) -> dict:
    """Write one configuration value.

    The value never reaches a command line and is never echoed back: the
    response only confirms which key was written.
    """
    key = _choice(params, "key", tuple(_CONFIGURABLE) + ("twitter-cookies",))
    value = _text(params, "value", max_len=1024 * 1024)

    config = Config()
    if key == "twitter-cookies":
        from agent_reach.cli import _parse_twitter_cookie_input

        auth_token, ct0 = _parse_twitter_cookie_input(value)
        if not (auth_token and ct0):
            raise OperationError(
                "Could not find auth_token and ct0. Paste the Cookie-Editor "
                "“Header String” export from x.com, or the two values separated "
                "by a space."
            )
        config.set("twitter_auth_token", auth_token)
        config.set("twitter_ct0", ct0)
        return {"saved": "twitter-cookies", "note": "Credentials are not verified automatically."}

    if key == "proxy":
        config.set("proxy", value)
        config.set("bilibili_proxy", value)
        return {"saved": key}

    config.set(_CONFIGURABLE[key], value)
    return {"saved": key}


def op_config_delete(params: dict) -> dict:
    key = _choice(params, "key", tuple(_CONFIGURABLE))
    config = Config()
    config.delete(_CONFIGURABLE[key])
    return {"deleted": key}


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

def _search_exa(query: str, limit: int) -> Iterator[str]:
    mcporter = _resolve("mcporter", "Install with: npm install -g mcporter")
    yield from _stream(
        [mcporter, "call", "exa.web_search_exa", f"query={query}", f"numResults={limit}"],
        SEARCH_TIMEOUT,
    )


def _search_github(query: str, limit: int) -> Iterator[str]:
    gh = _resolve("gh", "Install from https://cli.github.com")
    yield from _stream(
        [gh, "search", "repos", query, "--sort", "stars", "--limit", str(limit)],
        SEARCH_TIMEOUT,
    )


def _search_bilibili(query: str, limit: int) -> Iterator[str]:
    bili = _resolve("bili", "Install with: pipx install bilibili-cli")
    yield from _stream([bili, "search", query, "--type", "video", "-n", str(limit)], SEARCH_TIMEOUT)


def _search_twitter(query: str, limit: int) -> Iterator[str]:
    twitter = _resolve("twitter", "Install with: pipx install twitter-cli")
    from agent_reach.channels.twitter import twitter_cli_child_env

    env = twitter_cli_child_env(Config(read_only=True))
    if not env and not os.environ.get("TWITTER_AUTH_TOKEN"):
        raise OperationError(
            "Twitter credentials are not configured. Add them under Settings, "
            "using a Cookie-Editor export from x.com."
        )
    yield from _stream([twitter, "search", query, "-n", str(limit)], SEARCH_TIMEOUT, env)


def _search_opencli(site: str, query: str, limit: int) -> Iterator[str]:
    opencli = _resolve(
        "opencli",
        "Install with: npm install -g @jackwener/opencli, then add the Chrome extension.",
    )
    yield from _stream([opencli, site, "search", query, "-f", "yaml"], SEARCH_TIMEOUT)


def _search_v2ex(query: str, limit: int) -> Iterator[str]:
    from agent_reach.channels.v2ex import V2EXChannel

    try:
        results = V2EXChannel().search(query, limit=limit)
    except Exception as exc:
        raise OperationError(f"V2EX search failed: {scrub_url_credentials(exc)}") from exc
    for item in results:
        yield json.dumps(item, ensure_ascii=False)


def _search_xueqiu(query: str, limit: int) -> Iterator[str]:
    from agent_reach.channels.xueqiu import XueqiuChannel

    try:
        results = XueqiuChannel().search_stock(query, limit=limit)
    except Exception as exc:
        raise OperationError(f"Xueqiu search failed: {scrub_url_credentials(exc)}") from exc
    for item in results:
        yield json.dumps(item, ensure_ascii=False)


#: Platform id -> (label, search function). Mirrors SKILL.md's routing table.
SEARCH_PLATFORMS: Dict[str, tuple] = {
    "exa":         ("Web search (Exa)",  _search_exa),
    "github":      ("GitHub",            _search_github),
    "v2ex":        ("V2EX",              _search_v2ex),
    "bilibili":    ("B站 Bilibili",      _search_bilibili),
    "twitter":     ("Twitter / X",       _search_twitter),
    "xueqiu":      ("雪球 Xueqiu",       _search_xueqiu),
    "reddit":      ("Reddit",            lambda q, n: _search_opencli("reddit", q, n)),
    "xiaohongshu": ("小红书",            lambda q, n: _search_opencli("xiaohongshu", q, n)),
    "facebook":    ("Facebook",          lambda q, n: _search_opencli("facebook", q, n)),
    "instagram":   ("Instagram",         lambda q, n: _search_opencli("instagram", q, n)),
}


def op_search(params: dict) -> Iterator[str]:
    platform = _choice(params, "platform", tuple(SEARCH_PLATFORMS))
    query = _text(params, "query", max_len=500)
    limit = _count(params, "limit", 10, 1, 50)
    yield from SEARCH_PLATFORMS[platform][1](query, limit)


# --------------------------------------------------------------------------- #
# read / browse / transcribe
# --------------------------------------------------------------------------- #

def op_read(params: dict) -> Iterator[str]:
    """Read any URL, routed to whichever channel claims it."""
    url = _public_url(params)

    from agent_reach.channels.v2ex import V2EXChannel
    from agent_reach.channels.web import WebChannel
    from agent_reach.channels.youtube import YouTubeChannel

    if YouTubeChannel().can_handle(url):
        yt = _resolve("yt-dlp", 'Install with: pip install -U "yt-dlp[default]"')
        yield from _stream(
            [yt, "--skip-download", "--write-auto-sub", "--write-sub",
             "--sub-format", "vtt", "--print", "%(title)s\n%(uploader)s\n%(duration)s sec",
             "--", url],
            SEARCH_TIMEOUT,
        )
        return

    if V2EXChannel().can_handle(url) and "/t/" in url:
        topic_id = url.rstrip("/").split("/t/")[-1].split("?")[0].split("#")[0]
        if topic_id.isdigit():
            try:
                topic = V2EXChannel().get_topic(int(topic_id))
            except Exception as exc:
                raise OperationError(f"V2EX read failed: {scrub_url_credentials(exc)}") from exc
            yield json.dumps(topic, ensure_ascii=False, indent=2)
            return

    try:
        text = WebChannel().read(url)
    except Exception as exc:
        raise OperationError(f"Could not read that page: {scrub_url_credentials(exc)}") from exc
    for line in text.splitlines():
        yield line


def op_browse(params: dict) -> Iterator[str]:
    """Trending / listing views that need no query."""
    feed = _choice(
        params, "feed",
        ("v2ex_hot", "xueqiu_hot_posts", "xueqiu_hot_stocks", "bilibili_hot"),
    )
    limit = _count(params, "limit", 20, 1, 50)

    if feed == "v2ex_hot":
        from agent_reach.channels.v2ex import V2EXChannel
        items = V2EXChannel().get_hot_topics(limit=limit)
    elif feed == "xueqiu_hot_posts":
        from agent_reach.channels.xueqiu import XueqiuChannel
        items = XueqiuChannel().get_hot_posts(limit=limit)
    elif feed == "xueqiu_hot_stocks":
        from agent_reach.channels.xueqiu import XueqiuChannel
        items = XueqiuChannel().get_hot_stocks(limit=limit)
    else:
        bili = _resolve("bili", "Install with: pipx install bilibili-cli")
        yield from _stream([bili, "hot", "-n", str(limit)], SEARCH_TIMEOUT)
        return

    for item in items:
        yield json.dumps(item, ensure_ascii=False)


def op_transcribe(params: dict) -> Iterator[str]:
    """Transcribe audio from a URL via a configured Whisper provider."""
    url = _public_url(params)
    provider = _choice(params, "provider", ("auto", "groq", "openai"), "auto")

    from agent_reach.transcribe import TranscribeError, transcribe

    yield f"Transcribing {url} …"
    yield "Downloading audio, compressing, then sending to the provider."
    try:
        text = transcribe(url, provider=provider, config=Config(read_only=True))
    except TranscribeError as exc:
        raise OperationError(scrub_url_credentials(exc)) from exc
    yield ""
    for line in text.splitlines():
        yield line


# --------------------------------------------------------------------------- #
# maintenance
# --------------------------------------------------------------------------- #

def op_install(params: dict) -> Iterator[str]:
    """Run the installer. `safe` mode makes no system changes."""
    channels = params.get("channels") or []
    if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
        raise OperationError("channels must be a list of names")
    known = {
        "twitter", "xiaoyuzhou", "xiaohongshu", "reddit", "facebook",
        "instagram", "bilibili", "opencli", "xueqiu", "linkedin", "all",
    }
    unknown = sorted(set(channels) - known)
    if unknown:
        raise OperationError(f"Unknown channel(s): {', '.join(unknown)}")

    mode = _choice(params, "mode", ("safe", "system", "dry-run"), "safe")
    argv = [sys.executable, "-m", "agent_reach.cli", "install", "--env=auto"]
    argv.append({"safe": "--safe", "system": "--system", "dry-run": "--dry-run"}[mode])
    if channels:
        argv.append("--channels=" + ",".join(channels))
    yield from _stream(argv, INSTALL_TIMEOUT)


def op_skill(params: dict) -> Iterator[str]:
    action = _choice(params, "action", ("install", "uninstall"))
    yield from _stream(
        [sys.executable, "-m", "agent_reach.cli", "skill", f"--{action}"],
        QUICK_TIMEOUT,
    )


def op_check_update(params: dict) -> Iterator[str]:
    yield from _stream(
        [sys.executable, "-m", "agent_reach.cli", "check-update"], QUICK_TIMEOUT
    )


def op_tool_versions(params: dict) -> dict:
    """Which upstream tools are on this machine, for the Tools panel."""
    from agent_reach.cli import _find_installed_tool

    tools = ["yt-dlp", "gh", "mcporter", "opencli", "twitter", "bili", "rdt",
             "ffmpeg", "node", "deno", "uvx", "pipx", "docker"]
    return {
        "tools": {name: bool(_find_installed_tool(name) or shutil.which(name)) for name in tools}
    }


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

#: name -> (handler, streaming?). Streaming handlers run as background jobs.
OPERATIONS: Dict[str, tuple] = {
    "status":         (op_status,        False),
    "config.get":     (op_config_get,    False),
    "config.set":     (op_config_set,    False),
    "config.delete":  (op_config_delete, False),
    "tools":          (op_tool_versions, False),
    "search":         (op_search,        True),
    "read":           (op_read,          True),
    "browse":         (op_browse,        True),
    "transcribe":     (op_transcribe,    True),
    "install":        (op_install,       True),
    "skill":          (op_skill,         True),
    "check_update":   (op_check_update,  True),
}


def get_operation(name: str) -> tuple:
    try:
        return OPERATIONS[name]
    except KeyError:
        raise OperationError(f"Unknown operation: {name}") from None


__all__ = [
    "OPERATIONS", "OperationError", "ToolMissing", "get_operation",
    "SEARCH_PLATFORMS", "MAX_OUTPUT_CHARS",
]
