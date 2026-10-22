# UTF-8
"""Open r1 Reddit, signup with that slot's Hide My Email, stop on Verify email.

No SIWA. No Skip. No uiopen Reddit. Dump-first.

  python misc/reddit_to_verify.py r1
"""

from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from friend_poster.core.config import REDDIT_BUNDLE  # noqa: E402
from friend_poster.reddit.reddit_open import open_container, proven_springboard, warn_siwa  # noqa: E402
from friend_poster.reddit.reddit_interests import proven_home_feed  # noqa: E402
from friend_poster.core.slots import CREATION_PROGRESS, get_slot  # noqa: E402
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import (  # noqa: E402
    Node,
    OperationCancelled,
    TapStack,
    collapsed_text,
    dump_named,
    find_label_exact,
)

os.environ["SKIP_SHOT"] = "1"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@icloud\.com", re.I)
EMAIL_PILL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-@])([A-Za-z0-9._%+\-]+@icloud\.com)"
    r"(?![A-Za-z0-9._%+\-@])",
    re.I,
)
GET_STARTED = ("Get Started",)
INITIAL_ACCOUNT = ("I already have an account",)
CONTINUE_WITH_EMAIL = ("Continue with email",)
PROVIDER_MARKERS = (
    "Continue with email",
    "Continue with Apple",
    "Continue with Google",
)
SIGNUP_CUSTOMIZE_TITLE = "Sign up to customize Reddit"
SIGNUP_CUSTOMIZE_BODY_1 = "Sign up to customize your home feed, upvote"
SIGNUP_CUSTOMIZE_BODY_2 = "content, and start joining conversations."
SIGNUP_CUSTOMIZE_ACTION = "Sign up / Log in"
SIGNUP_CUSTOMIZE_SETTINGS = "Settings"
CREATE_ACCOUNT = ("Create a new account", "Create new account", "Create a New Account")
PROFILE_MARKERS = ("Edit profile", "My profile")
PROFILE_TABS = ("Overview", "Posts", "Comments", "Saved")
EMAIL_TRANSITION_MAX_DUMPS = 8
SORRY_STOP_MESSAGE = (
    "Reddit rejected the email submission. No automatic retry was attempted; "
    "the slot and phone screen were preserved. Retry manually later."
)


class EmailFieldInvalid(PhoneError):
    """Reddit's email field never accepted the exact saved @icloud.com address.

    Raised only for the terminal 873 condition: after the one authorized type
    and at most one proven single-missing-character retype, the field still
    does not hold the saved address, so no further type, retype, or Continue
    tap is allowed.  ``failure_code`` is the machine-readable identity consumed
    by the Creator batch pipeline; it is never derived from message text.
    """

    failure_code = "reddit_email_field_invalid"


def log(msg: str) -> None:
    print(msg, flush=True)


def _check_stop(stop) -> None:
    is_set = getattr(stop, "is_set", None)
    if callable(is_set) and is_set():
        raise OperationCancelled("Stopped by user.")


def _wait(stop, seconds: float) -> None:
    _check_stop(stop)
    waiter = getattr(stop, "wait", None)
    if callable(waiter):
        if waiter(max(0.0, float(seconds))):
            raise OperationCancelled("Stopped by user.")
    else:
        time.sleep(max(0.0, float(seconds)))
    _check_stop(stop)


def blob_of(nodes) -> str:
    return " ".join(collapsed_text(n.labels()) for n in nodes if n.on_screen() and n.labels())


def emails_on(nodes) -> list[str]:
    found: list[str] = []
    for m in EMAIL_RE.findall(blob_of(nodes)):
        a = m.lower()
        if a not in found:
            found.append(a)
    return found


def tap_named(stack: TapStack, nodes, names: tuple[str, ...], why: str, *, stop=None):
    n = find_label_exact(nodes, names)
    if n is None:
        raise PhoneError(f"{why} not on screen.")
    low = collapsed_text(n.labels())
    if "apple" in low or "google" in low or low == "skip":
        raise PhoneError(f"Refusing tap {n.labels()!r}")
    log(f"{why} [{n.x},{n.y} {n.w}x{n.h}]")
    stack.tap_xy(*n.center())
    _wait(stop, 1.4)


def continue_btn(nodes):
    """Return the unique bounded Reddit email-form Continue action."""
    hits = [
        n
        for n in nodes
        if n.on_screen()
        and collapsed_text(n.labels()) == "continue"
        and "apple" not in collapsed_text(n.labels())
        and n.enabled
        and 650 <= n.y <= 1245
        and 250 <= n.center()[0] <= 500
        and n.w <= 260
        and n.h <= 80
    ]
    return hits[0] if len(hits) == 1 else None


def keyboard_done_btn(nodes):
    """Return only the iOS email-keyboard Done key in its bottom-right band."""
    hits = [
        node
        for node in nodes
        if node.ntype != "ocr-line"
        and node.on_screen()
        and node.visible
        and node.enabled
        and collapsed_text(node.labels()) == "done"
        and 600 <= node.x <= 740
        and 1100 <= node.y <= 1260
        and 45 <= node.w <= 140
        and node.h <= 70
    ]
    return hits[0] if len(hits) == 1 else None


def at_verify(nodes) -> bool:
    return find_label_exact(nodes, ("Verify your email",)) is not None


def proven_verify_back_screen(nodes) -> bool:
    """Bound the one coordinate-only Back tap to Reddit's exact Verify screen."""
    text = blob_of(nodes)
    return (
        at_verify(nodes)
        and find_label_exact(nodes, ("Verification code",)) is not None
        and "don't see our email" in text
        and find_label_exact(nodes, ("Continue",)) is not None
    )


