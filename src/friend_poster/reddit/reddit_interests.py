# UTF-8
"""Pick random Reddit Interests chips, small scroll. Different set each account.

  python misc/reddit_interests.py
"""

from __future__ import annotations

import os
import json
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
        marker = words == expected or (
            len(words) == len(expected) + 1
            and len(words[0]) == 1
            and words[1:] == expected
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
        raise PhoneError(
            f"{label} remained after one exact Continue tap. "
            "Did not tap Continue again."
        )
    raise PhoneError(
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
        raise PhoneError(f"{label} remained after one exact tap. Did not tap again.")
    raise PhoneError(f"Screen stayed blank after {label}. Did not guess a next action.")


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
                raise PhoneError(
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
                raise PhoneError(
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
                    raise PhoneError(
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

        raise PhoneError(
            "Unknown screen after Interests. Preserved it without tapping; "
            "see the latest 928-931 dump."
        )

    if proven_home_feed(nodes):
        log("POST_INTERESTS=HOME_FEED")
        return nodes
    raise PhoneError("Post-Interests prompts did not reach a proven Home feed.")


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


def _load_customize_checkpoint(sid: str) -> dict:
    path = _customize_checkpoint_path(sid)
    if not os.path.isfile(path):
        return {
            "version": 2, "slot_id": sid, "initial": [], "customize": [],
            "pending_customize": None,
        }
    try:
        with open(path, encoding="utf-8") as source:
            state = json.load(source)
    except (OSError, ValueError) as exc:
        raise PhoneError("Interests checkpoint could not be read safely.") from exc
    if isinstance(state, dict) and state.get("version") == 1 and set(state) == {
        "version", "slot_id", "initial", "customize",
    }:
        state = {
            **state, "version": 2, "pending_customize": None,
        }
    pending = state.get("pending_customize") if isinstance(state, dict) else None
    if (
        not isinstance(state, dict)
        or set(state) != {
            "version", "slot_id", "initial", "customize", "pending_customize",
        }
        or state["version"] != 2
        or state["slot_id"] != sid
        or any(
            not isinstance(state[key], list)
            or any(not isinstance(item, str) or not item for item in state[key])
            or len(state[key]) != len(set(state[key]))
            for key in ("initial", "customize")
        )
        or len(state["customize"]) > INTEREST_SELECTION_COUNT
        or (
            pending is not None
            and (
                not isinstance(pending, dict)
                or set(pending) != {"label", "bounds"}
                or not isinstance(pending["label"], str)
                or not pending["label"]
                or not isinstance(pending["bounds"], list)
                or len(pending["bounds"]) != 4
                or any(type(value) not in (int, float) for value in pending["bounds"])
            )
        )
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
    This deliberately small comparison is only used after the pill fill has
    already been classified as selected and the row still matches the durable
    pending-tap checkpoint.
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


class CustomizeSelectionUnclear(PhoneError):
    """A pending Customize tap that local evidence cannot safely reconcile."""

    ai_recoverable = False


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


def _selected_customize_topics(
    nodes,
    screenshot_stem: str,
    expected_labels: set[str] | None = None,
    pending: dict | None = None,
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
    try:
        with Image.open(png_path) as source:
            image = source.convert("RGB")
            if image.width < 750 or image.height < 1334:
                raise PhoneError("Customize screenshot has unexpected dimensions.")
            pending_matches: list[tuple[str, object]] = []
            for node in customize_topics(nodes):
                label = customize_topic_label(node)
                classifications: list[str] = []
                # XXTouch JSON coordinates are text centers. The pill fill is
                # visible just above the glyphs, away from the checkmark.
                cx, cy = node.center()
                spread = min(node.w * 0.15, 25)
                for offset_x in (-spread, 0, spread):
                    # The lower samples can land on the tall glyphs in this
                    # device's OCR (observed on a0048 Public Transit). Both
                    # positions stay inside the pill above those glyphs.
                    for offset in (-18, -14):
                        sx = round((cx + offset_x) * image.width / 750)
                        sy = round((cy + offset) * image.height / 1334)
                        if not (0 <= sx < image.width and 0 <= sy < image.height):
                            raise PhoneError("Customize topic is outside the screenshot.")
                        red, green, blue = image.getpixel((sx, sy))
                        if 180 <= red <= 230 and 195 <= green <= 240 and 205 <= blue <= 245 and 7 <= green - red <= 30 and 4 <= blue - green <= 25:
                            classifications.append("selected")
                        elif min(red, green, blue) >= 245:
                            classifications.append("unselected")
                        else:
                            classifications.append("unknown")
                if classifications.count("selected") >= 5:
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
                elif classifications.count("unselected") < 5:
                    raise PhoneError(
                        f"Customize topic {label!r} has ambiguous selected state; no topic was tapped."
                    )
            if pending is not None and len(pending_matches) != 1:
                raise PhoneError(
                    "Pending Customize tap did not map to exactly one checked pill; no topic was tapped."
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
):
    _blob, nodes = dump_named(stack, phone, stem, log, shot=True)
    if not customize_screen(nodes):
        dump_named(stack, phone, stem + "_unproven", log, shot=True)
        raise PhoneError(
            "Customize your feed layout is unproven; saved a fresh diagnostic screenshot."
        )
    return nodes, _selected_customize_topics(
        nodes, stem, expected_labels, pending=pending
    )


def _capture_pending_customize_state(
    stack: TapStack,
    phone: Phone,
    stem: str,
    expected_labels: set[str],
    pending: dict,
):
    """Re-capture a pending tap without ever issuing a second tap.

    Reddit can paint the selected fill before OCR settles on the checkmark
    glyph.  A bounded series of fresh screenshots safely resolves that visual
    race.  If every frame remains ambiguous, a typed non-AI error preserves the
    checkpoint and pauses rather than spending recovery API calls.
    """
    last_error: PhoneError | None = None
    for attempt in range(CUSTOMIZE_PENDING_VERIFY_RETRIES + 1):
        retry_stem = stem if attempt == 0 else f"{stem}_retry_{attempt}"
        try:
            return _capture_customize_state(
                stack,
                phone,
                retry_stem,
                expected_labels,
                pending=pending,
            )
        except PhoneError as exc:
            last_error = exc
            if attempt >= CUSTOMIZE_PENDING_VERIFY_RETRIES:
                raise CustomizeSelectionUnclear(str(exc)) from exc
            log(
                "Pending Customize selection is not yet proven; waiting for "
                f"a fresh frame ({attempt + 1}/{CUSTOMIZE_PENDING_VERIFY_RETRIES})."
            )
            time.sleep(CUSTOMIZE_PENDING_VERIFY_SETTLE_SECONDS)
    raise last_error or PhoneError("Pending Customize selection could not be proven.")


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


def pick_customize_feed(
    stack: TapStack,
    phone: Phone,
    sid: str,
    *,
    initial_nodes=None,
    prior: list[str] | None = None,
) -> list[str]:
    checkpoint = _load_customize_checkpoint(sid)
    checkpoint.setdefault("pending_customize", None)
    if prior is not None:
        initial = list(dict.fromkeys(prior))
        if checkpoint["initial"] and checkpoint["initial"] != initial:
            raise PhoneError("Saved initial interests differ from this run; no topic was tapped.")
        checkpoint["initial"] = initial
        _save_customize_checkpoint(sid, checkpoint)
    initial = checkpoint["initial"]
    pending = checkpoint["pending_customize"]
    expected = set(checkpoint["customize"])
    if pending is not None:
        expected.add(str(pending["label"]))
    # Prove the topic-list boundary from the live screen before anything else is
    # classified or tapped.  A clipped boundary is the a0140 failure mode.
    anchor_nodes = initial_nodes if initial_nodes is not None else []
    nodes = _prove_customize_anchor(
        stack, phone, anchor_nodes, "924_customize_live", expected_labels=expected
    )
    if pending is None:
        nodes, selected = _capture_customize_state(
            stack, phone, "924_customize_live", expected
        )
    else:
        nodes, selected = _capture_pending_customize_state(
            stack, phone, "924_customize_live", expected, pending=pending
        )
    saved = set(checkpoint["customize"])
    if not saved.issubset(selected):
        raise PhoneError("Saved customize selections are missing on screen; no topic was tapped.")
    if pending is not None:
        canonical = str(pending["label"])
        if selected != saved | {canonical}:
            raise PhoneError(
                "Pending Customize tap did not produce exactly one selected pill; no topic was tapped."
            )
        checkpoint["customize"] = sorted(selected)
        _save_customize_checkpoint(sid, checkpoint)
        CREATION_PROGRESS.note()
    if len(selected) > INTEREST_SELECTION_COUNT:
        raise PhoneError("Customize your feed has more than four selected topics; no topic was tapped.")
    checkpoint["customize"] = sorted(selected)
    _save_customize_checkpoint(sid, checkpoint)

    want = INTEREST_SELECTION_COUNT
    if len(selected) < want and checkpoint["pending_customize"] is not None:
        # The recovered tap is proven, but another tap needs its own identity.
        checkpoint["pending_customize"] = None
        _save_customize_checkpoint(sid, checkpoint)
    while len(selected) < want:
        anchor = show_more_topics_node(nodes)
        pool: dict[str, object] = {}
        for node in customize_topics(nodes):
            if anchor is not None and _reads_show_more(node):
                # Belt and braces for the clipped boundary: even if the anchor's
                # mangled tail survived the shape filter, only its canonical
                # "show more" head may ever enter the pool - never as a topic.
                node = _clip_leading_words(node, SHOW_MORE_KEEP_WORDS)
            # Selected pills may expose their checkmark as an OCR prefix
            # (``V Italy``, ``• Hiking``, etc.).  Canonicalize every visible
            # row against the already verified selections before constructing
            # the candidate pool, so an existing selection can never be
            # offered and tapped a second time under its prefixed spelling.
            visible_label = customize_topic_label(node)
            canonical_label = _canonical_checked_label(visible_label, selected)
            pool[canonical_label] = node
        choices = [label for label in pool if label not in selected]
        if len(choices) < want - len(selected):
            raise PhoneError("Customize your feed has too few unselected topics; no topic was tapped.")
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
        )
        if observed != selected | {label}:
            raise PhoneError(
                f"Customize tap {label!r} was not verified as one new selection; "
                "saved a screenshot and did not tap again."
            )
        selected = observed
        checkpoint["customize"] = sorted(selected)
        checkpoint["pending_customize"] = None
        _save_customize_checkpoint(sid, checkpoint)
        CREATION_PROGRESS.note()

    if checkpoint["pending_customize"] is None:
        ready, confirmed = _capture_customize_state(
            stack, phone, "926_customize_picked", selected
        )
    else:
        ready, confirmed = _capture_pending_customize_state(
            stack, phone, "926_customize_picked", selected,
            pending=checkpoint["pending_customize"],
        )
    if confirmed != selected:
        raise PhoneError("Customize selections changed before Continue; did not tap.")
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
