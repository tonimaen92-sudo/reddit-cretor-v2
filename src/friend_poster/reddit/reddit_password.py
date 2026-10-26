# UTF-8
"""Type a Keychain-style password on Reddit (one key at a time, never paste). Save to logs/."""

from __future__ import annotations

import io
import os
import secrets
import string
import sys
import time
from friend_poster.core.slots import (  # noqa: E402
    CREATION_PROGRESS,
    get_slot,
    set_slot_password,
    write_account_text,
)
from friend_poster.reddit.reddit_to_verify import (  # noqa: E402
    continue_btn as email_continue_btn,
    email_ready,
    keyboard_done_btn,
    welcome as email_screen,
)
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402
from friend_poster.services.ui_recovery_agent import UnknownVisualStateError  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

ABOUT_MARKERS = ("How do you identify?", "About you")
PASSKEY_ACTIONS = ("Set Up Face ID & Passcode", "Save on Another Device")
REDDIT_RETRY_LATER = "Sorry, please try again later."
VERIFICATION_CODE_EXPIRED = "Verification code is expired or has already been used."
REQUESTED_PASSWORD_MIN_LENGTH = 8
REQUESTED_PASSWORD_MAX_LENGTH = 128
PASSWORD_TRANSITION_POLLS = 8
PASSWORD_STALL_POLL_SECONDS = 5.0
PASSWORD_STALL_SAFETY_SECONDS = 195.0
EXPIRED_EMAIL_RETURN_POLLS = 8
EXPIRED_EMAIL_TRANSITION_POLLS = 8
EXPIRED_EMAIL_KEYBOARD_POLLS = 3


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


def _expired_try_again_node(nodes):
    """Return only the popup action observed with Reddit's expired-code banner."""
    if not verification_code_expired(nodes):
        return None
    if find_label_exact(nodes, ("Set a password",)) is None:
        return None
    action = find_label_exact(nodes, ("Try again",))
    if action is None or not action.on_screen() or not action.enabled:
        return None
    if not (180 <= action.center()[1] <= 360 and 500 <= action.center()[0] <= 740):
        return None
    return action


def _wait_for_selected_email(
    stack: TapStack,
    phone: Phone,
    email: str,
    lg=log,
):
    """Require Reddit's returned email form with the already selected address."""
    last = []
    for attempt in range(EXPIRED_EMAIL_RETURN_POLLS):
        _cancelable_wait(stack, 0.8)
        _blob, last = dump_named(
            stack,
            phone,
            f"905_expired_email_return_{attempt}",
            lg,
            shot=True,
            preview=False,
        )
        if email_screen(last) and email_ready(last, email):
            return last
    raise PhoneError(
        "Try again did not return to the email screen with the exact selected "
        "address. Did not type, replace, or submit another email."
    )


def _email_submit_outcome(nodes, email: str) -> str:
    """Classify only the selected-email screen versus a proven transition away."""
    if email_screen(nodes):
        if not email_ready(nodes, email):
            raise PhoneError(
                "Reddit changed or invalidated the selected email after Continue. "
                "Did not type or replace it."
            )
        return "email"
    if (
        find_label_exact(
            nodes,
            (
                "Verify your email",
                "Create your username",
                "Set a password",
                *ABOUT_MARKERS,
            ),
        )
        is not None
        or passkey_prompt(nodes)
    ):
        return "advanced"
    return "unknown"


def _wait_after_email_continue(
    stack: TapStack,
    phone: Phone,
    email: str,
    stem: str,
    lg=log,
):
    """Wait long enough to distinguish a slow transition from a failed tap."""
    last = []
    for attempt in range(EXPIRED_EMAIL_TRANSITION_POLLS):
        _cancelable_wait(stack, 0.8)
        _blob, last = dump_named(
            stack,
            phone,
            f"{stem}_{attempt}",
            lg,
            shot=True,
            preview=False,
        )
        if _email_submit_outcome(last, email) == "advanced":
            return last
    return last


