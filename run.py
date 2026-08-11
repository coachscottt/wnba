"""wnba-props entry point. Every project action is a subcommand of this file."""

import argparse
import sys

from src.log import get_logger

# Which build phase delivers each subcommand.
NOT_IMPLEMENTED = {
    "evaluate": 9,
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
            sub.add_argument(
                "--backfill", nargs=2, metavar=("START", "END"),
                help="fetch historical odds snapshots for ET dates START..END "
                     "(10x credit cost)")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "update":
        from src.ingest_odds import run_odds

        if args.dry_run:
            return run_odds(dry_run=True)
        if args.backfill:
            from src.ingest_odds import run_backfill

            return run_backfill(*args.backfill)

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

    if args.command == "project":
        from src.price import run_project

        return run_project()

    if args.command == "train":
        from src.model_minutes import run_train
        from src.model_rates import run_train_rates

        rc = run_train()
        if rc != 0:
            return rc
        return run_train_rates()

    if args.command == "clean":
        from src.clean import run_clean
        from src.features import run_features

        rc = run_clean()
        run_features()
        return rc

    if args.command == "audit":
        from src.clean import run_audit

        return run_audit()

    log = get_logger("run")
    phase = NOT_IMPLEMENTED[args.command]
    log.info(f"{args.command}: not implemented — phase {phase} builds this")
    return 0


if __name__ == "__main__":
    sys.exit(main())
