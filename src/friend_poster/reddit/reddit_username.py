# UTF-8
"""Keep Reddit's username, with a bounded generated-suggestion fallback."""

from __future__ import annotations

import io
import os
import re
import secrets
import sys
import time
from typing import Callable
from friend_poster.core.slots import (  # noqa: E402
    CREATION_PROGRESS,
    clear_username_variant_attempt,
    get_slot,
    mark_username_variant_attempt,
    set_slot_username,
    username_variant_attempt,
    write_account_text,
)
from friend_poster.core.username_lifecycle import (  # noqa: E402
    UsernameLedgerError,
    claim_username,
    username_owners,
)
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,28}$")
MAX_SUGGESTION_REFRESHES = 2
USERNAME_AVAILABILITY_POLLS = 4
USERNAME_TRANSITION_POLLS = 8
USERNAME_TYPED_VALUE_POLLS = 2
USERNAME_RETYPE_ATTEMPTS = 1
USERNAME_STALL_POLL_SECONDS = 5.0
USERNAME_STALL_SAFETY_SECONDS = 195.0
REFRESH_LABELS = (
    ("see", "more", "options"),
    ("see", "more", "usernames"),
    ("reload",),
)


class UsernameRecoveryError(PhoneError):
    """A deterministic username result that must never invoke AI recovery."""

    ai_recoverable = False


class RequestedUsernameTaken(UsernameRecoveryError):
    """Reddit explicitly rejected the exact value currently in the field."""


class UsernameAvailabilityUnclear(UsernameRecoveryError):
    """Reddit did not prove either availability or an explicit rejection."""


class UsernameVariantSpaceExhausted(UsernameRecoveryError):
    """No additional valid deterministic candidate can be produced."""


class UsernameVariantPersistenceError(UsernameRecoveryError):
    """The durable candidate or queue checkpoint could not be saved."""


def log(msg: str) -> None:
    print(msg, flush=True)


def _cancelable_wait(stack: TapStack, seconds: float) -> None:
    """Wait through TapStack so Stop and the inactivity watchdog can interrupt."""
    waiter = getattr(stack, "wait", None)
    if callable(waiter):
        waiter(seconds)
    else:
        time.sleep(seconds)


def requested_username_available(nodes) -> bool:
    """Recognize Reddit's positive availability sentence despite punctuation OCR."""
    text = " ".join(
        collapsed_text(node.labels())
        for node in nodes
        if node.on_screen() and node.labels()
    )
    return "great name" in text and "not taken" in text and "all yours" in text


def requested_username_taken(nodes) -> bool:
    """Recognize Reddit's explicit rejection; absence is never treated as taken."""
    text = " ".join(
        collapsed_text(node.labels())
        for node in nodes
        if node.on_screen() and node.labels()
    )
    return "username already taken" in text and "try another" in text


_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(value: int) -> str:
    if value < 1:
        raise ValueError("base36 value must be positive")
    encoded = ""
    while value:
        value, remainder = divmod(value, len(_BASE36))
        encoded = _BASE36[remainder] + encoded
    return encoded


