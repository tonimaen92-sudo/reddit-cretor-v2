# UTF-8
"""Read Ghost Tutorial end to end. Dump-first. No Generate. Stop if Runtime Error."""

from __future__ import annotations

import os
import sys
import time
from friend_poster.core.config import GHOST_BUNDLE, LOG_DIR  # noqa: E402
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def body_text(nodes) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen() or n.y < 50:
            continue
        blob = collapsed_text(n.labels())
        if not blob or blob in seen:
            continue
        if blob in ("no sim", "no sim c", "no sim o", "no sim r", "home", "x"):
            continue
        if blob.replace(":", "").isdigit() and len(blob) <= 5:
            continue
        seen.add(blob)
        lines.append(blob)
    return lines


def dismiss_alert(stack: TapStack, nodes) -> bool:
    btn = find_label_exact(nodes, ("Dismiss", "OK", "Close"))
    err = find_label_exact(nodes, ("Runtime Error",))
    if btn is None and err is None:
        return False
    target = btn or err
    log(f"ALERT {target.labels()!r} — dismissing and stopping scroll")
    stack.tap_xy(*target.center())
    time.sleep(1.0)
    return True


def main() -> int:
    os.environ["SKIP_SHOT"] = "1"
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        try:
            stack._xxt_post("/recycle", b"", timeout=5)
        except Exception:
            pass
        phone.exec_ok(f"uiopen --bundleid {GHOST_BUNDLE}", timeout=10)
        time.sleep(2.2)
        blob, nodes = dump_named(stack, phone, "240_before_tutorial", log)
        if find_label_exact(nodes, ("Tutorial",)) is None or find_label_exact(
            nodes, ("Step 2", "Step 3", "Ghost Mode")
        ) is None:
            tut = find_label_exact(nodes, ("Tutorial",))
            if tut:
                stack.tap_row(tut)
                time.sleep(1.8)
                blob, nodes = dump_named(stack, phone, "241_tutorial_open", log)
        if dismiss_alert(stack, nodes):
            print("TUTORIAL=ALERT")
            return 1

        transcript: list[str] = []
        allow_swipe = True
        for i in range(0, 8):
            stem = f"242_tutorial_{i}"
            blob, nodes = dump_named(stack, phone, stem, log)
            if dismiss_alert(stack, nodes):
                allow_swipe = False
                blob, nodes = dump_named(stack, phone, stem + "_cleared", log)
            chunk = body_text(nodes)
            new = 0
            for line in chunk:
                if line not in transcript:
                    transcript.append(line)
                    new += 1
                    log(f"TUT {line}")
            if i == 7 or not allow_swipe:
                break
            if i > 0 and new == 0:
                log("no new tutorial text")
                break
            # Scroll the tutorial body, not the nav bar or a possible tab bar.
            stack.swipe(375, 980, 375, 420)
            time.sleep(0.8)

        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "ghost_tutorial.txt")
        text = "\n".join(transcript) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print("==== GHOST TUTORIAL ====")
        print(text)
        print("WROTE", path)
        print("TUTORIAL=OK")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
