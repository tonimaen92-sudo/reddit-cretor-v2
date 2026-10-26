# UTF-8
"""Open Reddit through Crane's SpringBoard haptic menu. Dump-first. No SIWA.

Locked signup taps after the container opens:
  1. Use email or username  (front, not the later Email field)
  2. Create a new account   (login footer; not Log in)
Stop. Type Hide My Email only when asked.

  python misc/reddit_open.py r1
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable
from friend_poster.core.config import REDDIT_BUNDLE  # noqa: E402
from friend_poster.device import ghost_live  # noqa: E402
from friend_poster.device.ghost_live import name_keys  # noqa: E402
from friend_poster.reddit.reddit_interests import (  # noqa: E402
    ads_personalization_screen,
    proven_home_feed,
)
from friend_poster.core.slots import (  # noqa: E402
    CREATION_PROGRESS,
    get_slot,
    index_to_name,
    loose_index,
    name_to_index,
)
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import (  # noqa: E402
    OperationCancelled,
    TapStack,
    collapsed_text,
    dump_named,
    find_label_exact,
    save_dump,
)

os.environ["SKIP_SHOT"] = "1"

LogFn = Callable[[str], None]


class ContainerSelectionError(PhoneError):
    """The requested Crane container was not safely selected and confirmed."""

    failure_code = "reddit_container_selection_failed"


class ContainerLaunchTransitionUnclear(ContainerSelectionError):
    """Exact Crane selection was tapped, but its bounded launch stayed visual-only.

    This is deliberately narrower than :class:`ContainerSelectionError`.  A
    missing, duplicate or otherwise unproven container must never be delegated
    to a model.  Once the exact local target has been tapped and both the normal
    launch check and one fresh-route retry have failed, however, safe visual
    navigation may use the scoped container-selection recovery.
    """

    ai_recoverable = True


class CraneDoorwayUnavailable(ContainerSelectionError):
    """Stock iOS haptic had no Crane doorway and UUID recovery failed."""


FEED_TRANSITION_MIN_SHIFT = 40
FEED_TRANSITION_MAX_SHIFT = 220
FEED_TRANSITION_MAX_X_DRIFT = 12
FEED_TRANSITION_SHIFT_TOLERANCE = 20
FEED_TRANSITION_MIN_MATCHES = 4
FEED_SPARSE_MIN_MATCHES = 2
FEED_SPARSE_MIN_TEXT_LENGTH = 3
SIWA = ("sign in with apple", "continue with apple")
PROTECTED_REDDIT_MARKERS = (
    "verify your email",
    "verification code",
    "create your username",
    "pick a name to use on reddit",
    "great name!",
    "need inspiration?",
    "set a password",
    "password must be at least",
    "about you",
    "tell us about yourself",
    "how do you identify?",
    "choose your interests",
    "interests",
    "pick things you'd like to see",
    "customize your feed",
    "please turn on notifications",
    "your ads personalization choices",
    "building your home feed",
)
CRANE_SUBMENU_LABELS = (
    "Select Container",
    "Switch Container",
    "Containers",
    "Crane",
)
CRANE_LIST_TITLES = (
    "Select Container",
    "Switch Container",
    "Containers",
    "Container",
)
HIDE_MY_EMAIL_TITLE = ("Hide My Email",)
HAPTIC_SYSTEM_ACTIONS = (
    "Remove App",
    "Edit Home Screen",
    "Share App",
    "Delete App",
    "Delete",
)
REDDIT_EXACT_MARKERS = (
    "Not now",
    "Not Now",
    "Get Started",
    "Continue with email",
    "Continue with Apple",
    "Continue with Google",
    "Use email or username",
    "Create a new account",
    "Create new account",
    "Log in to Reddit",
    "Enter your email to get started",
    "Hi new friend,",
    "welcome to Reddit",
    "Verify your email",
)
DAY1_SPARSE_LAUNCH_BLOCKERS = SIWA + (
    "Continue with email",
    "Continue with Google",
    "Use email or username",
    "Create a new account",
    "Create new account",
    "Log in to Reddit",
    "Enter your email to get started",
    "Hi new friend,",
    "welcome to Reddit",
    "Verify your email",
    "Log in or sign up",
    "Sign up / Log in",
    "I already have an account",
)
DAY1_SPARSE_LAUNCH_BLOCKER_SUBSTRINGS = (
    "continue with apple",
    "sign in with apple",
    "continue with google",
    "continue with email",
    "use email or username",
    "log in or sign up",
    "sign up / log in",
    "create a new account",
    "create new account",
    "log in to reddit",
    "enter your email to get started",
    "i already have an account",
)
REDDIT_BOTTOM_MARKERS = {
    "home",
    "communities",
    "create",
    "chat",
    "inbox",
}
CRANE_LIST_BODY_MIN_Y = 260
_REDDIT_ATTRIBUTION_RE = re.compile(
    r"^[ru]/[a-z0-9][a-z0-9_-]*(?:\s|$)"
)
_REDDIT_OCR_PREFIX_ATTRIBUTION_RE = re.compile(
    r"^\(h r/[a-z0-9][a-z0-9_-]* [1-9]\d*[mhdwy]$"
)
_REDDIT_OCR_AD_ATTRIBUTION_RE = re.compile(
    r"^4/[a-z][a-z0-9_-]*\s+ad$"
)
_REDDIT_OCR_ARTIFACT_ATTRIBUTION_RE = re.compile(
    r"^[0-9�§•@)(]{1,2}\s+[ru]/[a-z0-9][a-z0-9_-]*(?:\s|$)"
)


@dataclass(frozen=True, slots=True)
class FeedCleanupProof:
    """Immutable handle to the last frame in a trusted Reddit feed chain."""

    reference_nodes: tuple[object, ...]
    # Sparse continuity is never inferred from an arbitrary live frame.  It is
    # enabled only by make_feed_cleanup_proof() or a successfully advanced
    # proof when the trusted reference has fewer than four long OCR anchors.
    sparse_continuity: bool = False


def log(msg: str) -> None:
    print(msg, flush=True)


def switcher_changed(before: str, after: str) -> bool:
    """Switcher-open proof: post-double_home dump must differ from Home."""
    return bool((before or "") != (after or ""))


def protected_reddit_marker(nodes) -> str:
    """Return the protected signup marker currently visible, if any."""
    blob = " ".join(
        collapsed_text(n.labels())
        for n in nodes or []
        if n.on_screen() and n.labels()
    )
    return next((marker for marker in PROTECTED_REDDIT_MARKERS if marker in blob), "")


def day1_sparse_launch_blocker(nodes) -> str:
    """Return a login/signup marker that must not seed a Day 1 feed proof."""
    visible = [
        node
        for node in nodes or ()
        if callable(getattr(node, "on_screen", None))
        and node.on_screen()
        and getattr(node, "visible", True)
        and callable(getattr(node, "labels", None))
        and node.labels()
    ]
    exact = find_label_exact(visible, DAY1_SPARSE_LAUNCH_BLOCKERS)
    if exact is not None:
        return collapsed_text(exact.labels())
    blob = " ".join(
        collapsed_text(node.labels())
        for node in visible
    )
    return next(
        (
            marker
            for marker in DAY1_SPARSE_LAUNCH_BLOCKER_SUBSTRINGS
            if marker in blob
        ),
        "",
    )


def _screen_signature(nodes) -> tuple[tuple[str, int, int], ...]:
    """Stable non-status-bar signature for proving a menu changed screens."""
    parts = []
    for n in nodes or []:
        if n.ntype == "ocr-line" or not n.on_screen() or not n.visible or n.y < 80:
            continue
        text = collapsed_text(n.labels())
        if text:
            parts.append((text, int(n.x) // 10, int(n.y) // 10))
    return tuple(sorted(parts))


def switcher_app_card(nodes, app_name: str):
    """Return an exact app title only from the switcher's top title band."""
    wanted = collapsed_text(app_name)
    hits = []
    for node in nodes or []:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        if collapsed_text(node.labels()) != wanted:
            continue
        # The live centered iPhone-X Reddit card title is x=272,y=163,
        # w=106,h=24.  Bound the raw title box so in-app titles, SpringBoard
        # icons, and side cards cannot authorize a card-body flick.
        if node.x < 180 or node.x > 360 or node.y < 120 or node.y > 220:
            continue
        if node.w > 260 or node.h > 80:
            continue
        hits.append(node)
    if not hits:
        return None
    return min(hits, key=lambda node: node.center()[1])


def _switcher_title_band_node(nodes):
    """Return a plausible centered app title from the Recents title band."""
    hits = []
    for node in nodes or []:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        text = collapsed_text(node.labels())
        if not text or len(text) < 3:
            continue
        if node.x < 180 or node.x > 380 or node.y < 120 or node.y > 220:
            continue
        if node.w < 45 or node.w > 260 or node.h > 80:
            continue
        hits.append(node)
    if not hits:
        return None
    return min(hits, key=lambda node: node.center()[1])


def switcher_centered_title(nodes):
    """Return a centered Recents title only with a proven adjacent card preview.

    This helper never authorizes a vertical dismissal.  It only permits the one
    horizontal recovery swipe used when iOS opens Recents with the card beside
    Reddit centered.  Requiring several clipped nodes at the far-right edge
    keeps an ordinary in-app navigation title from being mistaken for Recents.
    """
    title = _switcher_title_band_node(nodes)
    if title is None:
        return None
    adjacent_preview_nodes = 0
    wide_preview_nodes = 0
    for node in nodes or []:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        if node.y < 240 or node.y > 1120:
            continue
        if not collapsed_text(node.labels()):
            continue
        if node.x >= 570:
            wide_preview_nodes += 1
        if node.x >= 610:
            adjacent_preview_nodes += 1
    # The established layout exposes three nodes beyond x=610. A video card
    # can clip most text slightly farther left; accept that variant only with
    # two hard-right nodes plus at least five total right-edge preview nodes.
    if adjacent_preview_nodes < 3 and not (
        adjacent_preview_nodes >= 2 and wide_preview_nodes >= 5
    ):
        return None
    return title


def switcher_app_removed(before_nodes, after_nodes, app_name: str) -> bool:
    """Require a proven card before the flick and a different proven card after."""
    after_title = _switcher_title_band_node(after_nodes)
    return (
        switcher_app_card(before_nodes, app_name) is not None
        and switcher_app_card(after_nodes, app_name) is None
        and after_title is not None
        and collapsed_text(after_title.labels()) != collapsed_text(app_name)
        and _screen_signature(before_nodes) != _screen_signature(after_nodes)
    )


def switcher_app_removed_or_home(before_nodes, after_nodes, app_name: str) -> bool:
    """Prove one exact Recents card disappeared after its dismissal flick.

    iOS normally leaves another centered card in Recents, which is handled by
    ``switcher_app_removed``.  When the dismissed app was the only card, iOS
    may instead return directly to SpringBoard.  Accept that outcome only when
    the requested card was exact and centered before the flick and the fresh
    post-flick frame is a proven, changed SpringBoard frame.
    """
    if switcher_app_removed(before_nodes, after_nodes, app_name):
        return True
    return (
        switcher_app_card(before_nodes, app_name) is not None
        and proven_springboard(after_nodes)
        and _screen_signature(before_nodes) != _screen_signature(after_nodes)
    )


