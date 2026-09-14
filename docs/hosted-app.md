# Hosted client application

A client-facing web app: sign in with Supabase, then search the web, read pages,
pull YouTube details, and transcribe audio. No commands, no terminal.

This is **separate from the local console** (`agent-reach ui`). That one runs on
an operator's own machine, installs software and writes credentials, and binds
loopback only. It must never be exposed. This application shares the channel
implementations but exposes a read-only subset.

```
browser → Supabase auth → FastAPI (JWT verified) → Agent Reach → the internet
```

The local setup is the same architecture as production — the only difference is
where the environment variables come from.

## Running locally

```bash
pip install -e ".[server,dev]"

export SUPABASE_URL="https://<ref>.supabase.co"
export SUPABASE_ANON_KEY="<anon key>"
export SUPABASE_JWT_SECRET="<jwt secret>"     # optional but faster

uvicorn agent_reach.webserver.app:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000>. The frontend is served by the backend — there is
no separate build step and no Node toolchain.

On Windows PowerShell, set variables with `$env:SUPABASE_URL = "…"`.

## Supabase setup

1. **Settings → API** — copy the Project URL, the `anon` key, and (optionally)
   the JWT Secret.
2. **Authentication → Sign In / Providers → Email** — enable it, and turn
   **"Allow new users to sign up" OFF**. Create the client's accounts yourself
   under Authentication → Users. Otherwise anyone on the internet can register
   and spend your search and transcription quota.
3. **Authentication → URL Configuration** — add your site URL.

No database tables are required. Supabase is used only for authentication.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_URL` | Yes | Project URL |
| `SUPABASE_ANON_KEY` | Yes | Public key, served to the browser |
| `SUPABASE_JWT_SECRET` | No | Verifies sessions locally with no round-trip. Without it the server asks Supabase per new token, which also works |
| `GITHUB_TOKEN` | No | Raises GitHub search from 60 to 5000 requests/hour |
| `GROQ_API_KEY` | No | Enables Transcribe. Without it the tab is hidden |
| `ALLOWED_ORIGINS` | No | Comma-separated origins for cross-origin access. Empty (default) means none |
| `RATE_LIMIT_*` | No | `requests/seconds`, e.g. `RATE_LIMIT_SEARCH=60/3600` |
| `HOST` / `PORT` | No | Bind address. Container hosts set `PORT` themselves |

`SUPABASE_SERVICE_ROLE_KEY` is **not used** by this application. It bypasses Row
Level Security; never put it in frontend code.

## What works, and what cannot

**Available** — needs nothing from the end user:

| | |
|---|---|
| Web pages | Any public URL as clean text, via Jina Reader |
| Web search | Exa, called over MCP-HTTP directly (no Node required) |
| GitHub | Repository search over the REST API |
| V2EX | Search, hot topics, threads with replies |
| RSS / Atom | Any public feed |
| YouTube | Details and subtitles — *unreliable*, see below |
| Transcription | yt-dlp → ffmpeg → Whisper. Off unless a key is set |

**Needs the end user's own account.** **Twitter/X** and **雪球** can be
connected for the duration of a session — see the next section. **Reddit**,
**小红书** and **LinkedIn** could also run on a server, but are not offered
here; the reasons are given below.

**Cannot work in a hosted service at all**: **Facebook** and **Instagram**.
Their only backend drives a signed-in desktop Chrome window through a browser
extension, and a server has no desktop browser. Meta's official APIs could be
built instead, but that is a separate project behind app review. The Channels
page says this plainly rather than implying they are merely unconfigured.

**YouTube is marked "Unreliable" on purpose.** YouTube rate-limits datacenter
IPs aggressively, so requests from any hosted provider fail intermittently. A
residential proxy largely fixes it. Transcription inherits the same limitation.

## Connecting your own account (session-only)

Some platforms only serve content to someone signed in. Rather than storing
those logins, this application holds them **in memory, for one session, and
never writes them anywhere**.

