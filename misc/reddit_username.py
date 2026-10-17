# UTF-8
"""Keep Reddit's username, with a bounded generated-suggestion fallback."""

from __future__ import annotations

import os
import re
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from slots import get_slot, set_slot_username, write_account_text  # noqa: E402
from ssh_device import Phone, PhoneError  # noqa: E402
from ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,28}$")
MAX_SUGGESTION_REFRESHES = 2
REFRESH_LABELS = (
    ("see", "more", "options"),
    ("see", "more", "usernames"),
    ("reload",),
)


def log(msg: str) -> None:
    print(msg, flush=True)


def suggested_username(nodes) -> str | None:
    """Return only the value beside the Username field, never inspiration rows."""
    field = find_label_exact(nodes, ("Username",))
    hits = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = n.labels().strip()
        token = t.split()[0] if t else ""
        if not token:
            continue
        if collapsed_text(token) in ("username", "continue", "skip"):
            continue
        if token.startswith("http"):
            continue
        # The actual field value is rendered below the floating Username
        # label.  iOS can leave a selection/preview bubble (for example a
        # truncated old value) above that label after repeated backspaces;
        # never treat that overlay as field content.
        field_delta = n.center()[1] - field.center()[1] if field else None
        if (
            field is not None
            and field_delta is not None
            and 5 < field_delta < 80
            and n is not field
        ):
            hits.append((n, token))
    if hits:
        hits.sort(key=lambda t: abs(t[0].y - field.y))
        token = hits[0][1]
        if USER_RE.match(token):
            return token
    return None


def _page_node(nodes, prefix: str):
    """Return a visible, non-merged OCR node whose normalized text has prefix."""
    prefix = prefix.casefold()
    for node in nodes:
        if node.ntype == "ocr-line" or not node.visible or not node.on_screen():
            continue
        if collapsed_text(node.labels()).startswith(prefix):
            return node
    return None


def _refresh_words(node) -> tuple[str, ...]:
    words = tuple(collapsed_text(node.labels()).split())
    for label in REFRESH_LABELS:
        if words == label:
            return label
        if len(words) == len(label) + 1 and len(words[0]) == 1 and words[1:] == label:
            return label
    return ()


def suggestion_refresh(nodes):
    """Find Reddit's page-local refresh button, allowing one OCR icon glyph."""
    page = find_label_exact(nodes, ("Create your username",))
    inspiration = _page_node(nodes, "need inspiration?")
    cont = find_label_exact(nodes, ("Continue",))
    if page is None or inspiration is None or cont is None:
        return None

    top = inspiration.center()[1]
    bottom = cont.center()[1]
    for n in nodes:
        if n.ntype == "ocr-line" or not n.visible or not n.on_screen():
            continue
        if _refresh_words(n) and top < n.center()[1] < bottom:
            return n
    return None


def generated_username_choices(nodes) -> list[tuple[object, str]]:
    """Return exactly 3-4 unique username rows from the inspiration region."""
    if find_label_exact(nodes, ("Create your username",)) is None:
        return []
    inspiration = _page_node(nodes, "need inspiration?")
    cont = find_label_exact(nodes, ("Continue",))
    if inspiration is None or cont is None:
        return []
    refresh = suggestion_refresh(nodes)
    bottom = (refresh or cont).center()[1]
    top = inspiration.center()[1]
    choices: list[tuple[object, str]] = []
    seen: set[str] = set()
    for node in nodes:
        if node.ntype == "ocr-line" or not node.visible or not node.on_screen():
            continue
        if not top < node.center()[1] < bottom:
            continue
        token = next(
            (
                str(value).strip()
                for value in (node.name, node.label, node.value, node.text)
                if str(value).strip()
            ),
            "",
        )
        folded = token.casefold()
        if not USER_RE.fullmatch(token) or folded in seen:
            continue
        seen.add(folded)
        choices.append((node, token))
    choices.sort(key=lambda item: (item[0].center()[1], item[0].center()[0]))
    return choices if len(choices) in (3, 4) else []


def _choose_generated_username(choices: list[tuple[object, str]]) -> tuple[object, str]:
    return secrets.choice(choices)


