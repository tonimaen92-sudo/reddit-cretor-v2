# UTF-8
"""Create a Reddit account, then verify and complete app cleanup.

  python misc/reddit_create_day1.py r2
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date
from friend_poster.core.config import REDDIT_BUNDLE  # noqa: E402
from friend_poster.reddit.feed_swipe import normalize_feed_swipe_count, run_feed_swipes  # noqa: E402
from friend_poster.device.icloud_code import (  # noqa: E402
    PollCancelled,
    VerificationCodeTimeout,
    poll_reddit_code,
)
from friend_poster.reddit.reddit_about import birthday_confirmation_screen, tap_woman  # noqa: E402
from friend_poster.reddit.reddit_enter_code import (  # noqa: E402
    invalid_code_message,
    resend_invalid_code_once,
    type_code,
)
from friend_poster.reddit.reddit_interests import (  # noqa: E402
    ads_personalization_screen,
    advance_post_interests,
    native_notification_prompt,
    notification_sheet,
    pick_interests,
    proven_home_feed,
)
from friend_poster.reddit.reddit_open import (  # noqa: E402
    FeedCleanupProof,
    _screen_signature,
    close_reddit,
    make_feed_cleanup_proof,
    make_terminal_feed_cleanup_proof,
    open_container,
    proven_reddit_feed_structure,
    proven_springboard,
    switcher_app_card,
    switcher_app_removed_or_home,
    switcher_centered_title,
)
from friend_poster.reddit.reddit_password import (  # noqa: E402
    ABOUT_MARKERS,
    dismiss_passkey_prompt,
    passkey_prompt,
    resume_saved_password,
    type_password,
)
from friend_poster.reddit.reddit_to_verify import (  # noqa: E402
    at_verify,
    blob_of,
    proven_verify_back_screen,
    return_verify_to_email_entry,
    run_to_verify,
    welcome,
)
from friend_poster.reddit.reddit_username import USER_RE, keep_suggested_username, use_requested_username  # noqa: E402
from friend_poster.core.slots import (  # noqa: E402
    creation_feed_swipe_progress,
    clear_email_verification,
    email_verification_state,
    get_slot,
    mark_email_verification,
    mark_creation_feed_swipe_completed,
    mark_reddit_cleanup,
    mark_reddit_created,
    reddit_cleanup_state,
    slot_reddit_done,
)
from friend_poster.device.ssh_device import Phone, PhoneError  # noqa: E402
from friend_poster.device.ui_tree import OperationCancelled, TapStack, dump_named, find_label_exact  # noqa: E402

os.environ["SKIP_SHOT"] = "1"

VERIFY_RESUME_LOOKBACK_SECONDS = 30 * 60
MAX_EMAIL_REPLACEMENTS = 3
FINAL_HOME_VERIFY_MAX_DUMPS = 3
FINAL_HOME_VERIFY_DELAY_SECONDS = 0.8


class EmailReplacementExhausted(PhoneError):
    """All allowed Hide My Email replacements timed out for one slot."""

    failure_code = "reddit_email_replacements_exhausted"


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


def wait_named(
    stack: TapStack,
    phone: Phone,
    names: tuple[str, ...],
    stem: str,
    tries: int = 8,
    *,
    preview: bool = True,
    stop=None,
):
    nodes = None
    for i in range(tries):
        _check_stop(stop)
        blob, nodes = dump_named(
            stack,
            phone,
            f"{stem}_{i}",
            log,
            shot=True,
            preview=preview,
        )
        nag = find_label_exact(nodes, ("Not now", "Not Now", "Maybe later"))
        if nag is not None:
            log(f"Dismiss {nag.labels()[:40]!r}")
            stack.tap_xy(*nag.center())
            _wait(stop, 1.0)
            continue
        if find_label_exact(nodes, names) is not None:
            return nodes
        _wait(stop, 1.1)
    return nodes


def wait_verification_outcome(
    stack: TapStack,
    phone: Phone,
    initial_nodes,
    *,
    stem: str,
    tries: int = 8,
    stop=None,
) -> tuple[str, list]:
    """Wait for Username or Reddit's exact invalid-code response.

    Privacy-sensitive Verify dumps never emit label previews.  The invalid
    response is terminal for this wait so recovery can stay explicitly
    one-shot.
    """
    nodes = list(initial_nodes or [])
    for attempt in range(tries + 1):
        _check_stop(stop)
        if find_label_exact(nodes, ("Create your username",)) is not None:
            return "username", nodes
        if invalid_code_message(nodes) is not None:
            return "invalid", nodes
        if attempt == tries:
            break
        _wait(stop, 1.1)
        _blob, nodes = dump_named(
            stack,
            phone,
            f"{stem}_{attempt}",
            log,
            shot=False,
            persist=False,
            preview=False,
        )
    return "unknown", nodes


def recover_invalid_verification_code(
    stack: TapStack,
    phone: Phone,
    email: str,
    invalid_nodes,
    *,
    log_fn=None,
    stop=None,
) -> list:
    """Perform the sole allowed Resend and require a Username transition."""
    emit = log_fn or log
    if not email.strip():
        raise PhoneError("Cannot recover verification without the slot email hint.")

    resend_at, stale_code, _cleared_nodes = resend_invalid_code_once(
        stack,
        phone,
        invalid_nodes,
        emit,
    )
    emit("IMAP poll for the code sent after the one Resend.")
    try:
        replacement = poll_reddit_code(
            log=emit,
            timeout=150,
            after_ts=resend_at,
            email_hint=email,
            stop=stop,
        )
    except PollCancelled as exc:
        raise PhoneError(str(exc)) from exc
    except RuntimeError as exc:
        raise PhoneError(str(exc)) from exc
    if replacement == stale_code:
        raise PhoneError(
            "The post-Resend poll returned the already-rejected code; did not enter it."
        )
    emit("IMAP verification code received after Resend.")
    result_nodes = type_code(stack, phone, replacement)
    outcome, nodes = wait_verification_outcome(
        stack,
        phone,
        result_nodes,
        stem="889_resend_user",
        stop=stop,
    )
    if outcome == "invalid":
        raise PhoneError(
            "The replacement verification code was rejected; Resend was not tapped again."
        )
    if outcome != "username":
        raise PhoneError(
            "Replacement code did not reach Create your username; preserved the current screen."
        )
    return nodes


def _active_email_identity(sid: str) -> tuple[str, int, str]:
    """Return the slot's active address plus its exact durable label identity."""
    slot = get_slot(sid)
    if not slot:
        raise PhoneError(f"No slot {sid}.")
    email = str(slot.get("email") or "").strip().lower()
    if not email:
        raise PhoneError(f"{sid} has no active Hide My Email address.")
    try:
        index = int(slot.get("email_replacement_index") or 0)
    except (TypeError, ValueError):
        raise PhoneError(f"{sid} has a malformed email replacement checkpoint.") from None
    label = str(slot.get("email_label") or sid).strip().lower()
    expected = sid if index == 0 else f"{sid}-{index}"
    if index < 0 or label != expected:
        raise PhoneError(f"{sid} active email label does not match its replacement checkpoint.")
    return email, index, label


