# UTF-8
"""Thin one-shot: same path as the GUI Post button.

Community name comes from env RA_SAFARI_URL (not stored). No SIWA. No Join.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reddit_open import log
from reddit_post import run_post
from ssh_device import Phone, PhoneError
from ui_tree import TapStack

IMAGE = r"YOUR_TEST_PHOTO_PATH_HERE"
TITLE = "Test test is this thing on wooo"
NAME = "a0001"


def community_from_env() -> str:
    url = (os.environ.get("RA_SAFARI_URL") or "").strip()
    m = re.search(r"/r/([A-Za-z0-9_]+)/?", url)
    if not m:
        print("ERROR missing RA_SAFARI_URL")
        raise SystemExit(2)
    return m.group(1)


def main() -> int:
    community = community_from_env()
    if not os.path.isfile(IMAGE):
        print("ERROR missing image " + IMAGE)
        return 2
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend")
        run_post(phone, stack, log, NAME, community, TITLE, IMAGE)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