def proven_springboard(nodes) -> bool:
    """Recognize any SpringBoard page without depending on one app icon."""
    search = find_label_exact(nodes, ("Q Search",))
    # The real SpringBoard Search pill occupies the lower launcher band, but
    # remains above the dock. Reddit posts can contain screenshots whose OCR
    # includes their own ``Q Search`` close to the very bottom (observed at
    # y=1272 in a Fortnite image). Without an upper bound, four labels inside
    # that image falsely prove SpringBoard and abort a healthy feed swipe.
    if search is None or not 1050 <= search.center()[1] <= 1200:
        return False
    icon_like = 0
    for node in nodes or []:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        center_x, center_y = node.center()
        if center_y < 180 or center_y > 1060:
            continue
        if center_x < 20 or center_x > 730:
            continue
        if node.w < 20 or node.w > 190 or node.h < 10 or node.h > 65:
            continue
        text = collapsed_text(node.labels())
        if not text or text in {"q search", "search"}:
            continue
        icon_like += 1
    return icon_like >= 4


def _feed_content_positions(nodes) -> dict[str, list[tuple[int, int]]]:
    """Return exact, non-navigation text anchors from one Reddit feed frame."""
    positions: dict[str, list[tuple[int, int]]] = {}
    excluded = {
        "search reddit",
        "home",
        "communities",
        "create",
        "chat",
        "inbox",
        "notifications",
        "you",
    }
    for node in nodes or []:
        if node.ntype == "ocr-line" or not node.on_screen():
            continue
        text = collapsed_text(node.labels())
        if len(text) < 12 or text in excluded:
            continue
        center_x, center_y = node.center()
        if center_y < 80 or center_y > 1160:
            continue
        positions.setdefault(text, []).append((center_x, center_y))
    return positions


def _unique_feed_content_positions(
    nodes,
    *,
    min_text_length: int,
) -> dict[str, tuple[int, int]]:
    """Return unambiguous feed anchors, including short card text when asked.

    The ordinary four-anchor proof intentionally retains its historical
    long-text filter.  Sparse proof uses this stricter unique-node inventory so
    a valid but low-text feed can retain continuity without treating fixed
    Reddit navigation, status-bar text, or generic action labels as content.
    """
    excluded = {
        "search reddit",
        "home",
        "communities",
        "create",
        "chat",
        "inbox",
        "notifications",
        "you",
        "for you",
        "following",
        "popular",
        "latest",
        "join",
        "share",
        "save",
        "open",
        "install",
        "promoted",
    }
    positions: dict[str, list[tuple[int, int]]] = {}
    for node in nodes or []:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        text = collapsed_text(node.labels())
        if len(text) < min_text_length or text in excluded:
            continue
        if re.fullmatch(r"\d{1,2}:\d{2}", text):
            continue
        if not re.search(r"[a-z0-9]{2}", text):
            continue
        center_x, center_y = node.center()
        # Include low card attribution text that is just above the bottom bar;
        # the original long-anchor path ends at 1160 and remains unchanged.
        if center_y < 80 or center_y > 1240:
            continue
        positions.setdefault(text, []).append((center_x, center_y))
    return {
        text: points[0]
        for text, points in positions.items()
        if len(points) == 1
    }


def _long_feed_anchor_count(nodes) -> int:
    """Count unique anchors used by the established four-anchor path."""
    return sum(
        1
        for points in _feed_content_positions(nodes).values()
        if len(points) == 1
    )


def _reddit_feed_structural_evidence(nodes) -> bool:
    """Require Reddit-specific framing for sparse feed continuity.

    Scrolling can hide both the header and bottom labels.  In that state a
    subreddit/user attribution or Reddit's slash-bearing ``Ad`` attribution is
    accepted as structural evidence, but only alongside coherent exact anchor
    motion from an existing trusted proof.
    """
    if proven_home_feed(nodes):
        return True

    visible: list[tuple[str, object]] = []
    for node in nodes or []:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        text = collapsed_text(node.labels())
        if text:
            visible.append((text, node))

    if any(
        text == "search reddit" and 60 <= node.center()[1] <= 190
        for text, node in visible
    ):
        return True

    bottom = {
        text
        for text, node in visible
        if node.center()[1] >= 1040 and text in REDDIT_BOTTOM_MARKERS
    }
    if len(bottom) >= 2:
        return True

    # Attribution OCR may retain the observed bullet plus the existing small
    # punctuation prefix. Strip only those exact characters, then require an
    # anchored Reddit attribution token; prose merely containing ``r/`` or
    # ``u/`` remains insufficient.
    for text, node in visible:
        center_y = node.center()[1]
        if center_y < 80 or center_y > 1260:
            continue
        compact = text.lstrip(")(@ •")
        if (
            _REDDIT_ATTRIBUTION_RE.match(compact)
            or _REDDIT_OCR_PREFIX_ATTRIBUTION_RE.match(text)
            or _REDDIT_OCR_AD_ATTRIBUTION_RE.match(compact)
            or _REDDIT_OCR_ARTIFACT_ATTRIBUTION_RE.match(text)
        ):
            return True
    return False


def _sparse_feed_matches(
    before_nodes,
    after_nodes,
    *,
    moving: bool,
) -> bool:
    """Match two unique anchors on an already trusted sparse feed chain."""
    if not _feed_cleanup_screen_safe(before_nodes):
        return False
    if not _feed_cleanup_screen_safe(after_nodes):
        return False
    if not _reddit_feed_structural_evidence(before_nodes):
        return False
    if not _reddit_feed_structural_evidence(after_nodes):
        return False

    before = _unique_feed_content_positions(
        before_nodes,
        min_text_length=FEED_SPARSE_MIN_TEXT_LENGTH,
    )
    after = _unique_feed_content_positions(
        after_nodes,
        min_text_length=FEED_SPARSE_MIN_TEXT_LENGTH,
    )
    shifts: list[int] = []
    for text in before.keys() & after.keys():
        before_x, before_y = before[text]
        after_x, after_y = after[text]
        if abs(after_x - before_x) > FEED_TRANSITION_MAX_X_DRIFT:
            continue
        shift = after_y - before_y
        if moving:
            if not (
                -FEED_TRANSITION_MAX_SHIFT
                <= shift
                <= -FEED_TRANSITION_MIN_SHIFT
            ):
                continue
        elif abs(shift) > FEED_TRANSITION_SHIFT_TOLERANCE:
            continue
        shifts.append(shift)

    if not moving:
        return len(shifts) >= FEED_SPARSE_MIN_MATCHES
    for expected in shifts:
        if (
            sum(
                abs(observed - expected) <= FEED_TRANSITION_SHIFT_TOLERANCE
                for observed in shifts
            )
            >= FEED_SPARSE_MIN_MATCHES
        ):
            return True
    return False


def _feed_cleanup_screen_safe(nodes) -> bool:
    """Reject Home and protected creation screens without scanning feed posts.

    A strict Reddit Home-feed frame is structurally authoritative.  Generic
    words such as ``Interests`` or ``About you`` can occur in ordinary post or
    advertisement text and must not turn that proven feed into a signup screen.
    Frames without strict feed structure retain the full protected-marker gate.
    """
    if proven_springboard(nodes):
        return False
    if proven_home_feed(nodes):
        return True
    return not protected_reddit_marker(nodes)


def _stable_feed_frame(before_nodes, after_nodes) -> bool:
    """Require four exact anchors at effectively unchanged coordinates."""
    if not _feed_cleanup_screen_safe(after_nodes):
        return False
    before = _feed_content_positions(before_nodes)
    after = _feed_content_positions(after_nodes)
    matches = 0
    for text in before.keys() & after.keys():
        if any(
            abs(after_x - before_x) <= FEED_TRANSITION_MAX_X_DRIFT
            and abs(after_y - before_y) <= FEED_TRANSITION_SHIFT_TOLERANCE
            for before_x, before_y in before[text]
            for after_x, after_y in after[text]
        ):
            matches += 1
            if matches >= FEED_TRANSITION_MIN_MATCHES:
                return True
    return False


def proven_scrolled_feed_transition(
    before_nodes,
    after_nodes,
    *,
    require_proven_before: bool = True,
) -> bool:
    """Prove a coherent upward feed shift using four exact text anchors.

    The default remains strict for standalone callers.  A trusted
    ``FeedCleanupProof`` may opt out of the strict pre-frame classifier while
    advancing a proof chain after Reddit has hidden its bottom navigation.
    """
    if require_proven_before and not proven_home_feed(before_nodes):
        return False
    if not _feed_cleanup_screen_safe(before_nodes):
        return False
    if not _feed_cleanup_screen_safe(after_nodes):
        return False

    before = _feed_content_positions(before_nodes)
    after = _feed_content_positions(after_nodes)
    shifts: list[int] = []
    for text in before.keys() & after.keys():
        accepted = None
        for before_x, before_y in before[text]:
            for after_x, after_y in after[text]:
                shift = after_y - before_y
                if (
                    abs(after_x - before_x) <= FEED_TRANSITION_MAX_X_DRIFT
                    and -FEED_TRANSITION_MAX_SHIFT
                    <= shift
                    <= -FEED_TRANSITION_MIN_SHIFT
                ):
                    accepted = shift
                    break
            if accepted is not None:
                break
        if accepted is not None:
            shifts.append(accepted)

    for expected in shifts:
        if (
            sum(
                abs(observed - expected) <= FEED_TRANSITION_SHIFT_TOLERANCE
                for observed in shifts
            )
            >= FEED_TRANSITION_MIN_MATCHES
        ):
            return True
    return False


def make_feed_cleanup_proof(nodes) -> FeedCleanupProof | None:
    """Create a feed proof only from Reddit's strict Home-feed classifier."""
    if not proven_home_feed(nodes) or not _feed_cleanup_screen_safe(nodes):
        return None
    long_anchor_count = _long_feed_anchor_count(nodes)
    sparse_continuity = long_anchor_count < FEED_TRANSITION_MIN_MATCHES
    if sparse_continuity:
        sparse_anchors = _unique_feed_content_positions(
            nodes,
            min_text_length=FEED_SPARSE_MIN_TEXT_LENGTH,
        )
        if (
            len(sparse_anchors) < FEED_SPARSE_MIN_MATCHES
            or not _reddit_feed_structural_evidence(nodes)
        ):
            # Refuse before the caller makes a gesture: this strict feed frame
            # does not contain enough exact content to continue proving it.
            return None
    return FeedCleanupProof(
        tuple(nodes or ()),
        sparse_continuity=sparse_continuity,
    )


def make_terminal_feed_cleanup_proof(nodes) -> FeedCleanupProof | None:
    """Prove a Reddit content frame for cleanup, never another gesture.

    A video or ad can replace every prior content anchor and hide the bottom
    navigation after one bounded feed flick. Such a frame cannot extend the
    motion chain, but it may safely terminate browsing when it retains
    Reddit-specific structure, at least two unique content anchors, and no
    SpringBoard or protected-signup evidence.
    """
    if not _feed_cleanup_screen_safe(nodes):
        return None
    if not _reddit_feed_structural_evidence(nodes):
        return None
    anchors = _unique_feed_content_positions(
        nodes,
        min_text_length=FEED_SPARSE_MIN_TEXT_LENGTH,
    )
    if len(anchors) < FEED_SPARSE_MIN_MATCHES:
        return None
    return FeedCleanupProof(tuple(nodes or ()), sparse_continuity=True)


