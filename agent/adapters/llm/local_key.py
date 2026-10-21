"""LocalKeyAdapter (dev only) — LLMPort using your own provider key for fast local iteration.

Implements ports.llm.LLMPort. TODO(team): implement every method.
"""
from __future__ import annotations


class LocalKeyAdapter:
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("implement LocalKeyAdapter")
