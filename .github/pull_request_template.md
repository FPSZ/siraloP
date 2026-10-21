<!-- Keep PRs small and single-purpose. See CONTRIBUTING.md. -->

## What & why
<!-- One or two sentences: what this changes and the reason. Link the ADR/issue. -->

## Scope (tick what applies)
- [ ] domain (pure logic) — no new I/O, no `httpx`/`anyio` import
- [ ] adapter (implements a port) — contract test added/updated
- [ ] port signature change — **ADR + team heads-up** (frozen contract)
- [ ] `agent/domain/state.py` schema change — landed as its own small PR
- [ ] docs only

## Checklist
- [ ] `python tools/check.py` is green (ruff + import-linter + pytest)
- [ ] New/changed behavior is covered by a test (fakes/MockTransport, no real network)
- [ ] No secret, token, or `external-agents/` content is committed
- [ ] Wording stays neutral/defensive (this is an authorized competition agent)

## Notes for reviewers
<!-- Anything non-obvious: trade-offs, follow-ups, things you are unsure about. -->