def make_created_account_feed_resume_proof(nodes) -> FeedCleanupProof | None:
    """Prove a card feed only for a durably completed-account resume.

    A deeply scrolled Reddit feed can hide its header and bottom navigation.
    The normal classifier must stay strict because the same generic labels can
    occur during signup.  A caller that already owns a durable ``reddit_created``
    checkpoint may use this narrower two-card proof: two well-separated exact
    Join controls plus several unique content anchors, with no SpringBoard,
    login doorway, or prominent onboarding title.
    """
    if proven_springboard(nodes) or day1_sparse_launch_blocker(nodes):
        return None

    prominent_blockers = (
        "Verify your email",
        "Create your username",
        "Set a password",
        "Tell us about yourself",
        "Choose your interests",
        "Customize your feed",
        "Please turn on notifications",
        "Your ads personalization choices",
        "Building your home feed",
    )
    for label in prominent_blockers:
        blocker = find_label_exact(nodes, (label,))
        if blocker is not None and blocker.center()[1] <= 650:
            return None

    join_rows = sorted(
        int(node.center()[1])
        for node in nodes or ()
        if node.ntype != "ocr-line"
        and node.on_screen()
        and node.visible
        and collapsed_text(node.labels()) == "join"
        and 100 <= node.center()[1] <= 1160
    )
    if len(join_rows) < 2 or join_rows[-1] - join_rows[0] < 400:
        return None
    anchors = _unique_feed_content_positions(
        nodes,
        min_text_length=FEED_SPARSE_MIN_TEXT_LENGTH,
    )
    if len(anchors) < FEED_TRANSITION_MIN_MATCHES:
        return None
    return FeedCleanupProof(tuple(nodes or ()), sparse_continuity=True)


def proven_reddit_feed_structure(nodes) -> bool:
    """Recognize a safe Reddit feed frame without requiring old post anchors.

    This is intentionally weaker than a cleanup proof and is suitable only for
    periodic safety checkpoints between already-authorized feed gestures.
    """
    return _feed_cleanup_screen_safe(nodes) and _reddit_feed_structural_evidence(nodes)


def advance_feed_cleanup_proof(
    proof: FeedCleanupProof,
    after_nodes,
) -> FeedCleanupProof | None:
    """Advance a trusted feed chain after a stable frame or upward flick."""
    if not isinstance(proof, FeedCleanupProof):
        return None
    if not _feed_cleanup_screen_safe(after_nodes):
        return None
    before_nodes = proof.reference_nodes
    accepted = (
        _stable_feed_frame(before_nodes, after_nodes)
        or proven_scrolled_feed_transition(
            before_nodes,
            after_nodes,
            require_proven_before=False,
        )
    )
    if not accepted and proof.sparse_continuity:
        accepted = (
            _sparse_feed_matches(before_nodes, after_nodes, moving=False)
            or _sparse_feed_matches(before_nodes, after_nodes, moving=True)
        )
    if not accepted:
        return None
    return FeedCleanupProof(
        tuple(after_nodes or ()),
        sparse_continuity=(
            _long_feed_anchor_count(after_nodes) < FEED_TRANSITION_MIN_MATCHES
        ),
    )


def close_reddit(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    *,
    feed_proof: FeedCleanupProof | None = None,
    on_app_stopped: Callable[[], None] | None = None,
    resume_from_home: bool = False,
) -> bool:
    """Remove Reddit's proven Recents card, clear memory, then prove Home.

    HARDCODED GUARD: never leave or close Reddit while still on a protected
    signup screen (Verify/Username/Password/About/Interests/Building).

    Normal callers must prove the completed Home feed.  A cleanup-only resume
    may set ``resume_from_home`` and proceed only when the fresh precheck is a
    proven SpringBoard frame.  ``on_app_stopped`` is invoked once, after both
    exact-card removal and exact-bundle process termination are confirmed, but
    before the final SpringBoard checkpoint.
    """
    # Pre-close safety dump — refuse if still on signup sheets
    blob0, nodes0 = dump_named(stack, phone, "939_preclose_check", lg, shot=True)
    direct_feed_is_proven = proven_home_feed(nodes0)
    bad = "" if direct_feed_is_proven else protected_reddit_marker(nodes0)
    if bad:
        raise PhoneError(
            f"REFUSE close: still on Reddit signup sheet '{bad}' — return Home only after the proven feed. "
            "Hardcoded guard: never close on Verify/Username/Password/About/Interests/Building. "
            "Finish account creation first. See 939_preclose_check.png. Did not close."
        )
    resumed_from_home = bool(resume_from_home and proven_springboard(nodes0))
    if not resumed_from_home:
        feed_is_proven = direct_feed_is_proven
        if not feed_is_proven and isinstance(feed_proof, FeedCleanupProof):
            feed_is_proven = _stable_feed_frame(feed_proof.reference_nodes, nodes0)
            if not feed_is_proven and feed_proof.sparse_continuity:
                feed_is_proven = _sparse_feed_matches(
                    feed_proof.reference_nodes,
                    nodes0,
                    moving=False,
                )
            if not feed_is_proven:
                fresh_terminal_proof = make_terminal_feed_cleanup_proof(nodes0)
                if isinstance(fresh_terminal_proof, FeedCleanupProof):
                    feed_is_proven = True
                    lg(
                        "Reddit post content changed before cleanup, but the fresh "
                        "terminal Reddit feed was independently proven."
                    )
        if not feed_is_proven:
            raise PhoneError(
                "REFUSE close: the completed Reddit Home feed was not proven. "
                "Did not clear Reddit from memory."
            )
    else:
        lg("Cleanup resume: proven SpringBoard permits exact-card Recents recovery.")
    lg("Close Reddit: open Recents, remove only the proven Reddit card, then prove Home.")
    close_mode = ""
    close_error: PhoneError | None = None
    try:
        stack.open_app_switcher()
        _, switcher_nodes = dump_named(
            stack,
            phone,
            "941_reddit_switcher",
            lg,
            shot=False,
            persist=False,
            preview=False,
        )
        if _screen_signature(nodes0) == _screen_signature(switcher_nodes):
            raise PhoneError(
                "The app-switcher command left Reddit unchanged. Did not flick."
            )
        if proven_springboard(switcher_nodes):
            raise PhoneError(
                "The app-switcher command returned Home instead of Recents. Did not flick."
            )

        card = switcher_app_card(switcher_nodes, "Reddit")
        bundle_only_fallback = False
        if card is None:
            if resumed_from_home:
                lg(
                    "Cleanup resume could not prove Reddit's Recents card; "
                    "using exact-bundle shutdown without moving another card."
                )
                bundle_only_fallback = True
            elif switcher_centered_title(switcher_nodes) is None:
                lg(
                    "Recents title OCR unavailable; using exact-bundle Reddit "
                    "shutdown without swiping an unknown card."
                )
                bundle_only_fallback = True
            else:
                lg("Recents opened with the adjacent card centered; centering Reddit once.")
                stack.interp_flick(650, 700, 250, 700, steps=16, ms=22)
                time.sleep(0.8)
                _, switcher_nodes = dump_named(
                    stack,
                    phone,
                    "941_reddit_centered",
                    lg,
                    shot=False,
                    persist=False,
                    preview=False,
                )
                card = switcher_app_card(switcher_nodes, "Reddit")
                if card is None:
                    raise PhoneError(
                        "Reddit did not become the exact centered Recents card after one "
                        "bounded card swipe. Did not flick an unknown card."
                    )

        if not bundle_only_fallback:
            card_x = card.center()[0]
            lg("Exact Reddit Recents card proven; flicking it away once.")
            stack.interp_flick(card_x, 780, card_x, 60, steps=16, ms=22)
            time.sleep(0.8)
            _, after_nodes = dump_named(
                stack,
                phone,
                "942_after_reddit_flick",
                lg,
                shot=False,
                persist=False,
                preview=False,
            )
            if not switcher_app_removed_or_home(switcher_nodes, after_nodes, "Reddit"):
                raise PhoneError(
                    "Reddit Recents card removal was not confirmed after one exact flick. "
                    "Did not retry."
                )

        # This is the terminal creation cleanup, not a general application
        # shutdown.  Always invoke the exact-bundle force close.  A graceful
        # quit can report success while iOS immediately restores the foreground
        # Reddit process, which produced a false app_stopped checkpoint on the
        # live iPhone X.
        close_mode = stack.force_close_app(REDDIT_BUNDLE)
        if close_mode not in {"force-close", "already-stopped"}:
            raise PhoneError(
                "Reddit exact-bundle force close returned an unverified result."
            )
        if stack.app_is_running(REDDIT_BUNDLE):
            raise PhoneError(
                "Reddit remained running after the exact-bundle force close."
            )
        if bundle_only_fallback:
            lg("Titleless Recents fallback force-closed only the Reddit bundle.")
        else:
            lg("Reddit Recents card removed; exact-bundle force close verified stopped.")
    except PhoneError as exc:
        close_error = exc

    home_error: PhoneError | None = None
    home_verified = False
    try:
        stack.press_home()
        try:
            _, final_home_nodes = dump_named(
                stack,
                phone,
                "943_home_after_close",
                lg,
                shot=False,
                persist=False,
                preview=False,
            )
            home_verified = proven_springboard(final_home_nodes)
        except PhoneError as exc:
            lg(f"Final Home diagnostic dump failed: {exc}")
    except PhoneError as exc:
        home_error = exc

    # A foreground process can be relaunched while iOS is leaving Recents.
    # Recheck after the Home transition.  Recover exactly once by force-closing
    # only Reddit and issuing one additional Home command; never checkpoint a
    # stopped app before this postcondition is proven.
    if close_error is None:
        try:
            if stack.app_is_running(REDDIT_BUNDLE):
                lg(
                    "Reddit restarted during the Home transition; force-closing "
                    "the exact bundle once more before one bounded Home retry."
                )
                retry_mode = stack.force_close_app(REDDIT_BUNDLE)
                if retry_mode not in {"force-close", "already-stopped"}:
                    raise PhoneError(
                        "Reddit Home-transition force close returned an unverified result."
                    )
                stack.press_home()
                _, retry_home_nodes = dump_named(
                    stack,
                    phone,
                    "943_home_after_force_retry",
                    lg,
                    shot=False,
                    persist=False,
                    preview=False,
                )
                home_verified = proven_springboard(retry_home_nodes)
                if stack.app_is_running(REDDIT_BUNDLE):
                    raise PhoneError(
                        "Reddit restarted after the bounded exact-bundle force-close retry."
                    )
        except PhoneError as exc:
            close_error = exc

    if close_error is not None:
        lg("CLOSE_REDDIT=HOME_ONLY")
        raise close_error
    if home_error is not None or not home_verified:
        lg("CLOSE_REDDIT=CLOSED_HOME_UNVERIFIED")
        raise PhoneError(
            "Reddit card was removed and memory cleared, but the final SpringBoard "
            "screen was not verified."
        ) from home_error
    if on_app_stopped is not None:
        try:
            on_app_stopped()
        except Exception as exc:
            raise PhoneError(
                "Reddit card, process, and Home were verified, but the app-stopped "
                "checkpoint could not be saved."
            ) from exc
    lg(f"CLOSE_REDDIT=OK mode={close_mode}")
    return True