def requested_username_variant(base: str, index: int) -> str:
    """Return a stable, unique nearby candidate for a zero-based index.

    Existing digit positions are enumerated first, then one-letter changes,
    then an alphanumeric suffix.  The first two phases preserve exact length;
    the suffix never exceeds Reddit's 29-character limit.
    """
    base = str(base or "").strip()
    if USER_RE.fullmatch(base) is None or type(index) is not int or index < 0:
        raise UsernameVariantSpaceExhausted(
            "Requested username variant state is invalid; preserved the attempt."
        )
    if index == 0:
        return base

    digit_positions = [offset for offset, char in enumerate(base) if char.isdigit()]
    digit_count = (10 ** len(digit_positions) - 1) if digit_positions else 0
    if index <= digit_count:
        original = int("".join(base[pos] for pos in digit_positions))
        replacement = str((original + index) % (10 ** len(digit_positions))).zfill(
            len(digit_positions)
        )
        chars = list(base)
        for pos, value in zip(digit_positions, replacement):
            chars[pos] = value
        return "".join(chars)

    remaining = index - digit_count - 1
    letter_positions = [offset for offset, char in enumerate(base) if char.isalpha()]
    letter_count = len(letter_positions) * 25
    if remaining < letter_count:
        position = letter_positions[remaining // 25]
        shift = remaining % 25 + 1
        chars = list(base)
        original = chars[position]
        alphabet_start = ord("A" if original.isupper() else "a")
        chars[position] = chr(
            alphabet_start + ((ord(original) - alphabet_start + shift) % 26)
        )
        return "".join(chars)

    suffix_ordinal = remaining - letter_count + 1
    suffix = _base36(suffix_ordinal)
    if len(base) + len(suffix) > 29:
        raise UsernameVariantSpaceExhausted(
            "Every deterministic username variant within Reddit's length limit "
            "was exhausted; preserved the container, mail and queue item."
        )
    candidate = base + suffix
    if USER_RE.fullmatch(candidate) is None:
        raise UsernameVariantSpaceExhausted(
            "Could not produce another valid Reddit username; preserved the attempt."
        )
    return candidate


def username_availability_visual(png: bytes | None) -> bool:
    """Prove the green field check and green availability sentence visually."""
    if not png:
        return False
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as source:
            image = source.convert("RGB")
            width, height = image.size
            if width < 200 or height < 300:
                return False
            check = image.crop(
                (
                    int(width * 0.68),
                    int(height * 0.25),
                    int(width * 0.90),
                    int(height * 0.34),
                )
            )
            sentence = image.crop(
                (
                    int(width * 0.04),
                    int(height * 0.32),
                    int(width * 0.96),
                    int(height * 0.40),
                )
            )

            def green_pixels(region) -> int:
                return sum(
                    count
                    for count, (red, green, blue) in region.getcolors(
                        maxcolors=region.width * region.height
                    ) or []
                    if green > 75 and green > red * 1.35 and green > blue * 1.25
                )

            return green_pixels(check) >= 20 and green_pixels(sentence) >= 400
    except Exception:
        return False


def _prove_requested_username_available(
    stack: TapStack,
    phone: Phone,
    nodes,
    requested: str,
    lg=log,
):
    """Boundedly re-read availability, with screenshot proof when OCR omits it."""
    last = nodes
    for attempt in range(USERNAME_AVAILABILITY_POLLS + 1):
        field = find_label_exact(last, ("Username",))
        if field is None or not _username_field_matches_candidate(
            last, field, requested
        ):
            raise UsernameAvailabilityUnclear(
                "Reddit changed the requested username while checking availability. "
                "Did not tap Continue."
            )
        if requested_username_taken(last):
            raise RequestedUsernameTaken(
                "Reddit explicitly reported that the requested username is already "
                "taken; no Continue tap was made."
            )
        if requested_username_available(last):
            return last
        if username_availability_visual(stack.screenshot_png()):
            lg(
                f"Requested username {requested!r} availability proven by the green "
                "field check and confirmation sentence."
            )
            return last
        if attempt >= USERNAME_AVAILABILITY_POLLS:
            break
        _cancelable_wait(stack, 0.8)
        _blob, last = dump_named(
            stack,
            phone,
            f"890_username_availability_{attempt + 1}",
            lg,
            shot=True,
            preview=False,
        )
    raise UsernameAvailabilityUnclear(
        "Reddit has not confirmed username availability or an explicit "
        "already-taken result after bounded rechecks. Did not tap Continue."
    )


def _set_password_screen(nodes) -> bool:
    return find_label_exact(nodes, ("Set a password",)) is not None


def _username_screen(nodes) -> bool:
    return find_label_exact(nodes, ("Create your username",)) is not None


def _wait_after_requested_username(
    stack: TapStack,
    phone: Phone,
    nodes,
    requested: str,
    lg=log,
):
    """Wait, retry username Continue once, then yield to the 180s watchdog."""
    last = nodes
    for attempt in range(USERNAME_TRANSITION_POLLS):
        if _set_password_screen(last):
            return last
        if not _username_screen(last):
            raise PhoneError(
                "Reddit left Create your username but did not reach Set a password."
            )
        _cancelable_wait(stack, 0.8)
        _blob, last = dump_named(
            stack,
            phone,
            f"891_username_transition_{attempt}",
            lg,
            shot=True,
            preview=False,
        )
    if _set_password_screen(last):
        return last
    if not _username_screen(last):
        raise PhoneError(
            "Reddit left Create your username but did not reach Set a password."
        )

    last = _prove_requested_username_available(stack, phone, last, requested, lg)
    cont = _continue_node(last)
    lg(
        "Username Continue did not advance; retrying the exact enabled Continue once "
        f"[{cont.x},{cont.y} {cont.w}x{cont.h}]."
    )
    stack.tap_xy(*cont.center())
    CREATION_PROGRESS.note()

    # Device reads normally report liveness. After the one allowed retry they
    # are only state probes, not workflow progress, so suppress that hook while
    # polling; otherwise the 180-second inactivity watchdog could never fire.
    had_progress_hook = hasattr(phone, "progress_note")
    progress_hook = getattr(phone, "progress_note", None)
    if had_progress_hook:
        phone.progress_note = None
    deadline = time.monotonic() + USERNAME_STALL_SAFETY_SECONDS
    poll = 0
    announced_stall = False
    try:
        while True:
            _cancelable_wait(stack, 2.0 if poll == 0 else USERNAME_STALL_POLL_SECONDS)
            _blob, last = dump_named(
                stack,
                phone,
                f"892_username_retry_stall_{poll}",
                lg,
                shot=True,
                preview=False,
            )
            if _set_password_screen(last):
                return last
            if not _username_screen(last):
                raise PhoneError(
                    "Reddit left Create your username after the Continue retry but "
                    "did not reach Set a password."
                )
            if not announced_stall:
                lg(
                    "Username Continue retry did not advance; waiting without progress "
                    "updates so the 180-second inactivity watchdog can reset the attempt."
                )
                announced_stall = True
            if time.monotonic() >= deadline:
                raise PhoneError(
                    "Username Continue retry remained stalled, but the inactivity "
                    "watchdog did not cancel within its safety window."
                )
            poll += 1
    finally:
        if had_progress_hook:
            phone.progress_note = progress_hook


_USERNAME_OCR_TRANSLATION = str.maketrans(
    {
        # Apple's rendered lowercase x is occasionally exposed by XXTouch OCR
        # as a multiplication/cross glyph. Reddit usernames cannot contain
        # these glyphs, so normalizing them is unambiguous.
        "×": "x",
        "✕": "x",
        "✖": "x",
    }
)


def _username_token_below_field(nodes, field) -> str | None:
    """Return any rendered token directly below *field*, even if OCR-corrupt."""
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
        return hits[0][1]
    return None


def _username_value_below_field(nodes, field) -> str | None:
    """Return a normalized username-shaped value rendered below *field*."""
    token = _username_token_below_field(nodes, field)
    if token is not None:
        normalized = token.translate(_USERNAME_OCR_TRANSLATION)
        if USER_RE.fullmatch(normalized):
            return normalized
    return None


def _username_field_matches_candidate(nodes, field, candidate: str) -> bool:
    """Match a known candidate through one proven XXTouch OCR ambiguity.

    XXTouch can render a lowercase ``l`` in Reddit's username field as an
    uppercase ``I``.  This remains expected-value-aware: every other position
    must be exact after the existing unambiguous x-glyph translation.
    """
    token = _username_token_below_field(nodes, field)
    if token is None:
        return False
    observed = token.translate(_USERNAME_OCR_TRANSLATION)
    if USER_RE.fullmatch(observed) is None or len(observed) != len(candidate):
        return False
    return all(
        actual == expected or (expected == "l" and actual == "I")
        for expected, actual in zip(candidate, observed)
    )


def suggested_username(nodes) -> str | None:
    """Return only the value beside the exact Username field, never inspiration rows."""
    field = find_label_exact(nodes, ("Username",))
    return _username_value_below_field(nodes, field)


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
    _claim_username(name, sid)
    log(f"KEEP username={name}")
    set_slot_username(sid, name, log)
    path = write_account_text(sid, email, name)
    log("WROTE " + path)


def _claim_username(name: str, sid: str) -> None:
    """Fail closed before Reddit advances if durable ownership cannot be saved."""
    try:
        claim_username(name, sid)
    except UsernameLedgerError as exc:
        raise PhoneError(
            "Could not reserve the Reddit username in durable processing state; "
            "did not tap Continue."
        ) from exc


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
    if selected_fallback:
        # The selected/generated value is now positively visible in Reddit's
        # field. Reserve it before Continue, including the path where the chip
        # tap was ignored and we typed that exact proven suggestion ourselves.
        _claim_username(name, sid)
        set_slot_username(sid, name, log)
    else:
        # Preserve the established pre-filled happy-path ordering and behavior.
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
                "username. Its processing claim was preserved; account text was not written."
            )
        path = write_account_text(sid, email, name)
        log("WROTE " + path)
    return name


