# UTF-8
"""Type a Keychain-style password on Reddit (one key at a time, never paste). Save to logs/."""

from __future__ import annotations

import io
import os
import secrets
import string
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from slots import get_slot, set_slot_password, write_account_text  # noqa: E402
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

ABOUT_MARKERS = ("How do you identify?", "About you")
PASSKEY_ACTIONS = ("Set Up Face ID & Passcode", "Save on Another Device")
REDDIT_RETRY_LATER = "Sorry, please try again later."
VERIFICATION_CODE_EXPIRED = "Verification code is expired or has already been used."


def log(msg: str) -> None:
    print(msg, flush=True)


def _screen_text(nodes) -> str:
    return " ".join(
        collapsed_text(n.labels()) for n in nodes if n.on_screen() and n.labels()
    )


def passkey_prompt(nodes) -> bool:
    """Recognize only the iOS passkey sheet observed after Reddit password submit."""
    if find_label_exact(nodes, ("Set a password",)) is None:
        return False
    if any(find_label_exact(nodes, (label,)) is None for label in PASSKEY_ACTIONS):
        return False
    text = _screen_text(nodes)
    return "save a passkey" in text and "icloud keychain" in text


def reddit_retry_later(nodes) -> bool:
    """Recognize Reddit's transient password-submit rejection verbatim."""
    return "sorry, please try again later" in _screen_text(nodes)


def verification_code_expired(nodes) -> bool:
    """Recognize Reddit's post-password expired-code banner, including OCR y/v."""
    text = _screen_text(nodes).replace("alreadv", "already")
    return (
        "verification code is expired or has already" in text
        and "been used" in text
    )


def _raise_retry_later() -> None:
    raise PhoneError(
        f"Reddit returned '{REDDIT_RETRY_LATER}' "
        "Saved password preserved; no automatic resubmit."
    )


def _raise_verification_expired() -> None:
    raise PhoneError(
        f"Reddit returned '{VERIFICATION_CODE_EXPIRED}' "
        "Saved credentials and queue item preserved; no automatic resubmit."
    )


def password_validation_visual(png: bytes | None) -> bool:
    """Prove the green password check and enabled orange Continue visually."""
    if not png:
        return False
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as source:
            image = source.convert("RGB")
            width, height = image.size
            if width < 200 or height < 300:
                return False
            validation = image.crop(
                (
                    int(width * 0.60),
                    int(height * 0.15),
                    int(width * 0.90),
                    int(height * 0.32),
                )
            )
            action = image.crop((0, int(height * 0.65), width, height))
            green = sum(
                count
                for count, (red, value, blue) in validation.getcolors(
                    maxcolors=validation.width * validation.height
                ) or []
                if value > 90 and value > red * 1.35 and value > blue * 1.25
            )
            orange = sum(
                count
                for count, (red, value, blue) in action.getcolors(
                    maxcolors=action.width * action.height
                ) or []
                if red > 180 and 35 < value < 130 and blue < 80
            )
            return green >= 20 and orange >= 500
    except Exception:
        return False


def passkey_close_button(nodes):
    """Return the proven top-right modal X, never an arbitrary X elsewhere."""
    if not passkey_prompt(nodes):
        return None
    for node in nodes:
        if node.ntype == "ocr-line" or not node.visible or not node.on_screen():
            continue
        if collapsed_text(node.labels()) != "x":
            continue
        if node.x < 630 or node.y < 650 or node.y > 850:
            continue
        if node.w > 100 or node.h > 100:
            continue
        return node
    return None


def dismiss_passkey_prompt(
    stack: TapStack,
    phone: Phone,
    nodes,
    lg=log,
):
    """Close the proven passkey sheet once and require Reddit's About screen."""
    if not passkey_prompt(nodes):
        return nodes
    close = passkey_close_button(nodes)
    if close is None:
        raise PhoneError(
            "iOS passkey popup is present but its proven top-right X is missing. "
            "Did not tap Set Up Face ID, Save on Another Device, or a guessed coordinate."
        )
    lg(f"Dismiss iOS passkey popup X [{close.x},{close.y} {close.w}x{close.h}]")
    stack.tap_xy(*close.center())

    last = nodes
    for attempt in range(5):
        time.sleep(0.8)
        _blob, last = dump_named(
            stack,
            phone,
            f"903_after_passkey_close_{attempt}",
            lg,
            shot=True,
            preview=False,
        )
        if find_label_exact(last, ABOUT_MARKERS) is not None:
            lg("iOS passkey popup dismissed; Reddit reached About you.")
            return last

    if passkey_prompt(last):
        raise PhoneError(
            "iOS passkey popup remained after one proven X tap. Did not repeat the tap."
        )
    if find_label_exact(last, ("Set a password",)) is not None:
        raise PhoneError(
            "iOS passkey popup closed, but Reddit remained on Set a password. "
            "Did not retype or resubmit the stored password."
        )
    raise PhoneError(
        "iOS passkey popup closed, but Reddit did not reach About you. Did not guess."
    )