def return_verify_to_email_entry(
    stack: TapStack,
    phone: Phone,
    *,
    stop=None,
    log_fn=None,
):
    """Tap Reddit Verify's proven top-left Back arrow once and prove email entry."""
    emit = log_fn or log
    _check_stop(stop)
    _blob, nodes = dump_named(
        stack, phone, "880_verify_alias_retry", emit, shot=True
    )
    if not proven_verify_back_screen(nodes):
        raise PhoneError(
            "Replacement email requires Reddit's exact Verify screen; "
            "the bounded Back tap was not authorized."
        )
    emit("Verify screen proven -> tapping the single top-left Back arrow for alias retry.")
    stack.tap_xy(55, 90)
    _wait(stop, 1.4)
    _blob, fresh = dump_named(
        stack, phone, "881_after_verify_alias_back", emit, shot=True
    )
    if not welcome(fresh):
        raise PhoneError(
            "Verify Back did not reach Reddit's email-entry screen. "
            "Did not repeat the tap or create another alias."
        )
    return fresh


def at_sorry(nodes) -> bool:
    b = blob_of(nodes)
    return "sorry, please try again later" in b or "please try again later" in b


def welcome(nodes) -> bool:
    return (
        find_label_exact(nodes, ("Hi new friend,", "welcome to Reddit")) is not None
        or find_label_exact(nodes, ("Enter your email to get started",)) is not None
    )


def provider_sheet(nodes) -> bool:
    """Recognize Reddit's provider picker without treating its title as a button."""
    return find_label_exact(nodes, PROVIDER_MARKERS) is not None


def home_feed_you_node(nodes):
    """Return only the exact bottom-nav You control on a proven Reddit feed.

    The feed itself is account-neutral: logged-in and logged-out feeds can have
    the same Home / Inbox / You navigation. The caller must inspect the fresh
    post-tap dump before treating the session as logged out.
    """

    if not proven_home_feed(nodes):
        return None

    def exact_hits(label: str, min_x: int, max_x: int, min_y: int, max_y: int):
        hits = []
        for node in nodes:
            if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
                continue
            if collapsed_text(node.labels()) != collapsed_text(label):
                continue
            cx, _cy = node.center()
            if node.y < min_y or node.y > max_y or cx < min_x or cx > max_x:
                continue
            if node.w > 400 or node.h > 80:
                continue
            hits.append(node)
        return hits

    search_hits = exact_hits("Search Reddit", 120, 560, 70, 180)
    login_hits = exact_hits("Log in", 520, 740, 70, 180)
    home_hits = exact_hits("Home", 20, 240, 1180, 1315)
    inbox_hits = exact_hits("Inbox", 250, 500, 1180, 1315)
    you_hits = exact_hits("You", 510, 730, 1180, 1315)
    if len(search_hits) != 1 or len(home_hits) != 1 or len(inbox_hits) != 1:
        return None
    search, home, inbox = search_hits[0], home_hits[0], inbox_hits[0]
    if len(you_hits) > 1:
        return None
    if len(you_hits) == 1:
        you = you_hits[0]
        nav_y = (home.center()[1], inbox.center()[1], you.center()[1])
        if max(nav_y) - min(nav_y) > 35:
            return None
        return you

    # XXTouch can repeatedly omit only the rightmost "You" label even though
    # the icon and label are visibly present.  Infer that tab only on the exact
    # logged-out layout observed in production: unique Search Reddit + Log in
    # top anchors and unique, aligned Home + Inbox bottom anchors.  Their equal
    # spacing determines the third tab center; no content or absolute blind
    # coordinate is accepted.
    if len(login_hits) != 1:
        return None
    login = login_hits[0]
    if abs(search.center()[1] - login.center()[1]) > 45:
        return None
    home_x, home_y = home.center()
    inbox_x, inbox_y = inbox.center()
    spacing = inbox_x - home_x
    inferred_x = inbox_x + spacing
    inferred_y = round((home_y + inbox_y) / 2)
    if not (
        180 <= spacing <= 320
        and 540 <= inferred_x <= 710
        and 1180 <= inferred_y <= 1310
        and abs(home_y - inbox_y) <= 35
    ):
        return None
    return Node(
        ntype="inferred-bottom-nav",
        text="You",
        x=inferred_x - 40,
        y=inferred_y - 40,
        w=80,
        h=80,
        visible=True,
        enabled=True,
        raw="inferred from logged-out Search/Log in/Home/Inbox anchors",
    )


def _signup_doorway_row_text(node) -> str:
    """Discard only a leading OCR icon before an exact doorway row label."""

    text = collapsed_text(node.labels())
    return re.sub(r"^[^0-9a-z]+", "", text, count=1)


def signup_customize_doorway_node(nodes):
    """Return the one safe Sign up / Log in row on Reddit's logged-out You sheet."""

    def exact_hits(label: str):
        wanted = collapsed_text(label)
        return [
            node
            for node in nodes
            if node.ntype != "ocr-line"
            and node.on_screen()
            and node.visible
            and collapsed_text(node.labels()) == wanted
        ]

    def icon_row_hits(label: str):
        wanted = collapsed_text(label)
        return [
            node
            for node in nodes
            if node.ntype != "ocr-line"
            and node.on_screen()
            and node.visible
            and node.enabled
            and _signup_doorway_row_text(node) == wanted
        ]

    search = exact_hits("Search Reddit")
    login = exact_hits("Log in")
    title = exact_hits(SIGNUP_CUSTOMIZE_TITLE)
    action = icon_row_hits(SIGNUP_CUSTOMIZE_ACTION)
    settings = icon_row_hits(SIGNUP_CUSTOMIZE_SETTINGS)
    # The close glyph is decorative and XXTouch OCR does not reliably expose it.
    # Prove the drawer from the stable top bar, title, and both actionable rows;
    # the script never needs to tap or otherwise depend on the close glyph.
    groups = (search, login, title, action, settings)
    if any(len(group) != 1 for group in groups):
        return None
    search_node, login_node = search[0], login[0]
    title_node, action_node, settings_node = title[0], action[0], settings[0]
    search_x, search_y = search_node.center()
    login_x, login_y = login_node.center()
    title_x, title_y = title_node.center()
    action_x, action_y = action_node.center()
    settings_x, settings_y = settings_node.center()

    # Semantic/relative proof: the Reddit top bar is above one lower-screen
    # drawer whose title, action, and Settings rows retain their order and
    # spacing even when the sheet moves vertically by dozens of pixels.
    if not (
        search_y < 250
        and login_y < 250
        and abs(search_y - login_y) <= 45
        and search_x < login_x
        and title_y > 700
        and 80 <= action_y - title_y <= 260
        and 35 <= settings_y - action_y <= 150
        and title_x < 500
        and action_x < 450
        and settings_x < 400
        and 250 <= title_node.w <= 700
        and 140 <= action_node.w <= 450
        and 80 <= settings_node.w <= 350
        and title_node.h <= 80
        and action_node.h <= 80
        and settings_node.h <= 80
    ):
        return None
    if existing_account_profile(nodes):
        return None
    return action[0]


