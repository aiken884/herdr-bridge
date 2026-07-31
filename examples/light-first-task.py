#!/usr/bin/env python3
"""Light mode's first task -- programmatic call example.

Regular users should use the CLI:
    herdr-commander run
    herdr-commander run --dry-run

This file demonstrates how to call LightCommander via the Python API.
"""

from __future__ import annotations

import argparse
import sys

from herdr_bridge import connect
from herdr_bridge.light import LightCommander


def main() -> int:
    ap = argparse.ArgumentParser(description="Run light mode's first task")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--socket", default=None)
    args = ap.parse_args()

    try:
        actions = connect(socket_path=args.socket)
    except Exception as exc:  # noqa: BLE001
        print("Could not connect to the working environment. Run herdr-commander start first.", file=sys.stderr)
        print(f"({type(exc).__name__})", file=sys.stderr)
        return 1

    result = LightCommander(actions).run_first_task(
        timeout_sec=args.timeout,
        dry_run=args.dry_run,
    )
    print(result.user_text())
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