def springboard_reddit(nodes):
    """Home-screen Reddit label, not Ghost title / status."""
    hits = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen() or not n.visible:
            continue
        if collapsed_text(n.labels()) != "reddit":
            continue
        # App titles/navigation live near the top. SpringBoard icon labels on
        # the supported layouts start below this band (real iPhone X: y=1015).
        # Live iPhone-X switcher title: raw y=163.  SpringBoard app labels
        # begin below this title band (first icon rows ~235; Reddit y=1015).
        if n.y < 220 or n.y > 1280:
            continue
        hits.append(n)
    if not hits:
        return None
    return min(hits, key=lambda n: abs(n.y - 900))


def tap_home_icon(stack: TapStack, node) -> None:
    """SpringBoard OCR is the name under the icon. Tap the icon, not the text."""
    x = node.center()[0]
    y = max(80, node.y - 48)
    stack.tap_xy(x, y)


def reddit_icon_point(node) -> tuple[int, int]:
    """Return the Reddit icon point from its SpringBoard text label."""
    return node.center()[0], max(80, node.y - 48)


def long_press_home_icon(stack: TapStack, node, lg: LogFn) -> None:
    """Open Reddit's haptic menu from a freshly detected SpringBoard icon."""
    x, y = reddit_icon_point(node)
    lg(
        f"Reddit icon label [{node.x},{node.y} {node.w}x{node.h}] "
        f"long-press icon ({x},{y}) hold=650ms"
    )
    stack.long_tap(x, y, hold_ms=650)


def find_picker_row(nodes, name: str, *, min_y: int = 0):
    """Crane slot entry, optionally restricted below a navigation/header band."""
    exact = {
        collapsed_text(key)
        for key in name_keys(name)
        if collapsed_text(key)
    }
    exact_nodes = [
        n
        for n in nodes
        if n.ntype != "ocr-line"
        and n.on_screen()
        and n.visible
        and n.y >= min_y
        and collapsed_text(n.labels()) in exact
    ]
    if exact_nodes:
        return min(exact_nodes, key=lambda n: n.y)
    want = name_to_index(name)
    if want is None:
        return None
    best: tuple[bool, object] | None = None
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen() or not n.visible or n.y < min_y:
            continue
        t = collapsed_text(n.labels())
        if len(re.sub(r"\s+", "", t)) < 2:
            continue
        if loose_index(t) != want:
            continue
        has_alpha = any(c.isalpha() for c in t)
        if best is None or (has_alpha and not best[0]) or (
            has_alpha == best[0] and n.y < best[1].y
        ):
            best = (has_alpha, n)
    if best is not None:
        log(f"{name} OCR read as {collapsed_text(best[1].labels())!r} -> tap that row")
        return best[1]
    return None


def find_container_list_row(nodes, name: str):
    """Final selectable Crane row, never the top current-container duplicate."""
    # A newly provisioned a0001 may coexist with older canonical duplicates.
    # Crane is chronological, so the bottommost body row is the newest one.
    # Keep this rule local to a0001; all other slots retain their established
    # exact/zero-tolerant selection behavior.
    if name_to_index(name) == 1:
        exact = {collapsed_text(value) for value in name_keys(name)}
        exact_rows = [
            node
            for node in nodes
            if node.ntype != "ocr-line"
            and node.on_screen()
            and node.visible
            and node.y >= CRANE_LIST_BODY_MIN_Y
            and collapsed_text(node.labels()) in exact
        ]
        if exact_rows:
            return max(exact_rows, key=lambda node: node.y)

    named = find_picker_row(nodes, name, min_y=CRANE_LIST_BODY_MIN_Y)
    if named is not None:
        return named

    # Current installation migration: the newly-created a0001 is rendered by
    # Crane as a second bare ``1`` after the older legacy rows ``1, 2, 3`` and
    # immediately before canonical ``a0002``.  Bare numbers remain forbidden
    # everywhere else.  Require this complete, exact, title-proven ordering so
    # a Calendar number, badge, keyboard key, or the first legacy ``1`` can
    # never authorize a tap.
    if name_to_index(name) != 1:
        return None
    title = find_label_exact(nodes, ("Container",))
    if title is None or title.center()[1] > CRANE_LIST_BODY_MIN_Y:
        return None
    next_row = find_picker_row(nodes, "a0002", min_y=CRANE_LIST_BODY_MIN_Y)
    if next_row is None:
        return None

    numeric_rows = []
    for node in nodes:
        if (
            node.ntype == "ocr-line"
            or not node.on_screen()
            or not node.visible
            or node.y < CRANE_LIST_BODY_MIN_Y
            or node.y >= next_row.y
        ):
            continue
        text = collapsed_text(node.labels())
        if text in {"1", "2", "3"}:
            numeric_rows.append((node.y, text, node))
    numeric_rows.sort(key=lambda item: item[0])
    numeric_text = [text for _y, text, _node in numeric_rows]
    if (
        len(numeric_text) < 4
        or numeric_text[:3] != ["1", "2", "3"]
        or any(text != "1" for text in numeric_text[3:])
    ):
        return None
    migrated = numeric_rows[-1][2]
    gap = next_row.y - (migrated.y + migrated.h)
    if gap < 0 or gap > 120:
        return None
    log("a0001 matched the proven migrated Crane row immediately before a0002")
    return migrated


def _bound_container_selection_required(name: str, slot: dict | None = None) -> bool:
    """Whether Crane must select the newest exact row at a proven list bottom.

    The persisted Ghost instance UUID is not rendered in Crane.  Its presence
    therefore acts only as a durable signal that this slot was provisioned by
    the duplicate-safe flow and that Crane's newest appended row is required.
    Migrated a0001 is deliberately excluded because that installation can
    still render its selected container as the specially proven bare ``1``.
    """
    if slot is None:
        slot = get_slot(name)
    row = slot or {}
    if row.get("crane_selection_policy") == "newest_exact":
        return True
    if name_to_index(name) == 1:
        return False
    return bool(str(row.get("ghost_container_instance") or "").strip())


def _bottommost_exact_alpha_row(nodes, name: str):
    """Return the unique bottommost exact canonical row, plus an error kind.

    Bottommost ordering deterministically distinguishes chronological Crane
    duplicates.  If two distinct nodes occupy the same bottom row, ordering
    alone cannot identify the intended tap and the caller must stop.
    """
    index = name_to_index(name)
    if index is None:
        return None, "absent"
    wanted = collapsed_text(index_to_name(index))
    rows = [
        node
        for node in nodes
        if node.ntype != "ocr-line"
        and node.on_screen()
        and node.visible
        and node.y >= CRANE_LIST_BODY_MIN_Y
        and any(char.isalpha() for char in collapsed_text(node.labels()))
        and collapsed_text(node.labels()) == wanted
    ]
    if not rows:
        return None, "absent"
    bottom_y = max(node.center()[1] for node in rows)
    bottom = [node for node in rows if node.center()[1] == bottom_y]
    if len(bottom) != 1:
        return None, "ambiguous"
    return bottom[0], ""


def _visible_newest_exact_row(nodes, name: str):
    """Return a uniquely visible exact row when it is already the newest row.

    This is the safe no-scroll path for a freshly appended container.  It is
    deliberately narrower than ordinary name matching: the canonical alpha
    label must occur exactly once, have the greatest visible slot index, and
    be the lowest visible Crane row.  Otherwise the existing duplicate-safe
    bottom scan remains responsible for resolving the target.
    """
    wanted = name_to_index(name)
    if wanted is None:
        return None
    exact = []
    visible_rows = []
    canonical = collapsed_text(index_to_name(wanted))
    for text, node in r_nodes(nodes):
        if node.y < CRANE_LIST_BODY_MIN_Y:
            continue
        index = _crane_slot_index(text)
        if index is None:
            continue
        visible_rows.append((index, node))
        if collapsed_text(node.labels()) == canonical:
            exact.append(node)
    if len(exact) != 1 or not visible_rows:
        return None
    row = exact[0]
    if wanted != max(index for index, _node in visible_rows):
        return None
    if row.center()[1] != max(node.center()[1] for _index, node in visible_rows):
        return None
    return row


def _bottommost_exact_in_complete_visible_prefix(nodes, name: str):
    """Resolve a row when Crane renders its complete compact prefix at once.

    On the iPhone X, Crane's non-haptic second level can render every canonical
    row from a0001 through a later sentinel without a scroll view.  Dragging
    that compact menu dismisses it.  A gap-free prefix beginning at a0001 and
    containing a later canonical row bounds the requested row in the visible
    menu, so its bottommost exact occurrence is deterministic.
    """
    wanted = name_to_index(name)
    if wanted is None:
        return None, "unproven"
    visible = normalized_slot_window(nodes)
    indices = [name_to_index(value) for value in visible]
    if not indices or any(value is None for value in indices):
        return None, "unproven"
    index_set = {int(value) for value in indices if value is not None}
    maximum = max(index_set)
    # OCR node order is not stable in Crane's compact multi-level menu.  Prove
    # completeness by the canonical ID set, not by the order in which OCR
    # happened to return nodes.
    if index_set != set(range(1, maximum + 1)) or maximum <= wanted:
        return None, "unproven"
    row, error = _bottommost_exact_alpha_row(nodes, name)
    return row, error or ""


def _crane_slot_index(text: str) -> int | None:
    """Crane-only OCR identity: require a letter before the slot digits.

    Shared slot parsing intentionally accepts legacy bare numbers, but Crane
    list proof must not turn SpringBoard dates, Clock text, or keyboard keys
    into containers.
    """
    compact = re.sub(r"\s+", "", (text or "")).lower()
    if not compact or not compact[0].isalpha():
        return None
    return loose_index(compact)


def r_nodes(nodes):
    out = []
    for n in nodes:
        if n.ntype == "ocr-line":
            continue
        if not n.on_screen():
            continue
        t = collapsed_text(n.labels()).lower()
        # Crane OCR can collapse a0002 to a2; require an alphabetic prefix so
        # an unrelated bare number never becomes list proof.
        if _crane_slot_index(t) is not None:
            out.append((t, n))
    out.sort(key=lambda t: (t[0], t[1].y))
    return out


def crane_container_list(nodes) -> bool:
    """True only for a proven Crane container list.

    A known title is sufficient only in the navigation/header band.  A low
    singular ``Container`` can be a haptic-parent doorway and must not turn a
    same-name current-slot row into a selectable list.  Without a header
    title, require at least two different slot IDs so an unrelated digit
    cannot authorize scrolling.
    """
    # Reddit content/user/product labels can legitimately end in digits and
    # satisfy the title-less slot fallback.  Feed structure is stronger,
    # app-specific evidence and must never authorize Crane list taps/scrolls.
    if proven_home_feed(nodes):
        return False

    # Hide My Email can contain several exact aNNNN labels from previously
    # created aliases.  Those labels are account metadata, not Crane rows, and
    # must never authorize an in-place container-list scroll.
    foreign_title = find_label_exact(nodes, HIDE_MY_EMAIL_TITLE)
    if (
        foreign_title is not None
        and foreign_title.center()[1] < CRANE_LIST_BODY_MIN_Y
    ):
        return False

    title = find_label_exact(nodes, CRANE_LIST_TITLES)
    if title is not None:
        return title.center()[1] < CRANE_LIST_BODY_MIN_Y
    indices = {
        loose_index(text)
        for text, _node in r_nodes(nodes)
        if loose_index(text) is not None
    }
    return len(indices) >= 2