def signup_landing_or_provider(nodes) -> bool:
    """Strong post-You proof that the locked signup path may continue."""

    provider_hits = sum(
        find_label_exact(nodes, (label,)) is not None for label in PROVIDER_MARKERS
    )
    has_email_provider = find_label_exact(nodes, CONTINUE_WITH_EMAIL) is not None
    if has_email_provider and (
        provider_hits >= 2 or find_label_exact(nodes, GET_STARTED) is not None
    ):
        return True
    if (
        find_label_exact(nodes, GET_STARTED) is not None
        and find_label_exact(nodes, INITIAL_ACCOUNT) is not None
    ):
        return True
    if (
        find_label_exact(nodes, ("Log in to Reddit",)) is not None
        and find_label_exact(nodes, CREATE_ACCOUNT) is not None
    ):
        return True
    return (
        find_label_exact(nodes, ("Use email or username",)) is not None
        and (
            find_label_exact(nodes, GET_STARTED) is not None
            or find_label_exact(nodes, INITIAL_ACCOUNT) is not None
        )
    )


def existing_account_profile(nodes) -> bool:
    """Recognize strong profile UI after the account-neutral You probe."""

    if find_label_exact(nodes, PROFILE_MARKERS) is not None:
        return True
    tabs = sum(find_label_exact(nodes, (label,)) is not None for label in PROFILE_TABS)
    if tabs >= 2:
        return True
    b = blob_of(nodes)
    return "reddit age" in b and "karma" in b


def post_you_state(nodes) -> str:
    """Classify the one fresh dump after bottom-nav You."""

    if signup_customize_doorway_node(nodes) is not None:
        return "doorway"
    if signup_landing_or_provider(nodes):
        return "signup"
    if existing_account_profile(nodes):
        return "profile"
    return "unknown"


def _tap_signup_doorway(stack: TapStack, doorway) -> None:
    """Tap a freshly located doorway row after validating current geometry."""

    center = doorway.center()
    if not (0 <= center[0] <= 750 and 0 <= center[1] <= 1334):
        raise PhoneError(
            "Reddit signup doorway was outside the canonical screen; did not tap."
        )
    refresh = getattr(stack, "refresh_screen_geometry", None)
    if callable(refresh):
        width, height = refresh()
        ratio = height / max(1, width)
        if height <= width or not 1.5 <= ratio <= 2.5:
            raise PhoneError(
                f"Reddit signup doorway requires verified portrait geometry; "
                f"device reported {width}x{height}. Did not tap."
            )
    stack.tap_xy(*center)


def dump_after_you(stack: TapStack, phone: Phone):
    """Permit continuation only from a proven signup landing/provider sheet."""

    _blob, nodes = dump_named(stack, phone, "872_after_you", log, shot=True)
    state = post_you_state(nodes)
    if state == "doorway":
        log("Bottom-nav You proved Reddit's logged-out signup doorway.")
        return nodes
    if state == "signup":
        log("Bottom-nav You proved a login/signup landing/provider sheet.")
        return nodes
    if state == "profile":
        raise PhoneError(
            "Bottom-nav You opened an existing account/profile. Stopped without "
            "logging out or changing it. See 872_after_you."
        )
    raise PhoneError(
        "Bottom-nav You did not prove a login/signup landing/provider sheet. "
        "Preserved the screen without another tap. See 872_after_you."
    )


def wait_after_signup_doorway(stack: TapStack, phone: Phone, *, stop=None, log_fn=None):
    """Wait for the doorway transition, with one verified same-row retry."""

    emit = log_fn or log
    last_nodes = []
    for attempt in range(2):
        for i in range(8):
            _check_stop(stop)
            CREATION_PROGRESS.note()
            _blob, nodes = dump_named(
                stack,
                phone,
                f"872_after_signup_doorway_{attempt}_{i}",
                emit,
                shot=True,
            )
            last_nodes = nodes
            if existing_account_profile(nodes):
                raise PhoneError(
                    "Signup doorway opened an existing account/profile. Stopped without "
                    "logging out or changing it."
                )
            if (
                signup_landing_or_provider(nodes)
                or welcome(nodes)
                or at_verify(nodes)
                or at_sorry(nodes)
            ):
                emit("Signup doorway advanced to a proven Reddit signup state.")
                return nodes
            if i < 7:
                if signup_customize_doorway_node(nodes) is not None:
                    emit(
                        "Signup doorway transition pending "
                        f"({attempt + 1}/2 tap, {i + 1}/8 frame)."
                    )
                else:
                    emit(
                        "Signup doorway next screen still loading "
                        f"({attempt + 1}/2 tap, {i + 1}/8 frame)."
                    )
                _wait(stop, 1.2)

        retry = signup_customize_doorway_node(last_nodes)
        if attempt == 0 and retry is not None:
            _check_stop(stop)
            emit(
                "Signup doorway remained exactly verified after the first tap; "
                "retrying Sign up / Log in once."
            )
            _tap_signup_doorway(stack, retry)
            _wait(stop, 1.4)
            continue
        break

    raise PhoneError(
        "Signup doorway did not advance to a proven Reddit login/signup screen "
        "after at most two verified taps and bounded fresh frames. "
        "No blind tap was attempted. See 872_after_signup_doorway_1_7."
    )


