#!/usr/bin/env python
"""Run the repository quality gates and print a per-tool summary.

Required (a failure exits non-zero): ruff, import-linter, pytest.
Advisory (reported, never blocks): mypy — types are being adopted gradually.

Usage:  python tools/check.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys


def _importlinter_cmd() -> list[str]:
    exe = shutil.which("lint-imports")
    return [exe] if exe else [sys.executable, "-m", "importlinter"]


REQUIRED: list[tuple[str, list[str]]] = [
    ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    ("import-linter", _importlinter_cmd()),
    ("pytest", [sys.executable, "-m", "pytest", "-q"]),
]
ADVISORY: list[tuple[str, list[str]]] = [
    ("mypy", [sys.executable, "-m", "mypy", "agent"]),
]


def _run(name: str, cmd: list[str]) -> bool:
    print(f"\n==== {name} ====", flush=True)
    try:
        return subprocess.run(cmd).returncode == 0
    except FileNotFoundError:
        print(f"  (skipped: {name} not installed)")
        return False


def main() -> int:
    required_names = {n for n, _ in REQUIRED}
    results: dict[str, bool] = {}
    for name, cmd in (*REQUIRED, *ADVISORY):
        results[name] = _run(name, cmd)

    print("\n==== summary ====")
    for name, _ in (*REQUIRED, *ADVISORY):
        ok = results[name]
        tag = "ok" if ok else ("FAIL" if name in required_names else "warn")
        print(f"  {name:14} {tag}")

    failed = [n for n in required_names if not results[n]]
    if failed:
        print(f"\nFAILED (required): {', '.join(sorted(failed))}")
        return 1
    print("\nall required gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
