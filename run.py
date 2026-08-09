"""wnba-props entry point. Every project action is a subcommand of this file."""

import argparse
import sys

from src.log import get_logger

# Which build phase delivers each subcommand.
NOT_IMPLEMENTED = {
    "clean": 3,
    "train": 5,
    "project": 8,
    "evaluate": 9,
    "audit": 3,
}

HELP = {
    "update": "fetch new games and odds since last run",
    "clean": "rebuild clean tables from raw",
    "train": "fit models, save to disk",
    "project": "today's slate -> console + CSV",
    "evaluate": "calibration and scoring on holdout",
    "audit": "data quality report",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="WNBA player prop projection model",
    )
    subparsers = parser.add_subparsers(dest="command")
    for name, help_text in HELP.items():
        sub = subparsers.add_parser(name, help=help_text)
        if name == "update":
            sub.add_argument(
                "--dry-run", action="store_true",
                help="re-parse the latest saved raw odds files; no network calls")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "update":
        from src.ingest_odds import run_odds

        if args.dry_run:
            return run_odds(dry_run=True)

        from src.ingest_stats import run_update
        from src.log import get_logger as _gl

        rc = 0
        try:
            rc = run_update()
        except Exception as e:  # noqa: BLE001 — a dead network must not traceback
            _gl("run").info(
                f"stats update failed ({type(e).__name__}: {e}) — "
                "continuing to odds snapshot")
            rc = 1
        return max(rc, run_odds())

    log = get_logger("run")
    phase = NOT_IMPLEMENTED[args.command]
    log.info(f"{args.command}: not implemented — phase {phase} builds this")
    return 0


if __name__ == "__main__":
    sys.exit(main())