def recover_expired_verification(
    stack: TapStack,
    phone: Phone,
    nodes,
    email: str,
    lg=log,
):
    """Tap expired-code Try again, then submit the selected email at most twice."""
    email = str(email or "").strip().lower()
    if not email:
        _raise_verification_expired()
    retry = _expired_try_again_node(nodes)
    if retry is None:
        raise PhoneError(
            "Expired verification popup is present, but its bounded Try again "
            "action could not be proven. Did not tap."
        )

    lg(f"Expired verification code -> Try again [{retry.x},{retry.y} {retry.w}x{retry.h}]")
    stack.tap_xy(*retry.center())
    CREATION_PROGRESS.note()
    selected = _wait_for_selected_email(stack, phone, email, lg)
    cont = email_continue_btn(selected)
    if cont is None:
        keyboard_done = keyboard_done_btn(selected)
        if keyboard_done is not None:
            lg(
                "Expired-code recovery returned the exact selected email with "
                "the keyboard covering Continue; tapping the proven Done key once "
                f"[{keyboard_done.x},{keyboard_done.y} "
                f"{keyboard_done.w}x{keyboard_done.h}]."
            )
            stack.tap_xy(*keyboard_done.center())
            CREATION_PROGRESS.note()
            for attempt in range(EXPIRED_EMAIL_KEYBOARD_POLLS):
                _cancelable_wait(stack, 0.8)
                _blob, selected = dump_named(
                    stack,
                    phone,
                    f"905_expired_email_keyboard_done_{attempt}",
                    lg,
                    shot=True,
                    preview=False,
                )
                outcome = _email_submit_outcome(selected, email)
                if outcome == "advanced":
                    lg(
                        "Keyboard Done submitted the exact selected email and "
                        "advanced; not tapping Continue again."
                    )
                    return selected
                if outcome == "unknown":
                    raise PhoneError(
                        "Keyboard Done left the exact selected-email form. Did not "
                        "guess or submit another control."
                    )
                cont = email_continue_btn(selected)
                if cont is not None:
                    break
    if cont is None:
        raise PhoneError(
            "Reddit returned to the exact selected email, but its enabled Continue "
            "button was not proven. Did not tap another control."
        )

    lg(f"Expired-code recovery email Continue [{cont.x},{cont.y} {cont.w}x{cont.h}]")
    stack.tap_xy(*cont.center())
    CREATION_PROGRESS.note()
    after = _wait_after_email_continue(
        stack,
        phone,
        email,
        "906_expired_email_continue",
        lg,
    )
    outcome = _email_submit_outcome(after, email)
    if outcome == "advanced":
        return after
    if outcome == "unknown":
        raise UnknownVisualStateError(
            "The first selected-email Continue left an unrecognized screen. "
            "Did not risk a duplicate submit."
        )

    cont = email_continue_btn(after)
    if cont is None:
        raise PhoneError(
            "Selected email remained after the first Continue, but the exact enabled "
            "Continue button was no longer proven. Did not guess a retry."
        )
    lg(
        "Expired-code recovery email Continue did not advance; retrying the exact "
        f"Continue once [{cont.x},{cont.y} {cont.w}x{cont.h}]."
    )
    stack.tap_xy(*cont.center())
    CREATION_PROGRESS.note()
    after = _wait_after_email_continue(
        stack,
        phone,
        email,
        "907_expired_email_continue_retry",
        lg,
    )
    outcome = _email_submit_outcome(after, email)
    if outcome == "advanced":
        return after
    if outcome == "unknown":
        raise UnknownVisualStateError(
            "The selected-email Continue retry left an unrecognized screen. "
            "Did not submit it a third time."
        )
    raise UnknownVisualStateError(
        "Selected email remained after the one allowed Continue retry. "
        "Did not submit it a third time."
    )


def _cancelable_wait(stack: TapStack, seconds: float) -> None:
    """Wait through TapStack so the bulk inactivity watchdog can interrupt."""
    waiter = getattr(stack, "wait", None)
    if callable(waiter):
        waiter(seconds)
    else:
        time.sleep(seconds)


def _completed_password_transition(
    stack: TapStack,
    phone: Phone,
    nodes,
    lg=log,
    *,
    email: str = "",
):
    """Return the proven next screen, or None while Reddit remains in transition."""
    if verification_code_expired(nodes):
        return recover_expired_verification(stack, phone, nodes, email, lg)
    if find_label_exact(nodes, ABOUT_MARKERS) is not None:
        CREATION_PROGRESS.note()
        return nodes
    if passkey_prompt(nodes):
        return dismiss_passkey_prompt(stack, phone, nodes, lg)
    return None


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
        CREATION_PROGRESS.note()
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
        raise UnknownVisualStateError(
            "iOS passkey popup remained after one proven X tap. Did not repeat the tap."
        )
    if find_label_exact(last, ("Set a password",)) is not None:
        raise PhoneError(
            "iOS passkey popup closed, but Reddit remained on Set a password. "
            "Did not retype or resubmit the stored password."
        )
    raise UnknownVisualStateError(
        "iOS passkey popup closed, but Reddit did not reach About you. Did not guess."
    )


