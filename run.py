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
        subparsers.add_parser(name, help=help_text)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "update":
        from src.ingest_stats import run_update

        return run_update()

    log = get_logger("run")
    phase = NOT_IMPLEMENTED[args.command]
    log.info(f"{args.command}: not implemented — phase {phase} builds this")
    return 0


if __name__ == "__main__":
    sys.exit(main())
