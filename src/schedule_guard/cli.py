"""Command-line entry point: `schedule-guard`."""

import argparse

from schedule_guard import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schedule-guard",
        description="Keep field progress updates from overwriting a master schedule or baseline.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