def _requested_username_controls(nodes):
    title = find_label_exact(nodes, ("Create your username",))
    field = find_label_exact(nodes, ("Username",))
    cont = find_label_exact(nodes, ("Continue",))
    keyboard_open = _username_keyboard_open(nodes)
    inspiration = _page_node(nodes, "need inspiration?")

    # A transient in-app notification can cover only the page title after the
    # requested username has been typed. Accept that narrowly proven state:
    # the field, inspiration copy, keyboard and Continue geometry must still
    # match, and Reddit must explicitly say the name is available.
    if (
        title is None
        and field is not None
        and cont is not None
        and inspiration is not None
        and keyboard_open
        and requested_username_available(nodes)
        and 350 <= field.center()[1] <= 470
        and 480 <= inspiration.center()[1] <= 650
        and 650 <= cont.center()[1] <= 800
    ):
        return field
    if field is None and title and cont and inspiration and keyboard_open:
        clipped = [
            node
            for node in nodes
            if node.visible
            and node.on_screen()
            and collapsed_text(node.labels()) in {"sername", "bername"}
            and 350 <= node.center()[1] <= 470
        ]
        if (
            len(clipped) == 1
            and 150 <= title.center()[1] <= 260
            and 480 <= inspiration.center()[1] <= 650
            and 650 <= cont.center()[1] <= 800
        ):
            field = clipped[0]
    if not (
        title
        and field
        and cont
        and 150 <= title.center()[1] <= 260
        and 350 <= field.center()[1] <= 470
        and (
            1100 <= cont.center()[1] <= 1300
            or (keyboard_open and 650 <= cont.center()[1] <= 800)
        )
    ):
        raise UsernameAvailabilityUnclear(
            "Requested username screen controls are unproven. Did not type or "
            "tap Continue."
        )
    return field