def wait_after_password(stack: TapStack, phone: Phone, nodes, lg=log):
    """Wait for delayed About/passkey UI after tapping password Continue."""
    last = nodes
    for attempt in range(8):
        if verification_code_expired(last):
            _raise_verification_expired()
        if reddit_retry_later(last):
            _raise_retry_later()
        if find_label_exact(last, ABOUT_MARKERS) is not None:
            return last
        if passkey_prompt(last):
            return dismiss_passkey_prompt(stack, phone, last, lg)
        time.sleep(0.8)
        _blob, last = dump_named(
            stack,
            phone,
            f"903_password_transition_{attempt}",
            lg,
            shot=True,
            preview=False,
        )
    if verification_code_expired(last):
        _raise_verification_expired()
    if reddit_retry_later(last):
        _raise_retry_later()
    if find_label_exact(last, ABOUT_MARKERS) is not None:
        return last
    if passkey_prompt(last):
        return dismiss_passkey_prompt(stack, phone, last, lg)
    if find_label_exact(last, ("Set a password",)) is not None:
        raise PhoneError(
            "Reddit remained on Set a password after Continue. "
            "Did not retype or resubmit the stored password."
        )
    raise PhoneError(
        "Reddit left Set a password but did not reach About you or the proven "
        "passkey prompt. Did not guess or resubmit."
    )


def resume_saved_password(stack: TapStack, phone: Phone, nodes, lg=log):
    """Submit an already-filled saved password once, with visual proof first."""
    if verification_code_expired(nodes):
        _raise_verification_expired()
    if find_label_exact(nodes, ("Set a password",)) is None:
        raise PhoneError("Saved-password resume requires the Set a password screen.")
    if find_label_exact(nodes, ("Password must be at least 8 characters",)) is None:
        raise PhoneError(
            "Saved-password resume could not find Reddit's password validation row. "
            "Did not tap Continue."
        )
    cont = find_label_exact(nodes, ("Continue",))
    if cont is None or not cont.on_screen():
        raise PhoneError(
            "Saved-password resume could not prove the Continue control. Did not tap."
        )
    png = stack.screenshot_png()
    if not password_validation_visual(png):
        raise PhoneError(
            "Saved-password resume could not prove the green validation state and "
            "enabled Continue button. Did not tap or retype the password."
        )
    lg("Resume: saved password is visually valid; tapping Continue once.")
    stack.tap_xy(*cont.center())
    time.sleep(2.0)
    _blob, after = dump_named(
        stack,
        phone,
        "902_resume_after_saved_password",
        lg,
        shot=True,
        preview=False,
    )
    return wait_after_password(stack, phone, after, lg)


def manager_password() -> str:
    """iCloud Keychain / 1Password look: four alnum groups, not a dictionary+123 bot string."""
    pool = string.ascii_letters + string.digits
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ]
    chars.extend(secrets.choice(pool) for _ in range(13))
    rng = secrets.SystemRandom()
    rng.shuffle(chars)
    raw = "".join(chars)
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def type_password(stack: TapStack, phone: Phone, sid: str) -> str:
    slot = get_slot(sid)
    if not slot:
        raise PhoneError(f"no {sid} slot")
    email = str(slot.get("email") or "").strip()
    username = str(slot.get("username") or "").strip()
    if not email or not username:
        raise PhoneError(f"{sid} missing email or username")
    pw = manager_password()
    set_slot_password(sid, pw, log)
    path = write_account_text(sid, email, username, pw)
    log("SAVED password to " + path + " (not printed)")
    blob, nodes = dump_named(stack, phone, "900_password", log, shot=True)
    if find_label_exact(nodes, ("Set a password",)) is None:
        raise PhoneError("Not on Set a password. See 900_password.png")
    field = find_label_exact(nodes, ("Password",))
    if field is None:
        raise PhoneError("Password field not in dump.")
    y = field.center()[1]
    log(f"Password field tap (375,{y}) then type one key at a time")
    stack.tap_xy(375, y)
    time.sleep(0.5)
    stack.type_text(pw)
    time.sleep(0.6)
    blob, nodes = dump_named(stack, phone, "901_password_typed", log, shot=True)
    if "invalid" in collapsed_text(" ".join(n.labels() for n in nodes)):
        raise PhoneError("Reddit rejected the password. See 901_password_typed.png")
    cont = find_label_exact(nodes, ("Continue",))
    if cont is None:
        raise PhoneError("Continue not on screen after typing.")
    if "apple" in collapsed_text(cont.labels()):
        raise PhoneError("Refusing Continue with Apple.")
    log(f"Continue [{cont.x},{cont.y} {cont.w}x{cont.h}]")
    stack.tap_xy(*cont.center())
    time.sleep(2.0)
    _blob, nodes = dump_named(
        stack,
        phone,
        "902_after_password",
        log,
        shot=True,
        preview=False,
    )
    wait_after_password(stack, phone, nodes, log)
    return pw


def main() -> int:
    sid = (sys.argv[1] if len(sys.argv) > 1 else "a0001").strip().lower()
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        type_password(stack, phone, sid)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
