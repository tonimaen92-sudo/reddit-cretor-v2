# UTF-8
"""Pick random Reddit Interests chips, small scroll. Different set each account.

  python misc/reddit_interests.py
"""

from __future__ import annotations

import os
import json
import math
import random
import re
import sys
import tempfile
import time
import unicodedata
from friend_poster.core.config import DUMP_DIR, LOG_DIR
from friend_poster.core.slots import (  # noqa: E402
    CREATION_PROGRESS,
    get_slot,
    write_account_text,
)
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import Node, TapStack, collapsed_text, dump_named, find_label_exact  # noqa: E402
from friend_poster.services.ui_recovery_agent import UnknownVisualStateError  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

HEADERS = {
    "popular",
    "games",
    "places & travel",
    "news",
    "sports",
    "music",
    "movies",
    "tv",
    "art",
    "fashion",
    "food",
    "science",
    "tech",
    "technology",
    "politics",
    "business",
    "health",
    "fitness",
    "animals",
    "nature",
    "books",
    "anime",
    "crypto",
}
INTERESTS_MARKERS = ("Choose your interests", "Interests")
GRID_INTEREST_ALIASES = {
    "art": "art",
    "beaut": "beauty",
    "beauty": "beauty",
    "career": "career",
    "entertainment": "entertainment",
    "finance": "finance",
    "food": "food",
    "gaming": "gaming",
    "news": "news",
    "sports": "sports",
    "technology": "technology",
    "travel": "travel",
    "wellness": "wellness",
}
INTERESTS_TRANSITION_MAX_DUMPS = 8
POST_INTERESTS_MAX_DUMPS = 8
INTEREST_SELECTION_COUNT = 4
CUSTOMIZE_MARKERS = ("Customize your feed",)
SHOW_MORE_TOPICS_MARKERS = (
    "Show More Topics",
    "Shaw More Tonirc",
    "Chaw More Tonic",
)
# The anchor is OCR'd inconsistently by this device's engine ("Shaw More Tonirc",
# "Chaw More Tonic", "Chaw More Tonine", ...) and reading it at pill height clips
# its last word ("... Topics" -> "... Tonine").  Adding every mangling to the
# marker list is a losing game, so recognition is layout-first: a control of the
# right size and position whose two leading words still read as "show/shaw/chaw
# more" is the anchor even when its tail is unreadable.
SHOW_MORE_PREFIX_FIRST = ("show", "shaw", "chaw")
SHOW_MORE_PREFIX_SECOND = {"more"}
# A partially clipped anchor is rebuilt from its first words; a whole extra word
# that Reddit never renders (the observed "tonine") is dropped rather than trusted.
SHOW_MORE_CANONICAL = "show more topics"
SHOW_MORE_KEEP_WORDS = 2
# Photographing a pill at glyph height cuts its descenders, so the anchor is only
# trusted when its lower edge sits clearly inside the captured frame.
SHOW_MORE_RELATIVE_Y_MAX = 0.95
CUSTOMIZE_ANCHOR_FIX_SCROLLS = 2
CUSTOMIZE_ANCHOR_FIX_SETTLE_SECONDS = 0.6
CUSTOMIZE_PENDING_VERIFY_RETRIES = 3
CUSTOMIZE_PENDING_VERIFY_SETTLE_SECONDS = 0.8
CUSTOMIZE_CHECKPOINT_VERSION = 3
CUSTOMIZE_TITLE_Y = (140, 260)
CUSTOMIZE_ACTION_Y = (1180, 1285)
CUSTOMIZE_ANCHOR_X = (20, 350)
# Reddit varies the number of suggested pills per account.  A shorter valid list
# puts the lower-page anchor above the older 930px floor (a0051 observed it at
# parsed y=897).  Keep the anchor in the lower content area while proving the
# title and both bottom actions separately.
CUSTOMIZE_ANCHOR_Y = (820, 1195)
CUSTOMIZE_SEARCH_Y = (330, 430)
CUSTOMIZE_EXCLUDED = {
    "customize your feed",
    "show more topics",
    "shaw more tonirc",
    "chaw more tonic",
    "find more of your interests",
    "q find more of your interests",
    "skip",
    "continue",
    "back",
}


def log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        safe = str(msg).encode(encoding, errors="backslashreplace").decode(encoding)
        print(safe, flush=True)


def small_scroll(stack: TapStack, back: bool = False) -> None:
    """Short list nudge on the chip grid. Not Ghost Home. Stay above the orange bar."""
    if back:
        stack.interp_flick(375, 720, 375, 880, steps=16, ms=28)
    else:
        stack.interp_flick(375, 880, 375, 720, steps=16, ms=28)
    CREATION_PROGRESS.note()


def interests_screen(nodes) -> bool:
    return find_label_exact(nodes, INTERESTS_MARKERS) is not None


def choose_grid(nodes) -> bool:
    return find_label_exact(nodes, ("Choose your interests",)) is not None


def _unique_bounded_label_node(
    nodes,
    labels,
    *,
    x_min: int = 0,
    x_max: int = 750,
    y_min: int = 0,
    y_max: int = 1334,
):
    wanted = tuple(collapsed_text(label) for label in labels)
    hits = [
        node
        for node in nodes
        if node.ntype != "ocr-line"
        and node.on_screen()
        and collapsed_text(node.labels()) in wanted
        and x_min <= node.x <= x_max
        and y_min <= node.y <= y_max
    ]
    return hits[0] if len(hits) == 1 else None


def customize_title_node(nodes):
    return _unique_bounded_label_node(
        nodes,
        CUSTOMIZE_MARKERS,
        y_min=CUSTOMIZE_TITLE_Y[0],
        y_max=CUSTOMIZE_TITLE_Y[1],
    )


def customize_action_nodes(nodes):
    skip = _unique_bounded_label_node(
        nodes,
        ("Skip",),
        x_min=20,
        x_max=380,
        y_min=CUSTOMIZE_ACTION_Y[0],
        y_max=CUSTOMIZE_ACTION_Y[1],
    )
    continue_node = _unique_bounded_label_node(
        nodes,
        ("Continue",),
        x_min=350,
        x_max=700,
        y_min=CUSTOMIZE_ACTION_Y[0],
        y_max=CUSTOMIZE_ACTION_Y[1],
    )
    return skip, continue_node


def _label_words(node) -> list[str]:
    return [w for w in collapsed_text(node.labels()).split() if w]


def _reads_show_more(node) -> bool:
    """True when a node's leading words still identify the Show-More control."""
    words = _label_words(node)
    return (
        len(words) >= 2
        and words[0] in SHOW_MORE_PREFIX_FIRST
        and words[1] in SHOW_MORE_PREFIX_SECOND
    )


def show_more_topics_node(nodes):
    """Return Reddit's unique lower-page topic anchor on a proven action layout.

    Named markers are matched first.  When OCR mangles the tail, the control is
    recognised from its shape and its surviving "show/shaw/chaw more" prefix, and
    its label is normalised to the canonical ``show more topics`` so the clipped
    reading can never leak in as a topic.
    """
    skip, continue_node = customize_action_nodes(nodes)
    if skip is None or continue_node is None:
        return None
    action_top = min(skip.y, continue_node.y)
    named = tuple(collapsed_text(label) for label in SHOW_MORE_TOPICS_MARKERS)
    hits = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen():
            continue
        if not (CUSTOMIZE_ANCHOR_Y[0] <= node.y <= CUSTOMIZE_ANCHOR_Y[1]):
            continue
        if not (CUSTOMIZE_ANCHOR_X[0] <= node.x <= CUSTOMIZE_ANCHOR_X[1]):
            continue
        if not (140 <= node.w <= 320 and 12 <= node.h <= 35):
            continue
        if not node.y + node.h < action_top:
            continue
        blob = collapsed_text(node.labels())
        if blob in named:
            hits.append(node)
        elif _reads_show_more(node):
            hits.append(Node(
                ntype=node.ntype,
                name=node.name,
                label=SHOW_MORE_CANONICAL,
                value=node.value,
                text=SHOW_MORE_CANONICAL,
                x=node.x,
                y=node.y,
                w=node.w,
                h=node.h,
                visible=node.visible,
                enabled=node.enabled,
                raw=node.raw,
            ))
    return hits[0] if len(hits) == 1 else None


def customize_search_node(nodes):
    """Return the bounded search anchor used by Reddit's expanded topic grid."""
    skip, continue_node = customize_action_nodes(nodes)
    if skip is None or continue_node is None:
        return None
    expected = ("find", "more", "of", "your", "interests")
    hits = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen():
            continue
        words = tuple(collapsed_text(node.labels()).split())
        candidate = words
        if len(candidate) == len(expected) + 1 and len(candidate[0]) == 1:
            # The search glyph is sometimes exposed as one OCR character.
            candidate = candidate[1:]
        marker = candidate == expected or (
            len(candidate) == len(expected)
            and _at_most_one_ocr_edit(
                " ".join(candidate),
                " ".join(expected),
            )
        )
        if not marker:
            continue
        if not (20 <= node.x <= 400 and CUSTOMIZE_SEARCH_Y[0] <= node.y <= CUSTOMIZE_SEARCH_Y[1]):
            continue
        if not (350 <= node.w <= 600 and 18 <= node.h <= 45):
            continue
        hits.append(node)
    return hits[0] if len(hits) == 1 else None


