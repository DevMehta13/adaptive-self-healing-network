"""Command line for the run history: list runs and export one as CSV.

    uv run python -m iot_backend.history list
    uv run python -m iot_backend.history export 3 --out-dir exports

The database is the file named by `IOT_HISTORY_DB` (or `--db`). Reading it does not need the
backend to be running.
"""

import argparse
import os
import sys
from collections.abc import Sequence

from iot_backend.storage import HistoryError, RunStore, export_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m iot_backend.history", description=__doc__)
    parser.add_argument("--db", default=os.environ.get("IOT_HISTORY_DB", ""), help="history file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show the stored runs, newest first")
    export = sub.add_parser("export", help="write a run's readings and events as CSV files")
    export.add_argument("run_id", type=int)
    export.add_argument(
        "--out-dir", default=".", help="folder for run-<id>-readings.csv and run-<id>-events.csv"
    )
    return parser


def _list(store: RunStore) -> int:
    runs = store.runs()
    if not runs:
        print("no runs stored")
    for run in runs:
        ended = run["ended_utc"] or "not closed (the backend stopped without saying so)"
        note = "" if run["complete"] else "  [oldest rows were dropped to stay within the limits]"
        print(
            f"run {run['run_id']:>3}  {run['mode']:<10} {run['scenario'] or '-':<24} "
            f"started {run['started_utc']}  ended {ended}  "
            f"{run['events']} events, {run['readings']} readings{note}"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.db:
        print("error: no history file: set IOT_HISTORY_DB or pass --db", file=sys.stderr)
        return 2
    try:
        store = RunStore(args.db)
    except HistoryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        if args.command == "list":
            return _list(store)
        try:
            paths = export_run(store, args.run_id, args.out_dir)
        except HistoryError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        for path in paths:
            print(f"wrote {path}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