def _verify_email_with_replacements(
    sid: str,
    phone: Phone,
    stack: TapStack,
    initial_nodes,
    *,
    email_replacer=None,
    rotate_before_poll: bool = False,
    max_email_replacements: int = MAX_EMAIL_REPLACEMENTS,
    log_fn=None,
    stop=None,
) -> list:
    """Reach Username using one active alias at a time with crash-safe rotation."""
    emit = log_fn or log
    current_nodes = list(initial_nodes or [])
    email, active_index, active_label = _active_email_identity(sid)
    try:
        checkpoint = email_verification_state(get_slot(sid))
    except ValueError as exc:
        raise PhoneError(f"{sid} email verification checkpoint is malformed.") from exc

    if rotate_before_poll and checkpoint is None:
        if proven_springboard(current_nodes):
            emit(
                f"{sid}: saved no-code recovery found iPhone Home; reopening only "
                "the existing exact Crane container to prove its Verify screen."
            )
            open_container(stack, phone, emit, sid, email)
            _blob, current_nodes = dump_named(
                stack,
                phone,
                "880_migrated_timeout_reopen",
                emit,
                shot=True,
                preview=False,
            )
        if not proven_verify_back_screen(current_nodes):
            raise PhoneError(
                "Saved no-code recovery requires Reddit's exact Verify screen "
                "after reopening its existing container; did not create a "
                "replacement alias."
            )
        mark_email_verification(
            sid, active_index, active_label, "timed_out", emit
        )
        checkpoint = email_verification_state(get_slot(sid))
        emit(
            f"{sid}: migrated the proven no-code pause; the next attempt is "
            f"replacement {active_index + 1}."
        )

    for _transition in range((max_email_replacements + 1) * 4 + 4):
        _check_stop(stop)
        email, active_index, active_label = _active_email_identity(sid)
        try:
            checkpoint = email_verification_state(get_slot(sid))
        except ValueError as exc:
            raise PhoneError(f"{sid} email verification checkpoint is malformed.") from exc

        if checkpoint is None:
            sent_at = time.time()
            if at_verify(current_nodes):
                sent_at -= VERIFY_RESUME_LOOKBACK_SECONDS
            else:
                run_to_verify(
                    stack, phone, sid, email, stop=stop, log_fn=emit
                )
            mark_email_verification(
                sid, active_index, active_label, "submitted", emit
            )
            checkpoint = email_verification_state(get_slot(sid))
            current_nodes = []
        else:
            phase = checkpoint["phase"]
            checkpoint_index = int(checkpoint["replacement_index"])
            checkpoint_label = str(checkpoint["label"])

            if phase == "replacement_pending":
                expected_index = active_index + 1
                expected_label = f"{sid}-{expected_index}"
                if (
                    checkpoint_index != expected_index
                    or checkpoint_label != expected_label
                ):
                    raise PhoneError(
                        f"{sid} pending replacement does not follow its active alias."
                    )
                if email_replacer is None:
                    raise PhoneError(
                        "Reddit verification email timed out; replacement Hide My Email "
                        "creation is unavailable in this run."
                    )
                replacement = str(
                    email_replacer(
                        email, checkpoint_index, checkpoint_label
                    )
                    or ""
                ).strip().lower()
                new_email, new_index, new_label = _active_email_identity(sid)
                ready = email_verification_state(get_slot(sid))
                if (
                    not replacement
                    or replacement != new_email
                    or new_index != checkpoint_index
                    or new_label != checkpoint_label
                    or not ready
                    or ready["phase"] != "replacement_ready"
                ):
                    raise PhoneError(
                        f"{sid} replacement alias did not reach its durable ready checkpoint."
                    )
                current_nodes = []
                continue

            if phase == "replacement_ready":
                if (
                    checkpoint_index != active_index
                    or checkpoint_label != active_label
                ):
                    raise PhoneError(
                        f"{sid} ready replacement does not match its active alias."
                    )
                _blob, fresh = dump_named(
                    stack,
                    phone,
                    "882_replacement_ready_probe",
                    emit,
                    shot=True,
                    preview=False,
                )
                sent_at = time.time()
                if at_verify(fresh):
                    sent_at -= VERIFY_RESUME_LOOKBACK_SECONDS
                    emit(
                        f"{sid}: replacement {active_index} was already submitted; "
                        "resume polling without submitting it again."
                    )
                else:
                    run_to_verify(
                        stack, phone, sid, email, stop=stop, log_fn=emit
                    )
                mark_email_verification(
                    sid, active_index, active_label, "submitted", emit
                )
                checkpoint = email_verification_state(get_slot(sid))
                current_nodes = []
            elif phase == "submitted":
                if (
                    checkpoint_index != active_index
                    or checkpoint_label != active_label
                ):
                    raise PhoneError(
                        f"{sid} submitted alias does not match its active email."
                    )
                if current_nodes and not at_verify(current_nodes):
                    raise PhoneError(
                        f"{sid} has a submitted email checkpoint, but Reddit is not on Verify. "
                        "Did not resubmit the address."
                    )
                sent_at = time.time() - VERIFY_RESUME_LOOKBACK_SECONDS
            elif phase == "timed_out":
                if (
                    checkpoint_index != active_index
                    or checkpoint_label != active_label
                ):
                    raise PhoneError(
                        f"{sid} timed-out alias does not match its active email."
                    )
                if active_index >= max_email_replacements:
                    raise EmailReplacementExhausted(
                        f"No Reddit verification email arrived for {sid} after the "
                        f"original address and {max_email_replacements} replacement aliases."
                    )
                _blob, fresh = dump_named(
                    stack,
                    phone,
                    "880_verify_alias_retry_probe",
                    emit,
                    shot=True,
                    preview=False,
                )
                if proven_verify_back_screen(fresh):
                    return_verify_to_email_entry(
                        stack, phone, stop=stop, log_fn=emit
                    )
                elif welcome(fresh):
                    emit(
                        f"{sid}: Reddit email-entry screen already proven after the "
                        "timed-out alias."
                    )
                else:
                    raise PhoneError(
                        "Timed-out alias recovery requires Reddit's exact Verify or "
                        "email-entry screen; did not create another alias."
                    )
                next_index = active_index + 1
                next_label = f"{sid}-{next_index}"
                mark_email_verification(
                    sid, next_index, next_label, "replacement_pending", emit
                )
                current_nodes = []
                continue
            else:  # validated by slots.email_verification_state
                raise PhoneError(f"{sid} has an unsupported verification phase.")

        emit("IMAP poll for Reddit 6-digit (not Mail app).")
        try:
            code = poll_reddit_code(
                log=emit,
                timeout=150,
                after_ts=sent_at,
                email_hint=email,
                stop=stop,
            )
        except PollCancelled as exc:
            raise PhoneError(str(exc)) from exc
        except VerificationCodeTimeout:
            mark_email_verification(
                sid, active_index, active_label, "timed_out", emit
            )
            current_nodes = []
            continue
        except RuntimeError as exc:
            raise PhoneError(str(exc)) from exc

        emit("IMAP verification code received.")
        result_nodes = type_code(stack, phone, code)
        outcome, username_nodes = wait_verification_outcome(
            stack,
            phone,
            result_nodes,
            stem="889_user",
            stop=stop,
        )
        if outcome == "invalid":
            username_nodes = recover_invalid_verification_code(
                stack,
                phone,
                email,
                username_nodes,
                log_fn=emit,
                stop=stop,
            )
        elif outcome != "username":
            raise PhoneError(
                "Verification did not reach Create your username; preserved the current screen."
            )
        clear_email_verification(sid, emit)
        return username_nodes

    raise PhoneError(f"{sid} exceeded the bounded email-verification state transitions.")


