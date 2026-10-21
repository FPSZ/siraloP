"""CtfdAdapter (dev only) — talk to a CTFd-style platform / practice sets to get the loop working.

Implements ports.platform.PlatformPort. TODO(team): implement every method.
"""
from __future__ import annotations


class CtfdAdapter:
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("implement CtfdAdapter")