def compact_crane_target_row(nodes, name: str):
    """Return an exact row from Crane's fully proven compact picker.

    The compact long-press layout is not a normal Crane list: its ``Container``
    heading sits in the body band and the iOS ``Remove App`` OCR is frequently
    corrupted.  Do not relax :func:`crane_container_list` for that layout.
    Instead require one of the two complete, target-aware orderings observed
    on-device::

        Container -> Default -> Default -> aNNNN -> New Container -> Settings
        Container -> aNNNN(active) -> Default -> NNNN -> New Container -> Settings

    The second layout can OCR the selectable row either as ``aNNNN`` or as the
    bare zero-padded suffix ``NNNN``.  A bare number is accepted *only* in this
    fully anchored paired layout, strictly below the matching canonical active
    row and ``Default``.  It never becomes a general Crane row identity.

    ``New Container`` proves the visible list bottom.  The returned node is
    always the lower selectable row, never the active-container label.
    """
    if find_label_exact(nodes, HIDE_MY_EMAIL_TITLE) is not None:
        return None
    wanted = name_to_index(name)
    if wanted is None:
        return None
    canonical = collapsed_text(index_to_name(wanted))
    visible = [
        node
        for node in nodes
        if node.ntype != "ocr-line" and node.on_screen() and node.visible
    ]

    def exact(label: str):
        normalized = collapsed_text(label)
        return [
            node
            for node in visible
            if collapsed_text(node.labels()) == normalized
        ]

    headings = exact("Container")
    defaults = sorted(exact("Default"), key=lambda node: node.center()[1])
    new_rows = exact("New Container")
    settings_rows = exact("Settings")
    if not (
        len(headings) == 1
        and len(new_rows) == 1
        and len(settings_rows) == 1
    ):
        return None

    heading = headings[0]
    new_container = new_rows[0]
    settings = settings_rows[0]
    heading_y = heading.center()[1]
    new_y = new_container.center()[1]
    settings_y = settings.center()[1]
    if not heading_y < new_y < settings_y:
        return None

    canonical_rows = exact(canonical)
    # Slot-shaped text anywhere inside the anchored compact menu participates
    # in the proof, including rows above/between Defaults and the band between
    # New Container and Settings.  This prevents an otherwise valid suffix of
    # the menu from hiding an extra or wrong container row earlier or later.
    menu_alpha_rows = [
        (index, node)
        for text, node in r_nodes(visible)
        if heading_y < node.center()[1] < settings_y
        and (index := _crane_slot_index(text)) is not None
    ]
    menu_numeric_rows = [
        node
        for node in visible
        if heading_y < node.center()[1] < settings_y
        and (text := collapsed_text(node.labels())).isdigit()
        and len(text) >= 4
    ]

    # Original observed layout: two Default anchors followed by one unique,
    # canonical selectable row.  Keep this branch exact and letter-prefixed.
    if (
        len(defaults) == 2
        and len(canonical_rows) == 1
        and not exact(canonical[1:])
    ):
        first_default, second_default = defaults
        target = canonical_rows[0]
        ordered_y = [
            heading_y,
            first_default.center()[1],
            second_default.center()[1],
            target.center()[1],
            new_y,
            settings_y,
        ]
        if ordered_y != sorted(ordered_y) or len(set(ordered_y)) != len(ordered_y):
            return None
        if (
            len(menu_alpha_rows) != 1
            or menu_alpha_rows[0][0] != wanted
            or menu_alpha_rows[0][1] is not target
            or menu_numeric_rows
        ):
            return None
        return target

    # Current observed layout: the active canonical row is above Default and
    # the selectable row is below it.  OCR may drop only the leading Crane
    # letter from the selectable row; no other bare numeric label is trusted.
    if len(defaults) != 1:
        return None
    default = defaults[0]
    default_y = default.center()[1]
    if not heading_y < default_y < new_y:
        return None

    active_rows = [
        node
        for node in canonical_rows
        if heading_y < node.center()[1] < default_y
    ]
    selectable_labels = {canonical, canonical[1:]}
    selectable_rows = [
        node
        for node in visible
        if default_y < node.center()[1] < new_y
        and collapsed_text(node.labels()) in selectable_labels
    ]
    if len(active_rows) != 1 or len(selectable_rows) != 1:
        return None
    active = active_rows[0]
    target = selectable_rows[0]
    if active is target:
        return None
    target_label = collapsed_text(target.labels())
    bare_rows = exact(canonical[1:])
    if target_label == canonical:
        if len(canonical_rows) != 2 or bare_rows:
            return None
    elif (
        target_label != canonical[1:]
        or len(canonical_rows) != 1
        or len(bare_rows) != 1
        or bare_rows[0] is not target
    ):
        return None

    # Reject another letter-prefixed slot anywhere in the compact Crane body.
    # For the lower band also reject any other four-or-more digit value: inside
    # these exact anchors it is another possible slot, not safe menu noise.
    expected_alpha_nodes = {id(active)}
    if target_label == canonical:
        expected_alpha_nodes.add(id(target))
    if (
        any(index != wanted for index, _node in menu_alpha_rows)
        or {id(node) for _index, node in menu_alpha_rows} != expected_alpha_nodes
    ):
        return None
    if menu_numeric_rows and (
        len(menu_numeric_rows) != 1
        or menu_numeric_rows[0] is not target
        or target_label != canonical[1:]
    ):
        return None
    return target


def crane_submenu_marker(nodes):
    """Return only an exact, allow-listed Crane haptic submenu marker."""
    return find_label_exact(nodes, CRANE_SUBMENU_LABELS)


def _decorated_container_parent_marker(nodes):
    """Find the singular Container row when OCR prefixes a chevron/glyph.

    Crane's compact haptic parent is often read with a leading chevron glyph
    (for example ``"~ Container"``) rather than an exact label.  The leading
    decoration is not part of the row identity; ignore only non-letters before
    the word.
    """
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen() or not node.visible:
            continue
        text = collapsed_text(node.labels()).strip()
        if not text or text == "container":
            continue
        if re.sub(r"^[^a-z]+", "", text).strip() == "container":
            return node
    return None


def crane_singular_parent_marker(nodes):
    """Exact singular Container doorway, only on a proven iOS haptic menu."""
    if haptic_system_action(nodes) is None:
        return None
    marker = find_label_exact(nodes, ("Container", "> Container"))
    if marker is None:
        marker = _decorated_container_parent_marker(nodes)
    if marker is None:
        return None
    _cx, cy = marker.center()
    return marker if 300 <= cy <= 900 else None


def haptic_system_action(nodes):
    """Recognize iOS haptic actions for menu-state checks; never tap them."""
    return find_label_exact(nodes, HAPTIC_SYSTEM_ACTIONS)


def crane_haptic_parent(nodes) -> bool:
    """The first long-press surface, even if it also looks like a list.

    Crane can show the requested/current slot and a Containers label beside
    iOS system actions. Those slot rows are doorways to the real list, not
    proof that a container was selected.
    """
    return haptic_system_action(nodes) is not None


def _crane_safe_summary(nodes) -> str:
    """Return allow-listed menu evidence without exposing arbitrary OCR."""
    allowed = {collapsed_text(label) for label in CRANE_SUBMENU_LABELS}
    markers = set()
    slots = set()
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        text = collapsed_text(n.labels())
        if text in allowed:
            markers.add(text)
    for text, _node in r_nodes(nodes):
        idx = loose_index(text)
        if idx is not None:
            slots.add(index_to_name(idx))
    return (
        "CRANE_MARKERS="
        + (",".join(sorted(markers)) if markers else "(none)")
        + " CRANE_SLOTS="
        + (_visible_ids_text(slots) if slots else "(none)")
        + " SYSTEM_HAPTIC="
        + ("yes" if haptic_system_action(nodes) is not None else "no")
    )


def log_crane_summary(nodes, lg: LogFn, stage: str) -> None:
    """Log only allow-listed Crane markers and normalized slot IDs."""
    lg(f"{stage} {_crane_safe_summary(nodes)}")


def crane_parent_entry(nodes, name: str, *, require_list: bool = False):
    """Choose one safe parent-menu entry and describe why it is allowed.

    A single slot different from ``name`` is accepted only when an exact iOS
    haptic action proves this is the long-press menu. It is a doorway to the
    real list, never proof that the requested container was selected.
    """
    marker = crane_submenu_marker(nodes)
    if marker is not None:
        return "marker", marker, ""

    # A persisted Ghost instance must be resolved from the chronological list
    # itself.  Only an explicit Crane submenu marker (including the singular
    # marker on a proven iOS haptic menu) can safely open that list; an
    # identically named parent row could be an older duplicate and must not be
    # used as a direct selection shortcut.
    if require_list:
        singular = crane_singular_parent_marker(nodes)
        if singular is not None:
            return "container-marker", singular, ""
        return "unsafe", None, ""

    requested = find_picker_row(nodes, name)
    if requested is not None:
        return "requested-slot", requested, name

    singular = crane_singular_parent_marker(nodes)
    if singular is not None:
        return "container-marker", singular, ""

    slot_nodes = {}
    for text, node in r_nodes(nodes):
        idx = loose_index(text)
        if idx is None:
            continue
        sid = index_to_name(idx)
        slot_nodes.setdefault(sid, node)
    if len(slot_nodes) == 1 and haptic_system_action(nodes) is not None:
        sid, node = next(iter(slot_nodes.items()))
        return "current-slot", node, sid

    return "unsafe", None, ""


def stock_haptic_without_crane_doorway(nodes, name: str) -> bool:
    """Prove the stock iOS app menu has no safe Crane-list doorway.

    This predicate authorizes no tap.  It exists only to route an
    instance-bound slot to Ghost's UUID-aware selector when a correct Reddit
    icon long-press exposes the stock iOS actions but no Crane entry at all.
    """
    if haptic_system_action(nodes) is None or protected_reddit_marker(nodes):
        return False
    if compact_crane_target_row(nodes, name) is not None:
        return False
    _kind, doorway, _entry_slot = crane_parent_entry(
        nodes, name, require_list=True
    )
    return doorway is None


def _haptic_container_option_row(nodes, name: str):
    """Resolve the selectable row in Crane's compact haptic second level.

    This layout repeats the active slot immediately below ``Container`` and
    then renders the actual choices between exact ``Default`` and
    ``New Container`` sentinels.  Treating both same-name labels as ordinary
    rows makes an instance-bound resume look ambiguous.  The two sentinels,
    an iOS system action, and one exact canonical row strictly between them
    identify the selectable choice without relying on a stale coordinate.
    """
    if haptic_system_action(nodes) is None:
        return None
    default = find_label_exact(nodes, ("Default",))
    new_container = find_label_exact(nodes, ("New Container",))
    if default is None or new_container is None:
        return None
    lower = default.center()[1]
    upper = new_container.center()[1]
    if lower >= upper:
        return None
    wanted = name_to_index(name)
    if wanted is None:
        return None
    candidates = [
        node
        for text, node in r_nodes(nodes)
        if lower < node.center()[1] < upper
        and _crane_slot_index(text) == wanted
        and collapsed_text(node.labels()) == collapsed_text(index_to_name(wanted))
    ]
    return candidates[0] if len(candidates) == 1 else None