def _b_norm(nodes) -> str:
    return blob_of(nodes).replace(" ", "").lower()


def _pill_width(nodes, local: str) -> int:
    """Widest on-screen node that looks like the email pill. r13 fail 329, success 488."""
    local = (local or "").lower()
    best = 0
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen() or n.y < 500 or n.y > 750:
            continue
        t = collapsed_text(n.labels()).replace(" ", "").lower()
        if not t:
            continue
        if local[:6] in t or "@icloud" in t or "icloud.co" in t:
            best = max(best, int(n.w))
    return best


@dataclass(frozen=True)
class _EmailOcrRegion:
    text: str
    x: int
    y: int
    w: int
    h: int
    confidence: float | None = None


@dataclass(frozen=True)
class _EmailValidationReport:
    redacted_text: str
    local_length: int
    suffix_match: bool
    bbox: tuple[int, int, int, int] | None
    width: int
    width_ok: bool
    field_context: bool
    invalid_warning: bool
    expected_match: bool
    ocr_equivalence: str
    confidence: float | None
    valid: bool


def _node_ocr_text(node) -> str:
    """One OCR spelling, without the duplicate name/label/text aliases."""
    for value in (node.text, node.label, node.name, node.value):
        if str(value or "").strip():
            return str(value)
    return ""


