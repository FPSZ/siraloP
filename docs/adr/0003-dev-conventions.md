# ADR-0003: Development conventions (testing, tooling, collaboration)

- Status: Accepted
- Date: 2026-10-22
- Related: ADR-0001 (architecture), ADR-0002 (domain internals)

## Context

Several people and AI coding agents build this backend in parallel, on a hard
competition deadline. Parallel work is fast only if the boundaries are explicit
and machine-checked — otherwise the merge hotspots (the shared schema, the port
signatures) and silent contract drift eat the speed-up. We also must never ship
a run that fails to produce `result.json`, leak a credential, or upload the
`external-agents/` research material.

## Decision

**1. Ports are frozen contracts; the domain stays pure.** `agent/ports/*` and
`agent/domain/state.py` change only deliberately (ADR + heads-up / own small
PR). `agent/domain/` may import only `ports` + `state`; never adapters, wiring,
entrypoints, `httpx`, or `anyio`. This is enforced by **import-linter**
(`[tool.importlinter]`), not by convention alone.

**2. Fakes-first, contract-tested.** `agent/testing/fakes.py` ships one faithful
in-memory fake per port. Every adapter has a `tests/contract/` test (using the
fakes or `httpx.MockTransport`) proving it honors its port. **No test touches the
real network or real credentials.** `pytest` runs in `asyncio_mode = "auto"`.

**3. One gate.** `python tools/check.py` runs ruff + import-linter + pytest as
**required** and mypy as **advisory** (types are adopted gradually; the os-level
process calls are platform-specific and only type-clean on Linux). CI
(`.github/workflows/ci.yml`) runs the same gate on push/PR.

**4. Docs where the work is.** A canonical root `AGENTS.md` (imported by
`CLAUDE.md`) plus per-directory `AGENTS.md` files (deeper = more specific wins);
`CONTRIBUTING.md` for mechanics; `docs/llms.txt` as the machine-readable index.

**5. Collaboration guardrails.** Small single-purpose PRs; file ownership via
`CODEOWNERS`; a PR template checklist. Schema changes land alone. `.env`,
secrets, and `external-agents/` are gitignored and must never be committed;
`result.json` is secret-scrubbed before writing.

**6. The non-negotiable runtime guarantee.** The image entrypoint writes a valid
`result.json` on *every* exit path (normal, error, cancellation) and always
exits 0 (CTFY-0001 §3). The result sink is sync, atomic, and never raises.

## Consequences

- A wrong-direction import or a broken port contract fails the gate instead of
  surfacing as a confusing runtime bug days later.
- New contributors (human or AI) have a short, local, authoritative rule set and
  can work a slice without reading the whole tree.
- mypy is not blocking today; tightening it (removing `ignore_missing_imports`,
  fixing the platform-specific `os.*` typing) is a tracked follow-up.
- A little ceremony around `ports/` and `state.py` is the cost of safe
  parallelism; it is deliberate.