def wait_after_password(
    stack: TapStack,
    phone: Phone,
    nodes,
    lg=log,
    *,
    email: str = "",
):
    """Wait, retry password Continue once, then yield to the 180s watchdog."""
    last = nodes
    saw_retry_later = False
    for attempt in range(PASSWORD_TRANSITION_POLLS):
        completed = _completed_password_transition(
            stack, phone, last, lg, email=email
        )
        if completed is not None:
            return completed
        saw_retry_later = saw_retry_later or reddit_retry_later(last)
        _cancelable_wait(stack, 0.8)
        _blob, last = dump_named(
            stack,
            phone,
            f"903_password_transition_{attempt}",
            lg,
            shot=True,
            preview=False,
        )
    completed = _completed_password_transition(stack, phone, last, lg, email=email)
    if completed is not None:
        return completed
    saw_retry_later = saw_retry_later or reddit_retry_later(last)
    if find_label_exact(last, ("Set a password",)) is None:
        raise PhoneError(
            "Reddit left Set a password but did not reach About you or the proven "
            "passkey prompt. Did not guess or resubmit."
        )

    validation = find_label_exact(last, ("Password must be at least 8 characters",))
    cont = find_label_exact(last, ("Continue",))
    if validation is None or cont is None or not cont.on_screen():
        raise PhoneError(
            "Reddit remained on Set a password, but the saved valid-password state "
            "could not be proven for one Continue retry."
        )
    if not password_validation_visual(stack.screenshot_png()):
        raise PhoneError(
            "Reddit remained on Set a password, but the green validation state and "
            "enabled Continue button could not be proven for one retry."
        )

    reason = "the transient retry-later response" if saw_retry_later else "no transition"
    lg(f"Password Continue produced {reason}; retrying the exact enabled Continue once.")
    stack.tap_xy(*cont.center())
    # This is the one allowed retry and therefore the last real progress before
    # the inactivity watchdog must own recovery.
    CREATION_PROGRESS.note()
    _cancelable_wait(stack, 2.0)
    _blob, last = dump_named(
        stack,
        phone,
        "904_after_password_retry",
        lg,
        shot=True,
        preview=False,
    )

    deadline = time.monotonic() + PASSWORD_STALL_SAFETY_SECONDS
    announced_stall = False
    poll = 0
    while True:
        completed = _completed_password_transition(
            stack, phone, last, lg, email=email
        )
        if completed is not None:
            return completed
        if find_label_exact(last, ("Set a password",)) is None:
            raise PhoneError(
                "Reddit left Set a password after the one Continue retry but did not "
                "reach About you or the proven passkey prompt."
            )
        if not announced_stall:
            lg(
                "Password Continue retry did not advance; waiting without progress "
                "updates so the 180-second inactivity watchdog can reset the attempt."
            )
            announced_stall = True
        if time.monotonic() >= deadline:
            raise PhoneError(
                "Password Continue retry remained stalled, but the inactivity watchdog "
                "did not cancel within its safety window."
            )
        _cancelable_wait(stack, PASSWORD_STALL_POLL_SECONDS)
        _blob, last = dump_named(
            stack,
            phone,
            f"904_password_retry_stall_{poll}",
            lg,
            shot=True,
            preview=False,
        )
        poll += 1


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
    CREATION_PROGRESS.note()
    _cancelable_wait(stack, 2.0)
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


def validate_requested_password(value: str) -> bool:
    """Validate without including the secret in any diagnostic."""
    return (
        REQUESTED_PASSWORD_MIN_LENGTH <= len(value) <= REQUESTED_PASSWORD_MAX_LENGTH
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def type_password(
    stack: TapStack, phone: Phone, sid: str, requested_password: str = "",
) -> str:
    slot = get_slot(sid)
    if not slot:
        raise PhoneError(f"no {sid} slot")
    email = str(slot.get("email") or "").strip()
    username = str(slot.get("username") or "").strip()
    if not email or not username:
        raise PhoneError(f"{sid} missing email or username")
    requested_password = str(requested_password or "")
    if requested_password and not validate_requested_password(requested_password):
        raise PhoneError(
            "Requested Reddit password must contain 8–128 printable characters."
        )
    pw = requested_password or manager_password()
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
    log(f"Password field tap (375,{y}) then type through one bounded XXTouch script")
    stack.tap_xy(375, y)
    time.sleep(0.5)
    # Passwords used to go through ``type_text``, which starts one remote Lua
    # script (and therefore one SSH/SFTP round trip) per character.  A real
    # device run died inside that native/Paramiko path after reaching this
    # exact screen, without a Python exception or a typed-password checkpoint.
    # ``type_human`` keeps the same key-by-key input semantics on the phone but
    # executes the whole bounded sequence in one XXTouch script.  It neither
    # uses the clipboard nor logs the secret and avoids multiplying a degraded
    # SFTP connection into dozens of native calls.
    stack.type_human(pw, ms=95)
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
    CREATION_PROGRESS.note()
    _cancelable_wait(stack, 2.0)
    _blob, nodes = dump_named(
        stack,
        phone,
        "902_after_password",
        log,
        shot=True,
        preview=False,
    )
    wait_after_password(stack, phone, nodes, log, email=email)
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