def _requested_username_empty_after_clear(nodes) -> bool:
    """Prove the focused Reddit username field is empty after local clearing.

    A transient iOS/Reddit notification banner can cover the page title while the
    field and keyboard remain fully visible.  The pre-clear frame has already
    proven the complete page, so the post-clear check may use the remaining
    page-local anchors instead of requiring the temporarily occluded title.
    """
    title = find_label_exact(nodes, ("Create your username",))
    if title is not None:
        exact_field = find_label_exact(nodes, ("Username",))
        try:
            field = _requested_username_controls(nodes)
        except UsernameAvailabilityUnclear:
            return False
        if exact_field is not None:
            return _username_token_below_field(nodes, exact_field) is None
        # A clipped anchor is accepted only by _requested_username_controls()
        # with the full fixed page/keyboard geometry.  Since suggested_username
        # intentionally needs the exact label, inspect the proven anchor here.
        return _username_token_below_field(nodes, field) is None

    field = find_label_exact(nodes, ("Username",))
    cont = find_label_exact(nodes, ("Continue",))
    inspiration = _page_node(nodes, "need inspiration?")
    if not (
        field
        and cont
        and inspiration
        and _username_keyboard_open(nodes)
        and 350 <= field.center()[1] <= 470
        and 480 <= inspiration.center()[1] <= 650
        and 650 <= cont.center()[1] <= 800
    ):
        return False
    return _username_token_below_field(nodes, field) is None