def slow_list_nudge(stack: TapStack) -> None:
    """Short slow scroll on Ghost/Crane container picker. Not Ghost Home."""
    stack.interp_flick(375, 920, 375, 780, steps=18, ms=30)


def slow_list_to_older(stack: TapStack) -> None:
    """Short overlapping reverse scan from Crane bottom toward older rows."""
    stack.interp_flick(375, 780, 375, 920, steps=18, ms=30)


def fast_list_to_bottom(stack: TapStack) -> None:
    """Move a proven Crane list toward its newest rows with one long swipe."""
    # Keep both endpoints inside the list body, but use nearly the full safe
    # vertical range so a newly appended container is reached without walking
    # through each intermediate window.
    stack.interp_flick(375, 1120, 375, 260, steps=20, ms=18)


def normalized_slot_window(nodes) -> tuple[str, ...]:
    """Visible Crane slot IDs in row order, normalized without raw OCR."""
    rows = []
    for text, node in r_nodes(nodes):
        idx = loose_index(text)
        if idx is not None:
            rows.append((node.y, index_to_name(idx)))
    rows.sort(key=lambda item: item[0])
    out = []
    for _y, sid in rows:
        if sid not in out:
            out.append(sid)
    return tuple(out)


def _list_search_state(nodes):
    """Stable movement proof: full safe signature plus normalized slot window."""
    return _screen_signature(nodes), normalized_slot_window(nodes)


def _visible_ids_text(ids) -> str:
    return ",".join(sorted(ids, key=lambda sid: name_to_index(sid) or 0)) or "(none)"


def warn_siwa(nodes, lg: LogFn) -> None:
    blob = " ".join(collapsed_text(n.labels()) for n in nodes if n.on_screen())
    for bad in SIWA:
        if bad in blob:
            lg("SIWA on screen. Do not tap Continue with Apple / Sign in with Apple.")
            return


def _find_reddit_from_home(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    stem: str,
):
    """Return Reddit from a fresh transient SpringBoard dump without tapping."""
    stack.press_home()
    time.sleep(0.7)
    _blob, nodes = _transient_dump(stack, phone, f"{stem}_springboard", lg)
    icon = springboard_reddit(nodes)
    if icon is None:
        # Widget / Today view: Home again, then page-swipe.
        stack.press_home()
        time.sleep(0.6)
        _blob, nodes = _transient_dump(stack, phone, f"{stem}_home_retry", lg)
        icon = springboard_reddit(nodes)
    page = 0
    while icon is None and page < 4:
        lg(f"Reddit icon not on this page -> swipe next ({page + 1}/4)")
        stack.interp_flick(640, 700, 110, 700, steps=14, ms=22)
        time.sleep(0.6)
        _blob, nodes = _transient_dump(stack, phone, f"{stem}_page_{page}", lg)
        icon = springboard_reddit(nodes)
        page += 1
    if icon is None:
        raise PhoneError("Reddit icon not on SpringBoard after 4 pages. Did not uiopen (skips picker).")
    return icon


def _transient_dump(stack: TapStack, phone: Phone, stem: str, lg: LogFn):
    """Read SpringBoard/Crane UI without saving or previewing private labels."""
    return dump_named(
        stack,
        phone,
        stem,
        lg,
        persist=False,
        preview=False,
    )


def _container_view_continues(nodes) -> bool:
    """Require every post-scroll frame to remain a proven Crane list."""
    return crane_container_list(nodes) and not crane_haptic_parent(nodes)


def _locate_bound_container_from_bottom(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    name: str,
    nodes,
    *,
    allow_complete_visible_prefix: bool = False,
):
    """Resolve a provisioned duplicate by scanning backward from proven bottom."""
    visible_newest = _visible_newest_exact_row(nodes, name)
    if visible_newest is not None:
        lg(
            f"{name} is the unique exact newest visible Crane row; "
            "selecting it immediately without a scroll"
        )
        return visible_newest
    if allow_complete_visible_prefix:
        anchored, anchored_error = _bottommost_exact_in_complete_visible_prefix(
            nodes, name
        )
        if anchored_error == "ambiguous":
            raise PhoneError(
                f"Crane compact list has an ambiguous bottommost exact {name} row. "
                "Did not choose between indistinguishable duplicates."
            )
        if anchored is not None:
            visible = normalized_slot_window(nodes)
            maximum = max(
                name_to_index(value) or 0 for value in visible
            )
            lg(
                f"{name} matched the bottommost exact row in Crane's complete "
                f"visible ID set a0001 through {index_to_name(maximum)} "
                "(compact-prefix-v2); no dismissing scroll was needed"
            )
            return anchored
    initial_state = _list_search_state(nodes)
    previous_scrolled_state = None
    seen_ids = set(initial_state[1])

    # Do not inspect a matching row until the bottom is proven.  An exact row
    # visible earlier in the journey can be an older same-name container.
    for jump in range(6):
        CREATION_PROGRESS.note()
        lg(f"{name} is instance-bound -> prove Crane bottom {jump + 1}/6")
        fast_list_to_bottom(stack)
        time.sleep(0.65)
        _blob, nodes = _transient_dump(
            stack, phone, f"811_bound_bottom_{jump}", lg
        )
        if not _container_view_continues(nodes):
            raise PhoneError(
                "Crane list disappeared while proving the stable bottom for "
                f"{name}. Did not select a container."
            )
        state = _list_search_state(nodes)
        seen_ids.update(state[1])
        # Two identical *post-swipe* frames prove the long gesture has reached
        # a stable bottom.  Comparing against the pre-swipe frame required an
        # unnecessary third swipe on every normal run.
        if state == previous_scrolled_state:
            break
        previous_scrolled_state = state

    else:
        raise PhoneError(
            f"Could not prove a stable Crane list bottom for instance-bound {name}. "
            f"Visible normalized slots: {_visible_ids_text(seen_ids)}. "
            "Did not select a visible older duplicate."
        )

    # The target can have later-created slots beneath it on future runs.  From
    # the proven absolute bottom, inspect the current window and then move only
    # toward older rows.  The first exact match encountered is therefore the
    # globally bottommost/newest duplicate.  Short reverse gestures keep the
    # windows overlapping so no exact row can be skipped between frames.
    previous = _list_search_state(nodes)
    unchanged = 0
    for scan in range(25):
        CREATION_PROGRESS.note()
        if scan:
            lg(f"{name} not in bottom window -> scan older {scan}/24")
            slow_list_to_older(stack)
            time.sleep(0.55)
            _blob, nodes = _transient_dump(
                stack, phone, f"811_bound_older_{scan - 1}", lg
            )
            if not _container_view_continues(nodes):
                raise PhoneError(
                    "Crane list disappeared during the instance-bound reverse scan. "
                    "Did not select a container."
                )

        named, error_kind = _bottommost_exact_alpha_row(nodes, name)
        if error_kind == "ambiguous":
            raise PhoneError(
                f"Crane reverse scan has an ambiguous bottommost exact {name} row. "
                "Did not choose between indistinguishable duplicates."
            )
        if named is not None:
            lg(
                f"{name} matched the first bottommost exact row while scanning "
                "from the proven stable Crane bottom"
            )
            return named
        if not scan:
            continue

        state = _list_search_state(nodes)
        seen_ids.update(state[1])
        unchanged = unchanged + 1 if state == previous else 0
        previous = state
        if unchanged >= 2:
            raise PhoneError(
                f"Crane was scanned from its stable bottom to a stable top without "
                f"an exact alpha {name} row. Visible normalized slots: "
                f"{_visible_ids_text(seen_ids)}. Did not select a legacy numeric row."
            )

    raise PhoneError(
        f"Could not complete the bottom-to-top Crane scan for instance-bound {name}. "
        f"Visible normalized slots: {_visible_ids_text(seen_ids)}. "
        "Did not select a visible older duplicate."
    )


def _locate_container_in_list(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    name: str,
    nodes,
    *,
    newest_bound: bool = False,
    allow_complete_visible_prefix: bool = False,
):
    """Find a slot with bounded scrolling after the Crane list is proven."""
    if not crane_container_list(nodes):
        raise PhoneError("Refusing to scroll: current screen is not a proven Crane container list.")

    log_crane_summary(nodes, lg, "Crane list")
    if newest_bound:
        return _locate_bound_container_from_bottom(
            stack,
            phone,
            lg,
            name,
            nodes,
            allow_complete_visible_prefix=allow_complete_visible_prefix,
        )

    named = find_container_list_row(nodes, name)
    if named is not None:
        return named

    seen_ids = set(normalized_slot_window(nodes))

    # New Ghost containers are appended to Crane's ordered list.  Check the
    # bottom first with the same full-range gesture used by Crane restore so a
    # normal create run does not walk through every earlier slot.  Every move
    # is still followed by a fresh proven-list dump and only the exact body row
    # can authorize the eventual tap.
    previous = _list_search_state(nodes)
    unchanged = 0
    for jump in range(4):
        CREATION_PROGRESS.note()
        lg(f"{name} not in Crane list -> fast bottom {jump + 1}/4")
        fast_list_to_bottom(stack)
        time.sleep(0.65)
        _blob, nodes = _transient_dump(stack, phone, f"811_bottom_{jump}", lg)
        if not _container_view_continues(nodes):
            raise PhoneError(
                "Crane list disappeared during fast-bottom search. Did not keep scrolling."
            )
        named = find_container_list_row(nodes, name)
        if named is not None:
            lg(f"{name} HIT after fast bottom {jump + 1}")
            return named
        state = _list_search_state(nodes)
        seen_ids.update(state[1])
        unchanged = unchanged + 1 if state == previous else 0
        previous = state
        if unchanged >= 2:
            lg(f"{name} not visible at Crane bottom -> bounded full-scan fallback")
            break

    # Establish one deterministic boundary first. The old numeric heuristic
    # sent a0004 toward the top forever even though it followed visible a0003.
    # This is now only a compatibility fallback for an older/resume slot that
    # is not present in the newest rows reached by the fast-bottom pass.
    previous = _list_search_state(nodes)
    unchanged = 0
    top_confirmed = False
    for jump in range(4):
        CREATION_PROGRESS.note()
        lg(f"{name} not in Crane list -> calibrate top {jump + 1}/4")
        stack.interp_flick(375, 350, 375, 1050, steps=14, ms=22)
        time.sleep(0.6)
        _blob, nodes = _transient_dump(stack, phone, f"811_top_{jump}", lg)
        if not _container_view_continues(nodes):
            raise PhoneError("Crane list disappeared during bounded search. Did not keep scrolling.")
        named = find_container_list_row(nodes, name)
        if named is not None:
            lg(f"{name} HIT during top calibration {jump + 1}")
            return named
        state = _list_search_state(nodes)
        seen_ids.update(state[1])
        unchanged = unchanged + 1 if state == previous else 0
        previous = state
        if unchanged >= 2:
            top_confirmed = True
            break

    if not top_confirmed:
        raise PhoneError(
            f"Could not confirm the top of Crane's container list while looking for {name}. "
            f"Visible normalized slots: {_visible_ids_text(seen_ids)}. Did not scan or guess."
        )

    # From the proven top, move the content upward so later rows appear.
    previous = _list_search_state(nodes)
    unchanged = 0
    for nudge in range(24):
        CREATION_PROGRESS.note()
        lg(f"{name} not in Crane list -> scan forward {nudge + 1}/24")
        slow_list_nudge(stack)
        time.sleep(0.55)
        _blob, nodes = _transient_dump(stack, phone, f"811_scroll_{nudge}", lg)
        if not _container_view_continues(nodes):
            raise PhoneError("Crane list disappeared during bounded search. Did not keep scrolling.")
        named = find_container_list_row(nodes, name)
        if named is not None:
            return named
        state = _list_search_state(nodes)
        seen_ids.update(state[1])
        unchanged = unchanged + 1 if state == previous else 0
        previous = state
        if unchanged >= 2:
            raise PhoneError(
                f"Crane list reached a stable boundary without {name}. "
                f"Visible normalized slots: {_visible_ids_text(seen_ids)}. "
                "Ghost may contain it, but Crane does not expose it."
            )

    raise PhoneError(
        f"Crane list was scanned top-to-bottom without {name}. "
        f"Visible normalized slots: {_visible_ids_text(seen_ids)}. "
        "Ghost may contain it, but Crane does not expose it."
    )