def customize_screen(nodes) -> bool:
    return (
        customize_title_node(nodes) is not None
        and (
            show_more_topics_node(nodes) is not None
            or customize_search_node(nodes) is not None
        )
    )


def customize_topic_label(node) -> str:
    """Ignore OCR's leading checkmark mark on a selected topic label.

    The checkmark is only stripped when the remainder still reads like a topic.
    OCR renders a checked pill's glyph as ``~``/``v``-ish, and the real glyph was
    observed inside a genuine name (a0060 ``"~ Productivity"``), so a blind strip
    silently renames the topic and breaks every checkpoint comparison.  A bare
    ``~x``/``v x`` remainder (no second word) is therefore kept verbatim.
    """
    label = collapsed_text(node.labels())
    stripped = re.sub("^[\\u007e\\u2713\\u2714\\u2611\\u221a]\\s*", "", label)
    # Only strip when the remainder still reads like a topic; a lone glyph with no
    # second word (e.g. a genuine "~ x") is left verbatim.
    if stripped != label and len(stripped.split()) >= 2:
        return stripped
    return label


def _customize_titled_but_unproven(nodes) -> bool:
    return (
        find_label_exact(nodes, CUSTOMIZE_MARKERS) is not None
        and not customize_screen(nodes)
    )


def _raise_unproven_customize() -> None:
    raise PhoneError(
        "Customize your feed title is visible, but its exact title/action/anchor "
        "layout is not proven. Preserved the screen without another tap."
    )


def _label_blob(nodes) -> str:
    return " ".join(
        collapsed_text(node.labels())
        for node in nodes
        if node.on_screen() and node.labels()
    )


def _meaningful(nodes) -> bool:
    for node in nodes:
        if not node.on_screen():
            continue
        text = collapsed_text(node.labels())
        if not text or text.startswith("ocr_empty:"):
            continue
        return True
    return False


def notification_sheet(nodes) -> bool:
    """Reddit's post-signup notification explanation, not the signup nag."""
    return (
        find_label_exact(nodes, ("Please turn on notifications",)) is not None
        and find_label_exact(nodes, ("Not now", "Not Now")) is not None
        and find_label_exact(nodes, ("OK", "Ok")) is not None
    )


def native_notification_prompt(nodes) -> bool:
    """The iOS notification permission alert opened by Reddit's exact OK."""
    blob = _label_blob(nodes)
    return (
        "send you notifications" in blob
        and find_label_exact(nodes, ("Allow",)) is not None
        and find_label_exact(nodes, ("Don't Allow", "Don’t Allow")) is not None
    )


def ads_reject_node(nodes):
    """Return the bounded Reject action on the proven ads-consent page."""
    blob = _label_blob(nodes)
    has_title = find_label_exact(
        nodes, ("Your ads personalization choices",)
    ) is not None
    has_settings_copy = (
        "reddit allows you to manage your ads" in blob
        and "privacy policy" in blob
        and "cookie policy" in blob
        and find_label_exact(nodes, ("Accept",)) is not None
    )
    if not (has_title or has_settings_copy):
        return None
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen():
            continue
        if collapsed_text(node.labels()) not in ("reject", "relect"):
            continue
        if node.y < 1120 or node.y > 1280 or node.x < 200 or node.x + node.w > 550:
            continue
        return node
    return None


def ads_personalization_screen(nodes) -> bool:
    """Reddit's dedicated post-signup ads-consent page.

    Some app versions open this as a Settings sheet without rendering the old
    ``Your ads personalization choices`` heading.  Require the distinctive
    Reddit explanation, both policy names, and both bounded actions before
    accepting that layout; generic Settings pages remain excluded.
    """
    return ads_reject_node(nodes) is not None


def ads_close_node(nodes):
    """Return the bounded close action on the proven ads-consent sheet.

    After Reject, some Reddit builds raise the notification explanation over
    the still-visible ads sheet.  Dismissing that explanation reveals a close
    action in the sheet header; the preference has already been recorded and
    tapping Reject again would be both unnecessary and ambiguous.
    """
    if not ads_personalization_screen(nodes):
        return None
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen():
            continue
        if collapsed_text(node.labels()) not in ("x", "х", "×"):
            continue
        if node.x < 630 or node.x + node.w > 750 or node.y < 100 or node.y > 240:
            continue
        return node
    return None


def proven_home_feed(nodes) -> bool:
    """Require feed-specific evidence; a lone Reddit title is not enough."""
    blob = _label_blob(nodes)
    if "popular near you" in blob and find_label_exact(nodes, ("Join",)) is not None:
        return True
    home = find_label_exact(nodes, ("Home",))
    if home is None or home.y < 1040:
        return False
    bottom_labels = ("Communities", "Create", "Chat", "Inbox", "Notifications")
    return any(
        node is not None and node.y >= 1040
        for node in (find_label_exact(nodes, (label,)) for label in bottom_labels)
    )


def customize_topics(nodes):
    """Return only bounded topic pills from the proven Customize your feed screen."""
    if not customize_screen(nodes):
        return []
    anchor = show_more_topics_node(nodes)
    search = customize_search_node(nodes)
    skip, continue_node = customize_action_nodes(nodes)
    if anchor is None and search is None:
        return []
    content_bottom = anchor.y if anchor is not None else min(skip.y, continue_node.y) - 25
    # A selected pill can reflow the last OCR topic partly underneath Reddit's
    # fixed action bar (live a0292).  The pixel classifier deliberately rejects
    # that clipped region, so keep the topic extractor on the same boundary.
    # Otherwise an unrelated, unselected bottom label can abort verification of
    # an already-successful pending tap before its checkpoint is committed.
    content_bottom = min(content_bottom, CUSTOMIZE_ACTION_Y[0] - 25)
    out = []
    seen: set[str] = set()
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen():
            continue
        text = customize_topic_label(node)
        if not text or text in seen or text in CUSTOMIZE_EXCLUDED:
            continue
        if node.y < 420 or node.y + node.h >= content_bottom:
            continue
        if node.x < 8 or node.x + node.w > 748:
            continue
        if node.w < 60 or node.h < 14 or node.h > 35:
            continue
        seen.add(text)
        out.append(node)
    return out


def interest_name(node, *, grid: bool) -> str:
    text = collapsed_text(node.labels())
    if grid:
        return GRID_INTEREST_ALIASES.get(text, "")
    return text


def chips(nodes):
    out = []
    seen: set[str] = set()
    grid = choose_grid(nodes)
    for n in nodes:
        if n.ntype == "ocr-line" or not n.on_screen():
            continue
        t = collapsed_text(n.labels())
        if not t or t in seen:
            continue
        if grid:
            name = GRID_INTEREST_ALIASES.get(t, "")
            if not name or name in seen:
                continue
            if n.y < 430 or n.y > 1160 or n.w < 45 or n.h < 16 or n.h > 50:
                continue
        else:
            name = t
            if t in HEADERS:
                continue
            if t in ("interests", "skip", "continue", "back"):
                continue
            if "selected" in t or "home feed" in t or "pick things" in t:
                continue
            if n.y < 340 or n.y > 1180:
                continue
            if n.x < 8 or n.x + n.w > 748:
                continue
            if n.w < 50 or n.h < 18 or n.h > 50:
                continue
        seen.add(name)
        out.append(n)
    return out


