"""Command-line interface for FaultLine."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import scan_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="faultline", description="Find fault markers in source files.")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="scan a file or directory")
    scan.add_argument("path", nargs="?", default=".")
    scan.add_argument("--json", action="store_true", dest="as_json", help="emit JSON findings")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scan":
        try:
            findings = scan_path(args.path)
        except FileNotFoundError:
            print(f"faultline: path not found: {args.path}", file=sys.stderr)
            return 2
        if args.as_json:
            print(json.dumps([finding.__dict__ for finding in findings], indent=2))
        else:
            for finding in findings:
                print(f"{finding.path}:{finding.line}: {finding.marker}: {finding.text}")
            print(f"{len(findings)} finding(s)")
        return 1 if findings else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