def _tap_requested_container(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    name: str,
    nodes,
    *,
    newest_bound: bool = False,
    allow_complete_visible_prefix: bool = False,
) -> str:
    """Use at most one parent navigation tap and one proven-list row tap."""
    log_crane_summary(nodes, lg, "Crane parent")
    compact_target = compact_crane_target_row(nodes, name)
    if compact_target is not None:
        lg(
            f"Crane compact live picker exact container {name} "
            f"[{compact_target.x},{compact_target.y} "
            f"{compact_target.w}x{compact_target.h}]"
        )
        stack.tap_xy(*compact_target.center())
        return "compact-live-list"

    # A long-press parent may contain a list title or multiple slot-looking
    # rows. Exact iOS haptic actions take precedence so the first slot tap is
    # treated as navigation and the resulting list receives the final tap.
    if crane_container_list(nodes) and not crane_haptic_parent(nodes):
        named = _locate_container_in_list(
            stack,
            phone,
            lg,
            name,
            nodes,
            newest_bound=newest_bound,
            allow_complete_visible_prefix=allow_complete_visible_prefix,
        )
        lg(f"Crane list container {name} [{named.x},{named.y} {named.w}x{named.h}]")
        stack.tap_xy(*named.center())
        return "list"

    parent_summary = _crane_safe_summary(nodes)
    entry_kind, parent_entry, entry_slot = crane_parent_entry(
        nodes, name, require_list=newest_bound
    )
    if parent_entry is None:
        if newest_bound:
            raise PhoneError(
                "Crane haptic menu has no safe list doorway for instance-bound "
                f"{name}. {parent_summary}. Instance-bound selection requires an "
                "explicit Crane list doorway. Did not tap a same-name or unknown action."
            )
        raise PhoneError(
            "Crane haptic menu has no requested container or recognized submenu. "
            f"{parent_summary}. Did not tap an unknown action."
        )

    before = _screen_signature(nodes)
    if entry_kind in ("marker", "container-marker"):
        label = collapsed_text(parent_entry.labels())
        lg(
            f"Crane parent marker {label!r} "
            f"[{parent_entry.x},{parent_entry.y} {parent_entry.w}x{parent_entry.h}]"
        )
    elif entry_kind == "current-slot":
        lg(
            f"Crane parent current-slot doorway {entry_slot} "
            f"[{parent_entry.x},{parent_entry.y} {parent_entry.w}x{parent_entry.h}]"
        )
    else:
        lg(
            f"Crane parent candidate {name} "
            f"[{parent_entry.x},{parent_entry.y} {parent_entry.w}x{parent_entry.h}]"
        )
    stack.tap_xy(*parent_entry.center())
    time.sleep(0.8)
    _blob, level2_nodes = _transient_dump(stack, phone, "810_crane_level2", lg)
    log_crane_summary(level2_nodes, lg, "Crane level2")
    level2_summary = _crane_safe_summary(level2_nodes)
    if _screen_signature(level2_nodes) == before:
        raise PhoneError(
            "Crane parent tap left the haptic menu unchanged. "
            f"{level2_summary}. Did not guess."
        )

    compact_option = compact_crane_target_row(level2_nodes, name)
    if compact_option is not None:
        lg(
            f"Crane compact level2 exact container {name} "
            f"[{compact_option.x},{compact_option.y} "
            f"{compact_option.w}x{compact_option.h}]"
        )
        stack.tap_xy(*compact_option.center())
        return "list"

    compact_option = _haptic_container_option_row(level2_nodes, name)
    if compact_option is not None and not protected_reddit_marker(level2_nodes):
        lg(
            f"Crane haptic level2 bounded exact container {name} "
            f"[{compact_option.x},{compact_option.y} "
            f"{compact_option.w}x{compact_option.h}]"
        )
        stack.tap_xy(*compact_option.center())
        return "list"

    # After one verified parent-navigation tap, accept a changed bounded
    # second level with two different letter-prefixed slot IDs and the exact
    # requested body row.  It may remain haptic or become Crane-owned.
    wanted_index = name_to_index(name)
    level2_exact_rows = [
        node
        for text, node in r_nodes(level2_nodes)
        if node.y >= CRANE_LIST_BODY_MIN_Y
        and wanted_index is not None
        and _crane_slot_index(text) == wanted_index
    ]
    level2_named = (
        level2_exact_rows[0] if len(level2_exact_rows) == 1 else None
    )
    # A changed second level can be rendered either inside the iOS haptic
    # surface or as Crane's own short list after the haptic parent closes.
    # The parent was already a verified Crane/iOS doorway and this is a freshly
    # changed second level. One unique exact requested body row is sufficient,
    # even when it is the only row or the slot is UUID-bound. Other slot names
    # are harmless; duplicate occurrences of the requested name remain unsafe.
    bounded_level2 = (
        level2_named is not None
        and not protected_reddit_marker(level2_nodes)
    )
    if bounded_level2:
        surface = (
            "haptic"
            if haptic_system_action(level2_nodes) is not None
            else "non-haptic"
        )
        lg(
            f"Crane {surface} level2 unique exact container {name} "
            f"[{level2_named.x},{level2_named.y} "
            f"{level2_named.w}x{level2_named.h}]"
        )
        stack.tap_xy(*level2_named.center())
        return "list"

    if not crane_container_list(level2_nodes):
        if newest_bound:
            raise PhoneError(
                f"Crane doorway did not open a proven container list for instance-bound {name}. "
                f"{level2_summary}. Did not accept a direct or older duplicate selection."
            )
        state = post_selection_state(level2_nodes, name)
        if state in ("springboard", "reddit", "loading"):
            if entry_kind == "current-slot" and entry_slot != name:
                raise PhoneError(
                    f"Crane current-slot doorway {entry_slot} closed without proving or selecting "
                    f"requested container {name}. {level2_summary}. Did not launch Reddit."
                )
            return "direct"
        raise PhoneError(
            "Crane parent opened an unrecognized second level. "
            f"{level2_summary}. Did not tap another marker or unknown action."
        )

    named = _locate_container_in_list(
        stack,
        phone,
        lg,
        name,
        level2_nodes,
        newest_bound=newest_bound,
        allow_complete_visible_prefix=allow_complete_visible_prefix,
    )
    lg(f"Crane level2 container {name} [{named.x},{named.y} {named.w}x{named.h}]")
    stack.tap_xy(*named.center())
    return "list"


def _meaningful_nodes(nodes):
    """Ignore OCR status-bar noise while Reddit is launching."""
    return [
        n
        for n in nodes
        if n.ntype != "ocr-line"
        and n.on_screen()
        and n.y >= 80
        and collapsed_text(n.labels())
    ]


def reddit_ui(nodes) -> bool:
    """Recognize known Reddit signup or feed UI without accepting any app."""
    if ads_personalization_screen(nodes):
        return True
    if protected_reddit_marker(nodes):
        return True
    if find_label_exact(nodes, REDDIT_EXACT_MARKERS) is not None:
        return True

    labels = []
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        text = collapsed_text(n.labels())
        if not text:
            continue
        labels.append((text, n))
        if text == "reddit" and 80 <= n.y < 160:
            return True

    bottom = {
        text
        for text, node in labels
        if node.y > 900 and text in REDDIT_BOTTOM_MARKERS
    }
    if len(bottom) >= 2:
        return True

    blob = " ".join(text for text, _node in labels)
    return any(
        marker in blob
        for marker in (
            "log in or sign up",
            "popular near you",
            "search reddit",
        )
    )


def post_selection_state(nodes, name: str) -> str:
    """Classify the bounded transition after tapping a Crane container."""
    # Keep the stronger Reddit check first even though ``crane_container_list``
    # also rejects feeds.  This documents and preserves the classifier's
    # precedence if list recognition grows new anchored layouts later.
    if proven_home_feed(nodes):
        return "reddit"
    if (
        crane_container_list(nodes)
        or crane_submenu_marker(nodes) is not None
        or haptic_system_action(nodes) is not None
    ):
        return "menu"
    if springboard_reddit(nodes) is not None:
        return "springboard"
    if find_picker_row(nodes, name) is not None:
        return "menu"
    if reddit_ui(nodes):
        return "reddit"
    if not _meaningful_nodes(nodes):
        return "loading"
    return "unknown"


def _save_confirmed_reddit_dump(blob: str, lg: LogFn) -> None:
    """Persist only a dump already classified as Reddit, never SpringBoard."""
    stripped = (blob or "").lstrip()
    if stripped.startswith("<"):
        ext = ".xml"
    elif stripped.startswith("["):
        ext = ".json"
    else:
        ext = ".txt"
    save_dump("820_after_launch" + ext, blob, lg)


