# UTF-8
"""Create ONE iCloud Hide My Email address from Settings. Then stop. No Reddit signup."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from friend_poster.core.config import CONTINUE_LABELS, CREATE_LABELS, DUMP_DIR, FORBIDDEN_HME_LABELS, HME_SETTINGS_URL, LOG_DIR
from friend_poster.device.ssh_device import Phone, PhoneError, load_secrets
from friend_poster.device.ui_tree import Node, TapStack, collapsed_text, dump_named, find_label, find_label_exact

LogFn = Callable[[str], None]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@icloud\.com", re.I)
HME_CURSOR_BEFORE_AT_RE = re.compile(r"[|¦]\s*(?=@icloud\.com)", re.I)


def _icloud_emails(nodes) -> list[str]:
    """Extract full iCloud addresses, accepting only the HME cursor-bar OCR.

    The active Label field can render its insertion cursor as ``|`` directly
    before ``@icloud.com``. Normalize only that exact position, then require a
    full-token match so other malformed local parts stay rejected.
    """
    out: list[str] = []
    trim = "<>()[]{}'\".,;:"
    for node in nodes:
        text = HME_CURSOR_BEFORE_AT_RE.sub("", node.labels())
        for raw in text.split():
            token = raw.strip(trim)
            if EMAIL_RE.fullmatch(token) and token not in out:
                out.append(token)
    return out


def _forbidden(node_label: str) -> bool:
    blob = (node_label or "").casefold()
    return any(f in blob for f in FORBIDDEN_HME_LABELS)


def _save_address(addr: str, log: LogFn, label: str = "") -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "hidemyemail.json")
    rows = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f) or []
        except (OSError, json.JSONDecodeError):
            rows = []
    rec = {
        "email": addr,
        "label": label or "",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "iphone_settings_hide_my_email",
    }
    rows.append(rec)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
        f.write("\n")
    log(f"Saved {addr} to {path} (no password stored).")
    return path


class ICloudPasswordEntryError(PhoneError):
    """The saved iCloud password could not be entered on the re-auth sheet."""

    failure_code = "icloud_password_entry_failed"

    def __init__(
        self,
        message: str,
        *,
        credentials_rejected: bool = False,
        focus_never_proven: bool = False,
        verified_sheet=None,
        selection_overlay: bool = False,
    ):
        super().__init__(message)
        # True only when iCloud visibly refused the submitted password.  A
        # rejection is not a recoverable input hiccup: it must never be
        # Cancel-retried or automatically resubmitted.
        self.credentials_rejected = bool(credentials_rejected)
        # Internal recovery evidence. A caller may dismiss/re-open a stuck
        # sheet only when the failed focus path retained the immediately prior
        # verified modal identity; arbitrary changed screens remain terminal.
        self.focus_never_proven = bool(focus_never_proven)
        self.verified_sheet = verified_sheet
        self.selection_overlay = bool(selection_overlay)


@dataclass(frozen=True)
class _ICloudPasswordSheet:
    """A verified iCloud "Sign in to iCloud" password sheet.

    ``field`` is the real OCR placeholder node when the layout exposes one
    (older XXTouch builds did).  When the secure field is empty XXTouch omits
    it entirely, so ``field_point`` is then derived from the verified modal.
    ``instruction``/``apple_id`` are the extra modal labels the derived layout
    must prove; they may be absent on the older layout.
    """

    title: Node
    instruction: Node | None
    apple_id: Node | None
    cancel: Node
    ok: Node
    field: Node | None
    field_point: tuple[int, int]

    @property
    def derived(self) -> bool:
        return self.field is None


_SHEET_TITLE = "Sign in to iCloud"
_PASSWORD_INSTRUCTION = "Enter the password for your Apple ID"
_OK_LABELS = {"ok", "ок", "оk", "oк"}
# Phrases iOS uses when the submitted password was refused.  They never appear
# on a healthy password sheet, so they are a safe immediate-failure signal.
_AUTH_ERROR_MARKERS = (
    "incorrect",
    "invalid password",
    "wrong password",
    "could not sign in",
    "unable to sign in",
    "sign-in failed",
    "sign in failed",
    "verification failed",
    "try again",
)
# The on-screen keyboard is OCR-visible while a text field has focus.
_KEYBOARD_WORDS = {"space", "return"}
_KEYBOARD_MIN_Y = 700
_SHEET_SUBMIT_WAIT_SECONDS = 15.0
_SHEET_POLL_SECONDS = 1.5
# One verified tap is enough. The old long-press/Select-All/backspace oracle
# could select the Apple-ID row and could send a control character as text.
# Entry now starts only from the explicit empty ``Password`` placeholder and
# the saved password is sent exactly once.
_FOCUS_ATTEMPTS_TOTAL = 1
_FOCUS_KEYBOARD = "keyboard_visible"
_FOCUS_OVERLAY = "verified_selection_overlay"
_FOCUS_UNFOCUSED = "verified_unfocused_sheet"
_FOCUS_UNKNOWN = "unknown"
_ANCHOR_TOLERANCE = 12


def _sheet_gone(nodes) -> bool:
    """True when no iCloud password sheet is on the dumped screen."""
    return _SHEET_TITLE.casefold() not in " ".join(
        n.labels().casefold() for n in nodes
    )


def _icloud_apple_id_line(nodes, instruction, cancel):
    """The Apple ID line just below the password instruction (holds an '@')."""
    iy = instruction.center()[1]
    cy = cancel.center()[1]
    best = None
    for node in nodes:
        if node.ntype != "ocr" or not node.visible or not node.on_screen():
            continue
        if "@" not in node.labels():
            continue
        if not iy < node.center()[1] < cy:
            continue
        if best is None or node.y < best.y:
            best = node
    return best


def _derive_icloud_password_point(title, instruction, apple_id, cancel, ok):
    """Derive the invisible secure field's tap point from the verified modal.

    The empty secure field sits on its own row between the Apple ID line and
    the Cancel/OK row, spanning the modal body.  Tap the horizontal middle of
    the verified modal at the vertical middle of that gap.  Returns ``None``
    when the derived point leaves the bounded modal band.
    """
    left = min(title.x, instruction.x, apple_id.x, cancel.x, ok.x)
    right = max(
        title.x + title.w,
        instruction.x + instruction.w,
        apple_id.x + apple_id.w,
        cancel.x + cancel.w,
        ok.x + ok.w,
    )
    fx = (left + right) // 2
    apple_bottom = apple_id.y + apple_id.h
    fy = apple_bottom + (cancel.y - apple_bottom) // 2
    if not (100 <= fx <= 600 and 430 <= fy <= 550):
        return None
    return (fx, fy)


def _icloud_password_sheet_controls(nodes):
    """Verify the centered iCloud password sheet in either known layout.

    XXTouch OCR has rendered the right button as Cyrillic ОК.  Limit that
    confusable-label allowance to this sheet and require the controls in their
    separate, bounded positions before entering a password.

    * Older layout: an explicit ``Password`` placeholder marks the empty secure
      field, and it is used directly (this is the layout that must keep working).
    * Current layout: XXTouch omits the empty secure field, so every modal label
      (title, instruction, Apple ID line, Cancel, one OK) and the strict
      geometry are required and the tap point is derived from that modal.
    """
    title = find_label_exact(nodes, (_SHEET_TITLE,))
    cancel = find_label_exact(nodes, ("Cancel",))
    oks = [
        n for n in nodes
        if n.ntype == "ocr" and n.visible and n.on_screen()
        and collapsed_text(n.labels()) in _OK_LABELS
    ]
    if not (title and cancel and len(oks) == 1):
        return None
    ok = oks[0]
    tx, ty = title.center()
    cx, cy = cancel.center()
    ox, oy = ok.center()
    if not (
        200 <= tx <= 550 and 250 <= ty <= 450
        and 100 <= cx <= 350 and 530 <= cy <= 650
        and 420 <= ox <= 650 and 530 <= oy <= 650
        and abs(cy - oy) <= 35
    ):
        return None

    field = find_label_exact(nodes, ("Password",))
    if field is not None:
        # Preserved older layout: the empty secure field is OCR-visible.
        fx, fy = field.center()
        if not (100 <= fx <= 400 and 430 <= fy <= 550 and ty < fy < cy):
            return None
        return _ICloudPasswordSheet(
            title=title,
            instruction=None,
            apple_id=None,
            cancel=cancel,
            ok=ok,
            field=field,
            field_point=(fx, fy),
        )

    # Derived layout: the empty secure field is invisible, so every modal label
    # must be present in its bounded position before the tap point is derived.
    instruction = find_label(nodes, (_PASSWORD_INSTRUCTION,))
    if instruction is None:
        return None
    apple_id = _icloud_apple_id_line(nodes, instruction, cancel)
    if apple_id is None:
        return None
    ix, iy = instruction.center()
    ax, ay = apple_id.center()
    if not (
        150 <= ix <= 600 and ty < iy <= 470
        and 150 <= ax <= 600 and iy <= ay <= 500
        and ay < cy
    ):
        return None
    point = _derive_icloud_password_point(title, instruction, apple_id, cancel, ok)
    if point is None:
        return None
    return _ICloudPasswordSheet(
        title=title,
        instruction=instruction,
        apple_id=apple_id,
        cancel=cancel,
        ok=ok,
        field=None,
        field_point=point,
    )


def _keyboard_present(nodes) -> bool:
    """True when the on-screen keyboard is OCR-visible (secure-field focus)."""
    for node in nodes:
        if not node.on_screen() or node.y < _KEYBOARD_MIN_Y:
            continue
        if set(collapsed_text(node.labels()).split()) & _KEYBOARD_WORDS:
            return True
    return False


def _keyboard_verified(stack, nodes, log) -> bool:
    """Confirm keyboard visibility semantically, retaining OCR as fallback."""
    semantic_probe = getattr(stack, "semantic_keyboard_present", None)
    if callable(semantic_probe):
        try:
            semantic = semantic_probe()
        except PhoneError as exc:
            log(
                "iCloud password keyboard accessibility probe failed safely "
                f"({type(exc).__name__}); using OCR fallback."
            )
            semantic = None
        if semantic is True:
            return True
        if semantic is False:
            # Retain the established OCR route for XXTouch builds whose
            # keyboard-role exposure is incomplete.
            return _keyboard_present(nodes)
    return _keyboard_present(nodes)


def _centers_match(first, second, tolerance: int = _ANCHOR_TOLERANCE) -> bool:
    """True when two freshly observed controls occupy the same modal anchor."""
    if first is None or second is None:
        return False
    ax, ay = first.center()
    bx, by = second.center()
    return abs(ax - bx) <= tolerance and abs(ay - by) <= tolerance


def _selection_overlay_sheet(nodes, previous):
    """Return an anchored sheet for the exact a0083 selection-overlay state.

    The overlay partially corrupts the instruction and Apple-ID OCR, so the
    normal full-sheet parser must reject it. This narrow transition is trusted
    only when the title, Cancel and sole OK button still match the immediately
    preceding verified sheet and one OCR node contains both ``Select All`` and
    an iCloud address in the bounded strip above that sheet's secure-field
    point. The menu itself is never tapped.
    """
    if previous is None:
        return None
    title = find_label_exact(nodes, (_SHEET_TITLE,))
    cancel = find_label_exact(nodes, ("Cancel",))
    oks = [
        node for node in nodes
        if node.ntype == "ocr" and node.visible and node.on_screen()
        and collapsed_text(node.labels()) in _OK_LABELS
    ]
    if not (
        _centers_match(title, previous.title)
        and _centers_match(cancel, previous.cancel)
        and len(oks) == 1
        and _centers_match(oks[0], previous.ok)
    ):
        return None

    _fx, fy = previous.field_point
    instruction_y = (
        previous.instruction.center()[1]
        if previous.instruction is not None
        else previous.title.center()[1]
    )
    overlay = None
    for node in nodes:
        if node.ntype != "ocr" or not node.visible or not node.on_screen():
            continue
        label = collapsed_text(node.labels())
        cx, cy = node.center()
        if (
            "select all" in label
            and "@icloud.com" in label
            and 100 <= cx <= 650
            and instruction_y - 20 <= cy <= fy - 20
        ):
            overlay = node
            break
    if overlay is None:
        return None
    return _ICloudPasswordSheet(
        title=title,
        instruction=previous.instruction,
        apple_id=previous.apple_id,
        cancel=cancel,
        ok=oks[0],
        field=previous.field,
        field_point=previous.field_point,
    )


def _classify_icloud_focus_frame(nodes, previous, *, keyboard_visible=False):
    """Classify one post-focus frame without weakening the keyboard gate."""
    fresh = _icloud_password_sheet_controls(nodes)
    if fresh is not None:
        if keyboard_visible or _keyboard_present(nodes):
            return _FOCUS_KEYBOARD, fresh
        return _FOCUS_UNFOCUSED, fresh
    overlay = _selection_overlay_sheet(nodes, previous)
    if overlay is not None:
        if keyboard_visible:
            return _FOCUS_KEYBOARD, overlay
        return _FOCUS_OVERLAY, overlay
    return _FOCUS_UNKNOWN, None


def _icloud_auth_error(nodes) -> str:
    """The first authentication-error label shown on the sheet, else ``""``."""
    for node in nodes:
        blob = node.labels().casefold()
        if not blob:
            continue
        for marker in _AUTH_ERROR_MARKERS:
            if marker in blob:
                return collapsed_text(node.labels())[:80]
    return ""


def _focus_icloud_password_field(stack, phone, log, sheet, stop, *, remaining, stem_prefix):
    """Focus an explicitly empty password field with one verified short tap.

    A fully verified sheet supplies the password-row target. OCR keyboard labels
    are useful evidence but are not mandatory because the live iOS keyboard can
    be visibly open while XXTouch omits those labels. A selection overlay or
    changed modal is never typed into.
    """
    budget = int(remaining)
    if budget <= 0:
        raise ICloudPasswordEntryError(
            "iCloud password sheet: no secure-field focus attempts remain in "
            "the bounded budget; did not type the password."
        )
    if stop and stop.is_set():
        raise PhoneError("Stopped.")
    if not stack.can_tap():
        raise ICloudPasswordEntryError(
            "iCloud password sheet: no tap backend is available to focus the "
            "verified empty Password field; did not type the password."
        )
    proof = "explicit empty Password field" if sheet.field is not None else "fully verified password row"
    log(f"iCloud password field: tapping {proof} {sheet.field_point} once.")
    stack.tap_xy(*sheet.field_point)
    time.sleep(0.8)
    if stop and stop.is_set():
        raise PhoneError("Stopped.")
    _blob, nodes = dump_named(stack, phone, f"{stem_prefix}_1", log)
    keyboard_visible = _keyboard_verified(stack, nodes, log)
    state, fresh = _classify_icloud_focus_frame(
        nodes, sheet, keyboard_visible=keyboard_visible
    )
    if state == _FOCUS_OVERLAY:
        raise ICloudPasswordEntryError(
            "iCloud password field opened a selection overlay; Select All was "
            "not tapped and the password was not typed.",
            focus_never_proven=True,
            verified_sheet=sheet,
            selection_overlay=True,
        )
    if state == _FOCUS_UNKNOWN or fresh is None:
        raise ICloudPasswordEntryError(
            "iCloud password sheet changed after the verified field tap; the "
            "password was not typed into an unknown screen."
        )
    if not keyboard_visible:
        log(
            "iCloud password field: keyboard labels were absent from the dump; "
            "continuing because the tap targeted the freshly verified password "
            "row and the same modal was freshly re-verified."
        )
    return fresh, nodes, 1


def _type_and_submit_icloud_password(stack, phone, log, sheet, pwd) -> None:
    """Type the saved password (never logged) once, then submit it once.

    The OK control is re-acquired from a fresh verified dump after typing, so
    the single submission tap never uses a stale coordinate.
    """
    log("iCloud password sheet: typing the saved password (value not logged).")
    stack.type_text(pwd)
    time.sleep(0.4)
    if not stack.can_tap():
        raise ICloudPasswordEntryError(
            "iCloud password sheet: the password was typed, but there is no tap "
            "backend to reach the verified OK control. Did not submit blindly."
        )
    _blob, nodes = dump_named(stack, phone, "03_hme_password_before_ok", log)
    fresh = _icloud_password_sheet_controls(nodes)
    if fresh is None:
        raise ICloudPasswordEntryError(
            "iCloud password sheet could not be re-verified after typing; did not "
            "tap OK from a stale frame."
        )
    log("iCloud password sheet: re-verified after one type; submitting once via the fresh OK control.")
    stack.tap_xy(*fresh.ok.center())


def _poll_icloud_sheet(stack, phone, log, stop, *, stem, timeout):
    """Poll until the sheet closes; raise at once on an authentication error."""
    deadline = time.monotonic() + timeout
    nodes: list = []
    while True:
        if stop and stop.is_set():
            raise PhoneError("Stopped.")
        time.sleep(_SHEET_POLL_SECONDS)
        _blob, nodes = dump_named(stack, phone, stem, log)
        if _sheet_gone(nodes):
            return True, nodes
        error = _icloud_auth_error(nodes)
        if error:
            raise ICloudPasswordEntryError(
                f"iCloud rejected the saved password ({error!r}); did not type it again.",
                credentials_rejected=True,
            )
        if time.monotonic() >= deadline:
            return False, nodes


_SETTINGS_DESTINATION_MARKERS = (
    "hide my email",
    "icloud+",
    "icloud mail",
    "show all",
    "manage account",
    "passwords and keychain",
    "private relay",
    "icloud backup",
    "name, phone numbers",
    "password & security",
    "media & purchases",
)


def _authenticated_destination(nodes) -> bool:
    """Positive evidence the dismissed sheet returned to signed-in Settings.

    The full Apple ID sign-in wizard means authentication did NOT hold and is
    explicitly rejected; otherwise the frame must show a known Settings
    marker, so a sheet that merely vanished (SpringBoard, a blank screen)
    never passes as success.
    """
    if _full_apple_id_signin(nodes):
        return False
    joined = " ".join(n.labels().casefold() for n in nodes)
    return any(marker in joined for marker in _SETTINGS_DESTINATION_MARKERS)


def _require_authenticated_destination(nodes) -> list:
    if not _authenticated_destination(nodes):
        raise ICloudPasswordEntryError(
            "iCloud password sheet closed, but the authenticated Settings "
            "destination could not be verified; did not continue or submit again."
        )
    return nodes


def dismiss_icloud_password_sheet(
    stack, phone, log, stop=None, *, known_sheet=None
) -> bool:
    """Dismiss a stuck iCloud password sheet with ONE verified Cancel tap.

    The Cancel control must be re-verified from a fresh dump and dismissal is
    confirmed by a second fresh dump.  Exactly one tap is ever attempted;
    an unverifiable sheet or an unconfirmed dismissal returns ``False``
    without any blind retry.
    """
    if not stack.can_tap() or (stop and stop.is_set()):
        return False
    _blob, nodes = dump_named(stack, phone, "03_hme_icloud_cancel_attempt", log)
    sheet = _icloud_password_sheet_controls(nodes)
    if sheet is None and known_sheet is not None:
        overlay_sheet = _selection_overlay_sheet(nodes, known_sheet)
        if overlay_sheet is not None:
            log(
                "iCloud sheet dismissal: a verified selection overlay covers "
                "the modal; tapping the retained secure-field point once to "
                "close it. Select All will not be tapped."
            )
            stack.tap_xy(*known_sheet.field_point)
            time.sleep(0.8)
            if stop and stop.is_set():
                return False
            _blob, nodes = dump_named(
                stack, phone, "03_hme_icloud_overlay_closed", log
            )
            sheet = _icloud_password_sheet_controls(nodes)
    if sheet is None:
        log(
            "iCloud sheet dismissal: the sheet controls could not be verified "
            "from a fresh frame; no Cancel tap was made."
        )
        return False
    log(
        "iCloud sheet dismissal: tapping the verified Cancel control once at "
        f"{sheet.cancel.center()}."
    )
    stack.tap_xy(*sheet.cancel.center())
    time.sleep(1.0)
    if stop and stop.is_set():
        return False
    _blob, fresh = dump_named(stack, phone, "03_hme_icloud_cancelled", log)
    if not _sheet_gone(fresh):
        log(
            "iCloud sheet dismissal: the sheet is still present after the single "
            "verified Cancel tap; not repeating it."
        )
        return False
    log("iCloud sheet dismissal: confirmed dismissed from a fresh frame.")
    return True


def _unlock_icloud_if_needed(stack, phone, log, nodes, stop) -> list:
    """If Settings is sitting on the Apple ID password sheet, type the saved iCloud password."""
    if _sheet_gone(nodes):
        return nodes
    pwd = str(load_secrets().get("icloud_password") or "").strip()
    if not pwd:
        raise PhoneError("iCloud password sheet is on screen, but no saved iCloud password is available. Enter it on the phone; do not paste it in chat.")
    sheet = _icloud_password_sheet_controls(nodes)
    if sheet is None and stack.can_tap():
        # The Settings animation can overlap the modal with the underlying
        # iCloud rows for one OCR frame (the live a0236 frame merged Cancel/OK
        # into those rows).  Re-dump once without tapping, typing or reopening
        # the pane.  Only a fully verified fresh modal may continue.
        log(
            "iCloud password sheet: controls were incomplete in the first "
            "frame; waiting for one fresh local verification."
        )
        time.sleep(0.8)
        _blob, fresh_nodes = dump_named(
            stack, phone, "03_hme_password_controls_retry", log
        )
        fresh_sheet = _icloud_password_sheet_controls(fresh_nodes)
        if fresh_sheet is not None:
            nodes = fresh_nodes
            sheet = fresh_sheet
            log(
                "iCloud password sheet: fresh frame verified the bounded "
                "Password, Cancel and OK controls."
            )
    if sheet is None or not stack.can_tap():
        raise ICloudPasswordEntryError("iCloud password sheet is visible, but its bounded controls could not be verified. Did not type or tap.")
    focused = _focus_icloud_password_field(
        stack, phone, log, sheet, stop,
        remaining=_FOCUS_ATTEMPTS_TOTAL, stem_prefix="03_hme_password_focus",
    )
    sheet, nodes, _focus_used = focused
    if stop and stop.is_set():
        raise PhoneError("Stopped.")
    _type_and_submit_icloud_password(stack, phone, log, sheet, pwd)
    dismissed, nodes = _poll_icloud_sheet(
        stack, phone, log, stop,
        stem="03_hme_after_icloud_submit",
        timeout=_SHEET_SUBMIT_WAIT_SECONDS,
    )
    if dismissed:
        return _require_authenticated_destination(nodes)
    raise ICloudPasswordEntryError(
        "iCloud password sheet remained after one password submission; the "
        "password was not typed or submitted again."
    )


_FRESH_SHEET_STEM = "03_hme_after_fresh_sheet"


def _focus_never_proven(exc: BaseException) -> bool:
    """True when the secure field never raised a keyboard (not a layout change).

    Distinguishes the live failure seen on a0076/a0078/a0079 -- the sheet
    verified, the tap came from that fresh verification, and the field still
    never became first responder -- from a sheet whose layout changed and
    therefore must not be re-tapped at a stale point.
    """
    return bool(getattr(exc, "focus_never_proven", False)) or (
        "keyboard did not appear within the bounded focus attempts" in str(exc)
    )


def _unlock_icloud_with_fresh_sheet_retry(stack, phone, log, nodes, stop) -> list:
    """Unlock iCloud once; on a never-focused secure field, unlock a fresh sheet.

    A sheet that ignores touches cannot be fixed by touching it again, so the
    only recovery this path already trusts is used: dismiss the stuck sheet with
    ONE verified Cancel tap (dismissal re-confirmed from a fresh frame), re-open
    the Hide My Email pane so iOS presents a *new* sheet, and unlock once more.

    Nothing here types into an unknown frame. Every tap is derived from a frame
    dumped after the dismissal, the password is sent once only after the fresh
    sheet is re-verified, and a second failure re-raises instead of looping.
    """
    try:
        return _unlock_icloud_if_needed(stack, phone, log, nodes, stop)
    except ICloudPasswordEntryError as exc:
        if not _focus_never_proven(exc):
            raise
        if not stack.can_tap() or (stop and stop.is_set()):
            raise
        log(
            "iCloud recovery: the secure field never focused on this sheet, so "
            "re-tapping it cannot help. Dismissing it with the verified Cancel "
            "control and re-opening a fresh sheet once."
        )
        if not dismiss_icloud_password_sheet(
            stack,
            phone,
            log,
            stop,
            known_sheet=getattr(exc, "verified_sheet", None),
        ):
            log(
                "iCloud recovery: the stuck sheet could not be dismissed; not "
                "opening a second sheet."
            )
            raise
        stack.open_url(HME_SETTINGS_URL)
        time.sleep(3.0)
        if stop and stop.is_set():
            raise PhoneError("Stopped.")
        _blob, fresh = dump_named(stack, phone, _FRESH_SHEET_STEM, log)
        if _sheet_gone(fresh):
            log(
                "iCloud recovery: no fresh sheet appeared after re-opening Hide "
                "My Email; returning that frame without guessing."
            )
            return fresh
        log("iCloud recovery: a fresh sheet is up; unlocking it now.")
        return _unlock_icloud_if_needed(stack, phone, log, fresh, stop)


def _full_apple_id_signin(nodes) -> bool:
    """True for the full Apple ID login wizard, not password re-auth."""
    joined = " ".join(n.labels().casefold() for n in nodes)
    return (
        "apple id email or phone" in joined
        and "sign in with your apple id" in joined
        and "next" in joined
    )


def _hide_my_email_detail(nodes) -> bool:
    """True when Settings restored an existing HME address detail page."""
    joined = " ".join(n.labels().casefold() for n in nodes)
    return (
        "hide my email" in joined
        and "forward to" in joined
        and "label" in joined
        and bool(_icloud_emails(nodes))
    )


def _top_left_back(nodes):
    """Return only a proven navigation Back node in the top-left band."""
    for node in nodes:
        text = node.labels().casefold()
        if node.x < 180 and node.y < 180 and re.search(r"\bback\b", text):
            return node
    return None


def _leave_hme_detail_if_needed(stack, phone, log, nodes, stem: str) -> list:
    """Return from an existing-address detail page to the HME list once."""
    if not _hide_my_email_detail(nodes):
        return nodes
    back = _top_left_back(nodes)
    if back is None or not stack.can_tap():
        raise PhoneError(
            "Hide My Email address detail is open, but a verified top-left "
            "Back node is unavailable. Did not guess a tap."
        )
    log(f"Hide My Email address detail restored -> tapping verified Back at {back.center()}")
    stack.tap_xy(*back.center())
    time.sleep(1.5)
    _blob, fresh = dump_named(stack, phone, stem, log)
    if _hide_my_email_detail(fresh):
        raise PhoneError(
            "Hide My Email Back left the address-detail screen unchanged. "
            "Stopped without retrying blindly."
        )
    return fresh


def _find_create(nodes) -> object | None:
    node = find_label_exact(nodes, CREATE_LABELS)
    if node and not _forbidden(node.labels()):
        return node
    node = find_label(nodes, CREATE_LABELS)
    if node and not _forbidden(node.labels()):
        return node
    return None


def _icloud_main_hme_row(nodes):
    """Recognize the iCloud overview, not an HME list with the same title."""
    joined = " ".join(n.labels().casefold() for n in nodes)
    full_overview = (
        "apps using icloud" in joined and "manage account storage" in joined
    )
    scrolled_overview = (
        "icloud backup" in joined
        and "icloud+" in joined
        and "private relay" in joined
        and "hide my email" in joined
    )
    if not (full_overview or scrolled_overview):
        return None
    return find_label_exact(nodes, ("Hide My Email",))


def _label_field(nodes):
    fields = [
        n
        for n in nodes
        if collapsed_text(n.labels()) == "label your address" and n.on_screen()
    ]
    if not fields:
        return None
    return max(fields, key=lambda n: n.y)


def _apply_icloud_label(stack, phone, log, nodes, label: str, stop) -> list:
    """Type the slot id (r1, r2, ...) on the Hide My Email label screen."""
    if not (label or "").strip():
        return nodes

    def cancelled() -> bool:
        return bool(stop and stop.is_set())

    for step in range(5):
        if cancelled():
            raise PhoneError("Stopped.")
        field = _label_field(nodes)
        if field is not None and stack.can_tap():
            log(f"Hide My Email label field: typing {label!r}")
            stack.tap_xy(*field.center())
            time.sleep(0.5)
            stack.type_text(label)
            time.sleep(0.8)
            blob, nodes = dump_named(stack, phone, "05_hme_label_typed", log)
            nxt = find_label_exact(nodes, ("Next", "Done", "Continue"))
            if nxt and not _forbidden(nxt.labels()) and stack.can_tap():
                stack.tap_xy(*nxt.center())
                time.sleep(1.5)
                blob, nodes = dump_named(stack, phone, "06_hme_after_label", log)
            for fin in range(1, 4):
                if cancelled():
                    raise PhoneError("Stopped.")
                # All Set is a title. Tap bottom Done (~375,1251), never the title.
                dones = [
                    n
                    for n in nodes
                    if n.on_screen()
                    and collapsed_text(n.labels()) == "done"
                    and not _forbidden(n.labels())
                    and n.y > 1000
                ]
                if dones:
                    done = max(dones, key=lambda n: n.y)
                    log(f"All Set Done [{done.x},{done.y} {done.w}x{done.h}]")
                    stack.tap_xy(375, done.center()[1])
                elif find_label_exact(nodes, ("All Set",)) is not None:
                    log("All Set on screen, Done node missing -> tap (375,1251)")
                    stack.tap_xy(375, 1251)
                else:
                    break
                time.sleep(1.2)
                blob, nodes = dump_named(stack, phone, f"07_hme_after_done_{fin}", log)
            return nodes
        cont = find_label(nodes, CONTINUE_LABELS)
        if cont and not _forbidden(cont.labels()) and stack.can_tap():
            stack.tap_xy(*cont.center())
            time.sleep(1.5)
            blob, nodes = dump_named(stack, phone, f"05_hme_prelabel_{step}", log)
            continue
        break
    log("Hide My Email label screen not found after create; address still captured.")
    return nodes


def create_one_hide_my_email(
    phone: Phone,
    stack: TapStack,
    log: LogFn,
    stop=None,
    open_settings: bool = True,
    label: str = "",
) -> str:
    """Open Hide My Email, tap Create once, optional iCloud+ label, return the @icloud.com address."""

    def cancelled() -> bool:
        return bool(stop and stop.is_set())

    if open_settings:
        log("Hide My Email: opening Settings pane (no Sign in with Apple, no Reddit).")
        stack.open_url(HME_SETTINGS_URL)
        time.sleep(2.5)
        if cancelled():
            raise PhoneError("Stopped.")
    else:
        log("Hide My Email: dumping the live screen (already open).")

    blob, nodes = dump_named(stack, phone, "03_hme_before_create", log)
    nodes = _unlock_icloud_with_fresh_sheet_retry(stack, phone, log, nodes, stop)
    if _full_apple_id_signin(nodes):
        raise PhoneError(
            "Apple ID is signed out: the full Email or Phone sign-in screen "
            "is open. Sign in manually in Settings, confirm iCloud+ Hide My "
            "Email is available, then retry."
        )
    nodes = _leave_hme_detail_if_needed(
        stack, phone, log, nodes, "03_hme_after_initial_detail_back"
    )
    before = set(_icloud_emails(nodes))
    if before:
        log("Existing Hide My Email addresses on screen: " + ", ".join(before))

    create = _find_create(nodes)
    if create is None:
        # Fallback: uiopen landed on iCloud main (Manage Storage) not Hide My Email list — find and tap Hide My Email row
        # Try to locate Hide My Email, scroll if needed (r11 case 03 showed iCloud Backup etc)
        hme = None
        for attempt in range(6):
            hme = find_label_exact(nodes, ("Hide My Email",))
            if hme is not None:
                break
            log(f"Create not found, Hide My Email row not in viewport attempt {attempt+1}/6 -> scroll")
            stack.swipe(375, 1000, 375, 650)
            time.sleep(0.7)
            blob, nodes = dump_named(stack, phone, f"03_hme_before_create_scroll_{attempt}", log)
            nodes = _unlock_icloud_with_fresh_sheet_retry(stack, phone, log, nodes, stop)
            if _full_apple_id_signin(nodes):
                raise PhoneError(
                    "Apple ID is signed out on the full Email or Phone sign-in screen. "
                    "Sign in manually in Settings, then retry."
                )
        if hme is not None:
            log(f"Tapping Hide My Email row at {hme.center()} to open list")
            stack.tap_xy(*hme.center())
            time.sleep(1.5)
            blob, nodes = dump_named(stack, phone, "03_hme_before_create_retry", log)
            nodes = _unlock_icloud_with_fresh_sheet_retry(stack, phone, log, nodes, stop)
            if _full_apple_id_signin(nodes):
                raise PhoneError(
                    "Apple ID is signed out on the full Email or Phone sign-in screen. "
                    "Sign in manually in Settings, then retry."
                )
            # The first row tap can open an iCloud password sheet. After the
            # sheet is dismissed, Settings returns to the overview rather
            # than completing the original navigation. Only re-tap a row
            # proven on that overview, and do it at most once.
            hme_again = _icloud_main_hme_row(nodes)
            if hme_again is not None:
                if not stack.can_tap():
                    raise PhoneError("Hide My Email row is visible after iCloud unlock, but no tap backend is available.")
                log("Hide My Email still on iCloud overview after first tap; tapping the verified row once more.")
                stack.tap_xy(*hme_again.center())
                time.sleep(1.5)
                blob, nodes = dump_named(stack, phone, "03_hme_after_second_row_tap", log)
                had_password_sheet = (
                    "sign in to icloud" in " ".join(n.labels().casefold() for n in nodes)
                )
                nodes = _unlock_icloud_with_fresh_sheet_retry(stack, phone, log, nodes, stop)
                # Authentication dismisses back to the overview instead of
                # completing the row navigation.  One verified post-unlock
                # re-entry is safe; it is not a blind repeat.
                after_unlock = _icloud_main_hme_row(nodes)
                if had_password_sheet and after_unlock is not None:
                    log("iCloud unlock returned to the overview; re-entering the verified Hide My Email row once.")
                    stack.tap_xy(*after_unlock.center())
                    time.sleep(1.5)
                    blob, nodes = dump_named(stack, phone, "03_hme_after_unlock_row_tap", log)
                    if "sign in to icloud" in " ".join(n.labels().casefold() for n in nodes):
                        raise PhoneError("iCloud requested its password again after the bounded post-unlock Hide My Email tap.")
                if _icloud_main_hme_row(nodes) is not None:
                    raise PhoneError("Hide My Email row remained on the iCloud overview after bounded verified navigation; did not guess further taps.")
            nodes = _leave_hme_detail_if_needed(
                stack, phone, log, nodes, "03_hme_after_address_detail_back"
            )
            before = set(_icloud_emails(nodes))
            if before:
                log("Existing Hide My Email addresses on screen: " + ", ".join(before))
            create = _find_create(nodes)
            # After opening Hide My Email list, Create may be off-screen (r11: BloFin list without Create) — scroll to reveal it
            if create is None:
                for attempt in range(6):
                    log(f"Create still not found after entering Hide My Email list attempt {attempt+1}/6 -> scroll to reveal Create")
                    # Try revealing top (Create is at top) — swipe down then up
                    if attempt % 2 == 0:
                        stack.swipe(375, 500, 375, 850)
                    else:
                        stack.swipe(375, 850, 375, 500)
                    time.sleep(0.7)
                    blob, nodes = dump_named(stack, phone, f"03_hme_before_create_retry_scroll_{attempt}", log)
                    had_password_sheet = (
                        "sign in to icloud" in " ".join(n.labels().casefold() for n in nodes)
                    )
                    nodes = _unlock_icloud_with_fresh_sheet_retry(stack, phone, log, nodes, stop)
                    overview_row = _icloud_main_hme_row(nodes)
                    if had_password_sheet and overview_row is not None:
                        log("Delayed iCloud unlock returned to the overview; re-entering the verified Hide My Email row once.")
                        stack.tap_xy(*overview_row.center())
                        time.sleep(1.5)
                        blob, nodes = dump_named(stack, phone, "03_hme_after_delayed_unlock_row_tap", log)
                        if _icloud_main_hme_row(nodes) is not None:
                            raise PhoneError("Hide My Email row remained on the overview after delayed unlock re-entry; did not keep scrolling Settings.")
                    before = set(_icloud_emails(nodes))
                    if before:
                        log("Existing Hide My Email addresses on screen: " + ", ".join(before))
                    create = _find_create(nodes)
                    if create is not None:
                        log(f"Found Create after scroll {attempt+1}: {create.labels()!r} at {create.center()}")
                        break
        if create is None:
            raise PhoneError(
                "Hide My Email list is open (or Settings is) but no Create New Address node is in the live dump. "
                f"Read {os.path.join(DUMP_DIR, '03_hme_before_create.json')} and retune CREATE_LABELS. "
                "Did not guess a tap. Unlock iCloud+ Hide My Email on this Apple ID if the row is missing."
            )
    if _forbidden(create.labels()):
        raise PhoneError(f"Refusing to tap forbidden control {create.labels()!r}.")
    if not stack.can_tap():
        raise PhoneError(
            f"Found Create control {create.labels()!r} at {create.center()} in the dump, "
            "but there is no tap backend (need TrollStore WebDriverAgent or AutoTouch/XXTouch/ZXTouch). "
            "Did not guess a coordinate tap."
        )

    cx, cy = create.center()
    log(f"Tapping Create from dump: {create.labels()!r} center=({cx},{cy})")
    stack.tap_xy(cx, cy)
    time.sleep(2.5)
    if cancelled():
        raise PhoneError("Stopped.")

    addr = ""
    confirms = 0
    for step in range(1, 6):
        if cancelled():
            raise PhoneError("Stopped.")
        blob, nodes = dump_named(stack, phone, f"04_hme_after_create_{step}", log)
        now = _icloud_emails(nodes)
        new = [e for e in now if e not in before]
        if new:
            addr = new[-1]
            log(f"New @icloud.com on screen: {addr}")
        elif now and not addr:
            addr = now[-1]
            log(f"Address on screen (may be the suggestion): {addr}")

        cont = find_label(nodes, CONTINUE_LABELS)
        if addr and (cont is None or confirms >= 2):
            break
        if cont and not _forbidden(cont.labels()) and stack.can_tap() and confirms < 2:
            x, y = cont.center()
            log(f"Tapping confirm from dump: {cont.labels()!r} ({x},{y})")
            stack.tap_xy(x, y)
            confirms += 1
            time.sleep(2.0)
            continue
        if addr:
            break
        raise PhoneError(
            "Create was tapped but no @icloud.com address is in the live dump. "
            "iCloud+ may be off, Apple ID signed out, or the sheet uses a label not parsed. "
            "Retune from dumps/04_hme_after_create_*. Did not open Reddit."
        )

    if not addr:
        raise PhoneError("Hide My Email ran but no @icloud.com address was captured.")
    if not EMAIL_RE.fullmatch(addr):
        raise PhoneError(f"Refusing non-icloud address {addr!r} (SIWA relay is not this slice).")

    blob, nodes = dump_named(stack, phone, "04_hme_before_label", log)
    nodes = _apply_icloud_label(stack, phone, log, nodes, label, stop)

    _save_address(addr, log, label=label)
    log(f"HIDE MY EMAIL: {addr}" + (f" label={label}" if label else ""))
    log("This slice does not open Reddit signup.")
    return addr