def wait_feed(stack: TapStack, phone: Phone, *, stop=None) -> FeedCleanupProof:
    """Wait for a strict Home feed and return immutable cleanup evidence."""
    for i in range(10):
        _check_stop(stop)
        blob, nodes = dump_named(stack, phone, f"925_feed_{i}", log, shot=True)
        if (
            notification_sheet(nodes)
            or native_notification_prompt(nodes)
            or ads_personalization_screen(nodes)
        ):
            nodes = advance_post_interests(stack, phone, initial_nodes=nodes)
        b = blob_of(nodes)
        if find_label_exact(
            nodes,
            ("Choose your interests", "Interests", "Customize your feed"),
        ) is not None:
            raise PhoneError("Still on Interests after Continue.")
        if find_label_exact(nodes, ("Verify your email",)) is not None:
            raise PhoneError("Still on Verify after create.")
        if "log in to reddit" in b or welcome(nodes):
            raise PhoneError("Dropped back to signup. See 925_feed dump.")
        if proven_home_feed(nodes):
            proof = make_feed_cleanup_proof(nodes)
            if proof is not None:
                log("Home feed proven for immediate cleanup.")
                return proof
            log("Home feed detected but cleanup proof is incomplete; waiting for a fresh frame.")
        _wait(stop, 1.2)
    raise PhoneError(
        "Feed labels remained unclear after account creation. "
        "Did not mark the account created or close from an unproven screen."
    )


