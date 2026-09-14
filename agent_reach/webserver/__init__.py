# -*- coding: utf-8 -*-
"""Client-facing hosted application.

Separate from `agent_reach.webui`, which is the operator's local console and
must never be exposed: that one installs software and writes credentials.

This package serves a public, authenticated, read-only subset:
    browser -> Supabase auth -> FastAPI -> Agent Reach channels -> the internet
"""
