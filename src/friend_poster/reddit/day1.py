# UTF-8
"""Day 1 warmup on this iPhone: stay on the Reddit home feed and scroll.

No votes, joins, comments, posts. No uiopen Reddit (Crane picker). No SIWA.
Simple flicks only — 10–17 minutes unless --minutes is set.

  python misc/day1.py
  python misc/day1.py --minutes 12
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from friend_poster.core.config import LOG_DIR  # noqa: E402
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

MIN_MINUTES = 10
MAX_MINUTES = 17
LOG_PATH = os.path.join(LOG_DIR, "day1.log")
FORBIDDEN = (
    "join",
    "joined",
    "upvote",
    "downvote",
    "comment",
    "share",
    "award",
    "create",
    "follow",
    "continue with apple",
    "sign in with apple",
)


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + "  " + str(msg)
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def blob_of(nodes) -> str:
    return " ".join(collapsed_text(n.labels()) for n in nodes if n.on_screen() and n.labels())


def feed_flick(stack: TapStack, rng: random.Random, reverse: bool = False, big: bool = True) -> None:
    """Home-feed thumb flick. Not Ghost Home (375,780→60) and not a recents dismiss."""
    x1 = rng.randint(300, 450)
    x2 = x1 + rng.randint(-18, 18)
    if reverse:
        y1 = rng.randint(400, 520)
        y2 = rng.randint(780, 960)
        steps, ms = 14, 22
    elif big:
        y1 = rng.randint(930, 1060)
        y2 = rng.randint(400, 580)
        steps, ms = rng.randint(16, 24), rng.randint(16, 24)
    else:
        y1 = rng.randint(820, 960)
        y2 = rng.randint(620, 740)
        steps, ms = 14, 20
    stack.interp_flick(x1, y1, x2, y2, steps=steps, ms=ms)


def screen_ok(nodes) -> str:
    """Return ok / stop reason. Never tap Join."""
    b = blob_of(nodes)
    if find_label_exact(nodes, ("Select Container",)) is not None:
        return "STOP Crane picker"
    if "continue with apple" in b or "sign in with apple" in b:
        return "STOP SIWA"
    if "log in to reddit" in b or "create a new account" in b:
        return "STOP login/signup"
    if find_label_exact(nodes, ("Verify your email",)) is not None:
        return "STOP verify"
    if "your ads personalization choices" in b:
        return "STOP ads personalization"
    if find_label_exact(
        nodes, ("Choose your interests", "Interests", "Customize your feed")
    ) is not None:
        return "STOP interests"
    titles = [collapsed_text(n.labels()) for n in nodes if n.on_screen() and n.y < 200]
    if any(t in ("weather", "calendar", "photos") for t in titles) and "reddit" not in b:
        if find_label_exact(nodes, ("reddit",)) is None and "popular near you" not in b:
            return "STOP left Reddit"
    return "ok"


def dismiss_safe(stack: TapStack, nodes) -> bool:
    b = blob_of(nodes)
    if "please turn on notifications" in b or "get notified of the best content" in b:
        n = find_label_exact(nodes, ("OK", "Ok"))
        if n is not None and "apple" not in collapsed_text(n.labels()):
            log(f"Notifications OK [{n.x},{n.y} {n.w}x{n.h}]")
            stack.tap_xy(*n.center())
            time.sleep(1.2)
            return True
    if "would like to send you notifications" in b or "send you notifications" in b:
        n = find_label_exact(nodes, ("Allow", "OK", "Ok"))
        if n is not None and "don't" not in collapsed_text(n.labels()) and "not now" not in collapsed_text(n.labels()):
            log(f"System Allow [{n.x},{n.y} {n.w}x{n.h}]")
            stack.tap_xy(*n.center())
            time.sleep(1.2)
            return True
    n = find_label_exact(nodes, ("Maybe later", "No thanks"))
    if n is None:
        return False
    log(f"Dismiss nag {n.labels()[:40]!r}")
    stack.tap_xy(*n.center())
    time.sleep(1.0)
    return True


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=int, default=0)
    return p.parse_args()


def run_day1_session(phone: Phone, stack: TapStack, minutes: int = 0) -> int:
    """Scroll the live home feed. Caller already opened the Crane container. No Join."""
    rng = random.SystemRandom()
    if minutes < MIN_MINUTES or minutes > MAX_MINUTES:
        minutes = rng.randint(MIN_MINUTES, MAX_MINUTES)
    deadline = time.time() + minutes * 60
    log(f"Day 1 simple scrolls — {minutes} min. No Join/vote/comment/post. No uiopen Reddit.")
    if not stack.can_tap():
        raise PhoneError("No tap backend. Open XXTouch Elite.")
    blob, nodes = dump_named(stack, phone, "930_day1_start", log, shot=True)
    why = screen_ok(nodes)
    if why != "ok":
        raise PhoneError(why + ". See 930_day1_start.png")
    dismiss_safe(stack, nodes)

    n = 0
    last_dump = time.time()
    while time.time() < deadline:
        left = int(deadline - time.time())
        n += 1
        reverse = rng.random() < 0.10
        big = rng.random() > 0.22
        if reverse:
            log(f"scroll {n} reverse  {left}s left")
        elif not big:
            log(f"scroll {n} nudge  {left}s left")
        elif n % 8 == 1:
            log(f"scroll {n}  {left}s left")
        feed_flick(stack, rng, reverse=reverse, big=big)
        pause = rng.uniform(1.8, 3.5) if rng.random() < 0.14 else rng.uniform(0.55, 1.4)
        if rng.random() < 0.12:
            extra = rng.uniform(2.5, 6.0)
            log(f"pause {int(extra)}s")
            pause += extra
        time.sleep(pause)
        if time.time() - last_dump >= 45:
            blob, nodes = dump_named(stack, phone, "931_day1", log, shot=False)
            last_dump = time.time()
            why = screen_ok(nodes)
            if why != "ok":
                log(why)
                break
            dismiss_safe(stack, nodes)

    dump_named(stack, phone, "932_day1_end", log, shot=True)
    log(f"Day 1 finished. {n} scrolls in {minutes} min window.")
    return 0


def main() -> int:
    args = parse_args()
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        return run_day1_session(phone, stack, args.minutes)
    except PhoneError as e:
        log("ERROR " + str(e))
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