def feed_navigation_state(nodes) -> str:
    """Classify a cleanup-resume probe without treating SpringBoard as Reddit.

    SpringBoard wins if conflicting OCR is ever returned.  This keeps a Home
    Screen Reddit icon from satisfying the in-app feed check.
    """
    if proven_springboard(nodes):
        return "springboard"
    if proven_home_feed(nodes):
        return "feed"
    return "unknown"


def _mark_cleanup_app_stopped(sid: str) -> None:
    """Persist the durable checkpoint emitted by close_reddit()."""
    mark_reddit_cleanup(sid, "app_stopped", log)


def _close_and_finalize_cleanup(
    sid: str,
    phone: Phone,
    stack: TapStack,
    *,
    feed_proof: FeedCleanupProof | None = None,
    resume_from_home: bool = False,
) -> None:
    """Run exact cleanup and require its durable checkpoint before completion."""
    app_stopped_checkpointed = False

    def checkpoint_app_stopped() -> None:
        nonlocal app_stopped_checkpointed
        _mark_cleanup_app_stopped(sid)
        app_stopped_checkpointed = True

    closed = close_reddit(
        stack,
        phone,
        log,
        feed_proof=feed_proof,
        on_app_stopped=checkpoint_app_stopped,
        resume_from_home=resume_from_home,
    )
    if closed is not True:
        raise PhoneError(
            f"{sid}: cleanup did not return a verified success result. "
            "The account remains created and cleanup was not marked complete."
        )
    if not app_stopped_checkpointed:
        raise PhoneError(
            f"{sid}: cleanup returned without the durable app-stopped checkpoint. "
            "The account remains created and cleanup was not marked complete."
        )
    mark_reddit_cleanup(sid, "complete", log)


def _prove_final_home_and_reddit_stopped(
    sid: str,
    phone: Phone,
    stack: TapStack,
    *,
    stem: str,
) -> None:
    """Prove Home and Reddit stopped across bounded transition frames."""
    force_retry_used = False
    home_round = 0
    while True:
        stack.press_home()
        restart_detected = False
        for attempt in range(FINAL_HOME_VERIFY_MAX_DUMPS):
            if attempt:
                time.sleep(FINAL_HOME_VERIFY_DELAY_SECONDS)
            _blob, nodes = dump_named(
                stack,
                phone,
                f"{stem}_{home_round}_{attempt}",
                log,
                shot=False,
                persist=False,
                preview=False,
            )
            reddit_running = stack.app_is_running(REDDIT_BUNDLE)
            if reddit_running:
                if force_retry_used:
                    raise PhoneError(
                        f"{sid}: Reddit restarted after the one bounded exact-bundle "
                        "force-close retry. Cleanup remains resumable at app_stopped."
                    )
                log(
                    f"{sid}: Reddit restarted during the Home transition; applying one "
                    "bounded exact-bundle force-close retry."
                )
                retry_mode = stack.force_close_app(REDDIT_BUNDLE)
                if retry_mode not in {"force-close", "already-stopped"}:
                    raise PhoneError(
                        f"{sid}: Home-transition force close returned an unverified "
                        "result. Cleanup remains resumable at app_stopped."
                    )
                if stack.app_is_running(REDDIT_BUNDLE):
                    raise PhoneError(
                        f"{sid}: Reddit remained running after the bounded exact-bundle "
                        "force-close retry. Cleanup remains resumable at app_stopped."
                    )
                force_retry_used = True
                home_round += 1
                restart_detected = True
                break
            if proven_springboard(nodes):
                return
            log(
                f"{sid}: final Home transition is not proven yet; Reddit is stopped "
                f"({attempt + 1}/{FINAL_HOME_VERIFY_MAX_DUMPS})."
            )
        if restart_detected:
            continue
        raise PhoneError(
            f"{sid}: Reddit process is stopped, but iPhone Home was not proven after "
            f"{FINAL_HOME_VERIFY_MAX_DUMPS} fresh dumps. Cleanup remains resumable "
            "at app_stopped."
        )