def _normalize_email_ocr(text: str) -> str:
    """Normalize casing plus OCR-only whitespace/format characters."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(
        char
        for char in folded
        if not char.isspace() and unicodedata.category(char) != "Cf"
    )


def _ocr_confidence(node) -> float | None:
    """Read confidence when a future OCR backend supplies it."""
    for attr in ("confidence", "conf", "score"):
        value = getattr(node, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _email_ocr_regions(nodes) -> list[_EmailOcrRegion]:
    """Single and safely adjacent OCR boxes in the bounded email-field area."""
    source = [
        node
        for node in nodes
        if node.ntype == "ocr"
        and node.on_screen()
        and 400 <= node.y <= 750
        and _node_ocr_text(node).strip()
    ]
    regions = [
        _EmailOcrRegion(
            _normalize_email_ocr(_node_ocr_text(node)),
            int(node.x), int(node.y), int(node.w), int(node.h),
            _ocr_confidence(node),
        )
        for node in source
    ]

    # Merge only boxes on the same tight baseline with a small horizontal gap.
    # This supports an address split by OCR without joining nearby UI labels.
    ordered = sorted(source, key=lambda node: (node.center()[1], node.x))
    used: set[int] = set()
    for index, first in enumerate(ordered):
        if index in used:
            continue
        run = [first]
        run_indexes = [index]
        right = first.x + first.w
        baseline = first.center()[1]
        for next_index in range(index + 1, len(ordered)):
            candidate = ordered[next_index]
            if abs(candidate.center()[1] - baseline) > 12:
                continue
            gap = candidate.x - right
            if gap < -20 or gap > 32:
                continue
            run.append(candidate)
            run_indexes.append(next_index)
            right = max(right, candidate.x + candidate.w)
        if len(run) < 2:
            continue
        used.update(run_indexes)
        x1 = min(node.x for node in run)
        y1 = min(node.y for node in run)
        x2 = max(node.x + node.w for node in run)
        y2 = max(node.y + node.h for node in run)
        confidences = [value for node in run if (value := _ocr_confidence(node)) is not None]
        regions.append(
            _EmailOcrRegion(
                "".join(_normalize_email_ocr(_node_ocr_text(node)) for node in run),
                int(x1), int(y1), int(x2 - x1), int(y2 - y1),
                min(confidences) if confidences else None,
            )
        )
    return regions


def _email_pill_candidates(nodes) -> list[str]:
    """Full iCloud addresses from the bounded email-field region."""
    found: list[str] = []
    for region in _email_ocr_regions(nodes):
        for match in EMAIL_PILL_RE.finditer(region.text):
            candidate = match.group(1).lower()
            if candidate not in found:
                found.append(candidate)
    return found


def _email_ocr_equivalence(observed: str, expected: str) -> str:
    """Classify one proven, local-part-only OCR substitution.

    Direction matters for numeric aliases: OCR may render a saved digit as a
    similar letter, but the reverse must not make a different saved address
    look valid.
    """
    observed = (observed or "").strip().lower()
    expected = (expected or "").strip().lower()
    if observed == expected and observed.endswith("@icloud.com"):
        return "exact"
    if len(observed) != len(expected):
        return "none"

    try:
        observed_local, observed_domain = observed.rsplit("@", 1)
        expected_local, expected_domain = expected.rsplit("@", 1)
    except ValueError:
        return "none"
    if not observed_local or not expected_local:
        return "none"
    if observed_domain != "icloud.com" or expected_domain != "icloud.com":
        return "none"

    differences = [
        (seen, want)
        for seen, want in zip(observed_local, expected_local)
        if seen != want
    ]
    if len(differences) != 1:
        return "none"
    seen, want = differences[0]
    if set((seen, want)) == {"8", "s"}:
        return "eight-s"
    if (seen, want) == ("o", "0"):
        return "digit-0-read-as-letter-o"
    if seen in ("l", "i") and want == "1":
        return "digit-1-read-as-letter-l-or-i"
    return "none"


def _email_ocr_equivalent(observed: str, expected: str) -> bool:
    """Return whether the addresses are exact or one approved OCR variant."""
    return _email_ocr_equivalence(observed, expected) != "none"


def _tok(pat: str, text: str) -> bool:
    """Token-boundary match: pat must not be glued to other email chars.

    Substring check passed garbage like binder+bindery.hire_8g@icloud.com.
    """
    return re.search(
        r"(?<![a-z0-9_.@%+\-])" + pat + r"(?![a-z0-9_.@%+\-])",
        text,
    ) is not None


def _email_validation_report(nodes, email: str) -> _EmailValidationReport:
    expected = (email or "").strip().casefold()
    raw = " ".join(blob_of(nodes).casefold().split())
    invalid_warning = "please enter a valid email" in raw
    regions = [region for region in _email_ocr_regions(nodes) if "@icloud" in region.text]

    def observed_address(region: _EmailOcrRegion) -> str:
        match = EMAIL_PILL_RE.search(region.text)
        if match is not None:
            return match.group(1).casefold()
        # Retain a bounded partial value for suffix diagnostics only.
        partial = re.search(r"[a-z0-9._%+\-]+@icloud(?:\.[a-z]*)?", region.text, re.I)
        return partial.group(0).casefold() if partial is not None else ""

    ranked: list[tuple[tuple[int, int, int, int], _EmailOcrRegion, str]] = []
    expected_local = expected.split("@", 1)[0] if "@" in expected else ""
    for region in regions:
        observed = observed_address(region)
        expected_match = _email_ocr_equivalent(observed, expected)
        suffix_match = observed.endswith("@icloud.com")
        observed_local = observed.split("@", 1)[0] if "@" in observed else ""
        rank = (
            int(expected_match),
            int(suffix_match),
            -abs(len(observed_local) - len(expected_local)),
            int(region.w),
        )
        ranked.append((rank, region, observed))

    if not ranked:
        return _EmailValidationReport(
            "<none>", 0, False, None, 0, False, False,
            invalid_warning, False, "none", None, False,
        )

    _rank, selected, observed = max(ranked, key=lambda item: item[0])
    local = observed.split("@", 1)[0] if "@" in observed else ""
    domain = observed.rsplit("@", 1)[1] if "@" in observed else ""
    suffix_match = domain == "icloud.com"
    email_labels = [
        node
        for node in nodes
        if node.ntype != "ocr-line"
        and node.on_screen()
        and collapsed_text(node.labels()) == "email"
        and 390 <= node.y <= 650
    ]
    selected_center_y = selected.y + selected.h // 2
    field_context = welcome(nodes) and len(email_labels) == 1 and (
        abs(email_labels[0].center()[1] - selected_center_y) <= 120
    )
    # Width is retained for diagnostics only. OCR width changes with the
    # address, font rendering, and backend; it cannot decide validity.
    width_ok = selected.w >= 450
    ocr_equivalence = _email_ocr_equivalence(observed, expected)
    expected_match = ocr_equivalence != "none"
    valid = suffix_match and field_context and not invalid_warning and expected_match
    return _EmailValidationReport(
        f"***@{domain}" if domain else "<none>",
        len(local), suffix_match,
        (selected.x, selected.y, selected.w, selected.h),
        selected.w, width_ok, field_context, invalid_warning, expected_match,
        ocr_equivalence, selected.confidence, valid,
    )


def _log_email_validation_failure(nodes, email: str) -> None:
    report = _email_validation_report(nodes, email)
    confidence = (
        "unavailable" if report.confidence is None else f"{report.confidence:.3f}"
    )
    bbox = "none" if report.bbox is None else (
        f"[{report.bbox[0]},{report.bbox[1]} "
        f"{report.bbox[2]}x{report.bbox[3]}]"
    )
    log(
        "Email validator OCR=" + report.redacted_text
        + f" local_len={report.local_length} confidence={confidence}"
    )
    log(
        f"Email validator suffix_match={str(report.suffix_match).lower()} "
        f"expected_match={str(report.expected_match).lower()} "
        f"ocr_equivalence={report.ocr_equivalence}"
    )
    log(
        f"Email validator bbox={bbox} width={report.width} "
        f"width_ok={str(report.width_ok).lower()} "
        f"field_context={str(report.field_context).lower()}"
    )
    log(
        "Email validator invalid_warning="
        + str(report.invalid_warning).lower()
    )


def email_ready(nodes, email: str) -> bool:
    """Require the exact address in Reddit's bounded email form, without warnings."""
    return _email_validation_report(nodes, email).valid


def _single_missing_saved_email_char(nodes, email: str) -> tuple[int, str] | None:
    """Prove that the bounded field is the saved address minus one character.

    XXTouch normally types one character per script.  If iOS reports that a
    script is already running at an uncertain-delivery boundary, one character
    can be lost even though the rest of the address arrives.  This predicate
    authorizes a *retype*, never acceptance or submission: the exact address
    must still pass ``email_ready`` afterward.
    """
    expected = (email or "").strip().casefold()
    try:
        expected_local, expected_domain = expected.rsplit("@", 1)
    except ValueError:
        return None
    if not expected_local or expected_domain != "icloud.com":
        return None

    report = _email_validation_report(nodes, expected)
    if (
        not report.suffix_match
        or not report.field_context
        or report.invalid_warning
        or report.local_length != len(expected_local) - 1
    ):
        return None

    repairs: set[tuple[int, str]] = set()
    for observed in _email_pill_candidates(nodes):
        try:
            observed_local, observed_domain = observed.casefold().rsplit("@", 1)
        except ValueError:
            continue
        if observed_domain != "icloud.com" or len(observed_local) != len(expected_local) - 1:
            continue
        for index, char in enumerate(expected_local):
            if expected_local[:index] + expected_local[index + 1:] == observed_local:
                repairs.add((index, char))
    return next(iter(repairs)) if len(repairs) == 1 else None


def email_transition_pending(nodes, email: str) -> bool:
    """A valid email remains while its submit button is temporarily absent."""
    return (
        welcome(nodes)
        and email_ready(nodes, email)
        and continue_btn(nodes) is None
        and keyboard_done_btn(nodes) is None
    )


