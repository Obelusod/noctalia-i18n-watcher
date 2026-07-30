"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict

from noctalia_i18n_core import ResetMode

from .app import Application

_LOGGER = logging.getLogger(__name__)
_EXTERNAL_LOGGERS = ("discord", "urllib3")


class _Arguments(argparse.Namespace):
    command: str | None = None
    monitor: str | None = None
    dry_run: bool = False
    notify: bool = False
    page: int = 1
    reset_mode: ResetMode = "baseline"
    scheduled: bool = False


def _positive_page(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("page number must be at least 1")
    return number


def _monitor_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--monitor",
        metavar="ID",
        help="Select a monitor by ID.",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noctalia-i18n-watcher",
        description="Monitor Noctalia translation changes and notify Discord.",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    commands = parser.add_subparsers(dest="command")

    deliver = commands.add_parser(
        "deliver",
        help="Collect changes and immediately deliver queued notifications.",
    )
    _monitor_option(deliver)
    deliver.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview pending changes without sending or saving them.",
    )
    reset = commands.add_parser(
        "reset",
        help="Rebuild the source baseline without replaying history.",
    )
    reset_modes = reset.add_subparsers(dest="reset_mode", required=True)
    reset_help = {
        "baseline": "Replace the source baseline while preserving delivery state.",
        "full": "Clear all monitoring state and establish a new baseline.",
    }
    for mode, help_text in reset_help.items():
        mode_parser = reset_modes.add_parser(mode, help=help_text)
        _monitor_option(mode_parser)
        mode_parser.add_argument(
            "--notify",
            action="store_true",
            help="Send a new baseline notification to configured Discord routes.",
        )

    validate = commands.add_parser(
        "validate",
        help="Validate the runtime configuration.",
    )
    _monitor_option(validate)
    status = commands.add_parser("status", help="Show persisted monitoring state.")
    _monitor_option(status)
    status.add_argument(
        "--notify",
        action="store_true",
        help="Send the current status to configured Discord routes.",
    )
    history = commands.add_parser("history", help="Read a source history page.")
    _monitor_option(history)
    history.add_argument(
        "page",
        type=_positive_page,
        metavar="PAGE",
        help="One-based source page number.",
    )
    return parser


def _parse_args(argv: Sequence[str] | None = None) -> _Arguments:
    parser = _parser()
    arguments = parser.parse_args(argv, namespace=_Arguments())
    if arguments.scheduled:
        if arguments.command is not None:
            parser.error("--scheduled cannot be combined with a command")
    elif arguments.command is None:
        parser.error("a command is required")
    return arguments


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )
    for name in _EXTERNAL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    args = _parse_args(argv)
    try:
        application = Application.load()
        logging.getLogger().setLevel(application.log_level)
        if args.scheduled:
            application.run()
        elif args.command == "deliver":
            if args.dry_run:
                for item in application.preview(args.monitor):
                    _print_json(item)
            else:
                application.run(args.monitor, flush=True)
        elif args.command == "reset":
            application.reset(
                args.reset_mode,
                args.monitor,
                notify=args.notify,
            )
        elif args.command == "validate":
            application.validate(args.monitor)
            print("Configuration valid.")
        elif args.command == "status":
            _print_json(
                {
                    monitor_id: asdict(summary)
                    | {
                        "updated_at": (
                            None
                            if summary.updated_at is None
                            else summary.updated_at.isoformat()
                        )
                    }
                    for monitor_id, summary in application.status(
                        args.monitor, notify=args.notify
                    ).items()
                }
            )
        elif args.command == "history":
            _print_json(
                [
                    asdict(change) | {"occurred_at": change.iso_timestamp}
                    for change in application.history(args.page, args.monitor)
                ]
            )
        return 0
    except (ValueError, RuntimeError) as exc:
        _LOGGER.error("%s", exc)
        return 1
    except Exception:
        _LOGGER.exception("Unexpected failure")
        return 1
