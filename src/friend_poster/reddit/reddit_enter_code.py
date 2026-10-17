# UTF-8
"""Type the Reddit 6-digit on the live Verify screen. IMAP, not Mail.

Never Home. Never recents. Never open Mail.app. Never tap Skip.
Stay on Verify your email the whole time.

  python misc/reddit_enter_code.py --code=013987
  python misc/reddit_enter_code.py          # IMAP poll, then type. Reddit must already be on Verify.
"""

from __future__ import annotations

import os
import re
import sys
import time
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import Node, TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

INVALID_CODE_MESSAGE = "Please enter a valid verification code"
_RESEND_LINES = {
    "check your spam folder or resend",
    # XXTouch OCR in the saved 889_user fixtures reads the y as a v.
    "check vour spam folder or resend",
}
_SIX_DIGITS_RE = re.compile(r"^\d{6}$")
_FIELD_DIGITS_RE = re.compile(r"^\d{1,6}$")


def log(msg: str) -> None:
    print(msg, flush=True)


def keyboard_digit(nodes, ch: str):
    """On-screen key, not status-bar OCR. Dump-first."""
    want = collapsed_text(ch)
    hits = [
        n
        for n in nodes
        if n.ntype != "ocr-line"
        and n.on_screen()
        and n.y > 850
        and collapsed_text(n.labels()) == want
    ]
    if not hits:
        return None
    return max(hits, key=lambda n: (n.w * n.h, n.y))


def invalid_code_message(nodes) -> Node | None:
    """Return only Reddit's exact invalid-code error."""
    return find_label_exact(nodes or [], (INVALID_CODE_MESSAGE,))


def resend_control(nodes) -> Node | None:
    """Locate the exact Resend word without tapping a merged OCR row's center.

    XXTouch may expose ``Check your spam folder or Resend`` as one bounded OCR
    node.  For that one allow-listed line, derive the terminal word's bounds
    from its exact character span inside the observed node.  No fixed screen
    coordinate is used.
    """
    for item in nodes or []:
        if not item.on_screen() or not item.visible:
            continue
        if any(
            collapsed_text(part) == "resend"
            for part in (item.name, item.label, item.value, item.text)
            if part
        ):
            return item

    for item in nodes or []:
        if not item.on_screen() or not item.visible:
            continue
        text = collapsed_text(item.labels())
        if text not in _RESEND_LINES:
            continue
        start = text.rfind("resend")
        end = start + len("resend")
        if start <= 0 or end != len(text):
            continue
        left = item.x + round(item.w * start / len(text))
        right = item.x + round(item.w * end / len(text))
        if right <= left:
            continue
        return Node(
            ntype="derived-word",
            name="Resend",
            label="Resend",
            text="Resend",
            x=left,
            y=item.y,
            w=right - left,
            h=item.h,
            visible=item.visible,
            enabled=item.enabled,
            raw="derived from bounded OCR suffix",
        )
    return None


def _verification_digit_text(item: Node) -> str | None:
    for part in (item.value, item.text, item.label, item.name):
        normalized = collapsed_text(part)
        if _FIELD_DIGITS_RE.fullmatch(normalized):
            return normalized
    return None


def _verification_code_candidate(nodes, *, exactly_six: bool) -> Node | None:
    field = find_label_exact(nodes or [], ("Verification code",))
    if field is None:
        return None
    candidates = []
    field_x, field_y = field.center()
    for item in nodes or []:
        if item.ntype == "ocr-line" or not item.on_screen() or not item.visible:
            continue
        digits = _verification_digit_text(item)
        if digits is None or (exactly_six and _SIX_DIGITS_RE.fullmatch(digits) is None):
            continue
        item_x, item_y = item.center()
        dy = item_y - field_y
        if 0 <= dy <= 100 and abs(item_x - field_x) <= 250:
            candidates.append((dy, abs(item_x - field_x), item))
    if not candidates:
        return None
    return min(candidates, key=lambda hit: (hit[0], hit[1]))[2]


def verification_code_value(nodes) -> Node | None:
    """Find a six-digit value only when it is beside the proven code field."""
    return _verification_code_candidate(nodes, exactly_six=True)


def verification_code_digits(nodes) -> Node | None:
    """Find any retained 1-6 digit field value for a fail-closed clear proof."""
    return _verification_code_candidate(nodes, exactly_six=False)


def _verification_code_text(item: Node) -> str | None:
    digits = _verification_digit_text(item)
    return digits if digits is not None and len(digits) == 6 else None


