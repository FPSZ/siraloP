# CLAUDE.md

@AGENTS.md

## Claude Code notes (in addition to AGENTS.md)

- Before you finish a task, run `python tools/check.py` and make sure the
  required gates (ruff + import-linter + pytest) are green. Do not weaken a
  test to make it pass.
- Treat `agent/ports/*` and `agent/domain/state.py` as frozen: change them only
  deliberately (ADR + heads-up), never as a drive-by.
- When you implement an adapter, copy the shape of an existing one in the same
  family and add a `tests/contract/` test using the fakes — never hit the real
  network.
- `external-agents/` is local research material. Never read it into the product
  code, and never commit or upload it.
- Prefer editing one file/family per change; `state.py`, `wiring.py`, and
  `pyproject.toml` are shared — touch them in their own focused change.