def _type_requested_candidate(
    stack: TapStack,
    phone: Phone,
    nodes,
    candidate: str,
    index: int,
):
    """Replace the proven field value with one durable local candidate."""
    base_stem = (
        "890_username_requested"
        if index == 0
        else f"890_username_variant_{index}"
    )
    current_nodes = nodes
    for typing_attempt in range(USERNAME_RETYPE_ATTEMPTS + 1):
        field = _requested_username_controls(current_nodes)
        current = _username_value_below_field(current_nodes, field)
        if _username_field_matches_candidate(current_nodes, field, candidate):
            return current_nodes

        # On this proven Reddit layout, the username value is directly beneath
        # the floating label. Tapping the right side positions the cursor at the
        # end. A retry is allowed only after the old value is proven absent.
        stack.tap_xy(650, field.center()[1] + 32)
        if current:
            stack.press_backspace(40)
            _cancelable_wait(stack, 0.5)
            cleared_stem = (
                "890_username_cleared"
                if typing_attempt == 0 and index == 0
                else (
                    f"890_username_variant_{index}_cleared"
                    if typing_attempt == 0
                    else f"{base_stem}_retype_{typing_attempt}_cleared"
                )
            )
            _, cleared = dump_named(
                stack,
                phone,
                cleared_stem,
                log,
                shot=True,
            )
            if not _requested_username_empty_after_clear(cleared):
                raise UsernameAvailabilityUnclear(
                    "Requested username field did not clear; no candidate was typed "
                    "or saved."
                )
        elif not _requested_username_empty_after_clear(current_nodes):
            raise UsernameAvailabilityUnclear(
                "Requested username field state was unclear; no candidate was typed "
                "or saved."
            )

        stack.type_text(candidate)
        _cancelable_wait(stack, 1.2)
        typed_stem = (
            base_stem
            if typing_attempt == 0
            else f"{base_stem}_retype_{typing_attempt}"
        )
        _, fresh = dump_named(
            stack,
            phone,
            typed_stem,
            log,
            shot=True,
        )
        for poll in range(USERNAME_TYPED_VALUE_POLLS + 1):
            fresh_field = _requested_username_controls(fresh)
            if _username_field_matches_candidate(fresh, fresh_field, candidate):
                return fresh
            if poll >= USERNAME_TYPED_VALUE_POLLS:
                break
            _cancelable_wait(stack, 0.8)
            _, fresh = dump_named(
                stack,
                phone,
                f"{typed_stem}_verify_{poll + 1}",
                log,
                shot=True,
                preview=False,
            )

        if typing_attempt < USERNAME_RETYPE_ATTEMPTS:
            log(
                "Requested username field verification stayed mismatched after "
                "fresh reads; clearing and retyping the same durable candidate "
                "once (value redacted)."
            )
            current_nodes = fresh

    raise UsernameAvailabilityUnclear(
        "Reddit did not show the exact local username candidate after bounded "
        "fresh reads and one safe retype. Did not continue."
    )


