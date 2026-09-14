# Agent Reach — hosted client application (agent_reach.webserver)
#
# This image serves the client-facing web app ONLY. It deliberately does not
# carry the local operator console (`agent-reach ui`), which installs software
# and writes credentials and must never be exposed.
#
# Build:  docker build -t agent-reach .
# Run:    docker run --rm -p 8000:8000 --env-file .env agent-reach

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg is the only system dependency. Transcription pipes yt-dlp's audio
# through it before sending it to Whisper, and yt-dlp needs it to merge
# streams. There is deliberately no Node runtime: Exa is reached by speaking
# MCP over plain HTTP, so the one thing that used to need Node no longer does.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY pyproject.toml constraints.txt README.md LICENSE ./
COPY agent_reach/ ./agent_reach/

# constraints.txt pins the tested dependency set. yt-dlp is deliberately a
# floor rather than a pin: YouTube breaks extractors every few weeks, and a pin
# here would ship an image that cannot read YouTube on the day it is built.
#
# twitter-cli is what serves the Twitter/X channel when a user connects their
# own session. Without it that platform reports "not installed" rather than
# failing obscurely, so the image carries it and the feature works.
RUN pip install -c constraints.txt ".[server]" "twitter-cli>=0.8.5" \
 && python -c "import fastapi, uvicorn, jwt, agent_reach; print('agent-reach', agent_reach.__version__)" \
 && ffmpeg -version | head -1 \
 && yt-dlp --version

# Runs as a normal user: nothing here needs root, and a public deployment is
# the last place to keep it.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
 && mkdir -p /srv/work \
 && chown -R app:app /srv/work
USER app
ENV HOME=/home/app

# A writable working directory that is not the source tree. Transcription does
# its real work in the system temp directory, and yt-dlp writes nothing here
# (--print implies simulate), so this is defensive rather than required: any
# tool that does resolve a relative path has somewhere to put it, and /srv
# stays root-owned and read-only to the app.
WORKDIR /srv/work

# Documentation only; Render publishes whatever $PORT it assigns.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz',timeout=4)"

# 0.0.0.0 is required: a container host routes to the published port, and
# binding loopback would make the service unreachable. proxy-headers lets the
# app see the real client address through Render's load balancer, which the
# per-user rate limiting depends on.
CMD ["sh", "-c", "exec uvicorn agent_reach.webserver.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
