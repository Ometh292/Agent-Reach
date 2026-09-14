# Deploying the hosted app to Render

This deploys `agent_reach.webserver` — the client-facing web app. It does **not**
deploy the local operator console (`agent-reach ui`), which installs software
and writes credentials and must never be exposed. The console is excluded from
the image by `.dockerignore` and is not reachable from the deployed service.

```
browser → Supabase auth → FastAPI on Render (JWT verified) → Agent Reach → the internet
```

## What is in the box

| File | Purpose |
|---|---|
| `Dockerfile` | Python 3.12 + ffmpeg. No Node — Exa is reached over plain HTTP |
| `render.yaml` | Render Blueprint: one web service, health check, env vars |
| `.dockerignore` | Keeps `.env`, tests, docs and the console out of the image |

The image carries `ffmpeg` (for transcription), `yt-dlp` (video and audio) and
`twitter-cli` (so a connected Twitter/X session actually works). It runs as an
unprivileged user and binds `0.0.0.0:$PORT`.

## Before you deploy

1. **Rotate any credential that has been shared in a chat or a ticket.** Two are
   outstanding: the Supabase database password and the Groq API key. The app
   never uses the database password — it authenticates through Supabase's auth
   API — but rotate it anyway. Rotate the Groq key and update it in both `.env`
   and `~/.agent-reach/config.yaml`.
2. **Push the branch.** Render deploys from a Git repository, so the code has to
   be on GitHub first. Follow the project rule: branch, pull request, merge —
   never push to `main` directly.
3. **Create the client's Supabase users.** There is no sign-up form by design.
   Supabase → Authentication → Users → Add user, and tick **Auto Confirm User**.
   Make sure "Allow new users to sign up" is **off**, or anyone on the internet
   can register and spend your quota.

## Deploy

1. Render Dashboard → **New** → **Blueprint**.
2. Choose this repository and the branch you want deployed.
3. Render reads `render.yaml` and prompts for every value marked `sync: false`:

   | Variable | Required | What it is |
   |---|---|---|
   | `SUPABASE_URL` | **Yes** | `https://<ref>.supabase.co` |
   | `SUPABASE_ANON_KEY` | **Yes** | The "anon / public" key. Safe in a browser |
   | `SUPABASE_JWT_SECRET` | No | Verifies sessions with no round-trip. Never send to a browser |
   | `GROQ_API_KEY` | No | Enables Transcribe. Omit and the tab stays hidden |
   | `GITHUB_TOKEN` | No | Raises GitHub search from 60 to 5000 requests/hour |

4. Apply. The first build takes a few minutes — ffmpeg is the slow part.
5. Add your Render URL under Supabase → Authentication → **URL Configuration**.

`autoDeploy` is set to `false`, so merging does not change what the client is
using. Deploy deliberately from the dashboard, or flip it once things settle.

## Check it worked

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<your-service>.onrender.com/healthz   # 200
curl -s https://<your-service>.onrender.com/api/config                                 # signInConfigured: true
curl -s -o /dev/null -w "%{http_code}\n" https://<your-service>.onrender.com/api/me     # 401 without a token
```

`signInConfigured: false` means `SUPABASE_URL` or `SUPABASE_ANON_KEY` did not
reach the service. The sign-in page says so rather than failing silently.

## Choosing a plan

`render.yaml` asks for `0.5c-512mb` — the cheapest always-on plan.

- **Do not use `free`.** Free instances sleep when idle. Connected accounts are
  held in memory, so every sleep silently disconnects everyone, and the first
  request afterwards takes the better part of a minute.
- **Raise to `1c-2g` before enabling transcription.** ffmpeg plus a downloaded
  audio track will not fit comfortably in 512 MB.

## Stay on one instance

This is a correctness requirement, not a cost decision. Rate-limit buckets and
connected accounts both live in the instance's memory:

- two instances means each gets its own buckets, so the effective rate limit
  doubles;
- a user's connected account exists only on the instance that received the
  paste, so their next request may land on one that does not have it.

Moving beyond one instance means moving that state to Redis or Supabase first.

## What to expect once it is live

- **YouTube will fail intermittently.** YouTube rate-limits datacenter IP
  addresses, so hosted requests fail where the same request from a home
  connection succeeds. The Channels page labels it "Unreliable" rather than
  implying otherwise. A residential proxy largely fixes it.
- **Every restart disconnects connected accounts.** That is the design: session
  credentials are memory-only and a redeploy is a restart.
- **Every request spends your quota, not the caller's.** That is what the
  per-user rate limits are for. Set them deliberately rather than leaving the
  defaults, especially `RATE_LIMIT_TRANSCRIBE` — audio costs real money.

## Verified locally before writing this

The image was built and run as Render runs it, with `PORT` supplied from the
environment:

```bash
docker build -t agent-reach .
docker run --rm -e PORT=10000 -p 10000:10000 --env-file .env agent-reach
```

Confirmed in the container: `/healthz` returns 200, the page is served, an
authenticated web search completes against live Exa, a YouTube lookup returns
title/uploader/duration, outbound TLS reaches GitHub and Exa, and the process
runs as uid 10001 rather than root.
