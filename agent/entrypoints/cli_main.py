"""Local CLI entrypoint (the ``ctfy-agent`` console script).

A thin, dependency-free dispatcher that always exits cleanly. The full local
harness (run against a folder of practice challenges) is future work; today this
reports the version and points you at the dev entrypoint.
"""
from __future__ import annotations

import argparse


def main() -> int:
    p = argparse.ArgumentParser(prog="ctfy-agent", description="ctfy-agent local CLI")
    p.add_argument("path", nargs="?", help="local challenges dir (reserved for future use)")
    p.add_argument("--version", action="store_true", help="print version and exit")
    args = p.parse_args()

    if args.version:
        from agent import __version__

        print(__version__)
        return 0

    print(
        "ctfy-agent: for a dev run use\n"
        "  python -m agent.entrypoints.connected_main --challenge <id>\n"
        "the submission image runs agent.entrypoints.image_main automatically."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
