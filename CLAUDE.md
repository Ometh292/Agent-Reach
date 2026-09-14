# CLAUDE.md

## Project
Agent Reach — Python CLI + library that gives AI agents read/search access to 15 internet platforms.
Positioning: installer + doctor + config tool. NOT a wrapper — after install, agents call upstream tools directly.
Repo: github.com/Panniantong/Agent-Reach | License: MIT | Version: 1.5.0

## Commands
- `pip install -e .` — Dev install
- `pytest tests/ -v` — All tests
- `pytest tests/test_cli.py -v` — CLI tests only
- `bash test.sh` — Full integration test (creates venv, installs, runs doctor + channel tests)
- `python -m agent_reach.cli doctor` — Run diagnostics
- `python -m agent_reach.cli install --env=auto` — Auto-configure

## Structure
- `agent_reach/cli.py` — CLI entry point (argparse)
- `agent_reach/core.py` — Core read/search routing logic
- `agent_reach/config.py` — Config management (YAML, env vars)
- `agent_reach/doctor.py` — Diagnostics engine
- `agent_reach/channels/` — One file per platform (twitter.py, reddit.py, youtube.py, etc.)
- `agent_reach/channels/base.py` — Base channel class (all channels inherit from this)
- `agent_reach/integrations/mcp_server.py` — MCP server integration
- `agent_reach/skill/` — OpenClaw skill files
- `agent_reach/guides/` — Usage guides
- `tests/` — pytest tests
- `config/mcporter.json` — MCP tool config

## Conventions
- Python 3.10+ with type hints
- Each channel is a single file in `channels/`, inherits from `BaseChannel`
- Channel contract: `can_handle(url)` is the only abstract method; `check(config)` has a
  default in `BaseChannel` and should be overridden by any channel with an external backend.
  Most channels deliberately have NO `read`/`search` — Agent Reach routes and health-checks,
  agents call the upstream tool directly. Only `web`, `v2ex`, and `xueqiu` expose data methods.
- `backends` is an ORDERED list: `backends[0]` is preferred, the rest are fallbacks. Switching
  backends means reordering the list, not rewriting code. `check()` must set `active_backend`
  to whatever is really serving the channel (None when nothing usable is found).
- `check()` must not cause side effects: never run a command that writes credentials, starts a
  daemon, or lets an upstream tool auto-read browser cookies (that is why `gh auth status`,
  `twitter status`, `rdt status` and `opencli doctor` are all deliberately NOT executed).
  Prefer `warn` over a `ok` you cannot prove.
- Use `loguru` for logging, `rich` for CLI output
- Commit format: `type(scope): message` (one commit = one thing)
- All upstream tool calls go through public API/CLI, never hack internals

## Rules
- NEVER modify upstream open source projects' source code
- Agent Reach is a "glue layer" — only route and call, don't reimagine
- Version lives in TWO places and must match: `pyproject.toml` and `agent_reach/__init__.py`.
  (No test asserts this, so a mismatch will not be caught — check both by hand on a bump.)
- Always new branch for changes, PR to main, never push to main directly
- Run `pytest tests/ -v` before committing — all tests must pass
- `constraints.txt` pins everything EXCEPT `yt-dlp`/`yt-dlp-ejs`, which must stay floors:
  YouTube breaks extractors every few weeks, so a pin there ships a build that cannot read
  YouTube. Upgrade with `python -m pip install -U "yt-dlp[default]"` (`yt-dlp -U` cannot
  update a pip install)
- On Windows, 4 symlink tests skip unless the shell is elevated or Developer Mode is on
  (`WinError 1314`); that is expected, not a regression
- Cookie-based auth (Twitter, XHS): use Cookie-Editor export method only, no QR scan
- XHS login: Cookie-Editor browser export only (QR will hang)
