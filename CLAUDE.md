@AGENTS.md

# CLAUDE.md

AGENTS.md (imported above) is the single source of truth — commands, style,
architecture, security, and BOUNDARIES. Per-directory `AGENTS.md` files refine
it; the nearest one wins. Follow them.

Definition of done: `python tools/check.py` is green (ruff + import-linter +
pytest). Never weaken a test to pass.