def _load_suggested_username(
    stack: TapStack,
    phone: Phone,
    nodes,
) -> tuple[str | None, list, bool]:
    """Load a field value or refresh and select a proven generated username."""
    name = suggested_username(nodes)
    if name:
        return name, nodes, False

    for attempt in range(1, MAX_SUGGESTION_REFRESHES + 1):
        refresh = suggestion_refresh(nodes)
        if refresh is None:
            break
        log(f"Username field empty -> refresh suggestions {attempt}/{MAX_SUGGESTION_REFRESHES} "
            f"[{refresh.x},{refresh.y} {refresh.w}x{refresh.h}]")
        stack.tap_xy(*refresh.center())
        time.sleep(2.0)
        _, nodes = dump_named(
            stack,
            phone,
            f"890_username_refresh_{attempt}",
            log,
            shot=True,
        )
        choices = generated_username_choices(nodes)
        if not choices:
            continue
        choice, name = _choose_generated_username(choices)
        log(
            f"Username field empty -> choose Reddit suggestion {attempt}/"
            f"{MAX_SUGGESTION_REFRESHES} [{choice.x},{choice.y} {choice.w}x{choice.h}]"
        )
        stack.tap_xy(*choice.center())
        time.sleep(1.0)
        _, nodes = dump_named(
            stack,
            phone,
            f"890_username_choice_{attempt}",
            log,
            shot=True,
        )
        if find_label_exact(nodes, ("Create your username",)) is None:
            return None, nodes, False
        if find_label_exact(nodes, ("Continue",)) is None:
            return None, nodes, False
        selected_name = suggested_username(nodes)
        if selected_name == name:
            return name, nodes, True
        if selected_name:
            raise PhoneError(
                "Reddit populated a different username after the generated suggestion tap. "
                "Did not type over it or tap Continue."
            )

        # Some Reddit builds render the generated-name chips but ignore their
        # tap. Use the exact Reddit-generated value we already proved, then
        # require a fresh field-value proof before Continue can be reached.
        field = find_label_exact(nodes, ("Username",))
        if field is None:
            raise PhoneError(
                "Username field disappeared after selecting a generated suggestion. "
                "Did not tap Continue."
            )
        log(
            f"Generated username chip did not fill the field -> type the same "
            f"Reddit suggestion [{field.x},{field.y} {field.w}x{field.h}]"
        )
        stack.tap_xy(*field.center())
        stack.type_text(name)
        time.sleep(1.0)
        _, nodes = dump_named(
            stack,
            phone,
            f"890_username_typed_{attempt}",
            log,
            shot=True,
        )
        if suggested_username(nodes) != name:
            raise PhoneError(
                "Reddit did not confirm the selected generated username in the Username "
                "field after one typing attempt. Did not tap Continue or save the username."
            )
        return name, nodes, True

    return None, nodes, False


def load_suggested_username(stack: TapStack, phone: Phone, nodes) -> tuple[str | None, list]:
    """Public compatibility wrapper for the bounded username loader."""
    name, fresh_nodes, _selected = _load_suggested_username(stack, phone, nodes)
    return name, fresh_nodes


def _continue_node(nodes):
    cont = find_label_exact(nodes, ("Continue",))
    if cont is None:
        raise PhoneError("Continue not on screen.")
    if "apple" in collapsed_text(cont.labels()):
        raise PhoneError("Refusing Continue with Apple.")
    return cont


def _username_keyboard_open(nodes) -> bool:
    """Require the three stable lower keyboard anchors on this Reddit page."""
    anchors = [find_label_exact(nodes, (label,)) for label in ("123", "space", "return")]
    return all(node is not None and node.center()[1] >= 1050 for node in anchors)


def _record_username(sid: str, email: str, name: str) -> None:
    log(f"KEEP username={name}")
    set_slot_username(sid, name, log)
    path = write_account_text(sid, email, name)
    log("WROTE " + path)