def _confirm_container_launch(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    name: str,
    *,
    allow_existing_sparse_feed: bool = False,
) -> FeedCleanupProof | None:
    """Prove Crane selection, then launch Reddit once if it returned Home.

    The normal open/signup route accepts only the established Reddit UI
    classifier.  Day 1 may explicitly accept an already-scrolled sparse feed,
    but receives only a cleanup proof; its caller must relate a fresh,
    gesture-free frame to that proof before making a feed gesture.
    """
    time.sleep(1.2)
    launch_tapped = False
    relaunch_used = False
    probes_per_launch = 16
    while True:
        for attempt in range(probes_per_launch):
            CREATION_PROGRESS.note()
            probe_phase = "relaunch" if relaunch_used else "initial"
            blob, nodes = _transient_dump(
                stack,
                phone,
                f"820_post_select_{probe_phase}_probe_{attempt}",
                lg,
            )
            warn_siwa(nodes, lg)
            state = post_selection_state(nodes, name)
            if state == "menu":
                raise PhoneError(
                    f"{name} selection did not close the Crane/haptic menu. Did not continue."
                )
            if state == "springboard":
                if launch_tapped:
                    raise PhoneError(
                        f"{name} was selected, but Reddit remained on SpringBoard after one "
                        "verified icon tap. Did not retry."
                    )
                icon = springboard_reddit(nodes)
                if icon is None:
                    raise PhoneError("SpringBoard detected but the Reddit icon was not proven.")
                x, y = reddit_icon_point(icon)
                lg(
                    f"Crane selected {name} and returned Home -> "
                    f"tap freshly detected Reddit icon ({x},{y}) once"
                )
                tap_home_icon(stack, icon)
                launch_tapped = True
                time.sleep(1.4)
                continue
            if (
                allow_existing_sparse_feed
                and state in {"reddit", "unknown"}
                and not proven_home_feed(nodes)
            ):
                landing_marker = day1_sparse_launch_blocker(nodes)
                if landing_marker:
                    lg(
                        f"{name}: sparse Day 1 launch proof refused because a Reddit "
                        "login/signup landing marker is visible."
                    )
                else:
                    terminal_proof = make_terminal_feed_cleanup_proof(nodes)
                    if isinstance(terminal_proof, FeedCleanupProof):
                        lg(
                            f"{name}: existing sparse Reddit feed proven after container "
                            "selection; returning cleanup-only proof for a fresh "
                            "no-gesture Day 1 check."
                        )
                        return terminal_proof
            if state == "reddit":
                _save_confirmed_reddit_dump(blob, lg)
                return None
            if state == "unknown":
                raise PhoneError(
                    "Unrecognized screen after Crane container selection. "
                    "Did not tap or continue."
                )
            phase = "app relaunch" if relaunch_used else (
                "icon launch" if launch_tapped else "Crane selection"
            )
            lg(
                f"Reddit still loading after {phase} -> "
                f"wait {attempt + 1}/{probes_per_launch}"
            )
            time.sleep(1.2)

        if relaunch_used:
            raise PhoneError(
                "Reddit stayed blank after the doubled loading window and one "
                "verified Home -> Reddit relaunch. Did not continue."
            )

        lg(
            f"{name}: Reddit feed stayed blank for the doubled loading window; "
            "exit to Home and tap the freshly detected Reddit app once."
        )
        icon = _find_reddit_from_home(stack, phone, lg, "820_blank_relaunch")
        x, y = reddit_icon_point(icon)
        lg(f"{name}: retry Reddit app from Home -> tap verified icon ({x},{y}) once")
        tap_home_icon(stack, icon)
        launch_tapped = True
        relaunch_used = True
        time.sleep(1.4)


def select_existing_container_from_compact_menu(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    name: str,
    *,
    allow_existing_sparse_feed: bool = False,
) -> FeedCleanupProof | None:
    """Select and verify ``name`` only from a live proven compact Crane menu.

    This is the narrow recovery entry point used by the UI recovery supervisor.
    It never opens a menu, scrolls, creates, deletes, or accepts another slot.
    """
    _blob, nodes = _transient_dump(
        stack, phone, "810_recovery_compact_picker", lg
    )
    target = compact_crane_target_row(nodes, name)
    if target is None:
        raise ContainerSelectionError(
            f"Live compact Crane picker did not uniquely prove exact target {name}. "
            "Did not tap, create, delete, or resume."
        )
    lg(
        f"Crane recovery compact exact container {name} "
        f"[{target.x},{target.y} {target.w}x{target.h}]"
    )
    stack.tap_xy(*target.center())
    try:
        launch_proof = _confirm_container_launch(
            stack,
            phone,
            lg,
            name,
            allow_existing_sparse_feed=allow_existing_sparse_feed,
        )
    except PhoneError as exc:
        raise ContainerSelectionError(
            f"Exact compact Crane row {name} was tapped but Reddit launch was "
            f"not proven: {exc}"
        ) from exc
    lg(f"CRANE_ACTIVE_CONFIRMED={name}")
    return launch_proof


def open_container(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    name: str,
    email: str,
    *,
    allow_existing_sparse_feed: bool = False,
    require_newest_exact: bool = False,
    require_home_menu: bool = False,
) -> FeedCleanupProof | None:
    """Open one Reddit container through Crane's SpringBoard haptic menu.

    ``allow_existing_sparse_feed`` is reserved for the already-created Day 1
    caller.  Its default is deliberately false so signup and all existing
    callers retain the strict post-selection classifier.
    """
    slot = get_slot(name) or {}
    recovered_policy = slot.get("crane_selection_policy") == "newest_exact"
    newest_bound = require_newest_exact or _bound_container_selection_required(
        name, slot
    )
    allow_complete_visible_prefix = require_newest_exact or recovered_policy
    if newest_bound:
        lg(
            f"{name} requires newest-exact selection; Crane must prove its stable "
            "list bottom and select the bottommost exact row."
        )
    _blob, nodes = _transient_dump(stack, phone, "800_now", lg)
    bad = protected_reddit_marker(nodes)
    if bad:
        raise PhoneError(
            f"REFUSE container switch: Reddit is on protected signup step '{bad}'. "
            "Did not press Home or open the haptic menu."
        )

    # Reuse only a globally proven list or the stricter target-aware compact
    # picker.  ``require_home_menu`` still forces a fresh path for every other
    # screen, but must not discard a fully proven compact recovery state.
    try:
        if compact_crane_target_row(nodes, name) is not None:
            lg(
                "Existing compact Crane picker proved the exact requested row; "
                "selecting from the live screen without returning Home."
            )
            selection_path = _tap_requested_container(
                stack,
                phone,
                lg,
                name,
                nodes,
                newest_bound=newest_bound,
                allow_complete_visible_prefix=allow_complete_visible_prefix,
            )
        elif crane_container_list(nodes) and not require_home_menu:
            lg("Existing Crane container list detected; selecting from the live screen.")
            selection_path = _tap_requested_container(
                stack,
                phone,
                lg,
                name,
                nodes,
                newest_bound=newest_bound,
                allow_complete_visible_prefix=allow_complete_visible_prefix,
            )
        else:
            icon = _find_reddit_from_home(stack, phone, lg, "800")
            long_press_home_icon(stack, icon, lg)
            time.sleep(0.8)
            _blob, nodes = _transient_dump(stack, phone, "810_haptic", lg)
            recorded_instance = str(
                slot.get("ghost_container_instance") or ""
            ).strip()
            doorway_unavailable = stock_haptic_without_crane_doorway(
                nodes, name
            )
            if doorway_unavailable:
                if not recorded_instance:
                    raise CraneDoorwayUnavailable(
                        f"Crane exposed only the stock iOS haptic menu for {name}, "
                        "and the slot has no recorded Ghost container identity. "
                        "Did not tap an unknown action."
                    )
                lg(
                    f"{name}: stock iOS haptic menu has no Crane doorway; "
                    "selecting the exact recorded container through Ghost's "
                    "validated full inventory."
                )
                try:
                    ghost_live.select_recorded_container(
                        stack,
                        phone,
                        lg,
                        name,
                        recorded_instance,
                    )
                except OperationCancelled:
                    raise
                except Exception as ghost_exc:
                    raise CraneDoorwayUnavailable(
                        f"Could not safely select recorded Ghost container {name} "
                        "after Crane exposed only the stock iOS haptic menu: "
                        f"{ghost_exc}"
                    ) from ghost_exc
                selection_path = "ghost-recorded-instance"
            else:
                selection_path = _tap_requested_container(
                    stack,
                    phone,
                    lg,
                    name,
                    nodes,
                    newest_bound=newest_bound,
                    allow_complete_visible_prefix=allow_complete_visible_prefix,
                )
    except OperationCancelled:
        raise
    except ContainerSelectionError:
        raise
    except PhoneError as exc:
        raise ContainerSelectionError(
            f"Could not safely select exact Crane container {name}: {exc}"
        ) from exc

    lg(f"CRANE_SELECTION_PATH={selection_path}")
    try:
        launch_proof = _confirm_container_launch(
            stack,
            phone,
            lg,
            name,
            allow_existing_sparse_feed=allow_existing_sparse_feed,
        )
    except OperationCancelled:
        raise
    except PhoneError as first_exc:
        if selection_path == "ghost-recorded-instance":
            raise CraneDoorwayUnavailable(
                f"Recorded Ghost container {name} was selected exactly, but "
                f"Reddit launch was not locally proven: {first_exc}"
            ) from first_exc
        # One bounded recovery for a missed row tap or launch transition. Never
        # reuse the stale coordinate: return Home, reopen the verified Crane
        # doorway, resolve the requested row from a fresh frame, and confirm the
        # launch once more.
        lg(
            f"{name}: first Crane selection/launch was not confirmed "
            f"({first_exc}); retrying the verified Home -> Crane route once."
        )
        try:
            retry_icon = _find_reddit_from_home(stack, phone, lg, "800_retry")
            long_press_home_icon(stack, retry_icon, lg)
            time.sleep(0.8)
            _blob, retry_nodes = _transient_dump(
                stack, phone, "810_haptic_retry", lg
            )
            retry_path = _tap_requested_container(
                stack,
                phone,
                lg,
                name,
                retry_nodes,
                newest_bound=newest_bound,
                allow_complete_visible_prefix=allow_complete_visible_prefix,
            )
            lg(f"CRANE_SELECTION_RETRY_PATH={retry_path}")
        except OperationCancelled:
            raise
        except PhoneError as retry_route_exc:
            # Row discovery and identity proof remain entirely local.  An
            # absent/duplicate/unproven target cannot be made safe by AI.
            raise ContainerSelectionError(
                f"Could not safely reselect exact Crane row {name} during the "
                f"bounded fresh-route retry: {retry_route_exc}"
            ) from retry_route_exc
        try:
            launch_proof = _confirm_container_launch(
                stack,
                phone,
                lg,
                name,
                allow_existing_sparse_feed=allow_existing_sparse_feed,
            )
        except OperationCancelled:
            raise
        except PhoneError as retry_exc:
            raise ContainerLaunchTransitionUnclear(
                f"Exact Crane row {name} was not confirmed after one bounded "
                f"fresh-route retry: {retry_exc}"
            ) from retry_exc
    lg(f"CRANE_ACTIVE_CONFIRMED={name}")
    lg(f"OPENED {name} email={email}")
    lg("Stopped before signup. Did not type email. Did not tap Continue with Apple.")
    return launch_proof


def recover_existing_container(
    stack: TapStack,
    phone: Phone,
    lg: LogFn,
    name: str,
    email: str,
) -> FeedCleanupProof | None:
    """Prove and launch an existing Crane row without creating or changing it."""
    try:
        return open_container(
            stack,
            phone,
            lg,
            name,
            email,
            require_newest_exact=True,
            require_home_menu=True,
        )
    except PhoneError as exc:
        raise PhoneError(
            f"Could not prove and launch the existing Crane container {name}: {exc} "
            "Nothing was created, deleted, or renamed. Use New Ghost container "
            "if the exact container does not exist."
        ) from exc


def main() -> int:
    name = (sys.argv[1] if len(sys.argv) > 1 else "a0001").strip().lower()
    slot = get_slot(name)
    if not slot or not slot.get("ghost"):
        print(f"ERROR Slot {name} has no Ghost container.")
        return 1
    email = str(slot.get("email") or "").strip()
    if not email:
        print(f"ERROR Slot {name} has no Hide My Email.")
        return 1
    log(f"Slot {name} ghost=true email={email} reddit={slot.get('reddit')}")
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend. Open XXTouch Elite, Remote Service on.")
        open_container(stack, phone, log, name, email)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
