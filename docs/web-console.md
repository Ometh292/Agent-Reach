# Web Console

A local web interface for Agent Reach. Start it with:

```bash
agent-reach ui
```

Your browser opens on a link containing a one-time session token. Press Ctrl+C
in the terminal to stop.

```bash
agent-reach ui --port 8766     # if 8765 is taken
agent-reach ui --no-browser    # print the URL instead of opening it
```

## What it does

| Tab | |
|---|---|
| **Dashboard** | Live `doctor` status for all 15 channels, with the active backend for each |
| **Search** | Search any configured platform — Exa, GitHub, V2EX, B站, Twitter, 雪球, Reddit, 小红书, Facebook, Instagram |
| **Read a page** | Any URL. YouTube links return details and subtitles, V2EX threads return post plus replies, everything else is read as clean text |
| **Trending** | Listing views that need no search term |
| **Transcribe** | Audio to text via Whisper, with a copy button |
| **Setup** | Run the installer (check-only, dry-run, or real), reinstall the agent skill, check for updates, see which tools are present |
| **Settings** | Write API keys, tokens, cookies and proxy settings |

Long operations — installs, transcription — run as background jobs and stream
their output into the page as it arrives.

## Security

This server runs real commands on your machine, so it is built to be safe to
leave running.

**It is reachable only from this machine.** The server binds `127.0.0.1` and
refuses to start on any other address. There is no flag to expose it to the
network, deliberately: making it remotely reachable would turn it into a remote
shell.

**Every request needs the session token.** A fresh token is generated on each
start and printed in the launch URL. It is never written to disk. Loading the
page requires it too — serving the page means handing over the token embedded
in it.

**Cross-origin requests are refused.** The `Host` header must name a loopback
address and any `Origin` must match this exact server. This blocks DNS
rebinding, where a hostile site points its own domain at `127.0.0.1` and drives
this API from the browser you are already signed into.

**The browser never sends a command line.** It names an operation from a fixed
allowlist (`agent_reach/webui/operations.py`) plus typed parameters; the server
builds the argument list itself. `shell=True` is never used, URLs go through the
same public-address validation the channels use, and channel names are checked
against a known set. A request body cannot become a command.

**Secrets are one-way.** Settings can write an API key or cookie but never reads
one back: values are masked by `Config.to_dict()` before they leave the process,
the response to a save names only the key, and the input is cleared once saved.
Command output is scrubbed of URL credentials before reaching the page.

### What it does not protect against

Anything already running as you on this machine can read the token from the
server's own memory or read `~/.agent-reach/config.yaml` directly — but such a
process could do that whether or not the console is running. The console adds no
new exposure there.

Don't port-forward it, tunnel it, or put it behind a reverse proxy. If you need
remote access, use SSH to the machine and run the CLI.

## Troubleshooting

**"Could not start the web UI … address already in use"** — a previous session
is still running, or another program holds the port. Use `--port 8766`.

**"Missing or invalid session token"** — the link is from an earlier run. Tokens
change on every start; use the URL the current terminal printed.

**A search reports a tool is not installed** — the Setup tab shows what is
present and can install what is missing.

**Nothing opens** — use `--no-browser` and paste the URL yourself. On a headless
machine there is no browser to open, and the console is for desktop use anyway.
