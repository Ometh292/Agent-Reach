# -*- coding: utf-8 -*-
"""YouTube — check if yt-dlp is available with JS runtime."""

import datetime as _datetime
import re
import shutil

from agent_reach.probe import probe_command
from agent_reach.utils.paths import (
    PrivatePathError,
    get_ytdlp_config_path,
    read_small_text_no_follow,
    render_ytdlp_fix_command,
)

from .base import Channel

_JS_RUNTIMES_SUPPORTED_FROM = (2025, 11, 12)
_YTDLP_UPGRADE_COMMAND = 'python -m pip install -U "yt-dlp[default]"'

#: YouTube ships extractor-breaking changes continuously, so an old yt-dlp is
#: the single most likely reason this channel fails in practice — and version
#: presence alone cannot detect it: a stale build answers `--version` happily
#: and only fails at extraction time with "unable to extract yt initial data".
#: Doctor cannot prove extraction works without a network fetch, but it CAN
#: refuse to call a months-old release healthy. 42 days is ~3 release cycles.
_YTDLP_STALE_AFTER_DAYS = 42


def _ytdlp_release_age_days(version: tuple, today=None):
    """Days since a yt-dlp calendar release, or None if it is not a real date.

    yt-dlp versions are dates (2026.08.19), so age needs no network call.
    """
    try:
        released = _datetime.date(*version)
    except (TypeError, ValueError):
        return None
    reference = today or _datetime.date.today()
    return (reference - released).days


def _parse_ytdlp_version(version: str):
    """Return a comparable stable yt-dlp release tuple, if recognised."""
    match = re.fullmatch(r"\s*(\d{4})\.(\d{1,2})\.(\d{1,2})\s*", version)
    return tuple(map(int, match.groups())) if match else None


def _has_js_runtime_config(config_path) -> bool:
    """Return whether yt-dlp config explicitly enables a JS runtime."""
    try:
        payload = read_small_text_no_follow(
            config_path,
            max_bytes=1024 * 1024,
        )
        return payload is not None and "--js-runtimes" in payload
    except (OSError, UnicodeError, PrivatePathError):
        return False


class YouTubeChannel(Channel):
    name = "youtube"
    description = "YouTube 视频和字幕"
    backends = ["yt-dlp"]
    tier = 0

    def can_handle(self, url: str) -> bool:
        from agent_reach.utils.url import host_matches

        return host_matches(url, "youtube.com", "youtu.be")

    def check(self, config=None):
        # 真跑 yt-dlp --version 探活，区分未装 / venv 断链 / 跑不动
        probe = probe_command("yt-dlp", ["--version"], timeout=10, package="yt-dlp")
        if probe.status == "missing":
            self.active_backend = None
            return "off", f"yt-dlp 未安装。安装：{_YTDLP_UPGRADE_COMMAND}"
        if probe.status == "broken":
            self.active_backend = None
            return "error", (
                "yt-dlp 已安装但无法执行。重装（含 JS 支持）：\n"
                f"  {_YTDLP_UPGRADE_COMMAND}\n{probe.hint}"
            )
        if not probe.ok:  # timeout / error：装了但跑不动
            self.active_backend = None
            detail = probe.hint or probe.output or probe.status
            return "error", f"yt-dlp 无法正常运行：{detail}"
        # yt-dlp 本体是活的；后面的 JS runtime/转写检查只影响 ok/warn，不影响后端归属
        self.active_backend = "yt-dlp"
        # Check JS runtime
        has_js = shutil.which("deno") or shutil.which("node")
        if not has_js:
            return "warn", (
                "yt-dlp 已安装但缺少 JS runtime（YouTube 必须）。\n"
                "  安装 Node.js 或 deno，然后运行：agent-reach install --system"
            )
        # Check yt-dlp config for --js-runtimes
        # Deno works out of the box; Node.js requires explicit config
        has_deno = shutil.which("deno")
        if not has_deno:
            ytdlp_config = get_ytdlp_config_path()
            if not _has_js_runtime_config(ytdlp_config):
                version = _parse_ytdlp_version(probe.output)
                if version is None:
                    return "warn", (
                        "无法确认 yt-dlp 版本是否支持 JS runtime 配置。"
                        "请先升级并重新运行 doctor：\n"
                        f"  {_YTDLP_UPGRADE_COMMAND}"
                    )
                if version < _JS_RUNTIMES_SUPPORTED_FROM:
                    return "warn", (
                        "yt-dlp 版本过旧，不支持 JS runtime 配置。请先升级并重新运行 doctor：\n"
                        f"  {_YTDLP_UPGRADE_COMMAND}"
                    )
                return "warn", (
                    f"yt-dlp 已安装但未配置 JS runtime。运行：\n  {render_ytdlp_fix_command()}"
                )
        # Surface transcription readiness so `doctor` reports it.
        msg = "可提取视频信息和字幕"
        if config is not None:
            providers = []
            if config.is_configured("groq_whisper"):
                providers.append("groq")
            if config.is_configured("openai_whisper"):
                providers.append("openai")
            if providers:
                missing_media_tools = [
                    tool
                    for tool in ("ffmpeg", "ffprobe")
                    if not shutil.which(tool)
                ]
                if missing_media_tools:
                    msg += (
                        "（音频转写需安装 "
                        + "、".join(missing_media_tools)
                        + "）"
                    )
                else:
                    msg += f"，可转写音频（{'/'.join(providers)}）"

        # Last gate: everything above proves yt-dlp RUNS, not that it can still
        # extract. A stale release passes every check yet fails on real URLs,
        # so refuse to report "ok" for one — the prescription is the same
        # single command either way.
        version = _parse_ytdlp_version(probe.output)
        age_days = _ytdlp_release_age_days(version) if version else None
        if age_days is not None and age_days > _YTDLP_STALE_AFTER_DAYS:
            return "warn", (
                f"yt-dlp 版本已发布 {age_days} 天（{'.'.join(map(str, version))}），"
                "YouTube 提取器可能已失效（典型报错：unable to extract yt initial "
                f"data）。升级：\n  {_YTDLP_UPGRADE_COMMAND}\n"
                "注意 `yt-dlp -U` 无法更新 pip 安装的版本。"
            )
        return "ok", msg

    def transcribe(
        self,
        url: str,
        *,
        provider: str = "auto",
        config=None,
        allow_provider_fallback: bool = False,
    ) -> str:
        """Download a YouTube video's audio and return its transcript.

        Delegates to :func:`agent_reach.transcribe.transcribe`. Imported lazily
        so the channel module stays cheap to import for users who never
        transcribe.
        """
        from agent_reach.transcribe import transcribe as _transcribe

        return _transcribe(
            url,
            provider=provider,
            config=config,
            allow_provider_fallback=allow_provider_fallback,
        )