def use_requested_username(
    stack: TapStack,
    phone: Phone,
    sid: str,
    requested: str,
    *,
    username_committer: Callable[[str, str], None] | None = None,
    username_candidate_allowed: Callable[[str], bool] | None = None,
) -> str:
    """Use the requested name or a deterministic close local variant.

    A new candidate is generated only after Reddit explicitly says the current
    one is taken.  Missing or ambiguous status pauses without AI or Continue.
    """
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

    try:
        checkpoint = username_variant_attempt(slot)
    except ValueError as exc:
        raise UsernameVariantPersistenceError(
            "Username candidate checkpoint is malformed; preserved the attempt."
        ) from exc
    base = requested
    candidate = requested
    candidate_index = 0
    if checkpoint is not None:
        checkpoint_base = str(checkpoint.get("base") or "")
        checkpoint_candidate = str(checkpoint.get("candidate") or "")
        if checkpoint_base != requested and checkpoint_candidate != requested:
            raise UsernameVariantPersistenceError(
                "Username candidate checkpoint belongs to another queue value; "
                "preserved the attempt."
            )
        base = checkpoint_base
        candidate = checkpoint_candidate
        candidate_index = int(checkpoint.get("index") or 0)

    _, nodes = dump_named(stack, phone, "890_username", log, shot=True)
    _requested_username_controls(nodes)

    while True:
        if (
            username_candidate_allowed is not None
            and not username_candidate_allowed(candidate)
        ):
            log(
                f"Requested username candidate {candidate_index} is reserved by "
                "the durable batch; skipping it without typing (value redacted)."
            )
            candidate_index += 1
            candidate = requested_username_variant(base, candidate_index)
            continue

        try:
            mark_username_variant_attempt(
                sid, base, candidate, candidate_index, log
            )
        except (OSError, TypeError, ValueError) as exc:
            raise UsernameVariantPersistenceError(
                "Could not save the username candidate checkpoint; did not type "
                "or continue."
            ) from exc

        try:
            owners = username_owners()
        except UsernameLedgerError as exc:
            raise UsernameVariantPersistenceError(
                "Could not read local username ownership; preserved the attempt."
            ) from exc
        owner = owners.get(candidate.casefold())
        if owner is not None and str(owner.get("slot_id") or "") != sid:
            log(
                f"Requested username candidate {candidate_index} is already "
                "reserved locally; skipping it without typing (value redacted)."
            )
            candidate_index += 1
            candidate = requested_username_variant(base, candidate_index)
            continue

        nodes = _type_requested_candidate(
            stack, phone, nodes, candidate, candidate_index
        )
        try:
            nodes = _prove_requested_username_available(
                stack, phone, nodes, candidate, log
            )
            break
        except RequestedUsernameTaken:
            log(
                f"Reddit rejected username candidate {candidate_index}; trying "
                "the next deterministic local variant (values redacted)."
            )
            candidate_index += 1
            candidate = requested_username_variant(base, candidate_index)

    # The queue mutation precedes the ownership claim and Continue boundary.
    # A crash can therefore only resume the same accepted candidate, never the
    # original rejected value with a partially-created account.
    if candidate != requested:
        if username_committer is None:
            raise UsernameVariantPersistenceError(
                "A username variant was accepted, but no durable queue committer "
                "is available; did not claim it or tap Continue."
            )
        try:
            username_committer(requested, candidate)
        except Exception as exc:
            raise UsernameVariantPersistenceError(
                "The accepted username variant could not be saved to the queue; "
                "did not claim it or tap Continue."
            ) from exc

    # The availability proof is the irreversible ownership boundary. Persist
    # the same-slot processing claim before Continue, so a crash or stopped
    # batch can never hand this exact name to another slot.
    _claim_username(candidate, sid)
    set_slot_username(sid, candidate, log)
    try:
        clear_username_variant_attempt(sid, log)
    except (OSError, TypeError, ValueError) as exc:
        raise UsernameVariantPersistenceError(
            "Accepted username was saved, but its candidate checkpoint could not "
            "be cleared; did not tap Continue."
        ) from exc
    cont = _continue_node(nodes)
    log(
        f"Requested username candidate {candidate_index} verified available; "
        f"Continue [{cont.x},{cont.y} {cont.w}x{cont.h}] (value redacted)"
    )
    stack.tap_xy(*cont.center())
    CREATION_PROGRESS.note()
    _cancelable_wait(stack, 2.0)
    _, after_nodes = dump_named(stack, phone, "891_after_username", log, shot=True)
    _wait_after_requested_username(stack, phone, after_nodes, candidate, log)
    # The claim and slot ownership were persisted before Continue.  Only the
    # existing account text output is added after Reddit proves navigation.
    path = write_account_text(sid, email, candidate)
    log("WROTE " + path)
    return candidate


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