def type_slot_email(stack: TapStack, nodes, email: str, name: str, *, stop=None) -> None:
    _check_stop(stop)
    field = find_label_exact(nodes, ("Email",))
    if field is None:
        raise PhoneError("Welcome screen has no Email field.")
    y = field.center()[1]
    junk = emails_on(nodes) or "please enter a valid email" in blob_of(nodes).lower()
    if junk or "@" in _b_norm(nodes):
        # Retype path: tap the RIGHT end of the pill so the cursor lands at the
        # end (middle-tap left the cursor mid-text and backspaces left old prefix
        # like binder+bindery.hire_8g@icloud.com). Then 60 backspaces, one Lua.
        log(f"Email pill retype tap (620,{y}) clear type {name} {email}")
        stack.tap_xy(620, y)
        _wait(stop, 0.45)
        stack.press_backspace(60)
        _wait(stop, 0.2)
        stack.press_backspace(20)
        _wait(stop, 0.2)
    else:
        log(f"Email pill tap (375,{y}) type {name} {email}")
        stack.tap_xy(375, y)
        _wait(stop, 0.45)
    stack.type_text(email)
    _wait(stop, 1.2)


def step(
    stack: TapStack,
    phone: Phone,
    nodes,
    email: str,
    opened: list[bool],
    name: str,
    typed_once: list[int],
    continue_email_taps: list[int],
    email_continue_taps: list[int],
    email_transition_waits: list[int],
    home_you_taps: list[int] | None = None,
    *,
    email_done_taps: list[int] | None = None,
    signup_doorway_taps: list[int] | None = None,
    stop=None,
):
    _check_stop(stop)
    if home_you_taps is None:
        home_you_taps = [0]
    if email_done_taps is None:
        email_done_taps = [0]
    if signup_doorway_taps is None:
        signup_doorway_taps = [0]
    warn_siwa(nodes, log)
    b = blob_of(nodes)
    shown = emails_on(nodes)

    if at_verify(nodes):
        # STAY. Never Home / recents / Mail / close_reddit on this screen.
        # OCR often truncates the HME; closing because of a mismatch kills the code step.
        if email.lower() not in b:
            log(f"Verify OCR email {shown or 'truncated'}; want {email}. Staying. No Skip. No close.")
        return "VERIFY"

    if at_sorry(nodes):
        return "SORRY"

    if find_label_exact(nodes, ("Select Container",)) is not None:
        open_container(stack, phone, log, name, email)
        opened[0] = True
        return "OPENED"

    # A proven feed wins over every in-content CTA. Posts and ads can contain
    # words such as Join, SIGN UP, Get Started, or Continue; none authorizes a
    # tap. The exact bottom navigation is the sole account-state probe here.
    you = home_feed_you_node(nodes)
    if you is not None:
        if home_you_taps[0] >= 1:
            raise PhoneError(
                "Bottom-nav You was already tapped once but the feed is still present. "
                "Did not repeat the tap."
            )
        log(f"Bottom-nav You [{you.x},{you.y} {you.w}x{you.h}]")
        stack.tap_xy(*you.center())
        home_you_taps[0] += 1
        _wait(stop, 1.4)
        return "YOU_PROBE"

    signup_doorway = signup_customize_doorway_node(nodes)
    if signup_doorway is not None:
        if signup_doorway_taps[0] >= 1:
            raise PhoneError(
                "Reddit's Sign up / Log in doorway was already tapped once but "
                "remains visible. Did not repeat the tap."
            )
        log(
            "Signup doorway Sign up / Log in "
            f"[{signup_doorway.x},{signup_doorway.y} "
            f"{signup_doorway.w}x{signup_doorway.h}]"
        )
        _tap_signup_doorway(stack, signup_doorway)
        signup_doorway_taps[0] += 1
        _wait(stop, 1.4)
        return "DOORWAY_TAP"

    if find_label_exact(nodes, ("Not now", "Not Now")) is not None:
        tap_named(stack, nodes, ("Not now", "Not Now"), "Not now", stop=stop)
        return "TAP"

    if provider_sheet(nodes):
        if find_label_exact(nodes, CONTINUE_WITH_EMAIL) is None:
            raise PhoneError(
                "Reddit provider sheet has no Continue with email. "
                "Did not tap Apple, Google, Cancel, or another action."
            )
        if continue_email_taps[0] >= 1:
            raise PhoneError(
                "Continue with email was tapped once but the provider sheet is still present. "
                "Did not repeat the tap."
            )
        tap_named(stack, nodes, CONTINUE_WITH_EMAIL, "Continue with email", stop=stop)
        continue_email_taps[0] += 1
        return "TAP"

    if find_label_exact(nodes, GET_STARTED) is not None:
        if find_label_exact(nodes, INITIAL_ACCOUNT) is not None:
            tap_named(stack, nodes, GET_STARTED, "Get Started", stop=stop)
            return "TAP"
        raise PhoneError(
            "Get Started is present without the initial-page marker. "
            "Did not tap a possible heading."
        )

    if find_label_exact(nodes, ("Use email or username",)) is not None:
        tap_named(
            stack,
            nodes,
            ("Use email or username",),
            "Use email or username",
            stop=stop,
        )
        return "TAP"

    if find_label_exact(nodes, CREATE_ACCOUNT) is not None:
        tap_named(
            stack,
            nodes,
            CREATE_ACCOUNT,
            "Create a new account",
            stop=stop,
        )
        return "TAP"

    if welcome(nodes):
        if email_ready(nodes, email):
            btn = continue_btn(nodes)
            keyboard_done = keyboard_done_btn(nodes)
            if email_continue_taps[0] >= 1:
                # dump_named() obtains live OCR before its diagnostic screenshot.
                # Reddit can advance between those two captures, so a fresh OCR
                # frame may still show the valid email form even though the later
                # screenshot already shows Verify.  Never interpret that frame as
                # permission to submit again; wait for a fresh decision dump.
                email_transition_waits[0] += 1
                if email_transition_waits[0] >= EMAIL_TRANSITION_MAX_DUMPS - 1:
                    log(
                        "Email submit transition pending; no second tap "
                        f"({email_transition_waits[0]}/{EMAIL_TRANSITION_MAX_DUMPS})"
                    )
                    _wait(stop, 1.2)
                    _, final_nodes = dump_named(
                        stack,
                        phone,
                        "873_email_transition_timeout",
                        log,
                        shot=False,
                    )
                    # Keep the final decision artifact OCR-only.  A screenshot is
                    # diagnostic and would be captured later, reopening the same
                    # race this guard is meant to avoid.
                    if at_verify(final_nodes):
                        return "VERIFY"
                    if at_sorry(final_nodes):
                        return "SORRY"
                    if welcome(final_nodes) and email_ready(final_nodes, email):
                        raise PhoneError(
                            "Email screen remained after one submit for 8 fresh dumps. "
                            "Did not retype or tap Continue again. "
                            "See 873_email_transition_timeout."
                        )
                    raise PhoneError(
                        "Email submit transition did not reach Verify after 8 fresh dumps; "
                        "the final screen was not a valid email screen. Did not retype or "
                        "tap Continue again. See 873_email_transition_timeout."
                    )
                log(
                    "Email submit transition pending; no second tap "
                    f"({email_transition_waits[0]}/{EMAIL_TRANSITION_MAX_DUMPS})"
                )
                _wait(stop, 1.2)
                return "WAIT"
            if btn is None and keyboard_done is not None:
                if email_done_taps[0] >= 1:
                    raise PhoneError(
                        "The exact keyboard Done key was already tapped once, but the "
                        "email keyboard is still present. Did not tap it again."
                    )
                log(
                    "Email is exact and valid; keyboard hides Continue. "
                    f"Done [{keyboard_done.x},{keyboard_done.y} "
                    f"{keyboard_done.w}x{keyboard_done.h}]"
                )
                stack.tap_xy(*keyboard_done.center())
                email_done_taps[0] += 1
                email_transition_waits[0] = 0
                _wait(stop, 1.2)
                return "KEYBOARD_DONE"
            if btn is None:
                email_transition_waits[0] += 1
                if email_transition_waits[0] >= EMAIL_TRANSITION_MAX_DUMPS:
                    raise PhoneError(
                        "Email remained valid with Continue absent for 8 fresh dumps. "
                        "Did not retype or submit again."
                    )
                phase = "after one submit" if email_continue_taps[0] else "while enabling"
                log(
                    f"Email Continue transition pending ({phase}) -> wait "
                    f"{email_transition_waits[0]}/{EMAIL_TRANSITION_MAX_DUMPS}"
                )
                _wait(stop, 1.2)
                return "WAIT"
            email_transition_waits[0] = 0
            log(f"Continue [{btn.x},{btn.y} {btn.w}x{btn.h}]")
            stack.tap_xy(*btn.center())
            email_continue_taps[0] += 1
            _wait(stop, 2.4)
            return "TAP"
        if typed_once[0] == 1:
            missing = _single_missing_saved_email_char(nodes, email)
            if missing is not None:
                index, char = missing
                log(
                    "Email typing lost exactly one saved local-part character "
                    f"at index {index} (class={'digit' if char.isdigit() else 'letter'}); "
                    "clearing and retyping once before any submit."
                )
                type_slot_email(stack, nodes, email, name, stop=stop)
                typed_once[0] += 1
                return "RETYPED"
        if typed_once[0] >= 1:
            _log_email_validation_failure(nodes, email)
            dump_named(stack, phone, "873_email_still_invalid", log, shot=True)
            raise EmailFieldInvalid(
                "Email field still invalid after one type and at most one bounded "
                "single-missing-character retype (need the exact saved @icloud.com "
                "address in Reddit's email form and no 'Please enter a valid email'). "
                "Did not tap Continue. See 873_email_still_invalid."
            )
        type_slot_email(stack, nodes, email, name, stop=stop)
        typed_once[0] += 1
        return "TYPED"

    if find_label_exact(nodes, ("Log in to Reddit",)) is not None:
        raise PhoneError("Login screen but Create a new account missing. See dump.")

    return "UNKNOWN"