def continue_enabled(nodes, screenshot_stem: str) -> bool:
    """Prove Reddit's orange enabled Continue using a fresh same-stem screenshot."""
    node = find_label_exact(nodes, ("Continue",))
    if node is None:
        return False
    png_path = os.path.join(DUMP_DIR, screenshot_stem + ".png")
    dump_paths = [
        os.path.join(DUMP_DIR, screenshot_stem + ext)
        for ext in (".json", ".xml", ".txt")
        if os.path.isfile(os.path.join(DUMP_DIR, screenshot_stem + ext))
    ]
    try:
        if not dump_paths or not os.path.isfile(png_path):
            return False
        newest_dump_ns = max(os.stat(path).st_mtime_ns for path in dump_paths)
        if os.stat(png_path).st_mtime_ns < newest_dump_ns:
            return False
        from PIL import Image

        with Image.open(png_path) as source:
            image = source.convert("RGB")
            cx, cy = node.center()
            px = cx + max(20, node.w // 2)
            py = cy + min(10, max(4, node.h // 3))
            sx = min(image.width - 1, max(0, round(px * image.width / 750)))
            sy = min(image.height - 1, max(0, round(py * image.height / 1334)))
            red, green, blue = image.getpixel((sx, sy))
    except (OSError, ValueError):
        return False
    return red >= 160 and green <= 120 and blue <= 100 and red - green >= 70


def tap_continue_bar(
    stack: TapStack,
    nodes,
    *,
    allow_selected_bar: bool = True,
    enabled_stem: str | None = None,
) -> bool:
    n = find_label_exact(nodes, ("Continue",))
    if n is not None and "apple" not in collapsed_text(n.labels()):
        if enabled_stem is not None and not continue_enabled(nodes, enabled_stem):
            log("Continue is present but not proven orange/enabled; did not tap.")
            return False
        log(f"Continue [{n.x},{n.y} {n.w}x{n.h}]")
        stack.tap_xy(*n.center())
        time.sleep(1.8)
        return True
    if not allow_selected_bar:
        return False
    for n in nodes:
        if n.ntype == "ocr-line":
            continue
        t = collapsed_text(n.labels())
        if "selected" in t and n.y > 1180:
            log(f"selected bar [{n.x},{n.y} {n.w}x{n.h}] {t[:40]!r}")
            stack.tap_xy(375, n.center()[1])
            time.sleep(1.8)
            return True
    return False


def _wait_screen_close(stack, phone, predicate, stem: str, label: str):
    last = None
    for attempt in range(INTERESTS_TRANSITION_MAX_DUMPS):
        CREATION_PROGRESS.note()
        time.sleep(0.9)
        dump_stem = stem if attempt == 0 else f"{stem}_{attempt}"
        _blob, last = dump_named(
            stack,
            phone,
            dump_stem,
            log,
            shot=True,
        )
        if predicate(last):
            log(
                f"{label} submit transition pending; no second Continue tap "
                f"({attempt + 1}/{INTERESTS_TRANSITION_MAX_DUMPS})"
            )
            continue
        if _meaningful(last):
            return last
    if last is not None and predicate(last):
        raise UnknownVisualStateError(
            f"{label} remained after one exact Continue tap. "
            "Did not tap Continue again."
        )
    raise UnknownVisualStateError(
        f"{label} next screen stayed blank or unclear after one Continue tap."
    )


def _wait_after_single_tap(
    stack: TapStack,
    phone: Phone,
    predicate,
    stem: str,
    label: str,
    *,
    transition_predicates=(),
):
    last = None
    for attempt in range(POST_INTERESTS_MAX_DUMPS):
        CREATION_PROGRESS.note()
        time.sleep(0.9)
        dump_stem = stem if attempt == 0 else f"{stem}_{attempt}"
        _blob, last = dump_named(stack, phone, dump_stem, log, shot=True)
        if any(transition(last) for transition in transition_predicates):
            log(
                f"{label} produced a distinct verified follow-up prompt; "
                "treating the original tap as complete."
            )
            return last
        if predicate(last):
            log(
                f"{label} transition pending; no second tap "
                f"({attempt + 1}/{POST_INTERESTS_MAX_DUMPS})"
            )
            continue
        if _meaningful(last):
            return last
    if last is not None and predicate(last):
        raise UnknownVisualStateError(
            f"{label} remained after one exact tap. Did not tap again."
        )
    raise UnknownVisualStateError(
        f"Screen stayed blank after {label}. Did not guess a next action."
    )


def advance_post_interests(stack: TapStack, phone: Phone, *, initial_nodes=None):
    """Resolve only proven post-interest prompts and stop on the proven feed."""
    if initial_nodes is None:
        _blob, nodes = dump_named(
            stack, phone, "928_post_interests", log, shot=True
        )
    else:
        nodes = initial_nodes

    notification_tapped = False
    native_tapped = False
    ads_tapped = False
    ads_close_tapped = False
    for _stage in range(4):
        if proven_home_feed(nodes):
            log("POST_INTERESTS=HOME_FEED")
            return nodes

        if notification_sheet(nodes):
            if notification_tapped:
                raise UnknownVisualStateError(
                    "Notification explanation remained after exact OK. Did not tap again."
                )
            ok = find_label_exact(nodes, ("OK", "Ok"))
            if ok is None:
                raise PhoneError("Notification explanation has no exact OK. Did not tap.")
            log(f"Post-signup notifications OK [{ok.x},{ok.y} {ok.w}x{ok.h}]")
            stack.tap_xy(*ok.center())
            notification_tapped = True
            nodes = _wait_after_single_tap(
                stack,
                phone,
                notification_sheet,
                "929_after_notifications_ok",
                "notification OK",
            )
            continue

        if native_notification_prompt(nodes):
            if native_tapped:
                raise UnknownVisualStateError(
                    "iOS notification prompt remained after exact Allow. Did not tap again."
                )
            allow = find_label_exact(nodes, ("Allow",))
            if allow is None:
                raise PhoneError("iOS notification prompt has no exact Allow. Did not tap.")
            log(f"System notifications Allow [{allow.x},{allow.y} {allow.w}x{allow.h}]")
            stack.tap_xy(*allow.center())
            native_tapped = True
            nodes = _wait_after_single_tap(
                stack,
                phone,
                native_notification_prompt,
                "930_after_notifications_allow",
                "notification Allow",
            )
            continue

        if ads_personalization_screen(nodes):
            if ads_tapped:
                close = ads_close_node(nodes)
                if close is None or ads_close_tapped:
                    raise UnknownVisualStateError(
                        "Ads personalization remained after exact Reject and has no "
                        "single unused bounded close action. Did not tap again."
                    )
                log(
                    f"Ads personalization preference saved; close sheet "
                    f"[{close.x},{close.y} {close.w}x{close.h}]"
                )
                stack.tap_xy(*close.center())
                ads_close_tapped = True
                nodes = _wait_after_single_tap(
                    stack,
                    phone,
                    ads_personalization_screen,
                    "932_after_ads_close",
                    "ads close",
                    transition_predicates=(
                        notification_sheet,
                        native_notification_prompt,
                    ),
                )
                continue
            reject = ads_reject_node(nodes)
            if reject is None:
                raise PhoneError("Ads personalization has no exact Reject. Did not tap.")
            log(f"Ads personalization Reject [{reject.x},{reject.y} {reject.w}x{reject.h}]")
            stack.tap_xy(*reject.center())
            ads_tapped = True
            nodes = _wait_after_single_tap(
                stack,
                phone,
                ads_personalization_screen,
                "931_after_ads_reject",
                "ads Reject",
                transition_predicates=(
                    notification_sheet,
                    native_notification_prompt,
                ),
            )
            continue

        raise UnknownVisualStateError(
            "Unknown screen after Interests. Preserved it without tapping; "
            "see the latest 928-931 dump."
        )

    if proven_home_feed(nodes):
        log("POST_INTERESTS=HOME_FEED")
        return nodes
    raise UnknownVisualStateError(
        "Post-Interests prompts did not reach a proven Home feed."
    )


def _record_interests(sid: str, tapped: list[str]) -> None:
    slot = get_slot(sid)
    if slot and slot.get("email") and slot.get("username"):
        merged: list[str] = []
        note_path = os.path.join(LOG_DIR, f"{sid.lower()}.txt")
        try:
            with open(note_path, encoding="utf-8") as source:
                for line in source:
                    if line.startswith("interests="):
                        merged.extend(
                            item.strip()
                            for item in line.split("=", 1)[1].split(",")
                            if item.strip()
                        )
                        break
        except OSError:
            pass
        for item in tapped:
            if item and item not in merged:
                merged.append(item)
        write_account_text(
            sid,
            str(slot["email"]),
            str(slot["username"]),
            str(slot.get("password") or "") or None,
            interests=",".join(merged),
        )


def _customize_checkpoint_path(sid: str) -> str:
    if re.fullmatch(r"[a-z]\d{4,}", sid) is None:
        raise PhoneError("Invalid slot id for interests checkpoint.")
    return os.path.join(LOG_DIR, f"{sid}.interests_checkpoint.json")


def _valid_customize_bounds(bounds) -> bool:
    """Return whether durable OCR bounds can identify one Customize pill."""
    if (
        not isinstance(bounds, list)
        or len(bounds) != 4
        or any(type(value) not in (int, float) for value in bounds)
        or any(not math.isfinite(float(value)) for value in bounds)
    ):
        return False
    x, y, width, height = (float(value) for value in bounds)
    return (
        x >= 8
        and y >= 420
        and width >= 60
        and 14 <= height <= 35
        and x + width <= 748
        and y + height < CUSTOMIZE_ACTION_Y[0] - 25
    )


def _customize_bounds_overlap(left, right) -> bool:
    """Reject two labels whose durable proof could address the same pill."""
    lx, ly, lw, lh = (float(value) for value in left)
    rx, ry, rw, rh = (float(value) for value in right)
    horizontal = min(lx + lw, rx + rw) > max(lx, rx)
    vertical = min(ly + lh, ry + rh) > max(ly, ry)
    return horizontal and vertical


def _valid_customize_proofs(proofs: dict, pending: dict | None = None) -> bool:
    if not isinstance(proofs, dict) or len(proofs) > INTEREST_SELECTION_COUNT:
        return False
    rectangles: list[list[float]] = []
    for label, bounds in proofs.items():
        if not isinstance(label, str) or not label or not _valid_customize_bounds(bounds):
            return False
        if any(_customize_bounds_overlap(bounds, prior) for prior in rectangles):
            return False
        rectangles.append(bounds)
    if pending is not None:
        bounds = pending.get("bounds") if isinstance(pending, dict) else None
        if not _valid_customize_bounds(bounds):
            return False
        if any(_customize_bounds_overlap(bounds, prior) for prior in rectangles):
            return False
    return True


def _load_customize_checkpoint(sid: str) -> dict:
    path = _customize_checkpoint_path(sid)
    if not os.path.isfile(path):
        return {
            "version": CUSTOMIZE_CHECKPOINT_VERSION,
            "slot_id": sid,
            "initial": [],
            "customize": [],
            "selected_proofs": {},
            "pending_customize": None,
        }
    try:
        with open(path, encoding="utf-8") as source:
            state = json.load(source)
    except (OSError, ValueError) as exc:
        raise PhoneError("Interests checkpoint could not be read safely.") from exc
    if isinstance(state, dict):
        if state.get("version") == 1 and set(state) == {
            "version", "slot_id", "initial", "customize",
        }:
            state = {
                **state,
                "version": CUSTOMIZE_CHECKPOINT_VERSION,
                "selected_proofs": {},
                "pending_customize": None,
            }
        elif state.get("version") == 2 and set(state) == {
            "version", "slot_id", "initial", "customize", "pending_customize",
        }:
            state = {
                **state,
                "version": CUSTOMIZE_CHECKPOINT_VERSION,
                "selected_proofs": {},
            }
            # Version 2 used two writes after verification.  If the process
            # stopped between them, keep the action pending and verify it from
            # the live screen once more instead of trusting the partial write.
            old_pending = state.get("pending_customize")
            if isinstance(old_pending, dict) and old_pending.get("label") in state.get(
                "customize", []
            ):
                state["customize"] = [
                    label
                    for label in state["customize"]
                    if label != old_pending.get("label")
                ]
    pending = state.get("pending_customize") if isinstance(state, dict) else None
    proofs = state.get("selected_proofs") if isinstance(state, dict) else None
    if (
        not isinstance(state, dict)
        or set(state) != {
            "version", "slot_id", "initial", "customize", "selected_proofs",
            "pending_customize",
        }
        or state["version"] != CUSTOMIZE_CHECKPOINT_VERSION
        or state["slot_id"] != sid
        or any(
            not isinstance(state[key], list)
            or any(not isinstance(item, str) or not item for item in state[key])
            or len(state[key]) != len(set(state[key]))
            for key in ("initial", "customize")
        )
        or len(state["customize"]) > INTEREST_SELECTION_COUNT
        or not isinstance(proofs, dict)
        or not set(proofs).issubset(state["customize"])
        or not _valid_customize_proofs(proofs, pending)
        or (
            pending is not None
            and (
                not isinstance(pending, dict)
                or set(pending) != {"label", "bounds"}
                or not isinstance(pending["label"], str)
                or not pending["label"]
                or pending["label"] in state["customize"]
                or not _valid_customize_bounds(pending["bounds"])
            )
        )
        or len(state["customize"]) + (pending is not None) > INTEREST_SELECTION_COUNT
    ):
        raise PhoneError("Interests checkpoint is malformed; no topic was tapped.")
    return state


def _save_customize_checkpoint(sid: str, state: dict) -> None:
    path = _customize_checkpoint_path(sid)
    os.makedirs(LOG_DIR, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=LOG_DIR,
            prefix=f".{sid}.interests-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(state, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # OneDrive and antivirus scanners can briefly open the existing target
        # without delete sharing.  On Windows that makes an otherwise atomic
        # replace fail with PermissionError/WinError 5.  Keep the fully flushed
        # temp file and retry the replace instead of pausing a healthy phone run.
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(min(0.25 * (attempt + 1), 1.0))
    except OSError as exc:
        raise PhoneError("Interests checkpoint could not be saved.") from exc
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _canonical_checked_label(label: str, expected: set[str]) -> str:
    """Map any one-glyph checkmark OCR prefix to any expected topic label.

    This is topic-agnostic: ``expected`` comes from the durable checkpoint made
    before the tap, so new Reddit topic names need no code change.  A prefix is
    removed only when the entire remaining label is an exact expected topic and
    the prefix is one OCR glyph shaped like punctuation/symbol (or the observed
    ASCII ``V`` checkmark).  Ordinary words are never trimmed.
    """
    if label in expected:
        return label
    matches: list[str] = []
    for candidate in expected:
        if not candidate:
            continue
        prefix = ""
        remainder = ""
        if label.endswith(candidate):
            prefix = label[: -len(candidate)].strip()
            remainder = candidate
        else:
            parts = label.split(maxsplit=1)
            if len(parts) == 2:
                prefix, remainder = parts
        if len(prefix) != 1:
            continue
        category = unicodedata.category(prefix)
        is_checkmark_prefix = prefix.casefold() == "v" or category.startswith(("P", "S"))
        if is_checkmark_prefix and (
            remainder == candidate or _at_most_one_ocr_edit(remainder, candidate)
        ):
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    return label


def _at_most_one_ocr_edit(observed: str, expected: str) -> bool:
    """Return True for one OCR insertion, deletion, or substitution.

    Reddit can repaint a selected pill with a checkmark and move its text.  On
    the live a0231 frame the same topic changed from ``prut`` to ``pr$ut``.
    This deliberately small comparison is used only behind stronger identity
    evidence: either the selected pill still matches the durable pending-tap
    bounds, or the complete pixel-proven selected set has one unique bijection
    to the equally sized durable checkpoint set.
    """
    left = observed.casefold()
    right = expected.casefold()
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) == 1
    if len(left) < len(right):
        left, right = right, left
    # ``left`` is exactly one character longer.  Removing one glyph must make
    # it equal to ``right``; no broader fuzzy matching is allowed.
    for index in range(len(left)):
        if left[:index] + left[index + 1 :] == right:
            return True
    return False


def _reconcile_pixel_selected_labels(
    observed: set[str], expected: set[str]
) -> set[str] | None:
    """Canonicalize a pixel-proven selected set through one unique OCR bijection.

    The caller has already proved the selected fill for every member of
    ``observed`` from the matching screenshot.  This helper therefore changes
    identities only; it never promotes an unselected topic.  Exact identities
    are reserved first.  Every remaining observed label must then map to a
    different remaining checkpoint label by at most one OCR edit, and there
    must be exactly one complete mapping.  Unequal, missing, extra, or
    ambiguous sets are intentionally left unreconciled.
    """
    visible = set(observed)
    checkpoint = set(expected)
    if len(visible) != len(checkpoint):
        return None

    exact = visible & checkpoint
    remaining_visible = visible - exact
    remaining_checkpoint = checkpoint - exact
    if not remaining_visible:
        return set(checkpoint)

    candidates = {
        label: tuple(
            candidate
            for candidate in sorted(remaining_checkpoint)
            if _at_most_one_ocr_edit(label, candidate)
        )
        for label in remaining_visible
    }
    if any(not matches for matches in candidates.values()):
        return None

    # At most four topics are selected.  Count perfect matchings, stopping as
    # soon as a second one proves ambiguity.  Sorting by degree makes the
    # uniqueness proof deterministic and keeps the search tiny.
    ordered = sorted(remaining_visible, key=lambda label: (len(candidates[label]), label))
    matching_count = 0

    def count_matchings(index: int, used: set[str]) -> None:
        nonlocal matching_count
        if matching_count > 1:
            return
        if index == len(ordered):
            matching_count += 1
            return
        for candidate in candidates[ordered[index]]:
            if candidate in used:
                continue
            used.add(candidate)
            count_matchings(index + 1, used)
            used.remove(candidate)
            if matching_count > 1:
                return

    count_matchings(0, set())
    if matching_count != 1:
        return None
    return set(checkpoint)


class CustomizeSelectionUnclear(PhoneError):
    """A Customize selection frame that bounded local evidence cannot reconcile.

    Any tap is durably checkpointed before the action and pending verification
    obtains several fresh frames without tapping again.  If a pixel-proven set
    still cannot be matched uniquely to the checkpoint, scoped UI recovery may
    navigate the live screen; it still cannot change the checkpoint, queue, or
    container.
    """

    ai_recoverable = True


def _pending_checked_pill_matches(node, label: str, pending: dict) -> bool:
    """Bind one checked pill to the single tap saved before the live action.

    Exact labels survive Reddit's grid reflow.  OCR-drifted labels normally
    require the old row plus bounded proximity.  A selected pill may also gain
    a checkmark and expose/lose a diacritic (real a0244: the pending OCR and the
    checked OCR differed by more than one edit).  In that case, actual overlap
    with the saved pre-tap bounds on the same row is a stronger identity than
    the label.  Adjacent pills cannot satisfy the overlap gate.
    """
    canonical = str(pending["label"])
    px, py, pw, ph = pending["bounds"]
    same_row = abs(node.center()[1] - (py + ph / 2)) <= 24
    overlaps = min(node.x + node.w, px + pw) > max(node.x, px)
    nearby = abs(node.center()[0] - (px + pw / 2)) <= 160
    if label == canonical:
        return True
    if same_row and (overlaps or nearby) and _at_most_one_ocr_edit(label, canonical):
        return True
    return same_row and overlaps and nearby


def _customize_selected_pixel(pixel) -> bool:
    red, green, blue = pixel
    return (
        180 <= red <= 230
        and 195 <= green <= 240
        and 205 <= blue <= 245
        and 7 <= green - red <= 30
        and 4 <= blue - green <= 25
    )


def _pixel_only_selected_pill_proofs(image, nodes) -> list[list[int]]:
    """Find selected-color pills that have no overlapping OCR topic node.

    This is intentionally used only by the caller's first-selection pending
    recovery.  Reddit can widen that pill, move it to the next row, and omit
    its white label from OCR.  A unique selected-color component supplies new
    durable text-band bounds without guessing the missing label.
    """
    anchor = show_more_topics_node(nodes)
    skip, continue_node = customize_action_nodes(nodes)
    if skip is None or continue_node is None:
        return []
    content_bottom = anchor.y if anchor is not None else min(skip.y, continue_node.y) - 25

    def pixel_at(x: int, y: int):
        sample_x = round(x * image.width / 750)
        sample_y = round(y * image.height / 1334)
        if not (0 <= sample_x < image.width and 0 <= sample_y < image.height):
            return (0, 0, 0)
        return image.getpixel((sample_x, sample_y))

    bands: list[list[int]] = []
    active: list[int] | None = None
    for y in range(420, max(420, int(content_bottom))):
        selected_count = sum(
            _customize_selected_pixel(pixel_at(x, y)) for x in range(8, 748)
        )
        if selected_count >= 45:
            if active is None:
                active = [y, y]
            else:
                active[1] = y
        elif active is not None:
            bands.append(active)
            active = None
    if active is not None:
        bands.append(active)

    topic_nodes = customize_topics(nodes)
    proofs: list[list[int]] = []
    for top, bottom in bands:
        height = bottom - top + 1
        if not (20 <= height <= 70):
            continue
        minimum_column_pixels = max(6, height // 5)
        qualified = []
        for x in range(8, 748):
            count = sum(
                _customize_selected_pixel(pixel_at(x, y))
                for y in range(top, bottom + 1)
            )
            if count >= minimum_column_pixels:
                qualified.append(x)
        groups: list[list[int]] = []
        for x in qualified:
            if not groups or x - groups[-1][-1] > 8:
                groups.append([x])
            else:
                groups[-1].append(x)
        for group in groups:
            left, right = group[0], group[-1]
            width = right - left + 1
            if width < 60:
                continue
            proof = [left, round((top + bottom) / 2) - 9, width, 18]
            if not _valid_customize_bounds(proof):
                continue
            if any(_customize_topic_overlaps_bounds(node, proof) for node in topic_nodes):
                continue
            if _customize_pill_pixel_state(image, proof) == "selected":
                proofs.append(proof)
    return proofs


def _customize_pill_pixel_state(image, bounds) -> str:
    """Classify one proven topic rectangle from its fresh screenshot.

    ``bounds`` are the 750x1334 OCR coordinates durably saved before a tap.
    Reddit can replace that topic's entire OCR node with a checkmark after the
    tap (live a0267), so pending verification must be able to sample the saved
    rectangle directly.  The color thresholds and six sample points are the
    same strict evidence used for OCR-backed pills below.
    """
    if (
        not isinstance(bounds, (list, tuple))
        or len(bounds) != 4
        or any(type(value) not in (int, float) for value in bounds)
        or any(not math.isfinite(float(value)) for value in bounds)
    ):
        raise PhoneError("Saved pending Customize bounds are malformed; no topic was tapped.")
    x, y, width, height = (float(value) for value in bounds)
    if (
        x < 8
        or y < 420
        or width < 60
        or not (14 <= height <= 35)
        or x + width > 748
        or y + height >= CUSTOMIZE_ACTION_Y[0]
    ):
        raise PhoneError(
            "Saved pending Customize bounds are outside the topic region; no topic was tapped."
        )

    center_x = x + width / 2
    center_y = y + height / 2
    spread = min(width * 0.15, 25)
    selected_count = 0
    unselected_count = 0
    # OCR bounds describe the text, not the whole pill. Once Reddit inserts a
    # checkmark, long labels are shifted to the right and the text's right-side
    # sample can fall outside the pill entirely (live a0394). The left and
    # centre samples remain inside both selected and unselected pills, so use
    # only those four strict background samples. This keeps the classifier
    # fail-closed while allowing a pixel-proven checked pill to survive reflow.
    for offset_x in (-spread, 0):
        for offset_y in (-18, -14):
            sample_x = round((center_x + offset_x) * image.width / 750)
            sample_y = round((center_y + offset_y) * image.height / 1334)
            if not (0 <= sample_x < image.width and 0 <= sample_y < image.height):
                raise PhoneError("Customize topic is outside the screenshot.")
            pixel = image.getpixel((sample_x, sample_y))
            if _customize_selected_pixel(pixel):
                selected_count += 1
            elif min(pixel) >= 245:
                unselected_count += 1
    if selected_count >= 3:
        return "selected"
    if unselected_count >= 3:
        return "unselected"
    return "unknown"


def _clipped_expected_customize_nodes(nodes, expected_label: str):
    """Return exact expected pills confined to the narrow pre-action band.

    ``customize_topics`` intentionally excludes this band because an arbitrary
    OCR label partly hidden by the fixed action bar is unsafe to classify. A
    durable expected selection is different: when its exact checked label is
    fully above the action bar, the fresh pixels can prove it without a tap.
    """
    matches = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.on_screen():
            continue
        label = customize_topic_label(node)
        if _canonical_checked_label(label, {expected_label}) != expected_label:
            continue
        if (
            node.x < 8
            or node.x + node.w > 748
            or node.w < 60
            or not (14 <= node.h <= 35)
            or node.y + node.h < CUSTOMIZE_ACTION_Y[0] - 25
            or node.y + node.h >= CUSTOMIZE_ACTION_Y[0]
        ):
            continue
        matches.append(node)
    return matches


def _customize_bounds_belong_to_topic_region(nodes, bounds) -> bool:
    """Require durable bounds to remain inside this proven topic grid."""
    if not _valid_customize_bounds(bounds):
        return False
    x, y, width, height = (float(value) for value in bounds)
    anchor = show_more_topics_node(nodes)
    skip, continue_node = customize_action_nodes(nodes)
    if skip is None or continue_node is None:
        return False
    content_bottom = anchor.y if anchor is not None else min(skip.y, continue_node.y) - 25
    return (
        x >= 8
        and y >= 420
        and width >= 60
        and 14 <= height <= 35
        and x + width <= 748
        and y + height < content_bottom
    )


def _pending_bounds_belong_to_topic_region(nodes, pending: dict) -> bool:
    bounds = pending.get("bounds") if isinstance(pending, dict) else None
    return _customize_bounds_belong_to_topic_region(nodes, bounds)


def _customize_topic_overlaps_bounds(node, bounds) -> bool:
    """Return whether a live OCR topic occupies a durable proof rectangle."""
    x, y, width, height = (float(value) for value in bounds)
    same_row = abs(node.center()[1] - (y + height / 2)) <= 24
    horizontal = min(node.x + node.w, x + width) > max(node.x, x)
    return same_row and horizontal


def _selected_customize_topics(
    nodes,
    screenshot_stem: str,
    expected_labels: set[str] | None = None,
    pending: dict | None = None,
    pixel_selected_labels: set[str] | None = None,
    selected_proofs: dict[str, list] | None = None,
    observed_selected_bounds: dict[str, list] | None = None,
) -> set[str]:
    """Classify checked pills from the fresh same-stem screenshot, or stop."""
    from PIL import Image

    png_path = os.path.join(DUMP_DIR, screenshot_stem + ".png")
    dump_paths = [
        os.path.join(DUMP_DIR, screenshot_stem + ext)
        for ext in (".json", ".xml", ".txt")
        if os.path.isfile(os.path.join(DUMP_DIR, screenshot_stem + ext))
    ]
    if not dump_paths or not os.path.isfile(png_path):
        raise PhoneError("Customize screenshot is missing; no topic was tapped.")
    if os.stat(png_path).st_mtime_ns < max(os.stat(path).st_mtime_ns for path in dump_paths):
        raise PhoneError("Customize screenshot is stale; no topic was tapped.")
    selected: set[str] = set()
    expected = set(expected_labels or ())
    proofs = dict(selected_proofs or {})
    if not _valid_customize_proofs(proofs, pending):
        raise PhoneError("Customize selection proofs are malformed; no topic was tapped.")
    if pixel_selected_labels is not None:
        pixel_selected_labels.clear()
    if observed_selected_bounds is not None:
        observed_selected_bounds.clear()
    try:
        with Image.open(png_path) as source:
            image = source.convert("RGB")
            if image.width < 750 or image.height < 1334:
                raise PhoneError("Customize screenshot has unexpected dimensions.")
            topic_nodes = customize_topics(nodes)
            pending_matches: list[tuple[str, object]] = []
            for node in topic_nodes:
                label = customize_topic_label(node)
                pixel_state = _customize_pill_pixel_state(
                    image, [node.x, node.y, node.w, node.h]
                )
                if pixel_state == "selected":
                    # Preserve the exact label attached to this pixel-proven
                    # selected pill.  The caller uses only this same-frame
                    # identity to keep an OCR alias out of the unselected pool;
                    # no fuzzy label is ever excluded from that pool.
                    if pixel_selected_labels is not None:
                        pixel_selected_labels.add(label)
                    # A checked pill's glyph is occasionally merged into its
                    # OCR label as a checkmark-like prefix (a0051: "V RPGs";
                    # a0145: "• Hiking").  Only remove that prefix when the
                    # remainder exactly matches a selection already expected
                    # by the resumable checkpoint; this avoids corrupting
                    # genuine V- or symbol-prefixed topic names.
                    label = _canonical_checked_label(label, expected)
                    if pending is not None:
                        canonical = str(pending["label"])
                        # Reddit reflows the pill grid when a selected pill
                        # grows to make room for its checkmark (observed on
                        # a0056: Formula 1 moved onto the following row). An
                        # exact canonical label is still a unique identity
                        # after that reflow. OCR-drift recovery stays stricter:
                        # it requires the original row and bounded proximity.
                        if _pending_checked_pill_matches(node, label, pending):
                            pending_matches.append((label, node))
                            label = canonical
                    selected.add(label)
                    if observed_selected_bounds is not None and label in expected:
                        observed_selected_bounds[label] = [
                            node.x, node.y, node.w, node.h,
                        ]
                elif pixel_state != "unselected":
                    raise CustomizeSelectionUnclear(
                        f"Customize topic {label!r} has ambiguous selected state; no topic was tapped."
                    )
            if pending is not None:
                if len(pending_matches) > 1:
                    raise CustomizeSelectionUnclear(
                        "Pending Customize tap mapped to multiple checked pills; no topic was tapped."
                    )
                if not pending_matches:
                    if not _pending_bounds_belong_to_topic_region(nodes, pending):
                        raise CustomizeSelectionUnclear(
                            "Pending Customize bounds no longer match the proven topic grid; "
                            "no topic was tapped."
                        )
                    if any(
                        _customize_topic_overlaps_bounds(node, pending["bounds"])
                        for node in topic_nodes
                    ):
                        raise CustomizeSelectionUnclear(
                            "A different OCR topic occupies the pending Customize bounds; "
                            "no topic was tapped."
                        )
                    pending_state = _customize_pill_pixel_state(image, pending["bounds"])
                    if pending_state == "selected":
                        canonical = str(pending["label"])
                        selected.add(canonical)
                        pending_matches.append((canonical, None))
                        log(
                            "Pending Customize selection verified from its saved "
                            "bounds after OCR omitted the selected label."
                        )
                    elif (
                        not selected
                        and not proofs
                        and expected == {str(pending["label"])}
                    ):
                        pixel_only = _pixel_only_selected_pill_proofs(image, nodes)
                        if len(pixel_only) != 1:
                            raise CustomizeSelectionUnclear(
                                "First pending Customize selection did not map to one "
                                "unique OCR-missing selected pill; no topic was tapped."
                            )
                        canonical = str(pending["label"])
                        selected.add(canonical)
                        pending_matches.append((canonical, None))
                        if observed_selected_bounds is not None:
                            observed_selected_bounds[canonical] = pixel_only[0]
                        log(
                            "First pending Customize selection verified as the unique "
                            "OCR-missing selected pill after Reddit reflowed it."
                        )
                    else:
                        raise CustomizeSelectionUnclear(
                            "Pending Customize label is absent and its saved bounds are not "
                            "proven selected; no topic was tapped."
                        )
            if expected_labels is not None:
                reconciled = _reconcile_pixel_selected_labels(selected, expected)
                if reconciled is not None:
                    selected = reconciled
                elif selected.issubset(expected):
                    active_proofs = dict(proofs)
                    if pending is not None:
                        active_proofs[str(pending["label"])] = list(pending["bounds"])
                    for missing in sorted(expected - selected):
                        clipped = _clipped_expected_customize_nodes(nodes, missing)
                        if len(clipped) > 1:
                            raise CustomizeSelectionUnclear(
                                "A clipped expected Customize selection mapped to multiple "
                                "OCR pills; no topic was tapped."
                            )
                        if clipped:
                            if _customize_pill_pixel_state(
                                image,
                                [clipped[0].x, clipped[0].y, clipped[0].w, clipped[0].h],
                            ) != "selected":
                                raise CustomizeSelectionUnclear(
                                    "A clipped expected Customize selection is not "
                                    "pixel-proven selected; no topic was tapped."
                                )
                            selected.add(missing)
                            log(
                                "Clipped expected Customize selection verified from its exact "
                                "checked label above the fixed action bar."
                            )
                            continue
                        bounds = active_proofs.get(missing)
                        if bounds is None:
                            raise CustomizeSelectionUnclear(
                                "A saved Customize selection disappeared from OCR and has no "
                                "durable bounds proof; no topic was tapped."
                            )
                        if not _customize_bounds_belong_to_topic_region(nodes, bounds):
                            raise CustomizeSelectionUnclear(
                                "Saved Customize proof no longer belongs to the proven topic "
                                "grid; no topic was tapped."
                            )
                        if any(
                            _customize_topic_overlaps_bounds(node, bounds)
                            for node in topic_nodes
                        ):
                            raise CustomizeSelectionUnclear(
                                "A current OCR topic overlaps a missing Customize selection's "
                                "saved proof; no topic was tapped."
                            )
                        if _customize_pill_pixel_state(image, bounds) != "selected":
                            raise CustomizeSelectionUnclear(
                                "An OCR-missing Customize selection is not pixel-proven at its "
                                "saved bounds; no topic was tapped."
                            )
                        selected.add(missing)
                        log(
                            "Saved Customize selection verified from durable bounds after "
                            "OCR omitted its label."
                        )
                    if selected != expected:
                        raise CustomizeSelectionUnclear(
                            "Pixel-proven Customize selections do not exactly match the saved "
                            "selection set; no topic was tapped."
                        )
                else:
                    raise CustomizeSelectionUnclear(
                        "Pixel-proven Customize selections do not uniquely match "
                        "the saved selection set; no topic was tapped."
                    )
    except (OSError, ValueError) as exc:
        raise PhoneError("Customize screenshot could not be checked safely.") from exc
    return selected


def _capture_customize_state(
    stack: TapStack,
    phone: Phone,
    stem: str,
    expected_labels: set[str] | None = None,
    pending: dict | None = None,
    pixel_selected_labels: set[str] | None = None,
    selected_proofs: dict[str, list] | None = None,
    observed_selected_bounds: dict[str, list] | None = None,
):
    _blob, nodes = dump_named(stack, phone, stem, log, shot=True)
    if not customize_screen(nodes):
        dump_named(stack, phone, stem + "_unproven", log, shot=True)
        raise CustomizeSelectionUnclear(
            "Customize your feed layout is unproven; saved a fresh diagnostic screenshot."
        )
    return nodes, _selected_customize_topics(
        nodes,
        stem,
        expected_labels,
        pending=pending,
        pixel_selected_labels=pixel_selected_labels,
        selected_proofs=selected_proofs,
        observed_selected_bounds=observed_selected_bounds,
    )


def _capture_pending_customize_state(
    stack: TapStack,
    phone: Phone,
    stem: str,
    expected_labels: set[str],
    pending: dict,
    pixel_selected_labels: set[str] | None = None,
    selected_proofs: dict[str, list] | None = None,
    observed_selected_bounds: dict[str, list] | None = None,
):
    """Re-capture a pending tap without ever issuing a second tap.

    Reddit can paint the selected fill before OCR settles on the checkmark
    glyph.  A bounded series of fresh screenshots safely resolves that visual
    race.  If every frame remains ambiguous, a typed visual-recovery error
    preserves the checkpoint before the controller considers its bounded AI
    fallback.
    """
    if not _valid_customize_proofs(dict(selected_proofs or {}), pending):
        raise PhoneError(
            "Customize checkpoint proof is malformed; did not retry or call UI recovery."
        )
    last_error: PhoneError | None = None
    for attempt in range(CUSTOMIZE_PENDING_VERIFY_RETRIES + 1):
        retry_stem = stem if attempt == 0 else f"{stem}_retry_{attempt}"
        frame_pixel_selected: set[str] = set()
        frame_selected_bounds: dict[str, list] = {}
        try:
            captured = _capture_customize_state(
                stack,
                phone,
                retry_stem,
                expected_labels,
                pending=pending,
                pixel_selected_labels=frame_pixel_selected,
                selected_proofs=selected_proofs,
                observed_selected_bounds=frame_selected_bounds,
            )
            if pixel_selected_labels is not None:
                pixel_selected_labels.clear()
                pixel_selected_labels.update(frame_pixel_selected)
            if observed_selected_bounds is not None:
                observed_selected_bounds.clear()
                observed_selected_bounds.update(frame_selected_bounds)
            return captured
        except CustomizeSelectionUnclear as exc:
            last_error = exc
            if attempt >= CUSTOMIZE_PENDING_VERIFY_RETRIES:
                raise CustomizeSelectionUnclear(str(exc)) from exc
            log(
                "Pending Customize selection is not yet proven; waiting for "
                f"a fresh frame ({attempt + 1}/{CUSTOMIZE_PENDING_VERIFY_RETRIES})."
            )
            time.sleep(CUSTOMIZE_PENDING_VERIFY_SETTLE_SECONDS)
    raise last_error or PhoneError("Pending Customize selection could not be proven.")


def _capture_existing_customize_state(
    stack: TapStack,
    phone: Phone,
    stem: str,
    expected_labels: set[str],
    *,
    pixel_selected_labels: set[str] | None = None,
    selected_proofs: dict[str, list] | None = None,
    observed_selected_bounds: dict[str, list] | None = None,
):
    """Re-prove existing selections from fresh frames without issuing a tap."""
    if not _valid_customize_proofs(dict(selected_proofs or {})):
        raise PhoneError(
            "Customize checkpoint proof is malformed; did not retry or call UI recovery."
        )
    last_error: CustomizeSelectionUnclear | None = None
    for attempt in range(CUSTOMIZE_PENDING_VERIFY_RETRIES + 1):
        retry_stem = stem if attempt == 0 else f"{stem}_retry_{attempt}"
        frame_pixel_selected: set[str] = set()
        frame_selected_bounds: dict[str, list] = {}
        try:
            captured = _capture_customize_state(
                stack,
                phone,
                retry_stem,
                expected_labels,
                pixel_selected_labels=frame_pixel_selected,
                selected_proofs=selected_proofs,
                observed_selected_bounds=frame_selected_bounds,
            )
            if pixel_selected_labels is not None:
                pixel_selected_labels.clear()
                pixel_selected_labels.update(frame_pixel_selected)
            if observed_selected_bounds is not None:
                observed_selected_bounds.clear()
                observed_selected_bounds.update(frame_selected_bounds)
            return captured
        except CustomizeSelectionUnclear as exc:
            last_error = exc
            if attempt >= CUSTOMIZE_PENDING_VERIFY_RETRIES:
                raise CustomizeSelectionUnclear(str(exc)) from exc
            log(
                "Customize selections are not yet proven; waiting for a fresh "
                f"frame ({attempt + 1}/{CUSTOMIZE_PENDING_VERIFY_RETRIES})."
            )
            time.sleep(CUSTOMIZE_PENDING_VERIFY_SETTLE_SECONDS)
    raise last_error or CustomizeSelectionUnclear(
        "Customize selections could not be proven from fresh frames."
    )


def _anchor_fully_visible(anchor, height: int) -> bool:
    """True when the anchor is clearly inside the frame, not cut by its bottom."""
    return anchor.y + anchor.h <= SHOW_MORE_RELATIVE_Y_MAX * height


def _prove_customize_anchor(
    stack: TapStack,
    phone: Phone,
    nodes,
    stem: str,
    *,
    expected_labels: set[str] | None = None,
    allow_scroll: bool = True,
):
    """Prove the topic-list boundary before any pill is classified.

    A clipped "Show More Topics" control is not a confident boundary: its mangled
    label can slip into the topic list (the a0140 "Chaw More Tonine" failure) and
    the topics below it are not yet on screen.  Take a fresh snapshot, nudge the
    list with a bounded number of small scrolls until the control is fully
    visible, then re-prove.  If it never becomes reliable, stop without tapping.
    """
    for attempt in range(CUSTOMIZE_ANCHOR_FIX_SCROLLS + 1):
        anchor = show_more_topics_node(nodes)
        # Once all four durable identities are present, this pass cannot choose
        # or tap another topic.  Reddit may reflow the final row so the lower
        # anchor disappears behind its fixed Skip/Continue bar (live a0292).
        # The complete title/search/action geometry plus the action-bar-bounded
        # topic extractor is sufficient for a read-only verification pass.
        if (
            anchor is None
            and len(set(expected_labels or ())) >= INTEREST_SELECTION_COUNT
            and customize_screen(nodes)
            and customize_search_node(nodes) is not None
        ):
            log(
                "Customize anchor is hidden after all required selections; "
                "continuing with bounded read-only verification."
            )
            return nodes
        if anchor is not None and _label_words(anchor):
            # Only the saved png can prove the lower edge is inside the frame;
            # without it, a labelled marker is still trusted.
            from PIL import Image

            png_path = os.path.join(DUMP_DIR, stem + ".png")
            if not os.path.isfile(png_path):
                return nodes
            with Image.open(png_path) as source:
                height = source.height
            if _anchor_fully_visible(anchor, height):
                return nodes
        if attempt == CUSTOMIZE_ANCHOR_FIX_SCROLLS:
            break
        if not allow_scroll:
            raise CustomizeSelectionUnclear(
                "Customize anchor is clipped while durable selection bounds are active; "
                "preserved the screen without scrolling or tapping."
            )
        log("Customize topic anchor is clipped; scrolling once to prove the topic list.")
        small_scroll(stack)
        time.sleep(CUSTOMIZE_ANCHOR_FIX_SETTLE_SECONDS)
        stem = stem + "_anchor_fix"
        _blob, nodes = dump_named(stack, phone, stem, log, shot=True)
    raise PhoneError(
        "Customize topic list boundary (Show More Topics) is clipped or could not be "
        "proven; saved a fresh screenshot and did not tap any topic."
    )


def _clip_leading_words(node, keep: int):
    """Keep only the first ``keep`` OCR words of a node, preserving its bounds."""
    words = _label_words(node)
    if len(words) <= keep:
        return node
    trimmed = " ".join(words[:keep])
    return Node(
        ntype=node.ntype,
        name=node.name,
        label=trimmed,
        value=node.value,
        text=trimmed,
        x=node.x,
        y=node.y,
        w=node.w,
        h=node.h,
        visible=node.visible,
        enabled=node.enabled,
        raw=node.raw,
    )


def _customize_topic_pool(
    nodes,
    selected: set[str],
    pixel_selected_labels: set[str],
) -> dict[str, object]:
    """Build the visible pool without re-offering same-frame selected pills.

    ``pixel_selected_labels`` contains only exact OCR labels whose pill fill was
    proven selected in the screenshot paired with ``nodes``.  Skipping those
    exact labels keeps a reconciled OCR alias out of the candidate pool without
    fuzzy-matching (and therefore without hiding any unselected topic).
    """
    anchor = show_more_topics_node(nodes)
    pool: dict[str, object] = {}
    for node in customize_topics(nodes):
        if anchor is not None and _reads_show_more(node):
            # Belt and braces for the clipped boundary: even if the anchor's
            # mangled tail survived the shape filter, only its canonical
            # "show more" head may ever enter the pool - never as a topic.
            node = _clip_leading_words(node, SHOW_MORE_KEEP_WORDS)
        visible_label = customize_topic_label(node)
        if visible_label in pixel_selected_labels:
            continue
        # Prefix canonicalization is exact against an already verified
        # selection.  One-edit set reconciliation is deliberately not applied
        # to the unselected pool.
        canonical_label = _canonical_checked_label(visible_label, selected)
        pool[canonical_label] = node
    return pool


def _merge_customize_selected_proofs(
    existing: dict[str, list],
    observed: dict[str, list],
    selected: set[str],
    *,
    pending: dict | None = None,
) -> dict[str, list]:
    """Build the next durable proof map only from locally verified evidence."""
    merged = {
        label: list(bounds)
        for label, bounds in existing.items()
        if label in selected
    }
    for label, bounds in observed.items():
        if label in selected:
            merged[label] = list(bounds)
    if pending is not None:
        label = str(pending["label"])
        if label not in selected:
            raise PhoneError(
                "Pending Customize proof cannot be saved before local selection verification."
            )
        merged[label] = list(observed.get(label, pending["bounds"]))
    if not set(merged).issubset(selected) or not _valid_customize_proofs(merged):
        raise PhoneError(
            "Verified Customize selections produced ambiguous durable proof bounds."
        )
    return merged


def _customize_topic_follows_proof(node, bounds) -> bool:
    """Avoid moving an OCR-hidden selected pill by expanding an earlier pill."""
    x, y, width, height = (float(value) for value in bounds)
    proof_y = y + height / 2
    node_y = node.center()[1]
    if node_y > proof_y + 24:
        return True
    return abs(node_y - proof_y) <= 24 and node.x >= x + width


def pick_customize_feed(
    stack: TapStack,
    phone: Phone,
    sid: str,
    *,
    initial_nodes=None,
    prior: list[str] | None = None,
) -> list[str]:
    checkpoint = _load_customize_checkpoint(sid)
    checkpoint["version"] = CUSTOMIZE_CHECKPOINT_VERSION
    checkpoint.setdefault("pending_customize", None)
    checkpoint.setdefault("selected_proofs", {})
    if prior is not None:
        initial = list(dict.fromkeys(prior))
        if checkpoint["initial"] and checkpoint["initial"] != initial:
            raise PhoneError("Saved initial interests differ from this run; no topic was tapped.")
        checkpoint["initial"] = initial
        _save_customize_checkpoint(sid, checkpoint)
    initial = checkpoint["initial"]
    pending = checkpoint["pending_customize"]
    selected_proofs = {
        label: list(bounds)
        for label, bounds in checkpoint["selected_proofs"].items()
    }
    expected = set(checkpoint["customize"])
    if pending is not None:
        expected.add(str(pending["label"]))
    pixel_selected_labels: set[str] = set()
    observed_selected_bounds: dict[str, list] = {}
    # Prove the topic-list boundary from the live screen before anything else is
    # classified or tapped.  A clipped boundary is the a0140 failure mode.
    anchor_nodes = initial_nodes if initial_nodes is not None else []
    nodes = _prove_customize_anchor(
        stack,
        phone,
        anchor_nodes,
        "924_customize_live",
        expected_labels=expected,
        allow_scroll=not (pending is not None or selected_proofs),
    )
    if pending is None:
        nodes, selected = _capture_existing_customize_state(
            stack,
            phone,
            "924_customize_live",
            expected,
            pixel_selected_labels=pixel_selected_labels,
            selected_proofs=selected_proofs,
            observed_selected_bounds=observed_selected_bounds,
        )
    else:
        nodes, selected = _capture_pending_customize_state(
            stack,
            phone,
            "924_customize_live",
            expected,
            pending=pending,
            pixel_selected_labels=pixel_selected_labels,
            selected_proofs=selected_proofs,
            observed_selected_bounds=observed_selected_bounds,
        )
    saved = set(checkpoint["customize"])
    if not saved.issubset(selected):
        raise PhoneError("Saved customize selections are missing on screen; no topic was tapped.")
    recovered_pending = pending is not None
    if pending is not None:
        canonical = str(pending["label"])
        if selected != saved | {canonical}:
            raise PhoneError(
                "Pending Customize tap did not produce exactly one selected pill; no topic was tapped."
            )
        selected_proofs = _merge_customize_selected_proofs(
            selected_proofs,
            observed_selected_bounds,
            selected,
            pending=pending,
        )
        # Commit the verified selection, its proof, and clearing the pending
        # action in one atomic checkpoint replacement.  A failed save leaves
        # the old pending action intact for a safe resume.
        checkpoint.update(
            customize=sorted(selected),
            selected_proofs=selected_proofs,
            pending_customize=None,
        )
        pending = None
    else:
        selected_proofs = _merge_customize_selected_proofs(
            selected_proofs,
            observed_selected_bounds,
            selected,
        )
    if len(selected) > INTEREST_SELECTION_COUNT:
        raise PhoneError("Customize your feed has more than four selected topics; no topic was tapped.")
    checkpoint["customize"] = sorted(selected)
    checkpoint["selected_proofs"] = selected_proofs
    checkpoint["pending_customize"] = None
    _save_customize_checkpoint(sid, checkpoint)
    if recovered_pending:
        CREATION_PROGRESS.note()

    want = INTEREST_SELECTION_COUNT
    while len(selected) < want:
        pool = _customize_topic_pool(nodes, selected, pixel_selected_labels)
        hidden_proofs = [
            bounds
            for label, bounds in selected_proofs.items()
            if label not in observed_selected_bounds
        ]
        choices = [
            label
            for label, node in pool.items()
            if label not in selected
            and all(
                _customize_topic_follows_proof(node, bounds)
                for bounds in hidden_proofs
            )
        ]
        if len(choices) < want - len(selected):
            raise CustomizeSelectionUnclear(
                "Customize your feed has too few safe topics after the last OCR-hidden "
                "selection; no topic was tapped."
            )
        # Narrow pills are less likely to push the lower-page anchor beneath
        # Reddit's fixed Continue bar when the selected pill grows wider.
        choices.sort(key=lambda item: (pool[item].w, item))
        label = random.SystemRandom().choice(choices[:5])
        found = pool[label]
        checkpoint["pending_customize"] = {
            "label": label,
            "bounds": [found.x, found.y, found.w, found.h],
        }
        _save_customize_checkpoint(sid, checkpoint)
        log(f"tap customize {label!r} [{found.x},{found.y} {found.w}x{found.h}]")
        stack.tap_xy(*found.center())
        time.sleep(0.45)
        nodes, observed = _capture_pending_customize_state(
            stack,
            phone,
            f"925_customize_verified_{len(selected) + 1}",
            selected | {label},
            pending=checkpoint["pending_customize"],
            pixel_selected_labels=pixel_selected_labels,
            selected_proofs=selected_proofs,
            observed_selected_bounds=observed_selected_bounds,
        )
        if observed != selected | {label}:
            raise PhoneError(
                f"Customize tap {label!r} was not verified as one new selection; "
                "saved a screenshot and did not tap again."
            )
        selected = observed
        selected_proofs = _merge_customize_selected_proofs(
            selected_proofs,
            observed_selected_bounds,
            selected,
            pending=checkpoint["pending_customize"],
        )
        checkpoint.update(
            customize=sorted(selected),
            selected_proofs=selected_proofs,
            pending_customize=None,
        )
        _save_customize_checkpoint(sid, checkpoint)
        CREATION_PROGRESS.note()

    ready, confirmed = _capture_existing_customize_state(
        stack,
        phone,
        "926_customize_picked",
        selected,
        pixel_selected_labels=pixel_selected_labels,
        selected_proofs=selected_proofs,
        observed_selected_bounds=observed_selected_bounds,
    )
    if confirmed != selected:
        raise CustomizeSelectionUnclear(
            "Customize selections changed before Continue; did not tap."
        )
    selected_proofs = _merge_customize_selected_proofs(
        selected_proofs,
        observed_selected_bounds,
        selected,
    )
    checkpoint["selected_proofs"] = selected_proofs
    _save_customize_checkpoint(sid, checkpoint)
    if not tap_continue_bar(
        stack,
        ready,
        allow_selected_bar=False,
        enabled_stem="926_customize_picked",
    ):
        raise PhoneError("Customize topics selected but exact Continue is unavailable.")
    next_nodes = _wait_screen_close(
        stack,
        phone,
        customize_screen,
        "927_after_customize",
        "Customize your feed",
    )
    checkpoint["pending_customize"] = None
    _save_customize_checkpoint(sid, checkpoint)
    all_tapped = list(initial) + sorted(selected)
    _record_interests(sid, all_tapped)
    log("CUSTOMIZE_TAPPED " + ", ".join(sorted(selected)))
    advance_post_interests(stack, phone, initial_nodes=next_nodes)
    log("Customize your feed closed and post-signup prompts completed.")
    return all_tapped


def pick_interests(stack: TapStack, phone: Phone, sid: str) -> list[str]:
    want = INTEREST_SELECTION_COUNT
    blob, nodes = dump_named(stack, phone, "920_interests", log, shot=True)
    if (
        notification_sheet(nodes)
        or native_notification_prompt(nodes)
        or ads_personalization_screen(nodes)
        or proven_home_feed(nodes)
    ):
        advance_post_interests(stack, phone, initial_nodes=nodes)
        return []
    if _customize_titled_but_unproven(nodes):
        _raise_unproven_customize()
    if customize_screen(nodes):
        return pick_customize_feed(stack, phone, sid, initial_nodes=nodes)
    if not interests_screen(nodes):
        raise PhoneError("Not on the proven Interests screen. See 920_interests.png")
    grid = choose_grid(nodes)

    pool: dict[str, object] = {}
    for n in chips(nodes):
        pool[interest_name(n, grid=grid)] = n
    if not grid:
        small_scroll(stack)
        time.sleep(0.55)
        blob, nodes = dump_named(stack, phone, "921_scrolled", log, shot=True)
        for n in chips(nodes):
            pool.setdefault(interest_name(n, grid=False), n)

    names = list(pool.keys())
    if not names:
        raise PhoneError("No interest chips in dump.")
    if len(names) < want:
        raise PhoneError(
            f"Interests exposes {len(names)} choices but this run requires {want}. "
            "Did not select fewer than requested."
        )
    chosen = random.SystemRandom().sample(names, want)
    log("PICK " + ", ".join(chosen))

    tapped: list[str] = []
    for label in chosen:
        found = None
        attempts = 3 if grid else 6
        for attempt in range(attempts):
            blob, nodes = dump_named(stack, phone, f"922_{len(tapped)}", log, shot=False)
            if not interests_screen(nodes):
                raise PhoneError(
                    "Interests screen changed before all selected categories were tapped."
                )
            CREATION_PROGRESS.note()
            for n in chips(nodes):
                if interest_name(n, grid=grid) == label:
                    found = n
                    break
            if found is not None:
                break
            if not grid:
                small_scroll(stack, back=(attempt % 2 == 1))
            time.sleep(0.45)
        if found is None:
            log(f"SKIP missing chip {label!r}")
            continue
        log(f"tap {label!r} [{found.x},{found.y} {found.w}x{found.h}]")
        stack.tap_xy(*found.center())
        tapped.append(label)
        CREATION_PROGRESS.note()
        time.sleep(0.45)

    if not tapped:
        raise PhoneError("Did not tap any interest.")

    blob, nodes = dump_named(stack, phone, "923_picked", log, shot=True)
    if not interests_screen(nodes):
        raise PhoneError("Interests screen changed before Continue. Did not guess a tap.")
    if not tap_continue_bar(
        stack,
        nodes,
        allow_selected_bar=not grid,
        enabled_stem="923_picked" if grid else None,
    ):
        time.sleep(0.6)
        blob, nodes = dump_named(stack, phone, "923b_bar", log, shot=True)
        if not interests_screen(nodes) or not tap_continue_bar(
            stack,
            nodes,
            allow_selected_bar=not grid,
            enabled_stem="923b_bar" if grid else None,
        ):
            raise PhoneError("Interests selected but Continue / selected bar not tappable.")

    last = _wait_screen_close(
        stack,
        phone,
        interests_screen,
        "924_after_interests",
        "Interests",
    )
    log("TAPPED " + ", ".join(tapped))
    checkpoint = _load_customize_checkpoint(sid)
    if checkpoint["initial"] and checkpoint["initial"] != tapped:
        raise PhoneError("Saved initial interests differ from the proven selections.")
    checkpoint["initial"] = list(tapped)
    _save_customize_checkpoint(sid, checkpoint)
    _record_interests(sid, tapped)
    if _customize_titled_but_unproven(last):
        _raise_unproven_customize()
    if customize_screen(last):
        return pick_customize_feed(
            stack,
            phone,
            sid,
            initial_nodes=last,
            prior=tapped,
        )
    log("Interests screen closed; next screen preserved for the caller.")
    return tapped


def main() -> int:
    positional = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    sid = (positional[0] if positional else "a0001").strip().lower()
    stay_open = "--stay-open" in sys.argv[1:]
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        pick_interests(stack, phone, sid)
        if stay_open:
            log("Interests complete; Reddit left open for the next workflow stage.")
        else:
            from friend_poster.reddit.reddit_open import close_reddit

            close_reddit(stack, phone, log)
            log("INTERESTS_DONE=HOME")
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