def keep_suggested_username(stack: TapStack, phone: Phone, sid: str) -> str:
    slot = get_slot(sid)
    email = str((slot or {}).get("email") or "").strip()
    if not email:
        raise PhoneError(f"{sid} has no email")
    blob, nodes = dump_named(stack, phone, "890_username", log, shot=True)
    if find_label_exact(nodes, ("Create your username",)) is None:
        raise PhoneError("Not on Create your username. See 890_username.png")
    name, nodes, selected_fallback = _load_suggested_username(stack, phone, nodes)
    if not name:
        raise PhoneError(
            "Reddit did not load a value in the Username field after bounded "
            "suggestion refreshes. Did not choose a generated username or tap Continue."
        )
    if not selected_fallback:
        # Preserve the established happy-path ordering and behavior.
        _record_username(sid, email, name)
    cont = _continue_node(nodes)
    log(f"Continue [{cont.x},{cont.y} {cont.w}x{cont.h}]")
    stack.tap_xy(*cont.center())
    time.sleep(2.0)
    _, after_nodes = dump_named(stack, phone, "891_after_username", log, shot=True)
    if selected_fallback:
        if find_label_exact(after_nodes, ("Set a password",)) is None:
            raise PhoneError(
                "Reddit did not advance to Set a password after selecting a generated "
                "username. Username was not saved."
            )
        _record_username(sid, email, name)
    return name


def use_requested_username(
    stack: TapStack, phone: Phone, sid: str, requested: str,
) -> str:
    """Use exactly the batch name; never replace an unavailable one silently."""
    requested = str(requested or "").strip()
    if USER_RE.fullmatch(requested) is None:
        raise PhoneError("Requested batch username has an invalid format.")
    slot = get_slot(sid)
    email = str((slot or {}).get("email") or "").strip()
    saved = str((slot or {}).get("username") or "").strip()
    if not email:
        raise PhoneError(f"{sid} has no email")
    if saved and saved != requested:
        raise PhoneError(f"{sid} already has a different saved username. Did not overwrite it.")
    _, nodes = dump_named(stack, phone, "890_username", log, shot=True)
    title = find_label_exact(nodes, ("Create your username",))
    field = find_label_exact(nodes, ("Username",))
    cont = find_label_exact(nodes, ("Continue",))
    keyboard_open = _username_keyboard_open(nodes)
    if not (title and field and cont and
            150 <= title.center()[1] <= 260 and
            350 <= field.center()[1] <= 470 and
            (
                1100 <= cont.center()[1] <= 1300
                or (keyboard_open and 650 <= cont.center()[1] <= 800)
            )):
        raise PhoneError("Requested username screen controls are unproven. Did not type or tap Continue.")
    current = suggested_username(nodes)
    if current != requested:
        # On this proven Reddit layout, the username field fills x40..720 and
        # its value sits immediately beneath the Username label. Tapping the
        # right side positions the cursor after the current value.
        stack.tap_xy(650, field.center()[1] + 32)
        if current:
            stack.press_backspace(40)
            time.sleep(0.5)
            _, cleared = dump_named(stack, phone, "890_username_cleared", log, shot=True)
            if find_label_exact(cleared, ("Create your username",)) is None or suggested_username(cleared):
                raise PhoneError("Requested username field did not clear; no name was typed or saved.")
        stack.type_text(requested)
        time.sleep(1.2)
        _, nodes = dump_named(stack, phone, "890_username_requested", log, shot=True)
    if suggested_username(nodes) != requested:
        raise PhoneError("Reddit did not show the exact requested username in the field. Did not continue.")
    if find_label_exact(nodes, ("Great name! It's not taken, so it's all yours.",)) is None:
        raise PhoneError(
            f"Reddit has not confirmed requested username {requested!r} is available. "
            "Did not substitute a suggestion or tap Continue."
        )
    cont = _continue_node(nodes)
    log(f"Requested username {requested!r} verified available; Continue [{cont.x},{cont.y} {cont.w}x{cont.h}]")
    stack.tap_xy(*cont.center())
    time.sleep(2.0)
    _, after_nodes = dump_named(stack, phone, "891_after_username", log, shot=True)
    if find_label_exact(after_nodes, ("Set a password",)) is None:
        raise PhoneError(
            "Reddit did not advance to Set a password after the requested username. "
            "Username was not saved."
        )
    _record_username(sid, email, requested)
    return requested


def main() -> int:
    sid = (sys.argv[1] if len(sys.argv) > 1 else "a0001").strip().lower()
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        keep_suggested_username(stack, phone, sid)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
