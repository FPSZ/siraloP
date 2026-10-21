"""In-process message bus: solvers share structured, de-duplicated Findings.

Rule (ADR-0002 §5): share structured Finding entries only — never raw tool streams.
"""
from __future__ import annotations

from collections import defaultdict

from agent.domain.state import Finding


class MessageBus:
    def __init__(self) -> None:
        self._by_challenge: dict[str, list[Finding]] = defaultdict(list)
        self._seen: set[tuple[str, str]] = set()

    def publish(self, challenge_id: str, finding: Finding) -> bool:
        key = (challenge_id, f"{finding.category}:{finding.content}")
        if key in self._seen:
            return False
        self._seen.add(key)
        self._by_challenge[challenge_id].append(finding)
        return True

    def findings(self, challenge_id: str) -> list[Finding]:
        return list(self._by_challenge[challenge_id])