def _close_exact_bundle_after_completed_swipes(
    sid: str,
    phone: Phone,
    stack: TapStack,
    *,
    fresh_from_reddit: bool = False,
    already_complete: bool = False,
) -> None:
    """Dismiss Reddit from Recents, force-stop it, then prove Home.

    A titleless switcher frame never authorizes a vertical dismissal.  A fresh
    completed-swipe flow may make one bounded horizontal recovery gesture, but
    must then prove Reddit's exact centered title before dismissing any card.
    """
    card_error: PhoneError | None = None
    try:
        _, before_nodes = dump_named(
            stack,
            phone,
            "897_completed_swipes_pre_switcher",
            log,
            shot=False,
            persist=False,
            preview=False,
        )
        stack.open_app_switcher()
        _, switcher_nodes = dump_named(
            stack,
            phone,
            "898_completed_swipes_switcher",
            log,
            shot=False,
            persist=False,
            preview=False,
        )
        if proven_springboard(switcher_nodes):
            raise PhoneError(
                f"{sid}: app-switcher command returned Home. Reddit's Recents "
                "card was not removed."
            )
        if _screen_signature(before_nodes) == _screen_signature(switcher_nodes):
            raise PhoneError(
                f"{sid}: app-switcher command left the current screen unchanged. "
                "Did not flick; cleanup remains pending."
            )

        card = switcher_app_card(switcher_nodes, "Reddit")
        centered_title = switcher_centered_title(switcher_nodes)
        if card is not None:
            card_x = card.center()[0]
            log(f"{sid}: exact Reddit Recents card proven; dismissing it once.")
        elif centered_title is not None or fresh_from_reddit:
            # Recents may open with the immediately adjacent card centered
            # (live a0014 evidence: Settings centered, Reddit preview on the
            # right).  Move the carousel once, then require Reddit's exact
            # centered title before any vertical dismissal.
            if centered_title is not None:
                log(
                    f"{sid}: adjacent Recents card is centered; centering Reddit "
                    "with one bounded horizontal gesture."
                )
            else:
                log(
                    f"{sid}: Recents title OCR is unavailable; moving the carousel "
                    "once, then requiring Reddit's exact centered title."
                )
            stack.interp_flick(650, 700, 250, 700, steps=16, ms=22)
            time.sleep(0.8)
            _, switcher_nodes = dump_named(
                stack,
                phone,
                "898_completed_swipes_reddit_centered",
                log,
                shot=False,
                persist=False,
                preview=False,
            )
            card = switcher_app_card(switcher_nodes, "Reddit")
            if card is None:
                raise PhoneError(
                    f"{sid}: Reddit did not become the exact centered Recents "
                    "card after one bounded horizontal gesture. Did not dismiss "
                    "an unknown card."
                )
            card_x = card.center()[0]
            log(f"{sid}: exact Reddit Recents card proven; dismissing it once.")
        else:
            raise PhoneError(
                f"{sid}: Reddit's exact centered Recents card was not proven. "
                "Did not flick an unknown card; cleanup remains pending."
            )

        stack.interp_flick(card_x, 780, card_x, 60, steps=16, ms=22)
        time.sleep(0.8)
        _, after_nodes = dump_named(
            stack,
            phone,
            "898_after_completed_swipes_reddit_flick",
            log,
            shot=False,
            persist=False,
            preview=False,
        )
        removed = switcher_app_removed_or_home(
            switcher_nodes,
            after_nodes,
            "Reddit",
        )
        if not removed:
            raise PhoneError(
                f"{sid}: Reddit's Recents card removal was not confirmed after "
                "one dismissal. Cleanup remains pending."
            )
    except PhoneError as exc:
        card_error = exc
    except Exception as exc:
        card_error = PhoneError(
            f"{sid}: Reddit Recents-card cleanup failed ({type(exc).__name__}). "
            "Cleanup remains pending."
        )

    close_error: PhoneError | None = None
    try:
        close_mode = stack.force_close_app(REDDIT_BUNDLE)
        if close_mode not in {"force-close", "already-stopped"}:
            raise PhoneError(
                f"{sid}: exact Reddit force close returned an unverified "
                f"result {close_mode!r}. Cleanup remains pending."
            )
        if stack.app_is_running(REDDIT_BUNDLE):
            raise PhoneError(
                f"{sid}: Reddit restarted or remained running after the exact "
                "force close. Cleanup remains pending."
            )
        if card_error is None and not already_complete:
            try:
                _mark_cleanup_app_stopped(sid)
            except Exception as exc:
                raise PhoneError(
                    f"{sid}: Reddit was fully closed, but the app-stopped "
                    "checkpoint could not be saved. Cleanup remains pending."
                ) from exc
        log(
            f"{sid}: completed-swipe cleanup force-closed only {REDDIT_BUNDLE} "
            f"({close_mode})."
        )
    except PhoneError as exc:
        close_error = exc
    except Exception as exc:
        close_error = PhoneError(
            f"{sid}: exact Reddit force close failed ({type(exc).__name__}). "
            "Cleanup remains pending."
        )

    if card_error is not None:
        if close_error is not None:
            close_error = PhoneError(f"{card_error} Force close also failed: {close_error}")
        else:
            close_error = card_error

    home_error: PhoneError | None = None
    try:
        _prove_final_home_and_reddit_stopped(
            sid,
            phone,
            stack,
            stem="899_completed_swipes_home_verify",
        )
    except PhoneError as exc:
        home_error = exc
    except Exception as exc:
        home_error = PhoneError(
            f"{sid}: final iPhone Home verification failed "
            f"({type(exc).__name__})."
        )

    if close_error is not None:
        if home_error is not None:
            raise PhoneError(f"{close_error} Home recovery also failed: {home_error}")
        raise close_error
    if home_error is not None:
        raise home_error
    if not already_complete:
        mark_reddit_cleanup(sid, "complete", log)