def wait_ui(stack: TapStack, phone: Phone, stem: str, nodes, *, stop=None, log_fn=None):
    """Picker/open can dump a blank Reddit frame. Wait for real labels."""
    emit = log_fn or log
    for i in range(8):
        _check_stop(stop)
        CREATION_PROGRESS.note()
        if proven_springboard(nodes):
            raise PhoneError(
                "Reddit left the foreground and iPhone Home is visible. "
                "Stopped the signup wait without reopening or tapping anything."
            )
        b = blob_of(nodes)
        if (
            at_verify(nodes)
            or at_sorry(nodes)
            or welcome(nodes)
            or provider_sheet(nodes)
            or find_label_exact(nodes, GET_STARTED) is not None
            or find_label_exact(nodes, ("Use email or username",)) is not None
            or find_label_exact(nodes, ("Not now", "Not Now")) is not None
            or find_label_exact(nodes, ("Log in to Reddit",)) is not None
            or find_label_exact(nodes, ("Select Container",)) is not None
            or "log in or sign up" in b
            or signup_customize_doorway_node(nodes) is not None
            or home_feed_you_node(nodes) is not None
        ):
            return nodes
        emit(f"Reddit still loading -> wait {i + 1}/8")
        _wait(stop, 1.2)
        _, nodes = dump_named(stack, phone, f"{stem}_{i}", emit, shot=True)
    return nodes