def resend_invalid_code_once(
    stack: TapStack,
    phone: Phone,
    nodes,
    log_fn=log,
) -> tuple[float, str, list[Node]]:
    """Resend once and prove the rejected value was removed.

    The returned timestamp is captured immediately before the one tap so an
    IMAP caller can reject every earlier message.  The stale value is returned
    only for an in-memory equality guard and is never logged.
    """
    if find_label_exact(nodes or [], ("Verify your email",)) is None:
        raise PhoneError("Invalid-code recovery requires the Verify your email screen.")
    if invalid_code_message(nodes) is None:
        raise PhoneError("Invalid-code recovery requires Reddit's exact error message.")
    stale = verification_code_value(nodes)
    if stale is None:
        raise PhoneError(
            "Rejected verification value could not be bounded; did not tap Resend."
        )
    resend = resend_control(nodes)
    if resend is None:
        raise PhoneError("Exact Resend control was not bounded; did not guess a tap.")

    # IMAP INTERNALDATE is whole-second precision.  Record the containing
    # second so a genuinely new message delivered in this same second is not
    # excluded by a fractional cutoff.
    resend_at = float(int(time.time()))
    log_fn(f"Resend [{resend.x},{resend.y} {resend.w}x{resend.h}] (one attempt)")
    stack.tap_xy(*resend.center())
    time.sleep(0.7)
    _blob, after_resend = dump_named(
        stack,
        phone,
        "885_after_resend",
        log_fn,
        shot=False,
        persist=False,
        preview=False,
    )
    if find_label_exact(after_resend, ("Verify your email",)) is None:
        raise PhoneError("Verify screen disappeared after Resend; did not type or poll.")
    field = find_label_exact(after_resend, ("Verification code",))
    if field is None:
        raise PhoneError("Verification code field missing after Resend.")

    retained = verification_code_value(after_resend)
    if retained is not None:
        # Tap the right edge of the observed value so backspace starts after
        # its final digit.  Both coordinates come from the fresh OCR bounds.
        stack.tap_xy(retained.x + retained.w - 1, retained.center()[1])
        time.sleep(0.3)
        stack.press_backspace(8)
        time.sleep(0.4)
        _blob, cleared = dump_named(
            stack,
            phone,
            "886_after_code_clear",
            log_fn,
            shot=False,
            persist=False,
            preview=False,
        )
    else:
        cleared = after_resend

    if find_label_exact(cleared, ("Verify your email",)) is None:
        raise PhoneError("Verify screen disappeared while clearing the rejected code.")
    if verification_code_digits(cleared) is not None:
        raise PhoneError("Rejected verification value remained after bounded clear.")
    stale_text = _verification_code_text(stale)
    if stale_text is None:  # Kept defensive even though the node was proven above.
        raise PhoneError("Rejected verification value became unreadable after clearing.")
    return resend_at, stale_text, cleared


def type_code(stack: TapStack, phone: Phone, code: str) -> list[Node]:
    blob, nodes = dump_named(
        stack, phone, "880_verify", log, shot=True, preview=False
    )
    if find_label_exact(nodes, ("Verify your email",)) is None and find_label_exact(
        nodes, ("Verification code",)
    ) is None:
        raise PhoneError("Not on Verify email. See 880_verify.png")
    if find_label_exact(nodes, ("Skip",)) is not None:
        log("Skip is on screen. Will not tap it.")
    field = find_label_exact(nodes, ("Verification code",))
    if field is None:
        raise PhoneError("Verification code field missing.")
    if verification_code_digits(nodes) is not None:
        raise PhoneError(
            "Verification code field is not empty; refused to append another code."
        )
    stack.tap_xy(375, field.center()[1] + 36)
    time.sleep(0.7)
    blob, nodes = dump_named(
        stack, phone, "881_keyboard", log, shot=True, preview=False
    )
    for index, ch in enumerate(code, start=1):
        key = keyboard_digit(nodes, ch)
        if key is None:
            log(f"Verification digit {index}/6: using direct key input (not paste)")
            stack.type_text(ch)
        else:
            log(
                f"Verification digit {index}/6: keypad key located "
                f"[{key.x},{key.y} {key.w}x{key.h}]"
            )
            stack.tap_xy(*key.center())
        time.sleep(0.18)
        blob, nodes = dump_named(
            stack,
            phone,
            f"882_after_digit_{index}",
            log,
            shot=False,
            preview=False,
        )
    blob, nodes = dump_named(
        stack, phone, "883_code_typed", log, shot=True, preview=False
    )
    cont = find_label_exact(nodes, ("Continue",))
    if cont is None:
        raise PhoneError("Continue missing after typing code.")
    if "apple" in collapsed_text(cont.labels()):
        raise PhoneError("Refusing Continue with Apple.")
    log(f"Continue [{cont.x},{cont.y} {cont.w}x{cont.h}]")
    stack.tap_xy(*cont.center())
    time.sleep(2.2)
    _blob, result_nodes = dump_named(
        stack, phone, "884_after_code_continue", log, shot=True, preview=False
    )
    return result_nodes


def main() -> int:
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend.")
        code = ""
        for a in sys.argv[1:]:
            if a.startswith("--code="):
                code = a.split("=", 1)[1].strip()
        blob, nodes = dump_named(
            stack, phone, "880_now", log, shot=True, preview=False
        )
        if find_label_exact(nodes, ("Verify your email",)) is None:
            raise PhoneError(
                "Not on Verify your email. Do not Home. Do not open Mail. "
                "Do not close_reddit. See 880_now.png. Leave Reddit on Verify and retry."
            )
        if find_label_exact(nodes, ("Skip",)) is not None:
            log("Skip is on screen. Will not tap it.")
        if not code:
            from friend_poster.device.icloud_code import poll_reddit_code  # noqa: E402

            log("IMAP poll on PC. Phone stays on Verify. No Mail.app.")
            code = poll_reddit_code(log=log, timeout=150, after_ts=time.time() - 180)
            log("IMAP verification code received.")
        if not code.isdigit() or len(code) != 6:
            raise PhoneError("Need a 6-digit code.")
        type_code(stack, phone, code)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