def _finish_app_stopped_cleanup(
    sid: str,
    phone: Phone,
    stack: TapStack,
) -> None:
    """Finish strict cleanup without reopening signup or repeating feed swipes."""
    if stack.app_is_running(REDDIT_BUNDLE):
        log(
            f"{sid}: stale app_stopped checkpoint detected because Reddit is running; "
            "rerunning only the completed-swipe card/process cleanup."
        )
        _blob, nodes = dump_named(
            stack,
            phone,
            "899_stale_app_stopped_probe",
            log,
            shot=False,
            persist=False,
            preview=False,
        )
        if make_terminal_feed_cleanup_proof(nodes) is None:
            raise PhoneError(
                f"{sid}: Reddit is running despite app_stopped, but the current screen "
                "is not a proven Reddit feed. Did not flick an unknown Recents card."
            )
        _close_exact_bundle_after_completed_swipes(
            sid,
            phone,
            stack,
            fresh_from_reddit=True,
        )
        return
    _prove_final_home_and_reddit_stopped(
        sid,
        phone,
        stack,
        stem="899_cleanup_home_verify",
    )
    mark_reddit_cleanup(sid, "complete", log)


def _resume_created_cleanup(
    sid: str,
    slot,
    phone: Phone,
    stack: TapStack,
    *,
    stop=None,
    feed_pause_count: int = 0,
    fixed_delay_seconds: float | None = None,
) -> None:
    """Resume cleanup for a created slot without entering any signup stage."""
    cleanup_state = reddit_cleanup_state(slot)
    if cleanup_state == "complete":
        try:
            swipe_target, swipe_completed = creation_feed_swipe_progress(slot)
        except ValueError as exc:
            raise PhoneError(f"{sid}: {exc}") from exc
        if swipe_target > 0 and swipe_completed == swipe_target:
            log(
                f"{sid}: account is complete; auditing the strict Reddit card "
                "and process shutdown only."
            )
            _close_exact_bundle_after_completed_swipes(
                sid,
                phone,
                stack,
                fresh_from_reddit=False,
                already_complete=True,
            )
            return
        log(f"{sid}: account and verified cleanup are already complete.")
        return
    try:
        swipe_target, swipe_completed = creation_feed_swipe_progress(slot)
    except ValueError as exc:
        raise PhoneError(f"{sid}: {exc}") from exc
    if swipe_completed < swipe_target:
        _check_stop(stop)
        log(
            f"{sid}: resume creation feed swipes at "
            f"{swipe_completed + 1}/{swipe_target} before cleanup."
        )
        _blob, nodes = dump_named(
            stack,
            phone,
            "899_swipe_resume_probe",
            log,
            shot=False,
            persist=False,
            preview=False,
        )
        if proven_springboard(nodes):
            open_container(
                stack,
                phone,
                log,
                sid,
                str(slot.get("email") or ""),
                allow_existing_sparse_feed=True,
            )
            _check_stop(stop)
            _blob, nodes = dump_named(
                stack,
                phone,
                "899_swipe_resume_after_open",
                log,
                shot=False,
                persist=False,
                preview=False,
            )
        if not proven_reddit_feed_structure(nodes):
            raise PhoneError(
                f"{sid}: remaining creation feed swipes require a proven Reddit "
                "feed. Preserved the current screen."
            )
        initial_proof = make_feed_cleanup_proof(nodes)
        if initial_proof is None:
            initial_proof = make_terminal_feed_cleanup_proof(nodes)
        run_feed_swipes(
            stack,
            phone,
            log,
            target=swipe_target,
            completed=swipe_completed,
            stop=stop,
            progress_fn=lambda count: mark_creation_feed_swipe_completed(
                sid, count, log
            ),
            initial_proof=initial_proof,
            require_final_proof=True,
            continue_on_unclear=True,
            pause_count=feed_pause_count,
            fixed_delay_seconds=fixed_delay_seconds,
        )
        # The completed swipe loop is uninterrupted current-app provenance.
        # Its final captures already reject SpringBoard, Crane, and protected
        # signup screens.  Do not insert a second OCR classification before
        # Recents: dynamic feed content and OCR artifacts can invalidate that
        # redundant frame even though the completed feed was just proven.
        _close_exact_bundle_after_completed_swipes(
            sid,
            phone,
            stack,
            fresh_from_reddit=True,
        )
        return
    if cleanup_state == "app_stopped":
        log(f"{sid}: resume cleanup from the app-stopped checkpoint.")
        _finish_app_stopped_cleanup(sid, phone, stack)
        return
    if cleanup_state != "pending":
        raise PhoneError(
            f"{sid}: unknown cleanup checkpoint {cleanup_state!r}. "
            "Did not enter signup or change phone state."
        )

    if swipe_target > 0 and swipe_completed == swipe_target:
        log(
            f"{sid}: all {swipe_target} creation feed swipes are already saved; "
            "running exact-bundle Reddit cleanup only."
        )
        _close_exact_bundle_after_completed_swipes(
            sid,
            phone,
            stack,
            fresh_from_reddit=False,
        )
        return

    _blob, nodes = dump_named(
        stack,
        phone,
        "899_cleanup_resume_probe",
        log,
        shot=False,
        persist=False,
        preview=False,
    )
    screen_state = feed_navigation_state(nodes)
    feed_proof = None
    resume_from_home = False
    if screen_state == "feed":
        feed_proof = make_feed_cleanup_proof(nodes)
        if feed_proof is None:
            raise PhoneError(
                f"{sid}: cleanup is pending, but the visible feed lacks immutable "
                "cleanup proof. Preserved it without opening Recents."
            )
    elif make_terminal_feed_cleanup_proof(nodes) is not None:
        feed_proof = make_terminal_feed_cleanup_proof(nodes)
        log(f"{sid}: sparse post-swipe Reddit feed proven for cleanup resume.")
    elif screen_state == "springboard":
        resume_from_home = True
    else:
        raise PhoneError(
            f"{sid}: cleanup is pending, but the current screen is neither a proven "
            "Home feed nor iPhone Home. Did not enter signup or open Recents."
        )

    _close_and_finalize_cleanup(
        sid,
        phone,
        stack,
        feed_proof=feed_proof,
        resume_from_home=resume_from_home,
    )


