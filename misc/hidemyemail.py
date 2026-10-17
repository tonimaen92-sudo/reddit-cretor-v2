# UTF-8
"""Create ONE iCloud Hide My Email address from Settings. Then stop. No Reddit signup."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable

from config import CONTINUE_LABELS, CREATE_LABELS, DUMP_DIR, FORBIDDEN_HME_LABELS, HME_SETTINGS_URL, LOG_DIR
from ssh_device import Phone, PhoneError, load_secrets
from ui_tree import TapStack, collapsed_text, dump_named, find_label, find_label_exact

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


def _icloud_password_sheet_controls(nodes):
    """Find only the observed, centered iCloud password sheet controls.

    XXTouch OCR has rendered the right button as Cyrillic ОК. Limit that
    confusable-label allowance to this sheet and require all four controls
    in their separate, bounded positions before entering a password.
    """
    title = find_label_exact(nodes, ("Sign in to iCloud",))
    field = find_label_exact(nodes, ("Password",))
    cancel = find_label_exact(nodes, ("Cancel",))
    oks = [
        n for n in nodes
        if n.ntype == "ocr" and n.visible and n.on_screen()
        and collapsed_text(n.labels()) in {"ok", "ок", "оk", "oк"}
    ]
    if not (title and field and cancel and len(oks) == 1):
        return None
    ok = oks[0]
    tx, ty = title.center()
    fx, fy = field.center()
    cx, cy = cancel.center()
    ox, oy = ok.center()
    if not (
        200 <= tx <= 550 and 250 <= ty <= 450
        and 100 <= fx <= 400 and 430 <= fy <= 550
        and 100 <= cx <= 350 and 530 <= cy <= 650
        and 420 <= ox <= 650 and 530 <= oy <= 650
        and ty < fy < cy and abs(cy - oy) <= 35
    ):
        return None
    return field, ok


def _unlock_icloud_if_needed(stack, phone, log, nodes, stop) -> list:
    """If Settings is sitting on the Apple ID password sheet, type the saved iCloud password."""
    joined = " ".join(n.labels().casefold() for n in nodes)
    if "sign in to icloud" not in joined:
        return nodes
    pwd = str(load_secrets().get("icloud_password") or "").strip()
    if not pwd:
        raise PhoneError("iCloud password sheet is on screen, but no saved iCloud password is available. Enter it on the phone; do not paste it in chat.")
    controls = _icloud_password_sheet_controls(nodes)
    if controls is None or not stack.can_tap():
        raise PhoneError("iCloud password sheet is visible, but its bounded Password/OK controls could not be verified. Did not type or tap.")
    field, ok = controls
    log("iCloud password sheet: tapping Password field, typing saved iCloud password, tapping OK.")
    stack.tap_xy(*field.center())
    time.sleep(0.6)
    stack.type_text(pwd)
    time.sleep(0.4)
    stack.tap_xy(*ok.center())
    time.sleep(3.0)
    if stop and stop.is_set():
        raise PhoneError("Stopped.")
    _blob, nodes = dump_named(stack, phone, "03b_hme_after_icloud_unlock", log)
    if "sign in to icloud" in " ".join(n.labels().casefold() for n in nodes):
        raise PhoneError("iCloud password sheet remained after one saved-password attempt. Did not retry the password or scroll behind the sheet.")
    return nodes


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
    nodes = _unlock_icloud_if_needed(stack, phone, log, nodes, stop)
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
            nodes = _unlock_icloud_if_needed(stack, phone, log, nodes, stop)
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
            nodes = _unlock_icloud_if_needed(stack, phone, log, nodes, stop)
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
                nodes = _unlock_icloud_if_needed(stack, phone, log, nodes, stop)
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
                    nodes = _unlock_icloud_if_needed(stack, phone, log, nodes, stop)
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
