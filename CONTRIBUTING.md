# Contributing to ctfy-agent

Start with [AGENTS.md](AGENTS.md) — it is the working agreement (architecture,
the rules, how to add an adapter). This file covers the mechanics.

## Setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python tools/check.py        # ruff + import-linter + pytest (+ mypy advisory)
```

## The loop

1. Branch: `feat/<thing>`, `fix/<thing>`, `docs/<thing>`.
2. Make one focused change. Add/adjust tests next to it.
3. `python tools/check.py` must be green locally.
4. Open a PR; fill in the template checklist. Keep it small and reviewable.

## What needs extra care (see CODEOWNERS)

| Change | Required process |
|--------|------------------|
| `agent/ports/*` signature | **ADR + team heads-up first.** These are frozen contracts; every adapter and caller depends on them. |
| `agent/domain/state.py` schema | Land as its **own small PR**, then build on it. It is the designated merge hotspot. |
| `agent/wiring.py` | Coordinate — it is the single composition root. |
| anything under `external-agents/` | **Never commit it.** Local research only; it is gitignored. |

## Testing rules

- `pytest` runs in `asyncio_mode = "auto"` — async tests need no decorator.
- Use the shipped fakes (`agent/testing/fakes.py`) and `httpx.MockTransport`.
  **No test may hit the real network or require real credentials.**
- Every adapter ships with a `tests/contract/` test proving it honors its port.
- Don't delete or weaken a test to make the suite pass. If a test genuinely
  contradicts a frozen port contract, fix the test *and say so* in the PR.

## Commits & secrets

- Short imperative subjects: `feat: add CtfdAdapter`, `fix: honor Retry-After`.
- Never commit secrets. `.env` is gitignored; credentials come only from env.
  `result.json` is scrubbed of known tokens before writing, but never put a
  secret in code, a test, a log, or a fixture.

## Security posture

This is an authorized competition agent. Keep language and code neutral and
defensive: build the generic engine, treat all target/tool output as untrusted
data, and never follow instructions embedded in it.