def run_to_verify(
    stack: TapStack,
    phone: Phone,
    name: str,
    email: str,
    *,
    stop=None,
    log_fn=None,
    reuse_active_container: bool = False,
) -> None:
    """Locked taps through signup until Verify your email. No SIWA. No Skip."""
    emit = log_fn or log
    _check_stop(stop)
    emit(f"Slot {name} email={email}")
    emit("Goal: Verify your email. No SIWA. No Skip.")
    blob, nodes = dump_named(stack, phone, "870_now", emit, shot=True)
    opened = [False]
    if reuse_active_container:
        _check_stop(stop)
        emit(
            "Replacement email: reopening Reddit directly in the already active "
            "Crane container; container selection is not repeated."
        )
        stack.open_bundle(REDDIT_BUNDLE)
        opened[0] = True
        _check_stop(stop)
        blob, nodes = dump_named(
            stack, phone, "871_after_active_container_reopen", emit, shot=True
        )
    elif proven_springboard(nodes):
        _check_stop(stop)
        emit(
            "Initial iPhone Home detected -> open the exact selected Crane "
            "container before signup."
        )
        open_container(stack, phone, emit, name, email)
        opened[0] = True
        _check_stop(stop)
        blob, nodes = dump_named(stack, phone, "871_after_open", emit, shot=True)
    typed_once = [0]
    continue_email_taps = [0]
    email_continue_taps = [0]
    email_transition_waits = [0]
    email_done_taps = [0]
    home_you_taps = [0]
    signup_doorway_taps = [0]
    b = blob_of(nodes)
    if (
        not opened[0]
        and find_label_exact(nodes, ("Select Container",)) is None
        and not at_verify(nodes)
        and not at_sorry(nodes)
        and "log in to reddit" not in b
        and not welcome(nodes)
        and not provider_sheet(nodes)
        and find_label_exact(nodes, GET_STARTED) is None
        and find_label_exact(nodes, ("Use email or username",)) is None
        and signup_customize_doorway_node(nodes) is None
        and home_feed_you_node(nodes) is None
    ):
        _check_stop(stop)
        open_container(stack, phone, emit, name, email)
        opened[0] = True
        _check_stop(stop)
        blob, nodes = dump_named(stack, phone, "871_after_open", emit, shot=True)

    nodes = wait_ui(stack, phone, "871w", nodes, stop=stop, log_fn=emit)
    if email_transition_pending(nodes, email):
        email_continue_taps[0] = 1
        log(
            "Resume: valid email remains with Continue absent; wait for the prior "
            "submit transition without retyping or tapping again."
        )

    for i in range(14):
        _check_stop(stop)
        CREATION_PROGRESS.note()
        result = step(
            stack,
            phone,
            nodes,
            email,
            opened,
            name,
            typed_once,
            continue_email_taps,
            email_continue_taps,
            email_transition_waits,
            home_you_taps,
            email_done_taps=email_done_taps,
            signup_doorway_taps=signup_doorway_taps,
            stop=stop,
        )
        log(f"STEP {i} {result}")
        if result == "YOU_PROBE":
            nodes = dump_after_you(stack, phone)
            continue
        if result == "DOORWAY_TAP":
            nodes = wait_after_signup_doorway(
                stack,
                phone,
                stop=stop,
                log_fn=emit,
            )
            continue
        if result == "VERIFY":
            dump_named(stack, phone, "879_verify", log, shot=True)
            log(f"ON Verify your email for {email}. Did not tap Skip.")
            return
        if result == "SORRY":
            # The current 870/871/873 decision dump already contains the exact
            # rejection.  Do not touch the phone again merely to duplicate it:
            # a second dump delayed the terminal error and could be interrupted
            # before Creator logged the failure.
            raise PhoneError(SORRY_STOP_MESSAGE)
        if result == "UNKNOWN":
            nodes = wait_ui(
                stack,
                phone,
                f"877w_{i}",
                nodes,
                stop=stop,
                log_fn=emit,
            )
            result = step(
                stack,
                phone,
                nodes,
                email,
                opened,
                name,
                typed_once,
                continue_email_taps,
                email_continue_taps,
                email_transition_waits,
                home_you_taps,
                email_done_taps=email_done_taps,
                signup_doorway_taps=signup_doorway_taps,
                stop=stop,
            )
            log(f"STEP {i} retry {result}")
            if result == "YOU_PROBE":
                nodes = dump_after_you(stack, phone)
                continue
            if result == "DOORWAY_TAP":
                nodes = wait_after_signup_doorway(
                    stack,
                    phone,
                    stop=stop,
                    log_fn=emit,
                )
                continue
            if result == "UNKNOWN":
                dump_named(stack, phone, f"877_unknown_{i}", log, shot=True)
                raise PhoneError("Screen not in locked path. See dump. Did not guess.")
            if result == "VERIFY":
                dump_named(stack, phone, "879_verify", log, shot=True)
                log(f"ON Verify your email for {email}. Did not tap Skip.")
                return
            if result == "SORRY":
                raise PhoneError(SORRY_STOP_MESSAGE)
        blob, nodes = dump_named(stack, phone, f"873_{i}", log, shot=True)
        nodes = wait_ui(
            stack,
            phone,
            f"873w_{i}",
            nodes,
            stop=stop,
            log_fn=emit,
        )

    raise PhoneError("Too many steps without Verify.")


def main() -> int:
    name = (sys.argv[1] if len(sys.argv) > 1 else "a0001").strip().lower()
    slot = get_slot(name)
    if not slot or not slot.get("ghost"):
        print(f"ERROR Slot {name} has no Ghost container.")
        return 1
    email = str(slot.get("email") or "").strip()
    if not email or not email.lower().endswith("@icloud.com"):
        print(f"ERROR Slot {name} Hide My Email must end with @icloud.com.")
        return 1
    log(f"Slot {name} email={email} reddit={slot.get('reddit')}")
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend. Open XXTouch Elite, Remote Service on.")
        run_to_verify(stack, phone, name, email)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
