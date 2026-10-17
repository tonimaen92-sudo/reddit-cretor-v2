"""Canonical command dispatcher for the packaged application."""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Sequence


ENTRY_POINTS = {
    "creator": "friend_poster.apps.creator",
    "poster": "friend_poster.apps.poster",
    "commenter": "friend_poster.apps.commenter",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="friend-poster")
    parser.add_argument("app", choices=sorted(ENTRY_POINTS), help="GUI application to start")
    args, remaining = parser.parse_known_args(argv)
    module = importlib.import_module(ENTRY_POINTS[args.app])
    result = module.main(remaining) if args.app == "creator" else module.main()
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
