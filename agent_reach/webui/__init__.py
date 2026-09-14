# -*- coding: utf-8 -*-
"""Local web console for Agent Reach.

Started with `agent-reach ui`. Binds loopback only and executes just the
allowlisted operations in `operations.py`.
"""

from .server import serve  # noqa: F401

__all__ = ["serve"]