def run_slot(
    sid: str,
    phone: Phone,
    stack: TapStack,
    *,
    birth_date_override: date | None = None,
    log_fn=None,
    stop=None,
    feed_swipes: int | None = None,
    feed_pause_count: int = 0,
    fixed_delay_seconds: float | None = None,
    requested_username: str = "",
    email_replacer=None,
    rotate_before_poll: bool = False,
    max_email_replacements: int = MAX_EMAIL_REPLACEMENTS,
) -> None:
    emit = log_fn or log
    sid = sid.lower()
    slot = get_slot(sid)
    if not slot:
        raise PhoneError(f"No slot {sid}.")
    requested_username = str(requested_username or "").strip()
    if requested_username and USER_RE.fullmatch(requested_username) is None:
        raise PhoneError("Requested batch username has an invalid format.")
    saved_username = str(slot.get("username") or "").strip()
    if requested_username and saved_username and saved_username != requested_username:
        raise PhoneError(
            f"{sid} has a different saved username than this batch requests. "
            "Did not change the account or restart signup."
        )
    if slot_reddit_done(slot):
        _resume_created_cleanup(
            sid,
            slot,
            phone,
            stack,
            stop=stop,
            feed_pause_count=feed_pause_count,
            fixed_delay_seconds=fixed_delay_seconds,
        )
        return
    if not slot.get("ghost") or not slot.get("email"):
        raise PhoneError(f"{sid} needs Ghost + Hide My Email first.")
    email = str(slot.get("email") or "").strip()
    emit(f"=== Create Reddit for {sid} ===")
    emit("No SIWA. No Skip. IMAP for the code. Type keys one at a time.")

    _blob, initial_nodes = dump_named(
        stack,
        phone,
        "899_resume_probe",
        log,
        persist=False,
        preview=False,
    )
    resume_interests = find_label_exact(
        initial_nodes, ("Choose your interests", "Interests", "Customize your feed")
    ) is not None
    has_saved_credentials = bool(
        str(slot.get("username") or "").strip()
        and str(slot.get("password") or "")
    )
    resume_post_interests = (
        notification_sheet(initial_nodes)
        or native_notification_prompt(initial_nodes)
        or ads_personalization_screen(initial_nodes)
        # Home is account-neutral in this Reddit build. Without both saved
        # credentials, let run_to_verify perform its bounded bottom-You probe;
        # that path proves signup or stops on an existing profile.
        or (proven_home_feed(initial_nodes) and has_saved_credentials)
    )
    resume_about = (
        find_label_exact(initial_nodes, ABOUT_MARKERS) is not None
        or birthday_confirmation_screen(initial_nodes)
    )
    resume_verify = find_label_exact(initial_nodes, ("Verify your email",)) is not None
    resume_username = find_label_exact(initial_nodes, ("Create your username",)) is not None
    resume_invalid_code = resume_verify and invalid_code_message(initial_nodes) is not None
    resume_password = (
        find_label_exact(initial_nodes, ("Set a password",)) is not None
        and not passkey_prompt(initial_nodes)
    )
    if resume_verify:
        emit("Resume: already on Verify; skip Crane and email submission.")
    elif passkey_prompt(initial_nodes):
        if not slot.get("username") or not slot.get("password"):
            raise PhoneError(
                f"{sid} is on the passkey popup but has no saved username/password. "
                "Did not dismiss or restart signup."
            )
        log("Resume: proven iOS passkey popup after saved password; skip completed signup stages.")
        initial_nodes = dismiss_passkey_prompt(stack, phone, initial_nodes, log)
        resume_about = True
    elif resume_password:
        if not slot.get("username") or not slot.get("password"):
            raise PhoneError(
                f"{sid} is on Set a password but has no saved username/password. "
                "Did not tap, retype, or restart signup."
            )
        emit(
            "Resume: already on Set a password with saved credentials; skip email, "
            "verification, and username stages."
        )
        initial_nodes = resume_saved_password(stack, phone, initial_nodes, emit)
        resume_about = True
    elif resume_interests:
        if not slot.get("username") or not slot.get("password"):
            raise PhoneError(
                f"{sid} is on Interests but has no saved username/password. Did not resume."
            )
        log("Resume: already on Interests; skip completed signup and About you stages.")
    elif resume_post_interests:
        if not slot.get("username") or not slot.get("password"):
            raise PhoneError(
                f"{sid} is past Interests but has no saved username/password. Did not resume."
            )
        log("Resume: past Interests; resolve only proven post-signup prompts.")
    elif resume_about:
        if not slot.get("username") or not slot.get("password"):
            raise PhoneError(
                f"{sid} is on About you but has no saved username/password. Did not resume."
            )
        log("Resume: already on About you; skip completed signup stages.")

    if not resume_about and not resume_interests and not resume_post_interests:
        if resume_username:
            emit("Resume: already on Create your username; skip email and code submission.")
            if email_verification_state(get_slot(sid)) is not None:
                clear_email_verification(sid, emit)
            username_nodes = initial_nodes
        elif resume_invalid_code:
            username_nodes = recover_invalid_verification_code(
                stack,
                phone,
                email,
                initial_nodes,
                log_fn=emit,
                stop=stop,
            )
            clear_email_verification(sid, emit)
        else:
            username_nodes = _verify_email_with_replacements(
                sid,
                phone,
                stack,
                initial_nodes,
                email_replacer=email_replacer,
                rotate_before_poll=rotate_before_poll,
                max_email_replacements=max_email_replacements,
                log_fn=emit,
                stop=stop,
            )
        if find_label_exact(username_nodes, ("Create your username",)) is None:
            raise PhoneError("Create your username transition was not proven.")
        if requested_username:
            use_requested_username(stack, phone, sid, requested_username)
        else:
            keep_suggested_username(stack, phone, sid)
        wait_named(stack, phone, ("Set a password",), "899_pw", stop=stop)
        type_password(stack, phone, sid)
        wait_named(
            stack,
            phone,
            ABOUT_MARKERS,
            "909_about",
            preview=False,
            stop=stop,
        )
    if not resume_interests and not resume_post_interests:
        if birth_date_override is None:
            tap_woman(stack, phone)
        else:
            tap_woman(stack, phone, birth_date=birth_date_override)
        interests_nodes = wait_named(
            stack,
            phone,
            ("Choose your interests", "Interests"),
            "919_int",
            stop=stop,
        )
        if find_label_exact(
            interests_nodes or [], ("Choose your interests", "Interests")
        ) is None:
            raise PhoneError("About you completed but Interests did not remain on screen.")
    if resume_post_interests:
        advance_post_interests(stack, phone, initial_nodes=initial_nodes)
    else:
        pick_interests(stack, phone, sid)
    feed_proof = wait_feed(stack, phone, stop=stop)
    try:
        swipe_target = normalize_feed_swipe_count(
            0 if feed_swipes is None else feed_swipes,
            allow_zero=True,
        )
    except (TypeError, ValueError) as exc:
        raise PhoneError(str(exc)) from exc
    if swipe_target:
        mark_reddit_created(sid, log, feed_swipes=swipe_target)
        run_feed_swipes(
            stack,
            phone,
            emit,
            target=swipe_target,
            completed=0,
            stop=stop,
            progress_fn=lambda count: mark_creation_feed_swipe_completed(
                sid, count, log
            ),
            initial_proof=feed_proof,
            require_final_proof=True,
            continue_on_unclear=True,
            pause_count=feed_pause_count,
            fixed_delay_seconds=fixed_delay_seconds,
        )
        _close_exact_bundle_after_completed_swipes(
            sid,
            phone,
            stack,
            fresh_from_reddit=True,
        )
        return
    else:
        mark_reddit_created(sid, log)
    _close_and_finalize_cleanup(
        sid,
        phone,
        stack,
        feed_proof=feed_proof,
    )


def main() -> int:
    sid = (sys.argv[1] if len(sys.argv) > 1 else "a0001").strip().lower()
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        if not stack.can_tap():
            raise PhoneError("No tap backend. Open XXTouch Elite.")
        run_slot(sid, phone, stack)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