On the Channels page, a supported platform shows a **Connect** button. Paste a
Cookie-Editor "Header String" export and it becomes available as a search
source until you sign out.

| Platform | What is needed |
|---|---|
| Twitter / X | `auth_token` and `ct0` from x.com |
| 雪球 Xueqiu | `xq_a_token` from xueqiu.com |

Twitter search additionally needs `twitter-cli` present in the image
(`pipx install twitter-cli`); without it the platform reports that it is not
installed rather than failing obscurely.

### What the guarantee is

* Held in this process's memory only. Never written to disk, never to a
  database, never logged.
* Forgotten on sign-out, after an hour idle, after eight hours regardless, and
  on every restart or redeploy.
* Only the cookies the tool needs are kept; the rest of the paste is discarded
  immediately.
* No endpoint returns a credential. `/api/connections` reports *which*
  platforms are connected and never *with what*.
* Passed to tools through the environment, never as command arguments, so it
  cannot appear in the host's process list.
* One user's credential can never serve another's request. Xueqiu keeps its
  cookie jar in module state, so those calls are serialised and the jar is
  cleared before and after each one.

### What it is not

Say this plainly to users rather than implying more safety than exists:

* **It is in memory while in use.** Anyone who can read the process — a host
  operator, a crash dump, a debugger — can read it. This is a large reduction
  in risk, not elimination.
* **Python strings cannot be reliably wiped.** A disconnected credential may
  linger in freed memory until the allocator reuses it.
* **It crosses the network to reach the server.** Never run this over plain
  HTTP; TLS is doing real work here.
* **The platform may still object.** X and LinkedIn forbid automated access and
  suspend accounts used this way. That risk belongs to the account holder and
  should be stated before they connect.

### Platforms not offered this way, and why

**Reddit** and **小红书** can run on a server, but their tools read credentials
from a *file* (`~/.config/rdt-cli/credential.json`, and a cookie file for the
xiaohongshu-mcp service). Supporting them session-only means writing that file
for the duration of a call — which contradicts the guarantee above unless it is
written to memory-backed storage such as `/dev/shm`, which exists on Linux but
not on every host. They are left out until that is designed deliberately rather
than bolted on.

**LinkedIn** runs its own interactive login and persists credentials itself, so
this application cannot promise anything about their lifetime.

**Facebook** and **Instagram** have no server-capable backend at all.

## Security

**Sessions are verified server-side.** The API is reachable from the internet;
protecting only the frontend would protect nothing, since anyone can call the
endpoints with curl. Every `/api` route verifies the Supabase JWT — signature,
expiry, and audience — before anything runs.

**There is no command surface.** The browser names an operation from a fixed
allowlist and the server builds every argument itself. `install`, `config.set`,
`config.delete` and `skill` do not exist in this application. No `shell=True`
anywhere.

**URLs are checked before they are fetched.** A hosted server sits inside a
provider's network where a link-local address reaches the instance metadata
service, so every user-supplied URL goes through the same public-address
validator the channels use. Loopback, private ranges, link-local, and non-HTTP
schemes are rejected.

**Rate limits are per user, per operation.** Each request spends the operator's
quota, not the caller's. Transcription is deliberately much tighter than search.

**Secrets stay server-side.** `/api/config` serves only the project URL and anon
key. Errors are generic; stack traces are never returned. Command output passes
through `scrub_url_credentials` before reaching the browser.

**Job results are private.** Polling another user's job id returns the same 404
as a job that does not exist, so ids cannot be probed.

### Known limitation

Rate limit state lives in the process. On one instance that is correct; if the
service is scaled to several instances, each keeps its own counters and the
effective limit multiplies by the instance count. Move it to Redis or Supabase
before scaling out.

## Testing

```bash
pytest tests/test_webserver.py -q      # 59 tests: auth, injection, SSRF, limits
pytest tests/ -q                       # whole suite
```

The hosted tests use real HS256 tokens through the production verification path
rather than patching authentication out.
