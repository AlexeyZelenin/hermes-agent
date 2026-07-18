"""CLI entrypoint: ``python -m zeus_watchdog [--config PATH] [--dry-run]``.

One pass per invocation — the launchd agent calls this on an interval. Prints a
terse one-line status to stdout (which launchd captures to the log) so a human
tailing the log sees "ok" when all is quiet and the fired keys otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import default_home, load
from .runner import run_once
from .state import PROBLEM


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zeus_watchdog", description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="JSON config overlay")
    parser.add_argument("--home", type=Path, default=None, help="hermes root override")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="evaluate and print but send nothing and do not touch state",
    )
    args = parser.parse_args(argv)

    default_cfg = default_home() / "zeus" / "watchdog.config.json"
    config_path = args.config or (default_cfg if default_cfg.is_file() else None)
    cfg = load(config_path=config_path, home=args.home)

    result = run_once(cfg, dry_run=args.dry_run)

    if not result.conditions:
        print("watchdog: ok (all quiet)")
        return 0
    fired = ",".join(result.conditions)
    problems = sum(1 for a in result.alerts if a.kind == PROBLEM)
    recoveries = len(result.alerts) - problems
    verb = "would-alert" if args.dry_run else f"sent {result.delivered}"
    print(
        f"watchdog: conditions=[{fired}] alerts={problems}p/{recoveries}r ({verb})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
