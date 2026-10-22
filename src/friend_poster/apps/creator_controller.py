# UTF-8
"""GUI: Ghost slots, Hide My Email, Reddit creation, one-photo Reddit post."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import uuid
import webbrowser
import ctypes
from datetime import date, datetime, timedelta, timezone
from tkinter import filedialog, messagebox, scrolledtext, ttk

from friend_poster.core import diaglog
from friend_poster.core.crashlog import (
    add_sink as crash_add_sink,
    install as crash_install,
    remove_sink as crash_remove_sink,
)
from friend_poster.core.paths import LEGACY_MISC_DIR, PROJECT_ROOT

MISC_DIR = str(LEGACY_MISC_DIR)
PROJECT_DIR = str(PROJECT_ROOT)

from friend_poster.core.bulk_creation import (
    AUTO_SKIP_FAILURE_CODE,
    BULK_INACTIVITY_TIMEOUT_CODE,
    BulkError,
    BulkSaveError,
    LEGACY_CREATION_FAILURE_CODE,
    RECOVERY_DELAYS_SECONDS,
    archive_state as archive_bulk_state,
    archive_draft as archive_bulk_draft,
    consume_completed,
    consume_failed,
    consume_skipped,
    consume_stalled,
    empty_draft as empty_bulk_draft,
    empty_state as empty_bulk_state,
    extend_queue as extend_bulk_queue,
    load_draft as load_bulk_draft,
    load_state as load_bulk_state,
    parse_queue as parse_bulk_queue,
    prepare_retry as prepare_bulk_retry,
    claim_state as claim_bulk_state,
    save_draft as save_bulk_draft,
    save_state as save_bulk_state,
    save_state_cas as save_bulk_state_cas,
)
from friend_poster.core.bulk_owner_lock import acquire_bulk_owner_lock
from friend_poster.core.username_lifecycle import (
    UsernameLedgerError,
    reconcile_from_slots as reconcile_username_ledger,
    username_owners,
)
from friend_poster.core.config import LOG_DIR, REDDIT_BUNDLE, SECRETS_PATH
from friend_poster.services.ui_recovery_agent import (
    RecoveryAttemptBudget,
    RecoveryBudgetExhausted,
    RecoveryChain,
    RecoveryDirective,
    RecoveryWorkflowTool,
    RecoveryStopped,
    UIRecoveryAgent,
    is_ui_recoverable_error,
    recovery_configured,
)
from friend_poster.device.ghost_live import (
    ContainerProvisioningReconcileError,
    ICloudModalBlockingError,
    apply_reddit_containers,
    invalidate_ghost_container_cache,
    resolve_sole_user_container,
)
from friend_poster.device.crane_delete import (
    # Exact deletion is executed through Crane Settings only after a durable
    # UUID was recorded locally.
    delete_named_crane_instance as delete_named_instance,
    resolve_crane_container_identity,
)
from friend_poster.device.hidemyemail import (
    ICloudPasswordEntryError,
    create_one_hide_my_email,
    dismiss_icloud_password_sheet,
)
from friend_poster.reddit.reddit_about import BIRTH_YEAR_MAX, BIRTH_YEAR_MIN
from friend_poster.reddit.reddit_create_day1 import (
    EmailReplacementExhausted,
    run_slot as run_create_reddit,
)
from friend_poster.reddit.reddit_post import community_name, run_post
from friend_poster.reddit.reddit_profile_post import run_profile_post
from friend_poster.reddit.reddit_open import (
    ContainerSelectionError,
    CraneDoorwayUnavailable,
    FeedCleanupProof,
    advance_feed_cleanup_proof,
    post_selection_state,
    recover_existing_container,
    select_existing_container_from_compact_menu,
)
from friend_poster.reddit.reddit_to_verify import EmailFieldInvalid
from friend_poster.reddit.reddit_username import USER_RE
from friend_poster.core.slots import (
    CREATION_PROGRESS,
    bulk_reset_cleanup,
    bulk_reset_cleanup_instance,
    bulk_reset_cleanup_state,
    container_deletion_instance,
    container_deletion_state,
    crane_container_instance,
    creation_feed_swipe_progress,
    email_verification_state,
    ensure_ghost_container_needed,
    get_slot,
    ghost_container_state,
    index_to_name,
    indices_used,
    load_slots,
    mark_bulk_reset_cleanup_complete,
    mark_bulk_reset_cleanup_pending,
    mark_bulk_skipped,
    mark_container_deletion_complete,
    mark_container_deletion_pending,
    mark_creation_terminal_failure,
    mark_ghost_container_ready_from_crane,
    reconcile_crane_container_identity,
    reddit_cleanup_state,
    replace_slot_email,
    retired_indices,
    retire_slot_id,
    save_slots,
    set_slot_falcon_proxy_ip,
    set_slot_birth_date,
    set_slot_email,
    slot_bulk_skipped,
    slot_container_available,
    slot_container_deleted,
    slot_creation_unavailable,
    slot_reddit_cleanup_pending,
    slot_reddit_done,
    slot_reddit_ready,
    slot_creation_terminal_failure,
    name_to_index,
)
from friend_poster.device.ssh_device import (
    BridgeTransportError,
    Phone,
    PhoneError,
    SSHAuthError,
    diagnose_usb,
    load_secrets,
    usb_status_line,
    is_recoverable_bridge_failure,
)
from friend_poster.device.stay_awake import allow_sleep, stay_awake
from friend_poster.reddit.title_gen import generate_title
from friend_poster.device.ui_tree import OperationCancelled, TapStack, set_preview_detail
from friend_poster.services.ip_change import (
    DEFAULT_PUBLIC_IP_URL,
    IpChangeError,
    observe_proxy_without_trigger,
    run_macrodroid_change,
    sanitized_endpoint,
    validate_proxy_url,
    validate_webhook_url,
)
from friend_poster.services.ip_history import (
    HistoryError,
    configured_scope,
    latest_configured_transition,
    record_configured_operation,
)
from friend_poster.services.ip_evidence import validate_pair
from friend_poster.apps import creator_persistence as _persistence
from friend_poster.apps import creator_validation as _validation


_CREATOR_MUTEX_HANDLE = None


def _acquire_creator_single_instance() -> bool:
    """Keep one GUI process from competing for the iPhone lockdown session."""
    global _CREATOR_MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\FriendPosterCreatorPanel")
    if not handle:
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _CREATOR_MUTEX_HANDLE = handle
    return True

POST_SETTINGS_PATH = os.path.join(LOG_DIR, "post_settings.json")
CREATOR_STATE_PATH = os.path.join(LOG_DIR, "creator_state.json")
CREATOR_LOG_PATH = os.path.join(LOG_DIR, "creator.log")
IP_HISTORY_PATH = os.path.join(PROJECT_DIR, "ip_history.txt")
QA_SETTINGS_PATH = os.path.join(LOG_DIR, "qa_settings.json")
HIDE_MY_EMAIL_LOG_PATH = os.path.join(LOG_DIR, "hidemyemail.json")
PIPELINE_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@icloud\.com", re.I)
FAILED_CONTAINER_GRACE_SECONDS = 3 * 60
AUTO_DISCARD_FAILURE_CODE = "creation_auto_discarded"
# A running step gets the inactivity watchdog below.  If a step returns an
# explicit failure before that watchdog fires, the batch stays alive for this
# same three-minute grace window and then performs the same full reset.  This
# keeps the panel/worker alive instead of leaving the queue paused forever.
BULK_INACTIVITY_SECONDS = 180.0
BULK_FAILURE_GRACE_SECONDS = 180.0
_PIPELINE_RECOVERY_RETRY = object()
BULK_INACTIVITY_POLL_SECONDS = 1.0
BULK_SKIP_FAILURE_CODE = AUTO_SKIP_FAILURE_CODE
STALL_RESET_FAILURE_CODE = "creation_stall_reset"
CONTAINER_RECREATE_FAILURE_CODE = "container_recreate_required"
AI_RECOVERY_BUDGET_EXHAUSTED_CODE = "ai_recovery_budget_exhausted"
AI_DISCARD_REFUSED_CODE = "ai_discard_refused"

# --- Start Batch / Resume machine-readable outcomes ----------------------- #
# "Start Batch" always begins a genuinely new run; these codes make every
# refusal or abort distinguishable in the log without parsing free text.
START_BLOCKED_WORKER_CODE = "start_blocked_worker"
START_BLOCKED_IP_GUARD_CODE = "start_blocked_ip_guard"
START_BLOCKED_STAGE_CODE = "start_blocked_stage"
START_BLOCKED_FOREIGN_WRITER_CODE = "start_blocked_foreign_writer"
START_LOAD_FAILED_CODE = "start_load_failed"
START_ARCHIVE_FAILED_CODE = "start_archive_failed"
START_DRAFT_ARCHIVE_FAILED_CODE = "start_draft_archive_failed"
START_RESERVATION_FAILED_CODE = "start_reservation_failed"
START_RESERVATION_RESERVED_CODE = "start_reservation_reserved"
START_PERSIST_FAILED_CODE = "start_persist_failed"
START_ALLOCATED_CODE = "start_slot_allocated"
# "Retry / Resume" continues the saved batch; a saved slot row that no longer
# exists can never be rebuilt or guessed from credentials it does not have.
RESUME_SLOT_MISSING_CODE = "resume_slot_missing"

# A completed batch legitimately keeps these on an otherwise empty idle state:
# durable history and ownership bookkeeping, never outstanding work.  A saved
# batch whose remaining fields all equal an empty state is "provably clean
# idle", so a stale writer_id left by a previous Creator is safe to take over.
_BULK_CLEAN_IDLE_IGNORED = frozenset({
    "revision", "run_id", "writer_id", "requested", "failures", "delete_containers",
})

# --- Creator Log-tab diagnostics ----------------------------------------- #
# Heartbeats are status only; they never feed CREATION_PROGRESS and therefore
# never reset the inactivity watchdog or count as workflow progress.
BULK_HEARTBEAT_SECONDS = 15.0
# The Log tab retains the complete current session. Drain the producer queue in
# bounded batches so a busy panel cannot freeze Stop, but never discard a
# rendered event or traceback after it reaches the GUI.
LOG_DRAIN_BATCH = 400
# Events included before the failure block by "Copy last failure".
COPY_FAILURE_PRECEDING_EVENTS = 200

# Identity of the real on-disk writer, captured at import time. Offline
# harnesses replace ``save_bulk_state`` with a recording stand-in; an unpatched
# App must never reach the real writer from the harness seam. Comparing module
# names broke when bulk_creation moved into the friend_poster package, so this
# compares object identity instead.
_REAL_SAVE_BULK_STATE = save_bulk_state


def load_qa_swipes() -> int:
    """Return the creation/QA feed swipe count, clamped to the supported range."""
    return _persistence.load_qa_swipes(QA_SETTINGS_PATH)


def save_qa_swipes(value: int) -> None:
    _persistence.save_qa_swipes(QA_SETTINGS_PATH, LOG_DIR, value)


def load_post_settings() -> dict:
    """Saved communities list + selected. Migrates legacy {"community": ...}."""
    return _persistence.load_post_settings(POST_SETTINGS_PATH)


def save_post_settings(communities: list[dict[str, str]], selected: str = "") -> None:
    """Persist community list + selected. Never raises."""
    _persistence.save_post_settings(POST_SETTINGS_PATH, LOG_DIR, communities, selected)


def load_post_community() -> str:
    """Saved subreddit/community name. Empty when never saved or unreadable."""
    return load_post_settings().get("selected", "")


def save_post_community(community: str) -> None:
    """Persist current Community text as selected, keeping the saved list."""
    try:
        settings = load_post_settings()
    except Exception:
        settings = {"communities": [], "selected": ""}
    save_post_settings(settings.get("communities", []), community)


def write_creator_state(state: dict) -> str:
    """Write the agent-readable Post-tab snapshot. Returns path. Never raises."""
    return _persistence.write_creator_state(CREATOR_STATE_PATH, LOG_DIR, state)


def parse_slot_birth_date(raw: str, *, today: date | None = None) -> date:
    """Parse a slot's persisted DD.MM.YYYY birthday."""
    return _validation.parse_slot_birth_date(
        raw,
        today=today,
        year_min=BIRTH_YEAR_MIN,
        year_max=BIRTH_YEAR_MAX,
        error_type=PhoneError,
    )


def parse_creation_swipe_count(raw: str) -> int:
    """Parse one positive, unbounded creation-only swipe count."""
    return _validation.parse_creation_swipe_count(raw)


def parse_creation_pause_count(raw: str, swipe_count: int) -> int:
    """Parse the number of unique random pauses requested for this session."""
    return _validation.parse_creation_pause_count(raw, swipe_count)


def _unpack_creation_options(options) -> tuple[date, int, int]:
    """Accept legacy two-value prompt doubles while production returns pauses."""
    return _validation.unpack_creation_options(options)


def _valid_pipeline_email(value: object) -> bool:
    return PIPELINE_EMAIL_RE.fullmatch(str(value or "").strip()) is not None


def _pipeline_slot_complete(row: dict | None) -> bool:
    if not (
        row
        and slot_reddit_ready(row)
        and str(row.get("username") or "").strip()
        and str(row.get("password") or "").strip()
    ):
        return False
    try:
        target, completed = creation_feed_swipe_progress(row)
    except (TypeError, ValueError):
        return False
    return completed == target


def _pipeline_deletion_satisfied(row: dict | None, deletion_required: bool) -> bool:
    """Whether a slot's required local-container deletion is durably complete.

    When deletion is not required (pre-policy batches) this is always true, so
    those batches keep advancing on account completion exactly as before.
    """
    if not deletion_required:
        return True
    return container_deletion_state(row) == "complete"


def _pipeline_deletion_pending(row: dict | None, deletion_required: bool) -> bool:
    return deletion_required and container_deletion_state(row) != "complete"


def _resolve_creation_pipeline_target(
    rows: list[dict], retired: set[int]
) -> tuple[str, dict | None, bool]:
    """Return the newest unfinished slot, or reserve the monotonic next id.

    Only the highest reserved id is eligible for automatic resume.  This keeps
    older unfinished recovery rows available to the existing per-stage buttons
    without silently changing their state.
    """
    by_index: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PhoneError("PIPELINE slots contain a malformed row.")
        index = name_to_index(str(row.get("id") or ""))
        if index is None:
            raise PhoneError("PIPELINE slots contain a malformed slot id.")
        if index in by_index:
            raise PhoneError(
                f"PIPELINE found duplicate slot rows for {index_to_name(index)}."
            )
        by_index[index] = row
    reserved = set(by_index) | {int(value) for value in retired if int(value) >= 1}
    highest = max(reserved, default=0)
    newest = by_index.get(highest)
    if newest is not None and highest in retired:
        if slot_creation_unavailable(newest):
            # The bulk cleanup keeps a discarded failed slot's row as its
            # terminal diagnostic record while retiring its id in the same
            # operation.  The id and its saved email must never be handed to a
            # new container, so reserve it and allocate the next free id.
            return index_to_name(highest + 1), None, True
        raise PhoneError(
            f"PIPELINE found {index_to_name(highest)} both active and retired."
        )
    if (
        newest is not None
        and not _pipeline_slot_complete(newest)
        and not slot_creation_unavailable(newest)
    ):
        return index_to_name(highest), newest, False
    sid = index_to_name(highest + 1)
    return sid, None, True


class _AttemptCancel:
    """One attempt's cancellation view: user Stop OR its inactivity timeout.

    Device helpers only ever call ``is_set``/``wait`` (as with a
    ``threading.Event``), so the same cancellation reaches every phone command
    without changing their signatures.  A fresh instance is created per attempt
    and its timeout event is never cleared while an old worker could still hold
    it.
    """

    def __init__(
        self, user_stop: threading.Event, timeout_event: threading.Event
    ) -> None:
        self._user_stop = user_stop
        self._timeout_event = timeout_event

    def is_set(self) -> bool:
        return self._user_stop.is_set() or self._timeout_event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while not self.is_set():
            if deadline is None:
                remaining = 0.1
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                remaining = min(0.1, remaining)
            self._user_stop.wait(remaining)
        return True

    def set(self) -> None:
        # The pipeline never sets its own cancel view; kept for Event parity.
        self._timeout_event.set()

    def clear(self) -> None:
        self._timeout_event.clear()


class _InactivityWatch:
    """Flag one attempt after ``seconds`` without any verified progress.

    A daemon thread can only *signal* cancellation through a cooperative event;
    it never kills the worker.  Because the batch runs in a single worker, the
    worker returns to the queue loop before any next item can begin, so two
    attempts can never drive the phone at the same time.
    """

    def __init__(
        self,
        timeout_event: threading.Event,
        seconds: float,
        clock,
        log,
        sid: str,
        *,
        poll_seconds: float = BULK_INACTIVITY_POLL_SECONDS,
        heartbeat=None,
        heartbeat_seconds: float = BULK_HEARTBEAT_SECONDS,
    ) -> None:
        self._event = timeout_event
        self._seconds = float(seconds)
        self._clock = clock
        self._log = log
        self._sid = sid
        self._poll = max(0.001, float(poll_seconds))
        self._heartbeat = heartbeat
        self._heartbeat_seconds = max(0.0, float(heartbeat_seconds))
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._fired = False

    @property
    def fired(self) -> bool:
        return self._fired

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"creator-inactivity-{self._sid}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        started = time.monotonic()
        last_beat = started
        while not self._done.wait(self._poll):
            if self._done.is_set():
                return
            # Status-only heartbeat. It reads idle time and never calls
            # clock.note(), so it cannot reset the watchdog or count as
            # workflow progress.
            if self._heartbeat is not None and self._heartbeat_seconds > 0:
                now = time.monotonic()
                if now - last_beat >= self._heartbeat_seconds:
                    last_beat = now
                    try:
                        self._heartbeat(
                            elapsed_s=now - started,
                            idle_s=float(self._clock.idle_seconds()),
                            timeout_s=self._seconds,
                        )
                    except Exception:
                        pass
            if self._clock.idle_seconds() < self._seconds:
                continue
            self._fired = True
            self._event.set()
            try:
                self._log(
                    f"PIPELINE {self._sid}: no verified progress for "
                    f"{int(self._seconds)} seconds; cancelling this attempt so "
                    "the batch can perform its stall reset."
                )
            except Exception:
                pass
            return


def _reconcile_pipeline_email(
    sid: str,
    rows: list[dict],
    records: object,
    *,
    label: str | None = None,
) -> str | None:
    """Return one safe exact-label HME address, or None when none was saved."""
    exact_label = str(label or sid).strip().lower()
    if not isinstance(records, list):
        raise PhoneError("EMAIL Hide My Email history is malformed.")
    matches = [
        record
        for record in records
        if isinstance(record, dict)
        and str(record.get("label") or "").strip().casefold() == exact_label.casefold()
    ]
    malformed_label_rows = [
        record
        for record in records
        if not isinstance(record, dict)
        and str(record or "").strip().casefold() == exact_label.casefold()
    ]
    if malformed_label_rows:
        raise PhoneError(f"EMAIL history for label {exact_label} is malformed.")
    if not matches:
        return None
    if len(matches) != 1:
        raise PhoneError(
            f"EMAIL history has multiple exact-label records for {exact_label}."
        )
    address = str(matches[0].get("email") or "").strip().lower()
    if not _valid_pipeline_email(address):
        raise PhoneError(f"EMAIL history has a malformed address for {exact_label}.")
    for row in rows:
        if not isinstance(row, dict):
            continue
        owner = str(row.get("id") or "").strip().lower()
        assigned = str(row.get("email") or "").strip().lower()
        if assigned == address and owner != sid.lower():
            raise PhoneError(
                f"EMAIL history for {sid} conflicts with another slot assignment."
            )
    return address


def _load_pipeline_email_records() -> list[dict]:
    if not os.path.isfile(HIDE_MY_EMAIL_LOG_PATH):
        return []
    try:
        with open(HIDE_MY_EMAIL_LOG_PATH, "r", encoding="utf-8") as handle:
            records = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PhoneError("EMAIL Hide My Email history could not be read safely.") from exc
    if not isinstance(records, list):
        raise PhoneError("EMAIL Hide My Email history is malformed.")
    return records


class App:
    @staticmethod
    def _configure_theme(root: tk.Tk) -> None:
        """Apply one readable navy palette to the existing Tk/ttk controls."""
        root.configure(bg="#0b1426")
        style = ttk.Style(root)
        style.theme_use("clam")
        surface = "#101f36"
        raised = "#172b49"
        text = "#eaf2ff"
        muted = "#a9bad3"
        accent = "#4da3ff"
        style.configure("TFrame", background=surface)
        style.configure("TLabel", background=surface, foreground=text, font=("Segoe UI", 10))
        style.configure("TLabelFrame", background=surface, bordercolor="#29415f", relief="solid")
        style.configure("TLabelFrame.Label", background=surface, foreground=accent, font=("Segoe UI Semibold", 10))
        style.configure("TButton", background=raised, foreground=text, bordercolor="#365474", padding=(10, 7), font=("Segoe UI Semibold", 9))
        style.map("TButton", background=[("active", "#244668"), ("disabled", "#19263a")], foreground=[("disabled", "#71849e")])
        style.configure("Accent.TButton", background="#2475b9", foreground="#ffffff", bordercolor="#388ed1", padding=(11, 7), font=("Segoe UI Semibold", 9))
        style.map("Accent.TButton", background=[("active", "#348cd1"), ("disabled", "#19263a")], foreground=[("disabled", "#71849e")])
        style.configure("Stop.TButton", background="#513044", foreground="#ffeaf0", bordercolor="#795066", padding=(10, 7), font=("Segoe UI Semibold", 9))
        style.map("Stop.TButton", background=[("active", "#704056"), ("disabled", "#19263a")], foreground=[("disabled", "#71849e")])
        style.configure("TEntry", fieldbackground="#0c1a30", foreground=text, bordercolor="#365474", insertcolor=text, padding=5)
        style.configure("TSpinbox", fieldbackground="#0c1a30", foreground=text, bordercolor="#365474", arrowsize=14)
        style.configure("TCombobox", fieldbackground="#0c1a30", foreground=text, background=raised, arrowcolor=text)
        style.map("TCombobox", fieldbackground=[("readonly", "#0c1a30")], foreground=[("readonly", text)])
        style.configure("TRadiobutton", background=surface, foreground=text)
        style.map("TRadiobutton", background=[("active", surface)], foreground=[("active", text)])
        style.configure("TNotebook", background=surface, borderwidth=0)
        style.configure("TNotebook.Tab", background="#142741", foreground=muted, padding=(16, 9), font=("Segoe UI Semibold", 10))
        style.map("TNotebook.Tab", background=[("selected", raised), ("active", "#203d5f")], foreground=[("selected", text), ("active", text)])
        style.configure("Treeview", background="#10213a", fieldbackground="#10213a", foreground=text, bordercolor="#29415f", rowheight=29, font=("Segoe UI", 9))
        style.map("Treeview", background=[("selected", "#235582")], foreground=[("selected", text)])
        style.configure("Treeview.Heading", background=raised, foreground=text, relief="flat", font=("Segoe UI Semibold", 9), padding=7)

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("DataLost — Creator")
        self.root.geometry("1280x820")
        self.root.minsize(1060, 660)
        self._configure_theme(self.root)
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(400, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.log_q: queue.Queue = queue.Queue()
        # ---- Creator Log diagnostics ---------------------------------- #
        # One shared stream: every log() caller keeps working unchanged, and
        # the stream sanitizes, persists (independent of the Tk panel) and
        # fans the same events out to the Log tab.
        self._diag = diaglog.new_default_stream(log_path=CREATOR_LOG_PATH)
        self._diag.subscribe(self._on_diag_event)
        self._diag.set_context(
            run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6]
        )
        try:
            crash_add_sink(self._crash_sink)
        except Exception:
            pass
        try:
            self._diag.install_python_logging()
        except Exception:
            pass
        try:
            set_preview_detail(True)
        except Exception:
            pass
        # Rendered lines per session event. This is the complete current session;
        # no event is trimmed from the panel after it has been received.
        self._log_window: list[str] = []
        self._log_window_lines: list[int] = []
        self._log_window_seq: list[int] = []
        self._log_window_inserted = 0
        self._log_older_hidden = 0
        self._log_autoscroll = tk.BooleanVar(value=True)
        self._log_detailed = tk.BooleanVar(value=True)
        self._log_suspended = False
        self._log_persist_warned = False
        self._diag_attempt = 0
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.phone: Phone | None = None
        self.qa_process: subprocess.Popen[str] | None = None
        self.ip_reset_thread: threading.Thread | None = None
        self.ip_reset_busy = False
        self.pending_ip_action: dict | None = None
        self.ip_operation_generation = 0
        self.ip_operation_started = 0.0
        self.ip_cancel_event: threading.Event | None = None
        self.closing = False
        self.refresh_slot_id = ""
        self.bulk_mutex = threading.RLock()
        self.bulk_writer_id = str(uuid.uuid4())
        # Project-scoped, process-lifetime ownership lock.  Holding it proves
        # any other writer id saved in the batch belongs to a Creator that is
        # gone (restart/crash), so Start, Resume, Extend and Stop are allowed
        # to take that stale ownership over.  A panel that cannot take the
        # lock is a second live Creator and may only read the batch.
        try:
            self._bulk_owner_lock = bool(acquire_bulk_owner_lock())
        except Exception:
            self._bulk_owner_lock = False
        self.bulk_owner_lock_error = ""
        if not self._bulk_owner_lock:
            self.bulk_owner_lock_error = (
                "another Creator process holds the batch ownership lock"
            )
            self.log(
                "WARNING " + self.bulk_owner_lock_error + "; this panel can read the "
                "saved batch but cannot Start, Resume, Extend or Stop it."
            )
        self.bulk_password = ""
        self.bulk_load_error = ""
        self.bulk_draft_load_error = ""
        self.bulk_draft_after_id = None
        self.bulk_draft_suspended = False
        self.bulk_draft_reconcile_error = ""
        self.bulk_editor_baseline: list[dict] = []
        try:
            self.bulk_state = load_bulk_state()
        except BulkError as exc:
            self.bulk_state = empty_bulk_state()
            self.bulk_load_error = str(exc)
        try:
            self.bulk_draft = load_bulk_draft()
        except BulkError as exc:
            self.bulk_draft = empty_bulk_draft()
            self.bulk_draft_load_error = str(exc)
        try:
            local_secrets = load_secrets()
        except Exception:
            local_secrets = {}
        self.anthropic_recovery_enabled = recovery_configured()
        self.anthropic_recovery_enabled_var = tk.BooleanVar(
            value=self.anthropic_recovery_enabled
        )
        self.ai_recovery_status_var = tk.StringVar(
            value=(
                "AI recovery ready"
                if self.anthropic_recovery_enabled_var.get()
                else "AI recovery disabled — add anthropic_api_key to secrets.json"
            )
        )
        self._pending_ui_recovery = None
        self._pending_ui_recovery_agent = None
        self._pending_ui_recovery_chain = None
        self.jailbroken_iphone_udid = str(
            local_secrets.get("jailbroken_iphone_udid") or ""
        ).strip()
        try:
            self.macrodroid_webhook, self.falcon_proxy_url = (
                self._load_macrodroid_settings()
            )
            self.macrodroid_setup_ready = True
            self.macrodroid_config_error = ""
        except (IpChangeError, OSError, ValueError, TypeError):
            self.macrodroid_webhook = ""
            self.falcon_proxy_url = ""
            self.macrodroid_setup_ready = False
            self.macrodroid_config_error = (
                "MacroDroid webhook or FalconProxy verification is not configured; "
                "webhook will not be sent"
            )
        pad = {"padx": 8, "pady": 4}
        sidebar = tk.Frame(self.root, bg="#081225", width=176)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Frame(sidebar, bg="#4da3ff", height=4).pack(fill="x")
        tk.Label(sidebar, text="DataLost", bg="#081225", fg="#f2f7ff", font=("Segoe UI Semibold", 22), anchor="w").pack(fill="x", padx=17, pady=(25, 2))
        tk.Label(sidebar, text="CREATOR CONTROL", bg="#081225", fg="#6faeea", font=("Segoe UI Semibold", 9), anchor="w").pack(fill="x", padx=19)
        tk.Frame(sidebar, bg="#29415f", height=1).pack(fill="x", padx=17, pady=(23, 15))
        tk.Label(sidebar, text="WORKSPACE", bg="#081225", fg="#8fa6c5", font=("Segoe UI Semibold", 9), anchor="w").pack(fill="x", padx=19)
        tk.Label(sidebar, text="iPhone X  •  Reddit", bg="#081225", fg="#d4e4fa", font=("Segoe UI", 10), anchor="w").pack(fill="x", padx=19, pady=(9, 0))
        frm = ttk.Frame(self.root)
        frm.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=8)

        self.status = tk.StringVar(value="USB: probing…")
        ttk.Label(frm, textvariable=self.status, wraplength=880).pack(fill="x", **pad)

        row = ttk.LabelFrame(frm, text="iPhone X — Reddit / Creator Requests")
        row.pack(fill="x", **pad)
        ttk.Label(row, text="SSH password (not git):").pack(side="left")
        self.pw = ttk.Entry(row, show="*", width=24)
        self.pw.pack(side="left", padx=6)
        ttk.Label(row, text="New Ghost:").pack(side="left", padx=(12, 4))
        self.count = tk.IntVar(value=1)
        self.count_spin = ttk.Spinbox(
            row, from_=1, to=10, width=4, textvariable=self.count, state="readonly"
        )
        self.count_spin.pack(side="left")
        self.ghost_btn = ttk.Button(row, text="New Ghost container", command=self.on_new_ghost)
        self.ghost_btn.pack(side="left", padx=6)
        self.start_creation_btn = ttk.Button(
            row,
            text="Start Creation Reddit",
            command=self.on_start_creation_reddit,
            style="Accent.TButton",
        )
        self.start_creation_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = ttk.Button(row, text="Stop", command=self.on_stop, state="disabled", style="Stop.TButton")
        self.stop_btn.pack(side="left")

        ip_box = ttk.LabelFrame(frm, text="Local Proxy IP Change")
        ip_box.pack(fill="x", padx=8, pady=4)
        self.ip_reset_btn = ttk.Button(
            ip_box, text="Change IP", command=self.on_request_new_ip, style="Accent.TButton"
        )
        self.ip_reset_btn.pack(side="left", padx=8, pady=8)
        self.bulk_quick_resume_btn = ttk.Button(
            ip_box, text="Retry / Resume batch", command=self.retry_bulk_batch
        )
        self.bulk_quick_resume_btn.pack(side="left", padx=(0, 8), pady=8)
        self.ip_previous_var = tk.StringVar(value="—")
        self.ip_verified_new_var = tk.StringVar(value="—")
        self.ip_verified_at_var = tk.StringVar(value="—")
        self.ip_cycle_status_var = tk.StringVar(value="Ready")
        ip_details = ttk.Frame(ip_box)
        ip_details.pack(side="left", fill="x", expand=True, padx=(4, 8), pady=5)
        for row_index, (label, value) in enumerate((
            ("Status:", self.ip_cycle_status_var),
            ("Previous IP:", self.ip_previous_var),
            ("Verified new IP:", self.ip_verified_new_var),
            ("Verified at:", self.ip_verified_at_var),
        )):
            ttk.Label(ip_details, text=label).grid(row=row_index, column=0, sticky="w")
            ttk.Label(
                ip_details, textvariable=value,
                wraplength=440 if row_index == 0 else 0,
            ).grid(
                row=row_index, column=1, sticky="w", padx=(6, 0)
            )
        self._restore_manual_ip_history()

        ttk.Label(
            frm,
            text="Each slot is one Ghost/Crane name plus Hide My Email labeled the same. "
            "Names are padded: a0001, a0002, … a9999, then b10000. "
            "Create Reddit finishes signup, runs the configured feed swipes, fully closes Reddit, then proves iPhone Home. "
            "These swipes are not Day 1. Post wipes Recents, imports one photo, Safari OPEN, then wipes again.",
            wraplength=880,
        ).pack(fill="x", **pad)

        nb = ttk.Notebook(frm)
        nb.pack(fill="both", expand=True, padx=8, pady=4)
        self.nb = nb
        self.slots_tab = ttk.Frame(nb)
        self.batch_tab = ttk.Frame(nb)
        self.post_tab = ttk.Frame(nb)
        self.log_tab = ttk.Frame(nb)
        nb.add(self.slots_tab, text="Slots")
        nb.add(self.batch_tab, text="Batch")
        nb.add(self.post_tab, text="Post image")
        nb.add(self.log_tab, text="Log")
        self._build_batch_tab()

        cols = ("user", "birthday", "slot", "ghost", "email", "proxy", "done")
        self.tree = ttk.Treeview(
            self.slots_tab, columns=cols, show="headings", selectmode="browse", height=12
        )
        self.tree.heading("user", text="Reddit user")
        self.tree.heading("birthday", text="Birthday")
        self.tree.heading("slot", text="Slot")
        self.tree.heading("ghost", text="Ghost")
        self.tree.heading("email", text="Email")
        self.tree.heading("proxy", text="Verified proxy IP")
        self.tree.heading("done", text="Reddit / cleanup")
        self.tree.column("user", width=150, anchor="w")
        self.tree.column("birthday", width=90, anchor="center")
        self.tree.column("slot", width=70, anchor="w")
        self.tree.column("ghost", width=80, anchor="center")
        self.tree.column("email", width=280, anchor="w")
        self.tree.column("proxy", width=190, anchor="w")
        self.tree.column("done", width=120, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_row_buttons())

        actions = ttk.Frame(self.slots_tab)
        actions.pack(fill="x", padx=8, pady=(0, 8))
        self.email_btn = ttk.Button(
            actions, text="Create email for selected", command=self.on_create_email, state="disabled"
        )
        self.email_btn.pack(side="left")
        self.copy_btn = ttk.Button(
            actions, text="Copy email", command=self.on_copy_email, state="disabled"
        )
        self.copy_btn.pack(side="left", padx=6)
        self.profile_btn = ttk.Button(
            actions, text="Open profile", command=self.on_open_profile, state="disabled"
        )
        self.profile_btn.pack(side="left", padx=6)
        self.reddit_btn = ttk.Button(
            actions,
            text="Create Reddit (iPhone X)",
            command=self.on_create_reddit,
            state="disabled",
        )
        self.reddit_btn.pack(side="left")
        self.qa_btn = ttk.Button(
            actions, text="Run Combined QA", command=self.on_run_combined_qa,
            state="disabled",
        )
        self.qa_btn.pack(side="left", padx=6)
        self.qa_options_btn = ttk.Button(
            actions, text="Feed Swipe Options…", command=self.on_qa_options,
            state="disabled",
        )
        self.qa_options_btn.pack(side="left")
        self.delete_btn = ttk.Button(
            actions, text="Delete", command=self.on_delete_slot, state="disabled"
        )
        self.delete_btn.pack(side="left", padx=6)
        ttk.Button(actions, text="Post image…", command=self._show_post_tab).pack(
            side="left", padx=12
        )

        post = ttk.Frame(self.post_tab)
        post.pack(fill="both", expand=True, padx=12, pady=10)
        ttk.Label(
            post,
            text="Wipe Camera Roll, import one JPG through XXTouch so Recents has that photo only, "
            "open the community in Safari (OPEN), Create, tap that one cell, type the title, Post, wipe again. "
            "No SIWA. No Join. Community is a name, not a URL. SpringBoard icon, not uiopen Reddit. "
            "Keep iCloud Photos off on this phone.",
            wraplength=860,
        ).pack(anchor="w", pady=(0, 8))
        grid = ttk.Frame(post)
        grid.pack(fill="x")
        ttk.Label(grid, text="Container").grid(row=0, column=0, sticky="w", pady=3)
        self._post_container_ids: dict[str, str] = {}
        self.post_container = ttk.Combobox(grid, width=24, state="readonly")
        self.post_container.grid(row=0, column=1, sticky="w", padx=8, pady=3)
        self.post_container.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_row_buttons()
        )
        ttk.Label(grid, text="Post to").grid(row=0, column=2, sticky="e", padx=(16, 4), pady=3)
        self.post_dest = tk.StringVar(value="community")
        ttk.Radiobutton(
            grid, text="Community", variable=self.post_dest, value="community",
            command=self._on_dest_change,
        ).grid(row=0, column=3, sticky="w", pady=3)
        ttk.Radiobutton(
            grid, text="My Profile", variable=self.post_dest, value="profile",
            command=self._on_dest_change,
        ).grid(row=0, column=4, sticky="w", pady=3)
        ttk.Label(grid, text="Community").grid(row=1, column=0, sticky="w", pady=3)
        self.post_community = ttk.Entry(grid, width=28)
        self.post_community.grid(row=1, column=1, sticky="w", padx=8, pady=3)
        ttk.Label(grid, text="Niche / note").grid(row=2, column=0, sticky="w", pady=3)
        self.post_niche = ttk.Entry(grid, width=28)
        self.post_niche.grid(row=2, column=1, sticky="w", padx=8, pady=3)
        ttk.Label(grid, text="Flair").grid(row=4, column=0, sticky="w", pady=3)
        self.post_flair = ttk.Combobox(grid, width=26)
        self.post_flair.grid(row=4, column=1, sticky="w", padx=8, pady=3)
        settings = load_post_settings()
        saved_community = str(settings.get("selected") or "")
        saved_niche = ""
        saved_flair = ""
        for item in settings.get("communities", []):
            if str(item.get("community") or "") == saved_community:
                saved_niche = str(item.get("niche") or "")
                saved_flair = str(item.get("flair") or "")
                break
        if saved_community:
            self.post_community.insert(0, saved_community)
        if saved_niche:
            self.post_niche.insert(0, saved_niche)
        self._refresh_flair_history(settings.get("communities", []))
        if saved_flair:
            self.post_flair.set(saved_flair)
        self.post_community.bind("<FocusOut>", lambda _e: self._save_post_community())
        self.post_community.bind("<KeyRelease>", lambda _e: self._save_post_community())
        self.post_niche.bind("<FocusOut>", lambda _e: self._save_post_community())
        self.post_niche.bind("<KeyRelease>", lambda _e: self._save_post_community())
        self.post_flair.bind("<FocusOut>", lambda _e: self._save_post_community())
        self.post_flair.bind("<KeyRelease>", lambda _e: self._save_post_community())
        self.post_flair.bind("<<ComboboxSelected>>", lambda _e: self._save_post_community())
        ttk.Label(grid, text="Title").grid(row=3, column=0, sticky="w", pady=3)
        self.post_title = ttk.Entry(grid, width=52)
        self.post_title.grid(row=3, column=1, sticky="w", padx=8, pady=3)
        self.gen_btn = ttk.Button(grid, text="Generate", command=self.on_generate_title)
        self.gen_btn.grid(row=3, column=2, sticky="w", pady=3)
        ttk.Label(grid, text="Image(s)").grid(row=5, column=0, sticky="w", pady=3)
        img_row = ttk.Frame(grid)
        img_row.grid(row=5, column=1, sticky="we", padx=8, pady=3)
        self.post_image = tk.StringVar(value="")
        self.post_images: list[str] = []
        ttk.Entry(img_row, textvariable=self.post_image, width=48).pack(side="left")
        ttk.Button(img_row, text="Browse…", command=self.on_browse_image).pack(
            side="left", padx=6
        )
        self.comm_box = ttk.LabelFrame(post, text="Saved communities")
        self.comm_box.pack(fill="both", expand=True, pady=(10, 4))
        comm_cols = ("community", "niche")
        self.comm_tree = ttk.Treeview(
            self.comm_box, columns=comm_cols, show="headings", selectmode="browse", height=5
        )
        self.comm_tree.heading("community", text="Community")
        self.comm_tree.heading("niche", text="Niche / note")
        self.comm_tree.column("community", width=200, anchor="w")
        self.comm_tree.column("niche", width=320, anchor="w")
        self.comm_tree.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.comm_tree.bind("<<TreeviewSelect>>", self._on_community_chosen)
        comm_actions = ttk.Frame(self.comm_box)
        comm_actions.pack(fill="x", padx=8, pady=(0, 8))
        self.comm_add_btn = ttk.Button(
            comm_actions, text="Add community", command=self.on_add_community
        )
        self.comm_add_btn.pack(side="left")
        self.comm_remove_btn = ttk.Button(
            comm_actions, text="Remove", command=self.on_remove_community
        )
        self.comm_remove_btn.pack(side="left", padx=6)
        self._refresh_community_list(select=saved_community)
        self._refresh_container_choices()
        self._on_dest_change()
        self.post_btn = ttk.Button(post, text="Post on iPhone X", command=self.on_post, style="Accent.TButton")
        self.post_btn.pack(side="left", pady=12)
        self.post_stop_btn = ttk.Button(
            post, text="Stop", command=self.on_stop, state="disabled", style="Stop.TButton"
        )
        self.post_stop_btn.pack(side="left", padx=8, pady=12)
        self.save_state_btn = ttk.Button(
            post, text="Save for agent", command=self.on_save_state
        )
        self.save_state_btn.pack(side="left", padx=8, pady=12)

        self._build_log_tab()

        self._refresh_slots()
        self.root.after(200, self._probe_usb_idle)
        self.root.after(120, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        # Route Tk callback exceptions into the same diagnostic stream instead
        # of only printing them to a hidden console.
        try:
            self.root.report_callback_exception = self._report_callback_exception
        except Exception:
            pass
        if stay_awake():
            self.log("Laptop sleep blocked while open.")

    def _refresh_container_choices(self, select: str = "") -> None:
        """Dropdown lists created slots whose app cleanup is complete."""
        try:
            rows = load_slots()
        except Exception:
            rows = []
        ready = []
        for r in rows:
            if not slot_reddit_ready(r):
                continue
            sid = str(r.get("id") or "")
            if not sid:
                continue
            user = str(r.get("username") or "").strip()
            ready.append((f"{sid} ({user})" if user else sid, sid))
        self._post_container_ids = {label: sid for label, sid in ready}
        labels = [label for label, _ in ready]
        try:
            cur = self.post_container.get()
        except (tk.TclError, AttributeError):
            return
        try:
            self.post_container.configure(values=labels)
        except (tk.TclError, AttributeError):
            return
        if select:
            for label, sid in ready:
                if sid == select:
                    self.post_container.set(label)
                    return
        for label in labels:
            if label == cur:
                return
        self.post_container.set(labels[0] if labels else "")

    def _selected_container_id(self) -> str:
        try:
            disp = self.post_container.get().strip()
        except (tk.TclError, AttributeError):
            return ""
        return self._post_container_ids.get(disp, "")

    def _on_dest_change(self) -> None:
        profile = False
        try:
            profile = self.post_dest.get() == "profile"
        except (tk.TclError, AttributeError):
            return
        state = "disabled" if profile else "normal"
        for w in (self.post_community, self.post_niche, self.post_flair):
            try:
                w.configure(state=state)
            except (tk.TclError, AttributeError):
                pass
        try:
            self.comm_add_btn.configure(state=state)
            self.comm_remove_btn.configure(state=state)
        except (tk.TclError, AttributeError):
            pass
        self._sync_row_buttons()

    def _refresh_flair_history(self, rows: list | None = None) -> None:
        """Flair Combobox values: every flair saved on any community."""
        try:
            if rows is None:
                rows = load_post_settings().get("communities", [])
            seen: set[str] = set()
            values: list[str] = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                f = str(item.get("flair") or "").strip()
                if f and f.lower() not in seen:
                    seen.add(f.lower())
                    values.append(f)
            self.post_flair.configure(values=values)
        except (tk.TclError, AttributeError):
            pass

    def _save_post_community(self) -> None:
        try:
            community = self.post_community.get().strip()
            niche = self.post_niche.get().strip()[:120]
            flair = self.post_flair.get().strip()[:64]
        except (tk.TclError, AttributeError):
            return
        try:
            settings = load_post_settings()
            rows = settings.get("communities", [])
            if community:
                for item in rows:
                    if str(item.get("community") or "").lower() == community.lower():
                        item["niche"] = niche
                        item["flair"] = flair
                        break
            save_post_settings(rows, community)
            self._refresh_flair_history(rows)
        except Exception:
            pass

    def _refresh_community_list(self, select: str = "") -> None:
        try:
            rows = load_post_settings().get("communities", [])
        except Exception:
            rows = []
        try:
            for iid in self.comm_tree.get_children():
                self.comm_tree.delete(iid)
            for item in rows:
                c = str(item.get("community") or "")
                n = str(item.get("niche") or "")
                if c:
                    self.comm_tree.insert("", "end", iid=c.lower(), values=(c, n))
            if select:
                iid = select.lower()
                if iid in self.comm_tree.get_children():
                    self.comm_tree.selection_set(iid)
                    self.comm_tree.see(iid)
        except (tk.TclError, AttributeError):
            pass

    def _on_community_chosen(self, _event=None) -> None:
        try:
            sel = self.comm_tree.selection()
            if not sel:
                return
            community, niche = self.comm_tree.item(sel[0], "values")
        except (tk.TclError, AttributeError):
            return
        flair = ""
        try:
            for item in load_post_settings().get("communities", []):
                if str(item.get("community") or "") == community:
                    flair = str(item.get("flair") or "")
                    break
        except Exception:
            flair = ""
        try:
            self.post_community.delete(0, "end")
            self.post_community.insert(0, community)
            self.post_niche.delete(0, "end")
            if niche:
                self.post_niche.insert(0, niche)
            self.post_flair.set(flair)
        except (tk.TclError, AttributeError):
            pass
        self._save_post_community()

    def on_add_community(self) -> None:
        community = self.post_community.get().strip()
        niche = self.post_niche.get().strip()[:120]
        try:
            flair = self.post_flair.get().strip()[:64]
        except (tk.TclError, AttributeError):
            flair = ""
        try:
            community = community_name(community)
        except PhoneError as e:
            self.log("ERROR  " + str(e))
            return
        settings = load_post_settings()
        rows = settings.get("communities", [])
        updated = False
        for item in rows:
            if str(item.get("community") or "").lower() == community.lower():
                item["community"] = community
                item["niche"] = niche
                item["flair"] = flair
                updated = True
                break
        if not updated:
            rows.append({"community": community, "niche": niche, "flair": flair})
        save_post_settings(rows, community)
        self._refresh_community_list(select=community)
        self.log(f"Saved community {community}" + (f" ({niche})" if niche else ""))

    def on_remove_community(self) -> None:
        try:
            sel = self.comm_tree.selection()
            if not sel:
                self.log("Select a saved community first.")
                return
            community = self.comm_tree.item(sel[0], "values")[0]
        except (tk.TclError, AttributeError, IndexError):
            return
        settings = load_post_settings()
        rows = [
            item
            for item in settings.get("communities", [])
            if str(item.get("community") or "").lower() != community.lower()
        ]
        try:
            current = self.post_community.get().strip()
        except (tk.TclError, AttributeError):
            current = ""
        still_there = any(
            str(item.get("community") or "").lower() == current.lower() for item in rows
        )
        save_post_settings(rows, current if still_there else "")
        self._refresh_community_list()
        self.log(f"Removed community {community}")

    @staticmethod
    def _bulk_draft_has_content(draft: dict) -> bool:
        return bool(
            draft.get("baseline_queue")
            or any(
                str(draft.get(field) or "")
                for field in (
                    "count_text", "births_text", "swipes_text",
                    "delays_text", "usernames_text",
                )
            )
        )

    @staticmethod
    def _bulk_rows_payload(rows: list[dict], *, count_text: str = "") -> dict:
        def credential(row: dict) -> str:
            username = str(row.get("username") or "")
            password = str(row.get("password") or "")
            return f"{username}:{password}" if username and password else username

        return {
            "version": 1,
            "count_text": str(count_text or len(rows) or 1),
            "births_text": "\n".join(str(row["birth_date"]) for row in rows),
            "swipes_text": "\n".join(str(row["swipes"]) for row in rows),
            "delays_text": "\n".join(str(row["delay_seconds"]) for row in rows),
            "usernames_text": "\n".join(credential(row) for row in rows),
            "baseline_queue": [dict(row) for row in rows],
        }

    @staticmethod
    def _drop_text_lines(value: str, count: int) -> str:
        if count <= 0:
            return str(value)
        return "\n".join(str(value).split("\n")[count:])

    @staticmethod
    def _parse_bulk_text_rows(
        births: str, swipes: str, delays: str, usernames: str,
    ) -> list[dict]:
        line_count = len(str(births).splitlines())
        if line_count < 1:
            raise BulkError("The batch editor has no rows.")
        _requested, rows = parse_bulk_queue(
            str(line_count), births, swipes, delays, parse_slot_birth_date,
            usernames_text=usernames,
        )
        return rows

    def _prepare_loaded_bulk_draft(self, draft: dict) -> dict:
        """Drop completed baseline heads while preserving raw appended text."""
        self.bulk_draft_reconcile_error = ""
        if not self._bulk_draft_has_content(draft):
            rows = [dict(row) for row in self.bulk_state.get("queue") or []]
            self.bulk_editor_baseline = rows
            return self._bulk_rows_payload(
                rows,
                count_text=str(self.bulk_state.get("remaining") or len(rows) or 1),
            )

        restored = dict(draft)
        baseline = [dict(row) for row in draft.get("baseline_queue") or []]
        if (
            self.bulk_state.get("stage") == "idle"
            and int(self.bulk_state.get("remaining") or 0) == 0
        ):
            # A finished batch no longer owns any editor rows. Preserve the
            # user's raw next-batch draft exactly, including unequal surplus
            # column lengths and its requested count.
            self.bulk_editor_baseline = []
            restored["baseline_queue"] = []
            return restored
        try:
            editor_rows = self._parse_bulk_text_rows(
                draft["births_text"], draft["swipes_text"],
                draft["delays_text"], draft["usernames_text"],
            )
        except BulkError:
            self.bulk_editor_baseline = baseline
            self.bulk_draft_reconcile_error = (
                "Draft text is incomplete or invalid; it was restored exactly, "
                "but Save & Extend is disabled until the columns are corrected."
            )
            return restored

        if editor_rows[: len(baseline)] != baseline:
            self.bulk_editor_baseline = baseline
            self.bulk_draft_reconcile_error = (
                "Draft rows no longer match their saved queue baseline; raw text was "
                "preserved and no queue data was changed."
            )
            return restored

        current = [dict(row) for row in self.bulk_state.get("queue") or []]
        if current and editor_rows[: len(current)] == current:
            # The active queue is already the validated prefix of this raw
            # draft. Keep all raw surplus columns byte-for-byte and only move
            # the baseline forward; rebuilding rows here would duplicate the
            # active prefix or discard unequal surplus lengths.
            self.bulk_editor_baseline = current
            restored["baseline_queue"] = current
            return restored
        consumed = None
        shared_count = 0
        for offset in range(len(baseline) + 1):
            shared = baseline[offset:]
            if current[: len(shared)] == shared:
                consumed = offset
                shared_count = len(shared)
                break
        if consumed is None:
            self.bulk_editor_baseline = baseline
            self.bulk_draft_reconcile_error = (
                "Saved draft could not be reconciled with the live queue; raw text was "
                "preserved and Save & Extend is disabled."
            )
            return restored

        editor_additions = editor_rows[len(baseline):]
        current_after_shared = current[shared_count:]
        if editor_additions[: len(current_after_shared)] == current_after_shared:
            editor_additions = editor_additions[len(current_after_shared):]
        restored_rows = [*current, *editor_additions]
        self.bulk_editor_baseline = current
        restored = self._bulk_rows_payload(
            restored_rows, count_text=str(max(1, len(restored_rows)))
        )
        restored["baseline_queue"] = current
        return restored

    def _set_bulk_editor(self, draft: dict) -> None:
        self.bulk_draft_suspended = True
        try:
            self.bulk_count_var.set(str(draft.get("count_text") or "1"))
            for widget, field in (
                (self.bulk_births_text, "births_text"),
                (self.bulk_swipes_text, "swipes_text"),
                (self.bulk_delays_text, "delays_text"),
                (self.bulk_usernames_text, "usernames_text"),
            ):
                widget.delete("1.0", "end")
                widget.insert("1.0", str(draft.get(field) or ""))
                widget.edit_modified(False)
        finally:
            self.bulk_draft_suspended = False

    def _bulk_draft_payload(self) -> dict:
        return {
            "version": 1,
            "count_text": str(self.bulk_count_var.get()),
            "births_text": self.bulk_births_text.get("1.0", "end-1c"),
            "swipes_text": self.bulk_swipes_text.get("1.0", "end-1c"),
            "delays_text": self.bulk_delays_text.get("1.0", "end-1c"),
            "usernames_text": self.bulk_usernames_text.get("1.0", "end-1c"),
            "baseline_queue": [dict(row) for row in self.bulk_editor_baseline],
        }

    def _bulk_editor_validation_error(self) -> str:
        try:
            draft = self._bulk_draft_payload()
            rows = self._parse_bulk_text_rows(
                draft["births_text"], draft["swipes_text"],
                draft["delays_text"], draft["usernames_text"],
            )
            baseline = [dict(row) for row in self.bulk_editor_baseline]
            if rows[: len(baseline)] != baseline:
                return "Existing queued rows changed; restore them and append new rows only."
            current = [dict(row) for row in self.bulk_state.get("queue") or []]
            consumed = len(baseline) - len(current)
            if consumed < 0 or baseline[consumed:] != current:
                return "The editor baseline no longer matches the live queue; reopen the panel."
            return ""
        except (BulkError, tk.TclError) as exc:
            return str(exc)

    def _save_bulk_draft_now(self) -> bool:
        self.bulk_draft_after_id = None
        if getattr(self, "bulk_draft_suspended", False) or not hasattr(self, "bulk_births_text"):
            return False
        try:
            draft = self._bulk_draft_payload()
            save_bulk_draft(draft)
            self.bulk_draft = draft
            self.bulk_draft_reconcile_error = self._bulk_editor_validation_error()
            if hasattr(self, "bulk_draft_status_var"):
                self.bulk_draft_status_var.set(
                    self.bulk_draft_reconcile_error or "Draft saved locally"
                )
            if hasattr(self, "bulk_extend_btn"):
                self._refresh_bulk_panel()
            return True
        except (BulkError, tk.TclError):
            if hasattr(self, "bulk_draft_status_var"):
                self.bulk_draft_status_var.set("Draft could not be saved")
            return False

    def _schedule_bulk_draft_save(self, *_args) -> None:
        if self.bulk_draft_suspended or not hasattr(self, "root"):
            return
        if self.bulk_draft_after_id is not None:
            try:
                self.root.after_cancel(self.bulk_draft_after_id)
            except (tk.TclError, ValueError):
                pass
        self.bulk_draft_after_id = self.root.after(350, self._save_bulk_draft_now)

    def _on_bulk_text_modified(self, event) -> None:
        try:
            event.widget.edit_modified(False)
        except tk.TclError:
            return
        self._schedule_bulk_draft_save()

    def _build_batch_tab(self) -> None:
        """Build local queue controls; no network work runs on the UI thread."""
        frame = ttk.Frame(self.batch_tab, padding=12)
        frame.pack(fill="both", expand=True)
        # Offline panel tests construct App without __init__; keep this UI
        # self-contained while real startup still defaults from secrets.json.
        if not hasattr(self, "anthropic_recovery_enabled_var"):
            self.anthropic_recovery_enabled = False
            self.anthropic_recovery_enabled_var = tk.BooleanVar(
                master=frame, value=False
            )
        if not hasattr(self, "ai_recovery_status_var"):
            self.ai_recovery_status_var = tk.StringVar(
                master=frame, value="AI recovery disabled"
            )
        ttk.Label(
            frame,
            text=(
                "The first Items to process rows are used; extra rows stay saved for later. "
                "Use username or username:password per row. Valid unique usernames are used "
                "exactly; username-only rows receive a generated password. Each item verifies "
                "a fresh proxy IP."
            ),
            wraplength=900,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
        self.bulk_count_var = tk.StringVar(
            value=str(max(1, int(self.bulk_state.get("requested") or 10)))
        )
        ttk.Label(frame, text="Items to process:").grid(row=1, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.bulk_count_var, width=10).grid(
            row=1, column=1, sticky="w", pady=(0, 8)
        )
        self.bulk_count_save_btn = ttk.Button(
            frame, text="Save", command=self.save_bulk_item_count,
            style="Accent.TButton",
        )
        self.bulk_count_save_btn.grid(
            row=1, column=2, sticky="w", padx=(6, 0), pady=(0, 8)
        )
        headings = (
            "Birth dates (DD.MM.YYYY)", "Swipe counts", "Delay (2–7s)",
            "Username or username:password",
        )
        field_style = dict(
            height=12, wrap="none", font=("Consolas", 11),
            bg="#0c1a30", fg="#eaf2ff", insertbackground="#eaf2ff",
            selectbackground="#285783", relief="flat", highlightthickness=1,
            highlightbackground="#365474", highlightcolor="#4da3ff", padx=10, pady=9,
        )
        self.bulk_births_text = tk.Text(frame, width=23, **field_style)
        self.bulk_swipes_text = tk.Text(frame, width=15, **field_style)
        self.bulk_delays_text = tk.Text(frame, width=15, **field_style)
        self.bulk_usernames_text = tk.Text(frame, width=24, **field_style)
        widgets = (self.bulk_births_text, self.bulk_swipes_text, self.bulk_delays_text, self.bulk_usernames_text)
        for column, (heading, widget) in enumerate(zip(headings, widgets)):
            ttk.Label(frame, text=heading).grid(row=2, column=column, sticky="w")
            widget.grid(row=3, column=column, sticky="nsew", padx=(0, 8), pady=(2, 8))
            frame.columnconfigure(column, weight=1)
        frame.rowconfigure(3, weight=1)
        restored_draft = self._prepare_loaded_bulk_draft(self.bulk_draft)
        self._set_bulk_editor(restored_draft)
        self.bulk_count_var.trace_add("write", self._schedule_bulk_draft_save)
        for widget in widgets:
            widget.bind("<<Modified>>", self._on_bulk_text_modified)
            widget.edit_modified(False)

        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, columnspan=4, sticky="ew")
        self.bulk_start_btn = ttk.Button(actions, text="Start batch", command=self._on_start_bulk_batch, style="Accent.TButton")
        self.bulk_start_btn.pack(side="left")
        self.bulk_retry_btn = ttk.Button(actions, text="Retry / Resume", command=self.retry_bulk_batch)
        self.bulk_retry_btn.pack(side="left", padx=6)
        self.bulk_extend_btn = ttk.Button(
            actions, text="Save & Extend Batch", command=self.extend_bulk_batch,
            style="Accent.TButton",
        )
        self.bulk_extend_btn.pack(side="left", padx=(0, 6))
        self.bulk_stop_btn = ttk.Button(actions, text="Stop batch", command=self._stop_bulk_batch, style="Stop.TButton")
        self.bulk_stop_btn.pack(side="left")
        self.anthropic_recovery_check = ttk.Checkbutton(
            actions,
            text="Anthropic recovery (always on when key exists)",
            variable=self.anthropic_recovery_enabled_var,
            command=self._on_anthropic_recovery_toggle,
            state="disabled",
        )
        self.anthropic_recovery_check.pack(side="left", padx=(10, 0))
        ttk.Label(actions, text="Uses the SSH password entered above.").pack(side="left", padx=12)

        self.bulk_status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.bulk_status_var, wraplength=900).grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(10, 0)
        )
        ttk.Label(
            frame, textvariable=self.ai_recovery_status_var, wraplength=900,
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.bulk_draft_status_var = tk.StringVar(
            value=self.bulk_draft_reconcile_error or "Draft saved locally"
        )
        ttk.Label(
            frame, textvariable=self.bulk_draft_status_var, wraplength=900,
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))
        if self.bulk_load_error:
            self.log("WARNING " + self.bulk_load_error)
        if self.bulk_draft_load_error:
            self.log("WARNING " + self.bulk_draft_load_error)
        if self.bulk_draft_reconcile_error:
            self.log("WARNING " + self.bulk_draft_reconcile_error)
        self._save_bulk_draft_now()
        self._refresh_bulk_panel()

    def _refresh_bulk_panel(self) -> None:
        state = getattr(self, "bulk_state", empty_bulk_state())
        stage = str(state.get("stage") or "idle")
        requested = int(state.get("requested") or 0)
        remaining = int(state.get("remaining") or 0)
        failed = len(state.get("failures") or [])
        completed = max(0, requested - remaining - failed)
        slot_id = str(state.get("slot_id") or "—")
        verified = str(state.get("verified_ip") or "—")
        failure = str(state.get("failure_phase") or "—")
        failure_reason = str(state.get("failure_reason") or "")

        # Current slot identity and readiness for local-container deletion.
        slot_row = None
        if slot_id and slot_id != "—":
            try:
                slot_row = get_slot(slot_id)
            except Exception:
                slot_row = None
        account_ready = "unknown"
        deletion_state_label = "—"
        if slot_row is not None:
            try:
                if _pipeline_slot_complete(slot_row):
                    account_ready = "ready"
                else:
                    account_ready = "in progress"
            except Exception:
                account_ready = "in progress"
            try:
                del_st = str(container_deletion_state(slot_row) or "").strip()
            except Exception:
                del_st = ""
            if del_st:
                deletion_state_label = del_st
            else:
                deletion_state_label = "—"
        elif slot_id == "—":
            account_ready = "—"
            deletion_state_label = "—"

        _NEXT_STAGE_HINTS = {
            "ip_pending": "next: verified IP change",
            "may_have_triggered": "next: verify the IP change",
            "retry_ip": "next: re-verify the IP change",
            "ip_verified": "next: create account",
            "creating": "next: create account",
            "retry_creation": "next: retry account creation/recovery",
            "between": "next: batch between-delay",
            "paused": "next: manual Retry / Resume",
            "stopped": "next: manual Retry / Resume",
            "idle": "—",
            None: "—",
        }
        next_hint = _NEXT_STAGE_HINTS.get(stage, f"next: {stage}")

        if hasattr(self, "bulk_status_var"):
            self.bulk_status_var.set(
                f"Stage: {stage}  |  Account: {account_ready}  |  Deletion: {deletion_state_label}  |  "
                f"Completed: {completed}  |  Remaining: {remaining}  |  "
                f"Failed: {failed}  |  Current: {slot_id}  |  "
                f"Verified IP: {verified}  |  Failure: {failure}  |  {next_hint}"
            )
        active = stage not in {"idle", "paused", "stopped"}
        paused = stage == "paused"
        resumable = paused or stage == "stopped" or (
            active and not (getattr(self, "worker", None) and self.worker.is_alive())
        )
        if hasattr(self, "ip_cycle_status_var"):
            if paused:
                detail = failure_reason.rstrip(".") or f"{failure} check failed"
                self.ip_cycle_status_var.set(
                    f"Batch paused on {slot_id}: {detail}. "
                    "Use Retry / Resume batch to continue, or Start batch for a new run."
                )
            elif stage == "stopped":
                self.ip_cycle_status_var.set(
                    "Batch stopped. Use Retry / Resume batch to continue, "
                    "or Start batch for a new run."
                )
            elif resumable:
                self.ip_cycle_status_var.set("Batch interrupted. Use Retry / Resume batch.")
        if hasattr(self, "bulk_start_btn"):
            # Start batch is a fresh run: it stays available from idle, stopped
            # and paused (the API layer still refuses while a task is live).
            self.bulk_start_btn.configure(state="disabled" if active else "normal")
        if hasattr(self, "bulk_retry_btn"):
            self.bulk_retry_btn.configure(state="normal" if resumable else "disabled")
        if hasattr(self, "bulk_extend_btn"):
            can_extend = (
                not self.bulk_draft_reconcile_error
                and stage != "idle"
                and remaining > 0
            )
            self.bulk_extend_btn.configure(state="normal" if can_extend else "disabled")
        if hasattr(self, "bulk_count_save_btn"):
            can_save_count = (
                not self.bulk_draft_reconcile_error
                and stage != "idle"
                and remaining > 0
            )
            self.bulk_count_save_btn.configure(
                state="normal" if can_save_count else "disabled"
            )
        if hasattr(self, "bulk_quick_resume_btn"):
            self.bulk_quick_resume_btn.configure(state="normal" if resumable else "disabled")
        if hasattr(self, "bulk_stop_btn"):
            self.bulk_stop_btn.configure(state="normal" if active or paused else "disabled")

    def _on_start_bulk_batch(self) -> None:
        try:
            requested, items, claimed_skipped = self._parse_fresh_bulk_editor()
            if claimed_skipped:
                self.log(
                    f"BATCH start: ignored {claimed_skipped} username row(s) already "
                    "bound to processing or successful slots."
                )
            current = dict(getattr(self, "bulk_state", empty_bulk_state()))
            superseded = (
                current.get("stage") not in {"idle", None}
                or int(current.get("remaining") or 0) > 0
                or bool(current.get("run_id"))
            )
            if superseded:
                # Start always means a new batch; the saved one is archived and
                # its in-progress slot is never resumed.
                confirmed = messagebox.askokcancel(
                    "Start a new batch?",
                    "The saved batch will be archived and a new batch will start "
                    "from the entries currently shown. The in-progress slot will "
                    "not be resumed. Continue?",
                    parent=self.root,
                )
                if not confirmed:
                    self.log("BATCH start cancelled; saved queue was not changed.")
                    self.status.set("Batch start cancelled")
                    return
            if not self.start_bulk_batch(items, requested, self.pw.get()):
                raise BulkError(
                    "The batch was not started; see the blocked reason in the Creator Log."
                )
        except (BulkError, PhoneError, TypeError, ValueError) as exc:
            # Preserve the raw editor draft so nothing typed is lost on failure.
            try:
                self._save_bulk_draft_now()
            except Exception:
                pass
            self.log(f"BATCH not started: {exc}")
            self.status.set("Batch not started — see Log")

    def _username_claim_owners(self) -> dict[str, dict]:
        """Reconcile slots into the ledger and return all unavailable names."""
        # Offline harnesses construct App without __init__; never let them read
        # or migrate the live ledger/slots unless the test replaces this seam.
        if not hasattr(self, "bulk_writer_id"):
            return {}
        try:
            return reconcile_username_ledger(load_slots())
        except UsernameLedgerError as exc:
            raise BulkError(str(exc)) from exc

    def _parse_fresh_bulk_editor(self) -> tuple[int, list[dict], int]:
        """Parse a new run after removing rows whose username is already owned.

        The whole aligned row is skipped, so birth/swipe/delay data cannot drift
        relative to the next unused username. Surplus rows remain available and
        are pulled forward until the requested count is satisfied.
        """
        count_text = str(self.bulk_count_var.get() or "").strip()
        if re.fullmatch(r"[1-9]\d*", count_text) is None:
            raise BulkError("Batch size must be a positive whole number.")
        requested = int(count_text)
        raw_columns = [
            str(self.bulk_births_text.get("1.0", "end-1c")).split("\n"),
            str(self.bulk_swipes_text.get("1.0", "end-1c")).split("\n"),
            str(self.bulk_delays_text.get("1.0", "end-1c")).split("\n"),
            str(self.bulk_usernames_text.get("1.0", "end-1c")).split("\n"),
        ]
        owners = self._username_claim_owners()
        kept = [[], [], [], []]
        skipped = 0
        row_count = max(len(column) for column in raw_columns[:3])
        for index in range(row_count):
            raw_credential = (
                raw_columns[3][index].strip()
                if index < len(raw_columns[3]) else ""
            )
            username = raw_credential.partition(":")[0].strip()
            if username and username.casefold() in owners:
                skipped += 1
                continue
            for column_index, column in enumerate(raw_columns):
                kept[column_index].append(
                    column[index] if index < len(column) else ""
                )
        parsed_requested, items = parse_bulk_queue(
            str(requested),
            "\n".join(kept[0]),
            "\n".join(kept[1]),
            "\n".join(kept[2]),
            parse_slot_birth_date,
            usernames_text="\n".join(kept[3]),
        )
        return parsed_requested, items, skipped

    def _show_post_tab(self) -> None:
        self.nb.select(self.post_tab)
        sid = self._selected_id()
        self._refresh_container_choices(select=sid)

    def log(self, msg: str) -> None:
        """Route one execution message through the shared diagnostic stream.

        Existing ``log(message)`` callers keep their signature and meaning.  The
        stream sanitizes and persists the event *before* it reaches the panel, so
        a busy Log tab can never lose history.  Logging never touches
        ``CREATION_PROGRESS``, so it cannot reset the inactivity watchdog.
        """
        diag = getattr(self, "_diag", None)
        if diag is None:
            # Headless/test paths with no stream keep the previous behavior.
            try:
                self.log_q.put(msg)
            except Exception:
                pass
            return
        try:
            diag.log(msg)
        except Exception:
            # Diagnostics must never break automation or the caller.
            try:
                self.log_q.put(msg)
            except Exception:
                pass

    # ---- diagnostic stream wiring (producer threads; Tk-free) ----------- #

    def _on_diag_event(self, event) -> None:
        """Stream subscriber: queue only. The Tk thread does all widget work."""
        try:
            self.log_q.put(("__diag__", event))
        except Exception:
            pass

    def _crash_sink(self, kind: str, text: str) -> None:
        """Forward uncaught/thread crashes captured by crashlog into the stream."""
        if not str(kind).startswith(("UNCAUGHT", "THREAD")):
            return
        diag = getattr(self, "_diag", None)
        if diag is None:
            return
        try:
            diag.crash(str(kind).lower(), text)
        except Exception:
            pass

    def _report_failure(self, **kwargs) -> None:
        """Emit a clearly marked failure report without ever raising."""
        diag = getattr(self, "_diag", None)
        if diag is None:
            return
        try:
            diag.failure(**kwargs)
        except Exception:
            pass

    def _report_cleanup_error(self, message, exception=None) -> None:
        diag = getattr(self, "_diag", None)
        if diag is None:
            return
        try:
            diag.cleanup_error(message, exception=exception)
        except Exception:
            pass

    def _set_diag(self, method: str, value: str) -> None:
        """Record a disposition/queue/preservation outcome without ever raising."""
        diag = getattr(self, "_diag", None)
        if diag is None:
            return
        try:
            getattr(diag, method)(value)
        except Exception:
            pass

    def _report_callback_exception(self, exc, val, tb) -> None:
        """Tk callback exceptions: report them instead of only printing them."""
        name = getattr(exc, "__name__", None) or type(exc).__name__
        self.log("ERROR Tk callback failed: %s: %s" % (name, val))
        self._report_failure(
            kind="tk_callback",
            exception_type=getattr(exc, "__name__", str(exc)),
            exception_message=str(val),
            traceback_text="".join(traceback.format_exception(exc, val, tb)),
            disposition="Tk callback raised; the panel keeps running",
            last_operation=self._diag_operation_label(),
            note="A Tk callback raised; automation decisions are unchanged.",
        )

    def _diag_operation_label(self) -> str:
        diag = getattr(self, "_diag", None)
        if diag is None:
            return "unknown"
        try:
            return str(diag.operation or "unknown")
        except Exception:
            return "unknown"

    def _make_heartbeat(self, sid: str):
        """Return a status-only heartbeat callable for the watchdog (or None)."""
        diag = getattr(self, "_diag", None)
        if diag is None:
            return None

        def _heartbeat(*, elapsed_s: float, idle_s: float, timeout_s: float,
                       _stream=diag, _sid=sid) -> None:
            _stream.heartbeat(
                operation=_stream.operation or "creation_pipeline",
                elapsed_s=float(elapsed_s),
                last_progress_s=float(idle_s),
                timeout_s=float(timeout_s),
                slot_id=_sid,
            )

        return _heartbeat

    def _report_attempt_failure(
        self,
        sid: str,
        *,
        kind: str,
        exception=None,
        code: str = "",
        disposition: str,
        note: str = "",
        traceback_text: str = "",
        queue: str = "decided by the batch loop after this attempt",
        preservation: str = "verified checkpoints, account and container preserved",
        exception_type: str = "",
        exception_message: str = "",
    ) -> None:
        """Emit a failure report with real checkpoint/operation context."""
        diag = getattr(self, "_diag", None)
        if diag is None:
            return
        try:
            last_checkpoint = diag.last_checkpoint_message(sid)
            last_operation = diag.last_message(sid)
            tb = traceback_text
            if exception is not None and not tb:
                tb = "".join(
                    traceback.format_exception(
                        type(exception), exception, exception.__traceback__
                    )
                )
            self._report_failure(
                kind=kind,
                exception=exception,
                exception_type=exception_type,
                exception_message=exception_message,
                failure_code=str(
                    code or getattr(self, "_last_pipeline_failure_code", "") or ""
                ),
                disposition=disposition,
                queue=queue,
                preservation=preservation,
                last_checkpoint=last_checkpoint,
                last_operation=last_operation,
                traceback_text=tb,
                note=note,
                slot_id=sid,
                stage="pipeline",
                operation=self._diag_operation_label(),
            )
        except Exception:
            pass

    # ---- log panel rendering (GUI thread only) -------------------------- #

    def _append_log_block(self, event) -> None:
        try:
            text = event.render()
        except Exception:
            return
        self._log_window.append(text)
        self._log_window_lines.append(text.count("\n") + 1)
        try:
            seq = int(getattr(event, "seq", -1))
        except Exception:
            seq = -1
        self._log_window_seq.append(seq)

    def _append_log_line(self, text: str) -> None:
        self._log_window.append(text)
        self._log_window_lines.append(text.count("\n") + 1)
        self._log_window_seq.append(-1)

    def _auto_scroll_on(self) -> bool:
        try:
            return bool(self._log_autoscroll.get())
        except Exception:
            return False

    def _flush_log_render(self) -> None:
        box = getattr(self, "log_box", None)
        if box is None:
            return
        try:
            box.configure(state="normal")
        except Exception:
            return
        pending = self._log_window_inserted
        if pending < len(self._log_window):
            chunk = "".join(block + "\n" for block in self._log_window[pending:])
            box.insert("end", chunk)
            self._log_window_inserted = len(self._log_window)
        auto = self._auto_scroll_on()
        self._update_older_label()
        self._update_persist_label()
        if auto:
            box.see("end")
        try:
            box.configure(state="disabled")
        except Exception:
            pass

    def _update_older_label(self) -> None:
        label = getattr(self, "log_older_label", None)
        if label is None:
            return
        self._log_older_hidden = 0
        event_count = len(self._log_window)
        line_count = sum(self._log_window_lines)
        try:
            pending = int(self.log_q.qsize())
        except Exception:
            pending = 0
        if pending:
            text = (
                f"Rendering complete log: {event_count:,} event(s), "
                f"{line_count:,} line(s) shown; {pending:,} queued — nothing discarded."
            )
        else:
            text = (
                f"Complete current-session log: {event_count:,} event(s), "
                f"{line_count:,} line(s) — nothing hidden."
            )
        try:
            label.configure(text=text)
        except Exception:
            pass

    def _update_persist_label(self) -> None:
        label = getattr(self, "log_persist_label", None)
        if label is None:
            return
        diag = getattr(self, "_diag", None)
        err = str(getattr(diag, "persist_error", "") or "") if diag else ""
        try:
            label.configure(text=f"Persistent log write failing: {err}" if err else "")
        except Exception:
            pass

    def _log_status(self, text: str) -> None:
        label = getattr(self, "log_status_label", None)
        if label is None:
            return
        try:
            label.configure(text=str(text))
        except Exception:
            pass

    def _drain_log(self) -> None:
        drained = 0
        try:
            while drained < LOG_DRAIN_BATCH:
                item = self.log_q.get_nowait()
                if isinstance(item, tuple):
                    if item[:1] == ("__gen_done__",):
                        self._gen_done(item[1] if len(item) > 1 else "")
                        continue
                    if item[:1] == ("__diag__",):
                        self._append_log_block(item[1])
                        drained += 1
                        continue
                stamp = datetime.now().strftime("%H:%M:%S")
                self._append_log_line(f"{stamp}  {item}")
                drained += 1
        except queue.Empty:
            pass
        try:
            self._flush_log_render()
        except Exception:
            pass
        try:
            delay = 30 if drained >= LOG_DRAIN_BATCH else 120
            self.root.after(delay, self._drain_log)
        except Exception:
            pass

    # ---- Log-tab controls ----------------------------------------------- #

    def _copy_text(self, text: str, label: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            self._log_status(f"Copied {label} ({len(text)} characters) to the clipboard.")
        except Exception as exc:
            self._log_status(f"Could not copy the {label}: {exc}")

    def _log_copy_selection(self, _event=None):
        """Ctrl+C / Copy selection: normal text-widget copy semantics."""
        box = getattr(self, "log_box", None)
        if box is None:
            return None
        try:
            start = box.index("sel.first")
            end = box.index("sel.last")
            text = box.get(start, end)
        except tk.TclError:
            self._log_status("Select some log text first, then Copy (Ctrl+C).")
            return "break"
        except Exception:
            return None
        self._copy_text(text, "selection")
        return "break"

    def _log_copy_failure(self) -> None:
        diag = getattr(self, "_diag", None)
        if diag is None or not diag.has_failure():
            self._log_status("No failure has been recorded in this session.")
            return
        text = diag.last_failure_text(COPY_FAILURE_PRECEDING_EVENTS)
        self._copy_text(text, "last failure report")

    def _complete_session_log_text(self) -> str:
        """Return the canonical, sanitized, unabridged current-session log."""
        diag = getattr(self, "_diag", None)
        if diag is not None:
            try:
                return diag.session_text()
            except Exception:
                pass
        # DiagnosticStream is deliberately fail-safe. If it is unavailable,
        # preserve everything already visible instead of returning nothing.
        return "\n".join(self._log_window)

    def _log_copy_all(self) -> None:
        text = self._complete_session_log_text()
        if not text:
            self._log_status("No session events have been recorded yet.")
            return
        self._copy_text(text, "full session log")

    def _log_save(self) -> None:
        text = self._complete_session_log_text()
        if not text:
            self._log_status("No session events have been recorded yet.")
            return
        path = filedialog.asksaveasfilename(
            title="Save session log",
            defaultextension=".txt",
            initialfile="creator_session_log.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            self._log_status(f"Saved session log to {path}")
        except OSError as exc:
            self._log_status(f"Could not save the session log: {exc}")

    def _log_load_earlier(self) -> None:
        box = getattr(self, "log_box", None)
        if box is None:
            return
        try:
            self._log_autoscroll.set(False)
        except Exception:
            pass
        try:
            box.yview_moveto(0.0)
        except Exception:
            pass
        self._log_status(
            "Showing the first line. The complete current-session log is "
            "already loaded; nothing is hidden."
        )

    def _suspend_autoscroll(self) -> None:
        try:
            if self._log_autoscroll.get():
                self._log_autoscroll.set(False)
                self._log_status(
                    "Auto-scroll paused while you read; tick Auto-scroll to "
                    "resume following the latest output."
                )
        except Exception:
            pass

    def _on_autoscroll_toggle(self) -> None:
        if self._auto_scroll_on():
            self._log_status("Following the latest output.")
            box = getattr(self, "log_box", None)
            if box is not None:
                try:
                    box.see("end")
                except Exception:
                    pass
        else:
            self._log_status(
                "Auto-scroll off; new messages still arrive without moving your view."
            )

    def _on_detailed_toggle(self) -> None:
        # Creator always records every safe frame row. Privacy-sensitive frames
        # remain deliberately suppressed by the capture call itself.
        try:
            self._log_detailed.set(True)
            set_preview_detail(True)
        except Exception:
            pass
        self._log_status(
            "Detailed safe frame logging is always on; sensitive values stay redacted."
        )

    def _on_log_clicked(self, _event=None):
        try:
            box = getattr(self, "log_box", None)
            if box is not None:
                box.index("sel.first")
                box.index("sel.last")
                self._suspend_autoscroll()
        except Exception:
            pass
        return None

    _NAV_KEYS = frozenset(
        {"Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End"}
    )

    def _on_log_key(self, event=None):
        if event is not None and event.keysym in self._NAV_KEYS:
            self._suspend_autoscroll()
        return None

    def _on_log_navigate(self, _event=None):
        self._suspend_autoscroll()
        return None

    def _build_log_tab(self) -> None:
        tab = self.log_tab
        controls = ttk.Frame(tab)
        controls.pack(fill="x", padx=8, pady=(8, 0))

        def button(text, command):
            return ttk.Button(controls, text=text, command=command)

        button("Copy selection", self._log_copy_selection).pack(side="left", padx=(0, 6))
        button("Copy last failure", self._log_copy_failure).pack(side="left", padx=6)
        button("Copy all session logs", self._log_copy_all).pack(side="left", padx=6)
        button("Save session log…", self._log_save).pack(side="left", padx=6)
        button("Go to first log", self._log_load_earlier).pack(side="left", padx=6)
        ttk.Checkbutton(
            controls,
            text="Auto-scroll",
            variable=self._log_autoscroll,
            command=self._on_autoscroll_toggle,
        ).pack(side="left", padx=(12, 6))
        detailed_check = ttk.Checkbutton(
            controls,
            text="Detailed frames (always on)",
            variable=self._log_detailed,
            command=self._on_detailed_toggle,
        )
        detailed_check.pack(side="left", padx=6)
        try:
            detailed_check.state(["selected", "disabled"])
        except Exception:
            pass

        self.log_box = scrolledtext.ScrolledText(
            tab, wrap="word", height=24, font=("Consolas", 10),
            bg="#0c1a30", fg="#dce9fc", insertbackground="#eaf2ff",
            selectbackground="#285783", relief="flat", padx=10, pady=10,
        )
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_box.configure(state="disabled")

        footer = ttk.Frame(tab)
        footer.pack(fill="x", padx=8, pady=(0, 6))
        self.log_older_label = ttk.Label(footer, text="", foreground="#9fb6d6")
        self.log_older_label.pack(side="left")
        self.log_persist_label = ttk.Label(footer, text="", foreground="#ff8f8f")
        self.log_persist_label.pack(side="right")
        self.log_status_label = ttk.Label(footer, text="", foreground="#7fd1a0")
        self.log_status_label.pack(side="right", padx=12)

        box = self.log_box
        box.bind("<<Copy>>", self._log_copy_selection)
        box.bind("<ButtonRelease-1>", self._on_log_clicked)
        box.bind("<MouseWheel>", self._on_log_navigate)
        box.bind("<Key>", self._on_log_key)

    def _refresh_slots(self, select: str = "") -> None:
        current = str(select or self._selected_id() or "").strip().lower()
        rows = load_slots()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for row in rows:
            sid = str(row.get("id") or "")
            user = str(row.get("username") or "").strip() or sid
            birthday = str(row.get("reddit_birth_date") or "").strip() or "—"
            ghost = "yes" if row.get("ghost") else "—"
            email = str(row.get("email") or "") or "(none)"
            proxy_ip = str(row.get("falcon_proxy_verified_ip") or "").strip()
            proxy_status = f"Proxy IP verified: {proxy_ip}" if proxy_ip else "Unknown"
            if slot_reddit_ready(row):
                done = "yes / closed"
            elif slot_reddit_cleanup_pending(row):
                done = "cleanup pending"
            else:
                done = "not yet"
            self.tree.insert(
                "", "end", iid=sid,
                values=(user, birthday, sid, ghost, email, proxy_status, done),
            )
        kids = self.tree.get_children()
        if kids:
            if current not in kids:
                actionable = [
                    str(row.get("id") or "")
                    for row in rows
                    if row.get("id") and not slot_reddit_ready(row)
                ]
                current = actionable[-1] if actionable else kids[0]
            self.tree.selection_set(current)
        if hasattr(self, "post_container"):
            self._refresh_container_choices()
        self._sync_row_buttons()

    def _selected_id(self) -> str:
        sel = self.tree.selection()
        return sel[0] if sel else ""

    def _load_macrodroid_settings(self) -> tuple[str, str]:
        """Load and validate private URLs without returning them in errors or logs."""
        try:
            secrets = load_secrets()
        except Exception as exc:
            raise IpChangeError("Local secrets configuration could not be read.") from exc
        webhook = validate_webhook_url(
            str(secrets.get("macrodroid_change_ip_webhook_url") or "")
        )
        proxy_url = validate_proxy_url(str(secrets.get("falcon_proxy_url") or ""))
        return webhook, proxy_url

    def _macro_guard_active(self) -> bool:
        pending = getattr(self, "pending_ip_action", None)
        if not isinstance(pending, dict) or pending.get("backend") != "macrodroid":
            return False
        try:
            guard_until = float(pending.get("guard_until") or 0.0)
        except (TypeError, ValueError):
            return False
        if guard_until > time.monotonic():
            return True
        if guard_until and not getattr(self, "ip_reset_busy", False):
            self.pending_ip_action = None
        return False

    def _refresh_macrodroid_setup(self) -> bool:
        try:
            self.macrodroid_webhook, self.falcon_proxy_url = (
                self._load_macrodroid_settings()
            )
            self.macrodroid_setup_ready = True
            self.macrodroid_config_error = ""
        except (IpChangeError, OSError, ValueError, TypeError):
            self.macrodroid_webhook = ""
            self.falcon_proxy_url = ""
            self.macrodroid_setup_ready = False
            self.macrodroid_config_error = (
                "MacroDroid webhook or FalconProxy verification is not configured; "
                "webhook will not be sent"
            )
        return self.macrodroid_setup_ready

    @staticmethod
    def _display_utc(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return "—"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (TypeError, ValueError):
            return "—"

    def _restore_manual_ip_history(self) -> None:
        """Restore the latest verified transition for the configured proxy."""
        if not getattr(self, "falcon_proxy_url", ""):
            return
        try:
            latest = latest_configured_transition(
                IP_HISTORY_PATH,
                proxy_url=self.falcon_proxy_url,
                secrets_path=SECRETS_PATH,
            )
        except HistoryError:
            self.log("WARNING IP history could not be restored; reset remains available.")
            return
        if not latest:
            return
        self.ip_previous_var.set(str(latest.get("old_ip") or "—"))
        self.ip_verified_new_var.set(str(latest.get("new_ip") or "—"))
        self.ip_verified_at_var.set(self._display_utc(latest.get("confirmed_at_utc")))

    def _apply_ip_progress(self, operation_id: int, event: dict) -> None:
        pending = getattr(self, "pending_ip_action", None)
        if (
            operation_id != getattr(self, "ip_operation_generation", -1)
            or not isinstance(pending, dict)
            or pending.get("operation_id") != operation_id
        ):
            return
        status = str(event.get("status") or "Checking proxy IP…")
        if hasattr(self, "ip_cycle_status_var"):
            self.ip_cycle_status_var.set(status)
        if event.get("phase") == "baseline_warning":
            self.log("WARNING " + status)

    def request_ip_reset(
        self,
        *,
        trigger_source: str = "backend",
        show_dialog: bool = False,
    ) -> bool:
        """Start the same asynchronous reset used by the button.

        Backend code can call ``request_ip_reset()`` directly; no browser click
        is involved.  False means a reset/guard is already active or local
        configuration is unavailable.
        """
        bulk_state = getattr(self, "bulk_state", {})
        if (
            isinstance(bulk_state, dict)
            and bulk_state.get("stage") not in {None, "idle", "stopped"}
        ):
            self.log("IP reset blocked while a batch is active or paused.")
            return False
        if getattr(self, "ip_reset_busy", False) or getattr(self, "pending_ip_action", None):
            self.log("An IP change or duplicate-trigger guard is already active.")
            return False
        if trigger_source == "manual" and getattr(self, "worker", None) and self.worker.is_alive():
            self.log("Wait for the current Creator task to finish before resetting IP.")
            return False
        if trigger_source == "manual" and show_dialog:
            # Keep the button path compatible with existing panel integrations.
            return bool(self._start_macrodroid_ip_change())
        return self._start_macrodroid_ip_change(
            trigger_source=trigger_source, show_dialog=show_dialog
        )

    def _start_macrodroid_ip_change(
        self,
        *,
        trigger_source: str = "manual",
        show_dialog: bool = True,
    ) -> bool:
        if not self._refresh_macrodroid_setup():
            self.log("MacroDroid reset blocked: required local configuration is unavailable.")
            if show_dialog:
                messagebox.showerror(
                    "Change IP unavailable",
                    self.macrodroid_config_error + ".",
                    parent=self.root,
                )
            if hasattr(self, "ip_cycle_status_var"):
                self.ip_cycle_status_var.set("Unavailable — check local configuration")
            self._sync_row_buttons()
            return False
        self.ip_operation_generation = int(getattr(self, "ip_operation_generation", 0)) + 1
        operation_id = self.ip_operation_generation
        operation_uuid = str(uuid.uuid4())
        self.ip_operation_started = time.monotonic()
        self.ip_cancel_event = threading.Event()
        self.pending_ip_action = {
            "backend": "macrodroid",
            "operation_id": operation_id,
            "operation_uuid": operation_uuid,
            "trigger_source": str(trigger_source or "backend"),
            "show_dialog": bool(show_dialog),
            "guard_until": 0.0,
        }
        self.ip_reset_busy = True
        if hasattr(self, "ip_cycle_status_var"):
            self.ip_cycle_status_var.set("Checking baseline proxy IP…")
        self.log(f"=== Xiaomi / MacroDroid {trigger_source} IP change ===")
        self._sync_row_buttons()
        webhook = self.macrodroid_webhook
        proxy_url = self.falcon_proxy_url
        cancel_event = self.ip_cancel_event
        self.ip_reset_thread = threading.Thread(
            target=self._run_macrodroid_ip_change,
            args=(operation_id, operation_uuid, webhook, proxy_url, cancel_event),
            name=f"macrodroid-ip-{operation_id}",
            daemon=True,
        )
        self.ip_reset_thread.start()
        return True

    def _run_macrodroid_ip_change(
        self,
        operation_id: int,
        operation_uuid: str,
        webhook: str,
        proxy_url: str,
        cancel_event: threading.Event,
    ) -> None:
        observations: list[dict] = []

        def progress(event: dict) -> None:
            payload = dict(event)
            try:
                self.root.after(
                    0,
                    lambda oid=operation_id, item=payload: self._apply_ip_progress(oid, item),
                )
            except (tk.TclError, AttributeError):
                pass

        try:
            result = run_macrodroid_change(
                webhook,
                proxy_url,
                public_ip_url=DEFAULT_PUBLIC_IP_URL,
                cancel_event=cancel_event,
                operation_id=operation_uuid,
                progress=progress,
                on_observation=lambda item: observations.append(dict(item)),
            )
        except Exception:
            result = {
                "outcome": "verification_failed",
                "old_ip": "",
                "new_ip": "",
                "current_ip": "",
                "last_success_at": "",
                "trigger_attempted": False,
                "trigger_deadline": 0.0,
                "elapsed_seconds": max(0.0, time.monotonic() - self.ip_operation_started),
                "error": "Verification failed safely.",
            }
        result = dict(result)
        if result.get("outcome") == "verified":
            proof = result.get("verification_evidence") or {}
            confirmations = proof.get("confirmations") or []
            if confirmations:
                result["confirmed_at_utc"] = confirmations[-1].get("observed_at_utc")
            try:
                record_configured_operation(
                    IP_HISTORY_PATH,
                    proxy_url=proxy_url,
                    secrets_path=SECRETS_PATH,
                    operation_id=operation_uuid,
                    observations=observations,
                    result=result,
                )
                result["history_saved"] = True
            except HistoryError:
                result["history_saved"] = False
                result["history_error"] = "IP history was not saved."
                self.log("WARNING Verified IP change is valid, but IP history was not saved.")
        if getattr(self, "closing", False):
            return
        try:
            self.root.after(
                0,
                lambda oid=operation_id, payload=dict(result): self._finish_macrodroid_ip_change(
                    oid, payload
                ),
            )
        except (tk.TclError, AttributeError):
            pass

    def _finish_macrodroid_ip_change(self, operation_id: int, result: dict) -> None:
        if getattr(self, "closing", False):
            return
        pending = getattr(self, "pending_ip_action", None)
        if (
            operation_id != getattr(self, "ip_operation_generation", -1)
            or not isinstance(pending, dict)
            or pending.get("operation_id") != operation_id
        ):
            return
        self.ip_reset_thread = None
        self.ip_reset_busy = False
        self.ip_cancel_event = None
        outcome = str(result.get("outcome") or "verification_failed")
        old_ip = str(result.get("old_ip") or "")
        new_ip = str(result.get("new_ip") or "")
        uncertain = bool(result.get("trigger_uncertain"))
        show_dialog = bool(pending.get("show_dialog", True))
        endpoint = sanitized_endpoint(getattr(self, "falcon_proxy_url", ""))
        target = sanitized_endpoint(DEFAULT_PUBLIC_IP_URL)
        measurement = f"FalconProxy {endpoint} egress → {target}"
        route_note = "Creator application route not verified."
        elapsed = max(0.0, float(result.get("elapsed_seconds") or 0.0))
        if outcome == "verified":
            message = "IP change verified"
            if uncertain:
                message += " after uncertain trigger delivery"
            self.log(message + ".")
            if hasattr(self, "ip_previous_var"):
                self.ip_previous_var.set(old_ip or "—")
            if hasattr(self, "ip_verified_new_var"):
                self.ip_verified_new_var.set(new_ip or "—")
            if hasattr(self, "ip_verified_at_var"):
                self.ip_verified_at_var.set(
                    self._display_utc(result.get("confirmed_at_utc"))
                )
            if hasattr(self, "ip_cycle_status_var"):
                self.ip_cycle_status_var.set(message)
            dialog_title = "IP change verified"
            dialog_body = (
                f"Old IP: {old_ip or 'unavailable'}\n"
                f"New IP: {new_ip or 'unavailable'}\n"
                f"Measurement: {measurement}\n"
                f"Elapsed: {elapsed:.0f}s\n\n{route_note}"
            )
            if result.get("history_saved") is False:
                dialog_body += "\n\nIP history was not saved."
            dialog = messagebox.showinfo
        elif outcome == "no_change":
            message = str(result.get("error") or "No IP change observed.")
            self.log("IP observation completed without a verified change.")
            if hasattr(self, "ip_cycle_status_var"):
                self.ip_cycle_status_var.set("No IP change observed")
            dialog_title = "No IP change observed"
            dialog_body = (
                f"{message}\n\nOld IP: {old_ip or 'unavailable'}\n"
                f"Measurement: {measurement}\n"
                f"Elapsed: {elapsed:.0f}s\n\n{route_note}"
            )
            dialog = messagebox.showwarning
        elif outcome == "cancelled":
            message = str(
                result.get("error")
                or "Observation canceled; the submitted Android macro may continue."
            )
            self.log("MacroDroid observation canceled; no second trigger will be sent.")
            if hasattr(self, "ip_cycle_status_var"):
                self.ip_cycle_status_var.set("Canceled")
            dialog_title = "IP observation canceled"
            dialog_body = f"{message}\n\n{route_note}"
            dialog = messagebox.showwarning
        else:
            message = str(result.get("error") or "Verification timed out or failed.")
            self.log("ERROR MacroDroid IP change: verification did not complete.")
            if hasattr(self, "ip_cycle_status_var"):
                current_ip = str(result.get("current_ip") or "")
                self.ip_cycle_status_var.set(
                    "Observed but not verified" if current_ip else "IP change not verified"
                )
            dialog_title = "IP change not verified"
            dialog_body = (
                f"{message}\n\n"
                f"Old IP: {old_ip or 'unavailable'}\n"
                f"Measurement: {measurement}\n"
                f"Elapsed: {elapsed:.0f}s\n\n{route_note}"
            )
            dialog = messagebox.showerror

        if show_dialog:
            try:
                dialog(dialog_title, dialog_body, parent=self.root)
            except (tk.TclError, AttributeError):
                pass

        deadline = float(result.get("trigger_deadline") or 0.0)
        if outcome == "cancelled" and bool(result.get("trigger_attempted")):
            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0:
                pending["guard_until"] = deadline
                try:
                    self.root.after(
                        max(1, int(remaining * 1000) + 100),
                        lambda oid=operation_id: self._release_macrodroid_guard(oid),
                    )
                except (tk.TclError, AttributeError):
                    pass
            else:
                self.pending_ip_action = None
        else:
            self.pending_ip_action = None
        self._sync_row_buttons()

    def _release_macrodroid_guard(self, operation_id: int) -> None:
        if getattr(self, "closing", False):
            return
        pending = getattr(self, "pending_ip_action", None)
        if (
            isinstance(pending, dict)
            and pending.get("backend") == "macrodroid"
            and pending.get("operation_id") == operation_id
            and float(pending.get("guard_until") or 0.0) <= time.monotonic()
        ):
            self.pending_ip_action = None
            self._sync_row_buttons()

    def on_request_new_ip(self) -> None:
        """Run one explicit MacroDroid request with FalconProxy verification."""
        self.request_ip_reset(trigger_source="manual", show_dialog=True)

    def _sync_ip_setup_controls(self, busy: bool) -> None:
        pending = getattr(self, "pending_ip_action", None)
        bulk_state = getattr(self, "bulk_state", {})
        batch_ip_pause = (
            isinstance(bulk_state, dict)
            and bulk_state.get("stage") == "paused"
            and bulk_state.get("failure_phase") == "ip"
        )
        batch_active = (
            isinstance(bulk_state, dict)
            and bulk_state.get("stage") not in {None, "idle", "stopped"}
        )
        locked = bool(busy or pending is not None or batch_active)
        if hasattr(self, "ip_reset_btn"):
            self.ip_reset_btn.configure(
                text=(
                    "Batch paused — IP confirmation"
                    if batch_ip_pause else "Changing IP…" if locked else "Change IP"
                ),
                state="disabled" if locked else "normal",
            )

    def _sync_row_buttons(self) -> None:
        bulk_state = getattr(self, "bulk_state", {})
        bulk_locked = (
            isinstance(bulk_state, dict)
            and bulk_state.get("stage") not in {None, "idle", "stopped"}
        )
        active_busy = (
            bool(self.worker and self.worker.is_alive())
            or bool(getattr(self, "ip_reset_busy", False))
            or self._macro_guard_active()
            or bulk_locked
        )
        busy = active_busy
        if hasattr(self, "stop_btn"):
            self.stop_btn.configure(state="normal" if active_busy else "disabled")
        sid = self._selected_id()
        slot = get_slot(sid) if sid else None
        has_email = bool(slot and slot.get("email"))
        self.email_btn.configure(
            state="disabled"
            if busy or not sid or not slot or has_email
            else "normal"
        )
        self.copy_btn.configure(state="disabled" if not has_email else "normal")
        has_user = False
        try:
            has_user = bool(str((slot or {}).get("username") or "").strip())
        except Exception:
            has_user = False
        self.profile_btn.configure(
            state="disabled" if busy or not has_user else "normal"
        )
        cleanup_pending = slot_reddit_cleanup_pending(slot)
        reddit_action_ready = bool(slot) and (
            cleanup_pending or not slot_reddit_done(slot)
        )
        self.reddit_btn.configure(
            text=(
                "Finish cleanup (iPhone X)"
                if cleanup_pending
                else "Create Reddit (iPhone X)"
            ),
            state="disabled" if busy or not reddit_action_ready else "normal",
        )
        if hasattr(self, "qa_btn"):
            self.qa_btn.configure(
                state="normal"
                if not busy and slot_reddit_ready(slot)
                else "disabled"
            )
        if hasattr(self, "qa_options_btn"):
            self.qa_options_btn.configure(
                state="normal" if not busy and sid else "disabled"
            )
        self.ghost_btn.configure(state="disabled" if busy else "normal")
        if hasattr(self, "start_creation_btn"):
            self.start_creation_btn.configure(
                state="disabled" if busy else "normal"
            )
        self._sync_ip_setup_controls(active_busy)
        if hasattr(self, "delete_btn"):
            self.delete_btn.configure(
                state="disabled" if busy or not sid else "normal"
            )
        profile = False
        try:
            profile = self.post_dest.get() == "profile"
        except (tk.TclError, AttributeError):
            pass
        if hasattr(self, "post_btn"):
            post_sid = self._selected_container_id()
            post_slot = get_slot(post_sid) if post_sid else None
            self.post_btn.configure(
                state="normal"
                if not busy and slot_reddit_ready(post_slot)
                else "disabled"
            )
        if hasattr(self, "save_state_btn"):
            self.save_state_btn.configure(state="disabled" if busy else "normal")
        if hasattr(self, "comm_add_btn"):
            self.comm_add_btn.configure(
                state="disabled" if busy or profile else "normal"
            )
        if hasattr(self, "comm_remove_btn"):
            self.comm_remove_btn.configure(
                state="disabled" if busy or profile else "normal"
            )
        if sid and hasattr(self, "post_container"):
            try:
                typed = self.post_container.get().strip()
            except (tk.TclError, AttributeError):
                typed = ""
            if not typed:
                self._refresh_container_choices(select=sid)
        self._refresh_bulk_panel()

    def _persist_bulk(self, state: dict, *, allow_from_stopped: bool = False) -> None:
        """Durably replace batch state before exposing it to another thread."""
        with self.bulk_mutex:
            current = getattr(self, "bulk_state", empty_bulk_state())
            if (
                current.get("stage") == "stopped"
                and state.get("stage") != "stopped"
                and not allow_from_stopped
            ):
                raise BulkError("A stopped batch cannot be restarted by a late callback.")
            # Lightweight offline harnesses construct App without __init__.
            # Keep their isolated persistence seam while every real panel uses
            # CAS. Only a replaced (recording) writer is called, so an unpatched
            # harness can never touch the live state file.
            if not hasattr(self, "bulk_writer_id"):
                if save_bulk_state is not _REAL_SAVE_BULK_STATE:
                    save_bulk_state(state)
                self.bulk_state = dict(state)
                return
            writer_id = str(getattr(self, "bulk_writer_id", "") or uuid.uuid4())
            self.bulk_writer_id = writer_id
            claimed = claim_bulk_state(
                state,
                writer_id,
                new_run=not bool(state.get("run_id")),
            )
            saved = save_bulk_state_cas(
                claimed,
                int(current.get("revision") or 0),
                # Holding the project ownership lock proves the previous writer
                # is gone, so its stale writer id may be taken over through the
                # normal revision CAS (the revision check still rejects a stale
                # or concurrent write).
                allow_writer_takeover=allow_from_stopped
                or self._bulk_owner_lock_held(),
            )
            self.bulk_state = dict(saved)
        try:
            self.root.after(0, self._sync_row_buttons)
        except (tk.TclError, AttributeError):
            pass

    def _assert_bulk_requested_username(
        self, sid: str, entry: dict, slot: dict | None, *, require_saved: bool = False,
    ) -> None:
        requested = str(entry.get("username") or "").strip()
        requested_password = str(entry.get("password") or "")
        saved = str((slot or {}).get("username") or "").strip()
        saved_password = str((slot or {}).get("password") or "")
        if requested:
            owner = self._username_claim_owners().get(requested.casefold())
            if owner is not None and str(owner.get("slot_id") or "") != sid:
                raise BulkError(
                    f"Requested username {requested!r} is already bound to "
                    f"{owner.get('slot_id')}; it cannot be reused by {sid}."
                )
        if requested and ((saved and saved != requested) or (require_saved and saved != requested)):
            raise BulkError(
                f"{sid or 'Current slot'} has saved username {saved!r}, but this batch "
                f"requests {requested!r}. Did not reuse the slot or change its username."
            )
        if requested_password and (
            (saved_password and saved_password != requested_password)
            or (require_saved and saved_password != requested_password)
        ):
            raise BulkError(
                f"{sid or 'Current slot'} has a different saved password than this "
                "batch requests. Did not reuse the slot, overwrite, or type it."
            )

    def _commit_bulk_username_variant(
        self, sid: str, expected: str, candidate: str
    ) -> None:
        """Atomically replace only the live queue head's accepted username.

        Reddit availability is proven before this call, while the irreversible
        local username claim and Continue tap happen afterwards.  The password
        and every other queue field remain byte-for-byte unchanged.
        """
        expected = str(expected or "").strip()
        candidate = str(candidate or "").strip()
        if USER_RE.fullmatch(expected) is None or USER_RE.fullmatch(candidate) is None:
            raise BulkError("Accepted username variant is malformed.")
        if candidate == expected:
            return
        with self.bulk_mutex:
            state = dict(self.bulk_state)
            if str(state.get("slot_id") or "") != str(sid or ""):
                raise BulkError("Username variant does not match the active slot.")
            queue_rows = [dict(item) for item in state.get("queue") or []]
            if not queue_rows or int(state.get("remaining") or 0) < 1:
                raise BulkError("Username variant has no durable queue head.")
            current = str(queue_rows[0].get("username") or "").strip()
            if current == candidate:
                return
            if current != expected:
                raise BulkError(
                    "Username queue head changed while Reddit checked availability."
                )
            owner = self._username_claim_owners().get(candidate.casefold())
            if owner is not None and str(owner.get("slot_id") or "") != sid:
                raise BulkError("Accepted username variant is owned by another slot.")
            original_password = queue_rows[0].get("password")
            queue_rows[0]["username"] = candidate
            if queue_rows[0].get("password") != original_password:
                raise BulkError("Username variant unexpectedly changed the password.")
            updated = dict(state)
            updated["queue"] = queue_rows
            self._persist_bulk(updated)

            # Keep the panel's append-only editor baseline synchronized with
            # the durable head so Save & Extend cannot later resurrect the
            # rejected original value.
            baseline = [
                dict(item) for item in getattr(self, "bulk_editor_baseline", [])
            ]
            consumed = len(baseline) - len(queue_rows)
            if 0 <= consumed < len(baseline):
                baseline[consumed]["username"] = candidate
                self.bulk_editor_baseline = baseline
        self.log(
            f"BATCH {sid}: accepted username variant saved to the queue head; "
            "password unchanged (username redacted)."
        )

    def _bulk_username_candidate_available(self, sid: str, candidate: str) -> bool:
        """Reject names reserved by another durable queue/failure/slot owner."""
        candidate = str(candidate or "").strip()
        if USER_RE.fullmatch(candidate) is None:
            return False
        folded = candidate.casefold()
        with self.bulk_mutex:
            state = dict(self.bulk_state)
            if str(state.get("slot_id") or "") != str(sid or ""):
                return False
            queue_rows = list(state.get("queue") or [])
            for item in queue_rows[1:]:
                if str((item or {}).get("username") or "").strip().casefold() == folded:
                    return False
            for failure in state.get("failures") or []:
                item = (failure or {}).get("item") or {}
                if str(item.get("username") or "").strip().casefold() == folded:
                    return False
        owner = self._username_claim_owners().get(folded)
        return owner is None or str(owner.get("slot_id") or "") == sid

    def _filter_claimed_additions(
        self, additions: list[dict],
    ) -> tuple[list[dict], int]:
        """Remove already processing/success usernames from new queue additions."""
        owners = self._username_claim_owners()
        available: list[dict] = []
        skipped = 0
        for item in additions:
            username = str((item or {}).get("username") or "").strip()
            if username and username.casefold() in owners:
                skipped += 1
                continue
            available.append(dict(item))
        return available, skipped

    def _normalize_bulk_queue_usernames(self, state: dict, sid: str) -> dict:
        """Shift unused names forward and remove names owned by other slots.

        The account rows remain in their original order; only the optional
        username pool advances.  The current head may retain a processing claim
        only when that claim belongs to this same slot, which makes Retry
        idempotent.  A historical/stale panel row can therefore never reassign a
        successful username to a new slot.
        """
        queue_rows = [dict(item) for item in state.get("queue") or []]
        if not queue_rows:
            return state
        owners = self._username_claim_owners()
        available_credentials: list[tuple[str, str]] = []
        removed = 0
        for index, item in enumerate(queue_rows):
            username = str(item.get("username") or "").strip()
            if not username:
                continue
            owner = owners.get(username.casefold())
            if owner is None or (
                index == 0 and str(owner.get("slot_id") or "") == sid
            ):
                available_credentials.append(
                    (username, str(item.get("password") or ""))
                )
            else:
                removed += 1
        if not removed:
            return state
        for item in queue_rows:
            item.pop("username", None)
            item.pop("password", None)
        for item, (username, password) in zip(queue_rows, available_credentials):
            item["username"] = username
            if password:
                item["password"] = password
        updated = dict(state)
        updated["queue"] = queue_rows
        self._persist_bulk(updated)
        self.log(
            f"BATCH username queue: removed {removed} name(s) already bound to "
            "another processing/success slot and shifted unused names forward."
        )
        return dict(self.bulk_state)

    def _reserve_missing_slot(self, sid: str) -> bool:
        """Permanently reserve a referenced-but-missing slot id (no account row).

        A prior batch can point at a slot id whose row is gone (for example the
        reported ``a0072``).  That id is reserved in ``retired.json`` so it is
        never handed out again, without fabricating an account row for it.
        Returns True only when a new reservation was written.
        """
        if not sid:
            return False
        if get_slot(sid) is not None:
            return False
        try:
            return bool(retire_slot_id(sid))
        except (OSError, ValueError) as exc:
            raise BulkError(
                f"Could not reserve missing slot {sid}: {exc}. No queue data was changed."
            ) from exc

    def _allocate_fresh_slot_id(self, current: dict) -> str:
        """Pick a new slot id above every known reservation, without writing.

        The superseded batch's slot id is counted as reserved even when its row
        is missing, so a missing id (``a0072``) is never reused and the new run
        lands above it (``a0073``).  An unfinished existing slot is never reused
        either: Start Batch always advances past everything already known.
        """
        used = set(indices_used())
        old_sid = str((current or {}).get("slot_id") or "")
        old_index = name_to_index(old_sid)
        if old_index is not None:
            used.add(old_index)
        return index_to_name(max(used, default=0) + 1)

    def _plan_bulk_start(self, items: list[dict], requested: int) -> dict:
        """Build a brand-new run state from the editor rows, without writing.

        Start Batch always begins a new run: it never reuses a recovery head,
        never replays a stopped/incomplete account, and never compares the new
        rows against a prior queue baseline.  The new run targets a freshly
        allocated slot above every known reservation.
        """
        if type(requested) is not int or requested < 1 or requested > len(items):
            raise BulkError("The new batch has no valid entries to start.")
        state = empty_bulk_state()
        state.update(
            queue=[dict(item) for item in items],
            requested=int(requested),
            remaining=int(requested),
            stage="ip_pending",
            # A newly started batch requests automatic deletion of each
            # completed account's local container.
            delete_containers=True,
            slot_id=self._allocate_fresh_slot_id(
                dict(getattr(self, "bulk_state", empty_bulk_state()))
            ),
        )
        return state

    def _show_bulk_queue(self, state: dict) -> None:
        """Make the text fields reflect the exact durable queue after Start."""
        queue_rows = [dict(row) for row in state.get("queue") or []]
        self.bulk_editor_baseline = queue_rows
        if not hasattr(self, "bulk_count_var"):
            return
        count = int(state.get("remaining") or state.get("requested") or len(queue_rows) or 1)
        draft = self._bulk_rows_payload(queue_rows, count_text=str(count))
        self._set_bulk_editor(draft)
        self.bulk_draft_reconcile_error = ""
        self._save_bulk_draft_now()

    def extend_bulk_batch(self) -> bool:
        """Validate appended editor rows and atomically schedule them once."""
        if self._bulk_owner_gated() and not self._bulk_owner_lock_held():
            # Save & Extend writes the batch, so it needs the ownership lock
            # exactly like Start Batch.  Logged once here; the rows are kept.
            self.bulk_draft_reconcile_error = (
                "another Creator process holds the batch ownership lock"
            )
            if hasattr(self, "bulk_draft_status_var"):
                self.bulk_draft_status_var.set(self.bulk_draft_reconcile_error)
            self.log("BATCH not extended: " + self.bulk_draft_reconcile_error)
            return False
        self.bulk_draft_reconcile_error = self._bulk_editor_validation_error()
        if self.bulk_draft_reconcile_error:
            self._save_bulk_draft_now()
            self.bulk_draft_status_var.set(self.bulk_draft_reconcile_error)
            self.log("BATCH not extended: " + self.bulk_draft_reconcile_error)
            return False
        try:
            draft = self._bulk_draft_payload()
            editor_rows = self._parse_bulk_text_rows(
                draft["births_text"], draft["swipes_text"],
                draft["delays_text"], draft["usernames_text"],
            )
            baseline = [dict(row) for row in self.bulk_editor_baseline]
            if editor_rows[: len(baseline)] != baseline:
                raise BulkError(
                    "Existing queued rows were edited. Restore them and append new rows only."
                )
            additions, claimed_skipped = self._filter_claimed_additions(
                editor_rows[len(baseline):]
            )
            with self.bulk_mutex:
                current = dict(self.bulk_state)
                current_queue = [dict(row) for row in current.get("queue") or []]
                consumed = len(baseline) - len(current_queue)
                if consumed < 0 or baseline[consumed:] != current_queue:
                    raise BulkError(
                        "The live queue changed outside the saved editor baseline. "
                        "Reopen the panel before extending it."
                    )
                updated = extend_bulk_queue(current, additions)
                added_count = updated["remaining"] - current["remaining"]
                self._persist_bulk(updated)
            self._show_bulk_queue(updated)
            self.bulk_count_var.set(str(updated["remaining"]))
            self._save_bulk_draft_now()
            message = (
                f"{added_count} row(s) added; {updated['remaining']} item(s) remaining."
            )
            if claimed_skipped:
                message += (
                    f" Ignored {claimed_skipped} username row(s) already in processing "
                    "or success."
                )
            self.bulk_draft_status_var.set(message)
            self.status.set("Batch extended")
            self.log("BATCH extended: " + message)
            self._refresh_bulk_panel()
            return True
        except (BulkError, PhoneError, TypeError, ValueError) as exc:
            self._save_bulk_draft_now()
            self.bulk_draft_status_var.set(str(exc))
            self.status.set("Batch not extended — check Batch tab")
            self.log(f"BATCH not extended: {exc}")
            return False

    def save_bulk_item_count(self) -> bool:
        """Raise an existing batch's total target without interrupting its worker.

        The number in ``Items to process`` is the desired total for this run,
        not an amount to add.  For example, changing a 50-item batch to 150
        schedules exactly 100 additional validated editor rows.  A target may
        not shrink an in-progress batch, and no queue data is changed unless
        enough complete rows are available and the durable save succeeds.
        """
        if self._bulk_owner_gated() and not self._bulk_owner_lock_held():
            reason = "another Creator process holds the batch ownership lock"
            self.bulk_draft_reconcile_error = reason
            if hasattr(self, "bulk_draft_status_var"):
                self.bulk_draft_status_var.set(reason)
            self.log("BATCH target not saved: " + reason)
            return False

        try:
            target_text = str(self.bulk_count_var.get() or "").strip()
            if re.fullmatch(r"[1-9]\d*", target_text) is None:
                raise BulkError("Items to process must be a positive whole number.")
            target = int(target_text)

            validation_error = self._bulk_editor_validation_error()
            if validation_error:
                raise BulkError(validation_error)

            draft = self._bulk_draft_payload()
            editor_rows = self._parse_bulk_text_rows(
                draft["births_text"], draft["swipes_text"],
                draft["delays_text"], draft["usernames_text"],
            )
            baseline = [dict(row) for row in self.bulk_editor_baseline]
            if editor_rows[: len(baseline)] != baseline:
                raise BulkError(
                    "Existing queued rows were edited. Restore them before saving the target."
                )

            with self.bulk_mutex:
                current = dict(self.bulk_state)
                if current.get("stage") == "idle" or int(current.get("remaining") or 0) < 1:
                    raise BulkError("There is no active, paused, or stopped batch to update.")
                current_queue = [dict(row) for row in current.get("queue") or []]
                consumed = len(baseline) - len(current_queue)
                if consumed < 0 or baseline[consumed:] != current_queue:
                    raise BulkError(
                        "The live queue changed outside the saved editor baseline. "
                        "Reopen the panel before saving the target."
                    )

                old_target = int(current.get("requested") or 0)
                if target < old_target:
                    raise BulkError(
                        f"The active batch target cannot be reduced below {old_target}."
                    )
                needed = target - old_target
                additions, claimed_skipped = self._filter_claimed_additions(
                    editor_rows[len(baseline):]
                )
                if needed > len(additions):
                    raise BulkError(
                        f"Target {target} needs {needed} additional complete row(s), "
                        f"but only {len(additions)} are available."
                    )

                if needed:
                    updated = extend_bulk_queue(current, additions[:needed])
                    self._persist_bulk(updated)
                else:
                    updated = current

            # Remove already-consumed editor heads, but preserve every unused
            # surplus line so the target can be raised again later.
            rebased = dict(draft)
            for field in (
                "births_text", "swipes_text", "delays_text", "usernames_text",
            ):
                rebased[field] = self._drop_text_lines(rebased[field], consumed)
            rebased["count_text"] = str(target)
            rebased["baseline_queue"] = [
                dict(row) for row in updated.get("queue") or []
            ]
            self.bulk_editor_baseline = [
                dict(row) for row in updated.get("queue") or []
            ]
            self._set_bulk_editor(rebased)
            self._save_bulk_draft_now()

            if needed:
                message = (
                    f"Batch target saved: {old_target} -> {target}; "
                    f"{needed} item(s) added without interrupting the current creation."
                )
            else:
                message = f"Batch target already equals {target}; no items were duplicated."
            if claimed_skipped:
                message += (
                    f" Ignored {claimed_skipped} username row(s) already in processing "
                    "or success."
                )
            if hasattr(self, "bulk_draft_status_var"):
                self.bulk_draft_status_var.set(message)
            if hasattr(self, "status"):
                self.status.set("Batch target saved")
            self.log("BATCH " + message)
            self._refresh_bulk_panel()
            return True
        except (BulkError, PhoneError, TypeError, ValueError, tk.TclError) as exc:
            self._save_bulk_draft_now()
            if hasattr(self, "bulk_draft_status_var"):
                self.bulk_draft_status_var.set(str(exc))
            if hasattr(self, "status"):
                self.status.set("Batch target not saved — check Batch tab")
            self.log(f"BATCH target not saved: {exc}")
            return False

    def _bulk_owner_gated(self) -> bool:
        """True for real panels, which carry the ownership-lock attribute.

        Lightweight offline harnesses construct ``App`` without ``__init__``
        and keep the legacy writer-id checks, exactly like the persistence
        seam in :meth:`_persist_bulk`.
        """
        return hasattr(self, "_bulk_owner_lock")

    def _bulk_owner_lock_held(self) -> bool:
        """True only while this process holds the project ownership lock.

        Only true panels gate on the lock; a harness without the attribute
        reports False so its legacy writer-id expectations are unchanged.
        """
        return bool(getattr(self, "_bulk_owner_lock", False))

    def _bulk_foreign_writer(self, current: dict) -> bool:
        """True when the saved batch is owned by a different Creator process."""
        mine = str(getattr(self, "bulk_writer_id", "") or "")
        theirs = str((current or {}).get("writer_id") or "")
        return bool(mine and theirs and theirs != mine)

    def _latest_bulk_state(self) -> dict:
        """Return the freshest durable batch state for a Start decision.

        Real panels re-read the state file so the ownership decision, the safe
        takeover test, and the CAS revision all reflect another process's latest
        write.  Lightweight offline harnesses replace this seam so they never
        touch live state.
        """
        return dict(load_bulk_state())

    @staticmethod
    def _bulk_clean_idle(current: dict) -> bool:
        """True only for a saved batch that provably holds no work or ownership.

        A previous Creator can leave its ``writer_id`` behind on an otherwise
        empty idle state.  That stale owner is not an active second Creator, so
        taking the batch over is safe — but only when nothing is pending: idle
        stage, empty queue, no slot, and no IP/creation evidence or recovery
        state.  Anything active, paused, stopped, or inconsistent stays unsafe.
        """
        state = current or {}
        blank = empty_bulk_state()
        for field, empty_value in blank.items():
            if field in _BULK_CLEAN_IDLE_IGNORED:
                continue
            if state.get(field) != empty_value:
                return False
        return True

    def _log_bulk_start_blocked(
        self, code: str, detail: str, wait_seconds: float | None = None
    ) -> None:
        """Report one machine-readable start refusal without touching state."""
        extra = ""
        if wait_seconds is not None and wait_seconds > 0:
            extra = f" Bounded wait ~{wait_seconds:.0f}s."
        self.log(f"BATCH start blocked [{code}]: {detail}.{extra}")
        self._set_diag("set_disposition", f"start blocked [{code}]")

    def _start_fresh_bulk(self, state: dict, current: dict) -> bool:
        """Archive the superseded batch+draft, reserve gaps, then launch.

        Nothing is replaced until every durable side-effect succeeds, so an
        archive/reservation/ownership/persistence failure leaves the prior
        state recoverable and launches no worker.
        """
        old_sid = str((current or {}).get("slot_id") or "")
        # 1) Reserve a missing old slot id before the run is activated so the
        #    number is never reused.  This records a reservation only; it never
        #    fabricates an account row for the vanished slot.
        if old_sid and get_slot(old_sid) is None:
            try:
                self._reserve_missing_slot(old_sid)
            except BulkError as exc:
                self._log_bulk_start_blocked(
                    START_RESERVATION_FAILED_CODE,
                    f"missing slot {old_sid} could not be reserved ({exc}); prior state kept",
                )
                return False
            self.log(
                f"BATCH fresh start [{START_RESERVATION_RESERVED_CODE}]: missing slot "
                f"{old_sid} reserved; it will not be reused."
            )
        # 2) Archive the superseded batch state and the raw editor draft as
        #    timestamped, uniquely named copies before either is replaced.
        has_previous = (
            (current or {}).get("stage") not in {"idle", None}
            or int((current or {}).get("remaining") or 0) > 0
            or bool((current or {}).get("run_id"))
        )
        if has_previous:
            try:
                archive_path = archive_bulk_state(current, label="superseded")
            except Exception as exc:
                self._log_bulk_start_blocked(
                    START_ARCHIVE_FAILED_CODE,
                    f"the previous batch could not be archived ({exc}); prior state kept",
                )
                return False
            self.log(
                "BATCH fresh start: superseded batch archived to "
                f"{os.path.basename(str(archive_path))} "
                "(reason: replaced by a new batch from the current rows)."
            )
            try:
                draft_archive = archive_bulk_draft()
            except Exception as exc:
                self._log_bulk_start_blocked(
                    START_DRAFT_ARCHIVE_FAILED_CODE,
                    f"the previous draft could not be archived ({exc}); prior draft kept",
                )
                return False
            if draft_archive is not None:
                self.log(
                    "BATCH fresh start: superseded draft archived to "
                    f"{os.path.basename(str(draft_archive))}."
                )
        # 3) Durably replace the state (CAS).  On failure prior state survives.
        try:
            self._persist_bulk(state, allow_from_stopped=True)
        except BulkError as exc:
            self._log_bulk_start_blocked(
                START_PERSIST_FAILED_CODE,
                f"the new batch could not be saved ({exc}); prior state kept",
            )
            return False
        # Preserve the raw editor draft, including surplus rows not scheduled.
        self.bulk_editor_baseline = [dict(row) for row in state.get("queue") or []]
        self._save_bulk_draft_now()
        self.log(
            f"BATCH fresh start [{START_ALLOCATED_CODE}]: {state['remaining']} item(s) "
            f"on new slot {state.get('slot_id')}; IP verification before each new item."
        )
        self._start_bulk_worker()
        return True

    def start_bulk_batch(self, items: list[dict], requested: int, password: str) -> bool:
        """Start a genuinely new IP-first batch from the current editor rows.

        This is the "Start Batch" path.  It never resumes, reconciles, or
        replays a prior run — "Retry / Resume" continues the saved batch.  From
        idle/stopped/paused it is allowed only when no worker, foreign writer,
        or in-flight IP operation is unresolved; otherwise it refuses with a
        logged reason and launches nothing.
        """
        if getattr(self, "worker", None) and self.worker.is_alive():
            self._log_bulk_start_blocked(
                START_BLOCKED_WORKER_CODE, "a Creator or batch task is already active"
            )
            return False
        pending = getattr(self, "pending_ip_action", None)
        if getattr(self, "ip_reset_busy", False) or pending:
            wait = None
            if isinstance(pending, dict):
                try:
                    wait = max(0.0, float(pending.get("guard_until") or 0) - time.monotonic())
                except (TypeError, ValueError):
                    wait = None
            self._log_bulk_start_blocked(
                START_BLOCKED_IP_GUARD_CODE,
                "an IP change is still in progress",
                wait_seconds=wait,
            )
            return False
        # Re-read the durable state so ownership, the takeover test, and the CAS
        # revision all reflect another process's latest write.  A read failure
        # changes nothing: the saved batch is left exactly as it was.
        try:
            current = self._latest_bulk_state()
        except BulkError as exc:
            self._log_bulk_start_blocked(
                START_LOAD_FAILED_CODE,
                f"the saved batch state could not be read ({exc}); nothing was changed",
            )
            return False
        # Ownership is decided by the process-lifetime lock, not by the saved
        # writer id alone.  A panel that could not take the lock is a second
        # live Creator sharing this project folder, so it must not start,
        # archive, or otherwise change the saved batch.
        if self._bulk_owner_gated() and not self._bulk_owner_lock_held():
            self._log_bulk_start_blocked(
                START_BLOCKED_FOREIGN_WRITER_CODE,
                "another Creator process holds the batch ownership lock",
            )
            return False
        # A foreign writer blocks a start only while its batch is not provably
        # clean idle AND this process holds no lock that would prove the old
        # owner is gone.  Without the lock, a stale writer_id left on an empty
        # idle state (a previous Creator that has exited) is still taken over
        # by the clean-idle rule; with the lock, every other writer id belongs
        # to a Creator that is gone, even on an active state.
        if (
            not self._bulk_owner_lock_held()
            and self._bulk_foreign_writer(current)
            and not self._bulk_clean_idle(current)
        ):
            self._log_bulk_start_blocked(
                START_BLOCKED_FOREIGN_WRITER_CODE,
                "the saved batch is owned by another Creator process",
            )
            return False
        if self._bulk_owner_lock_held() and self._bulk_foreign_writer(current):
            self.log(
                "BATCH ownership: the saved batch was written by Creator process "
                f"{str(current.get('writer_id') or '(none)')[:8]} "
                f"({str(current.get('stage') or 'idle')}); the ownership lock proved "
                "that process is gone, so this Creator took the batch over."
            )
        if current.get("stage") not in {"idle", "stopped", "paused"}:
            self._log_bulk_start_blocked(
                START_BLOCKED_STAGE_CODE,
                f"saved stage {current.get('stage')!r} is not startable",
            )
            return False
        # Adopt the reloaded state so the fresh-start CAS compares against the
        # revision just read and the slot allocator sees the same queue.
        self.bulk_state = dict(current)
        # Validate the new rows and allocate a slot before touching any state.
        state = self._plan_bulk_start(items, requested)
        self.bulk_password = str(password or "")
        return self._start_fresh_bulk(state, current)

    def retry_bulk_batch(self, password: str | None = None) -> bool:
        if getattr(self, "worker", None) and self.worker.is_alive():
            return False
        if self._bulk_owner_gated() and not self._bulk_owner_lock_held():
            self.log(
                "BATCH retry unavailable: another Creator process holds the batch "
                "ownership lock for this project."
            )
            return False
        try:
            if hasattr(self, "bulk_writer_id"):
                current = dict(load_bulk_state())
                self.bulk_state = dict(current)
            else:
                current = dict(self.bulk_state)
            stage = current.get("stage")
            resume_sid = str(current.get("slot_id") or "")
            if (
                resume_sid
                and get_slot(resume_sid) is None
                and (
                    current.get("verified_ip")
                    or current.get("ip_attempt")
                    or current.get("failure_phase")
                    or current.get("skipped")
                    or stage in {
                        "may_have_triggered", "ip_verified", "creating",
                        "retry_creation", "retry_ip", "paused",
                    }
                )
            ):
                # The saved batch already worked against this slot, so its row
                # must exist.  Rebuilding it would fabricate credentials the
                # queue does not have; the only safe continuation is a new run.
                raise BulkError(
                    f"[{RESUME_SLOT_MISSING_CODE}] Saved batch references "
                    f"{resume_sid}, which has no slot row. Use Start Batch to "
                    "begin a new batch; this attempt cannot be resumed and its "
                    "account was not rebuilt or guessed."
                )
            if stage == "paused":
                legacy_reason = str(current.get("failure_reason") or "").strip()
                legacy_pause = (
                    current.get("failure_phase") == "creation"
                    and not str(current.get("failure_code") or "").strip()
                )
                if legacy_pause and current.get("slot_id"):
                    # A paused creation failure with no failure code predates the
                    # coded account-error path.  Its saved alias may already be
                    # gone, so it is recorded as a legacy creation failure,
                    # preserved, and skipped once instead of replaying it.
                    sid = str(current["slot_id"])
                    reason = legacy_reason or (
                        "Legacy paused creation failure had no saved failure code; "
                        "the attempt was preserved and skipped without replaying it."
                    )
                    try:
                        mark_bulk_skipped(
                            sid, LEGACY_CREATION_FAILURE_CODE, reason, self.log
                        )
                    except Exception as exc:
                        raise BulkError(
                            f"The paused attempt {sid} could not be preserved: {exc}"
                        ) from exc
                    head = (current.get("queue") or [{}])[0]
                    ready = datetime.now(timezone.utc) + timedelta(
                        seconds=float(head.get("delay_seconds") or 1)
                    )
                    state = consume_skipped(
                        current,
                        sid,
                        code=LEGACY_CREATION_FAILURE_CODE,
                        reason=reason,
                        ready_at_utc=ready.isoformat().replace("+00:00", "Z"),
                    )
                    self.log(
                        f"BATCH {sid}: legacy paused creation failure preserved and "
                        "skipped once; its saved email was not reused."
                    )
                elif legacy_pause:
                    raise BulkError(
                        "A paused creation failure has no saved slot id; it was left "
                        "paused instead of replaying."
                    )
                else:
                    state = prepare_bulk_retry(current)
            elif stage == "stopped":
                if current.get("ready_at_utc"):
                    state = dict(current, stage="between", failure_phase=None, failure_reason=None)
                elif current.get("ip_attempt") and current.get("trigger_deadline_utc") and not current.get("verified_ip"):
                    # A stopped in-flight reset may have been delivered. Reopen
                    # read-only verification, never dispatch a second webhook.
                    state = prepare_bulk_retry(
                        dict(current, stage="paused", failure_phase="ip")
                    )
                elif current.get("verified_ip"):
                    state = dict(current, stage="retry_creation", failure_phase=None, failure_reason=None)
                else:
                    state = dict(current, stage="ip_pending", failure_phase=None, failure_reason=None)
            elif stage not in {None, "idle"}:
                # An app restart leaves an active durable stage with no worker.
                state = current
            else:
                raise BulkError("There is no saved batch to resume.")
            self._persist_bulk(state, allow_from_stopped=True)
            # Resume must preserve the saved queue; show its real remaining
            # count without adopting the editor count or rewriting editor rows.
            if (
                hasattr(self, "bulk_count_var")
                and self.bulk_state.get("stage") != "idle"
            ):
                self._show_resumed_remaining_count()
        except BulkError as exc:
            self.log(f"BATCH retry unavailable: {exc}")
            return False
        if password is not None:
            self.bulk_password = str(password)
        elif hasattr(self, "pw"):
            self.bulk_password = str(self.pw.get() or "")
        if self.bulk_state.get("stage") == "idle":
            failures = len(self.bulk_state.get("failures") or [])
            self.log(
                f"BATCH complete with failures: {failures} item(s) preserved and skipped."
            )
            status = getattr(self, "status", None)
            if status is not None:
                status.set("Batch complete with failures")
            return True
        self.log(f"BATCH retrying {state.get('failure_phase') or 'saved'} stage.")
        self._start_bulk_worker()
        return True

    def _show_resumed_remaining_count(self) -> None:
        """Show a resumed batch's real remaining count; never adopt the editor.

        Retry / Resume continues the durable queue verbatim.  The count shown
        here is the saved queue's remaining number of items, not whatever the
        editor happens to hold, and the editor rows are deliberately left as
        typed so a later Extend still sees them.
        """
        if not hasattr(self, "bulk_count_var"):
            return
        remaining = int(getattr(self, "bulk_state", {}).get("remaining") or 0)
        try:
            typed = str(self.bulk_count_var.get() or "").strip()
        except tk.TclError:
            typed = ""
        try:
            self.bulk_count_var.set(str(remaining))
        except tk.TclError:
            return
        if typed and typed != str(remaining):
            self.log(
                f"BATCH resume: the saved queue has {remaining} item(s) remaining; "
                f"the editor count {typed!r} was not adopted and its rows were left "
                "as typed."
            )

    def _bulk_live_status(self, message: str) -> None:
        """Show a short live batch/scan status (thread-safe)."""

        def _set() -> None:
            try:
                if hasattr(self, "ip_cycle_status_var"):
                    self.ip_cycle_status_var.set(message)
            except (tk.TclError, AttributeError):
                pass

        try:
            self.root.after(0, _set)
        except (tk.TclError, AttributeError):
            pass

    def _stop_bulk_batch(self) -> None:
        self.stop.set()
        if not hasattr(self, "bulk_writer_id"):
            state = dict(getattr(self, "bulk_state", empty_bulk_state()))
            if state.get("remaining", 0):
                state.update(stage="stopped", failure_phase=None, failure_reason=None)
                self._persist_bulk(state)
            self.log("BATCH stopped; unprocessed queue items were preserved.")
            self._sync_row_buttons()
            return
        if self._bulk_owner_gated() and not self._bulk_owner_lock_held():
            self.log(
                "BATCH stop ignored: another Creator process holds the batch "
                "ownership lock for this project."
            )
            return
        latest = load_bulk_state()
        writer_id = str(getattr(self, "bulk_writer_id", "") or "")
        if (
            latest.get("writer_id") not in (None, writer_id)
            and not self._bulk_owner_lock_held()
        ):
            self.log("BATCH stop ignored for state owned by a newer Creator process.")
            return
        self.bulk_state = dict(latest)
        state = dict(latest)
        if state.get("remaining", 0):
            state.update(stage="stopped", failure_phase=None, failure_reason=None)
            self._persist_bulk(state)
        self.log("BATCH stopped; unprocessed queue items were preserved.")
        self._sync_row_buttons()

    def _start_bulk_worker(self) -> None:
        state = getattr(self, "bulk_state", empty_bulk_state())
        if state.get("stage") in {"idle", "paused", "stopped"}:
            return
        if getattr(self, "worker", None) and self.worker.is_alive():
            return
        self.stop.clear()
        self._busy(True)
        self.worker = threading.Thread(
            target=self._run_bulk_batch,
            name="creator-ip-first-batch",
            daemon=True,
        )
        self.worker.start()

    def _pause_bulk(
        self, phase: str, reason: str, *, code: str | None = None
    ) -> None:
        state = dict(self.bulk_state)
        safe_reason = str(reason).strip()[:300] or "Check failed."
        state.update(
            stage="paused",
            failure_phase=phase,
            failure_reason=safe_reason,
            failure_code=(str(code).strip()[:80] if code else None),
        )
        self._persist_bulk(state)
        self.log(f"BATCH paused ({phase}): {safe_reason}")
        self._set_diag(
            "set_disposition", f"batch paused during {phase}; use Retry / Resume"
        )
        self._set_diag(
            "set_queue", "queue preserved; the current item was not consumed"
        )
        try:
            self.status.set(f"Batch paused — {phase}; use Retry / Resume")
        except (tk.TclError, AttributeError):
            pass

    def _wait_bulk_failure_grace(
        self,
        phase: str,
        reason: str,
        *,
        already_elapsed: bool = False,
    ) -> bool:
        """Keep the batch worker alive for the three-minute recovery window.

        ``already_elapsed`` is used by the inactivity watchdog: that attempt
        has already spent the full window without progress, so adding another
        three-minute sleep would be incorrect.  User Stop always wins and
        leaves the durable queue untouched.
        """
        if self.stop.is_set():
            return False
        seconds = 0.0 if already_elapsed else float(BULK_FAILURE_GRACE_SECONDS)
        safe_reason = str(reason).strip()[:300] or "Check failed."
        if seconds > 0:
            self.log(
                f"BATCH recovery ({phase}): {safe_reason} Waiting "
                f"{int(seconds)} seconds before automatic reset."
            )
            self._bulk_live_status(
                f"{phase.title()} failed — automatic reset in {int(seconds)}s"
            )
            try:
                self.status.set(
                    f"Batch recovering — reset in {int(seconds)} seconds"
                )
            except (tk.TclError, AttributeError):
                pass
            if self.stop.wait(seconds):
                self.log(
                    "BATCH recovery cancelled by Stop; the current queue item "
                    "was preserved."
                )
                return False
        return not self.stop.is_set()

    def _retry_ip_after_failure(
        self,
        reason: str,
        *,
        code: str = "",
        already_elapsed: bool = False,
    ) -> bool:
        """After the grace window, clear stale IP evidence and retry normally."""
        if not self._wait_bulk_failure_grace(
            "ip", reason, already_elapsed=already_elapsed
        ):
            return False
        state = dict(self.bulk_state)
        state.update(
            stage="ip_pending",
            baseline_ip=None,
            verified_ip=None,
            verified_at_utc=None,
            trigger_deadline_utc=None,
            ready_at_utc=None,
            failure_phase=None,
            failure_reason=None,
            failure_code=None,
            ip_attempt=None,
            recovery=None,
        )
        self._persist_bulk(state)
        if already_elapsed:
            self.log(
                "BATCH IP: saved IP evidence could not be reconfirmed; starting "
                "a new verified IP reset immediately for the same slot and queue item."
            )
            self._set_diag(
                "set_disposition",
                "saved IP recheck failed; retrying IP immediately without deleting the slot",
            )
        else:
            self.log(
                "BATCH IP: the three-minute recovery window expired; starting a "
                "new verified IP reset for the same queue item."
            )
            self._set_diag(
                "set_disposition", "IP failure auto-retried after 180 seconds"
            )
        self._set_diag("set_queue", "queue preserved; retrying the same item")
        return True

    def _reset_failed_creation(
        self,
        sid: str,
        entry: dict,
        reason: str,
        *,
        already_elapsed: bool = False,
    ) -> bool:
        """Wait once, then discard only an incomplete account attempt."""
        slot = get_slot(sid)
        if slot and slot.get("reddit") is True:
            self.log(
                f"BATCH recovery {sid}: the Reddit account checkpoint already "
                "exists; the account and container will not be auto-deleted."
            )
            self._pause_bulk(
                "creation",
                reason or (
                    f"Reddit account {sid} exists but its remaining cleanup "
                    "checkpoint is incomplete."
                ),
                code="created_account_preserved",
            )
            return False
        if not self._wait_bulk_failure_grace(
            "creation", reason, already_elapsed=already_elapsed
        ):
            return False
        slot = get_slot(sid)
        if slot and slot.get("reddit") is True:
            self.log(
                f"BATCH recovery {sid}: the Reddit account checkpoint appeared "
                "inside the grace window; automatic deletion was cancelled."
            )
            self._pause_bulk(
                "creation",
                reason or (
                    f"Reddit account {sid} exists but its remaining cleanup "
                    "checkpoint is incomplete."
                ),
                code="created_account_preserved",
            )
            return False
        return self._stall_reset(sid, entry, reason, immediate=already_elapsed)

    def _retry_completed_cleanup_after_failure(self, sid: str, reason: str) -> bool:
        """Pause after one failed cleanup attempt; never enter a timed loop."""
        self.log(
            f"BATCH cleanup {sid}: one deterministic cleanup attempt failed; "
            "pausing immediately with the verified account and queue head preserved."
        )
        self._set_diag(
            "set_disposition",
            f"verified account {sid}: cleanup paused without timed retry",
        )
        self._set_diag("set_queue", "queue preserved until cleanup is verified")
        self._pause_bulk(
            "creation",
            reason or (
                f"Verified account {sid} still needs deterministic container cleanup."
            ),
            code="container_deletion_incomplete",
        )
        return False

    def _recover_bulk_loop_failure(self, reason: str, *, code: str) -> bool:
        """Apply the same bounded policy to failures escaping a batch step."""
        state = dict(getattr(self, "bulk_state", {}))
        phase = (
            "creation"
            if state.get("stage") in {"creating", "retry_creation", "ip_verified"}
            else "ip"
        )
        # A failed durable-state write is never a safe trigger for destructive
        # recovery.  In particular, do not enter ``_reset_failed_creation``:
        # that path deletes the current container before its queue transition
        # is persisted.  Best-effort the normal paused state/UI below; if the
        # same storage error prevents that write too, keep the in-memory state
        # and queue exactly where they were and stop this worker.
        if code == "bulk_save_failed":
            self.log(
                "BATCH durable-state save failed; automatic reset and all device "
                "actions were refused so the current queue item is preserved."
            )
            try:
                self._pause_bulk(phase, reason, code=code)
            except Exception as pause_exc:
                self.log(
                    f"BATCH pause state could not be persisted either: "
                    f"{type(pause_exc).__name__}: {pause_exc}"
                )
            return False
        try:
            if phase == "creation":
                sid = str(state.get("slot_id") or "")
                queue_rows = list(state.get("queue") or [])
                if not sid or not queue_rows:
                    raise BulkError(
                        "Automatic creation reset has no durable slot or queue head."
                    )
                return self._reset_failed_creation(sid, dict(queue_rows[0]), reason)
            return self._retry_ip_after_failure(reason, code=code)
        except (BulkError, OSError, TypeError, ValueError) as recovery_exc:
            # A durable-state write failure is the one case where destructive
            # recovery must stop: deleting a container before the queue update
            # can be saved would make the crash state ambiguous.
            self.log(
                f"BATCH automatic recovery could not be persisted: "
                f"{type(recovery_exc).__name__}: {recovery_exc}"
            )
            try:
                self._pause_bulk(phase, str(recovery_exc), code=code)
            except Exception as pause_exc:
                self.log(
                    f"BATCH pause state could not be persisted either: "
                    f"{type(pause_exc).__name__}: {pause_exc}"
                )
            return False

    def _skip_failed_bulk_slot(
        self,
        sid: str,
        entry: dict,
        reason: str,
        *,
        failure_code: str = "",
    ) -> bool:
        """Preserve one account-specific failed attempt and advance exactly once.

        The Crane container, iCloud alias, slot row, credentials and every
        successful checkpoint are deliberately kept: nothing is deleted,
        retired, or retried.  Returning True means the queue head was consumed
        and the caller may continue with the next item; any uncertainty pauses
        the batch so the queue is preserved instead.
        """
        if self.stop.is_set():
            # User Stop preserves the queue and never advances to the next item.
            self.log(
                f"BATCH {sid}: Stop requested; the failed attempt and its queue "
                "item were preserved."
            )
            self._set_diag(
                "set_disposition", "user Stop: attempt preserved and not retried"
            )
            self._set_diag(
                "set_queue", "queue preserved; the item was not consumed"
            )
            self._set_diag(
                "set_preservation",
                "account, container, credentials and queue item preserved",
            )
            return False
        slot = get_slot(sid)
        if slot_bulk_skipped(slot):
            # A crash between the durable slot record and the queue write: the
            # row already exists, so keep its original code and finish the same
            # head exactly once.
            recorded = dict(slot.get("bulk_skip") or {})
            if not failure_code:
                failure_code = str(recorded.get("code") or "")
            self.log(
                f"BATCH {sid}: preserved skip record already saved; advancing the "
                "queue head exactly once."
            )
        else:
            try:
                mark_bulk_skipped(
                    sid, failure_code or AUTO_SKIP_FAILURE_CODE, reason, self.log
                )
            except Exception as exc:
                self._pause_bulk(
                    "creation",
                    f"Failed attempt {sid} could not be preserved: {exc}",
                    code="bulk_skip_persist_failed",
                )
                return False
        code = failure_code or AUTO_SKIP_FAILURE_CODE

        slot = get_slot(sid) or {}
        if slot.get("reddit") is True:
            self.log(
                f"BATCH {sid}: the created account, credentials and success "
                "checkpoints were preserved; the later failure is recorded "
                "separately."
            )

        ready = datetime.now(timezone.utc) + timedelta(
            seconds=float(entry["delay_seconds"])
        )
        try:
            self._persist_bulk(
                consume_skipped(
                    dict(self.bulk_state),
                    sid,
                    code=code,
                    reason=reason,
                    ready_at_utc=ready.isoformat().replace("+00:00", "Z"),
                )
            )
        except (BulkError, OSError) as exc:
            self._pause_bulk(
                "creation",
                f"Preserved failure {sid} could not be recorded in the queue: {exc}",
                code="bulk_skip_queue_failed",
            )
            return False
        self.log(
            f"BATCH {sid}: attempt preserved and skipped; continuing with a fresh "
            "slot, container and email after the normal delay."
        )
        self._set_diag(
            "set_disposition",
            "attempt preserved and skipped; the batch continues with the next item",
        )
        self._set_diag(
            "set_queue",
            "queue head consumed exactly once after preserving the attempt",
        )
        self._set_diag(
            "set_preservation",
            "account, credentials, container and prior checkpoints kept and recorded",
        )
        return True

    def _discard_stalled_container(self, sid: str) -> str:
        """Delete one stalled container through Crane's Reddit Settings.

        The exact UUID is durably checkpointed before the first destructive
        tap.  A resumed cleanup reuses only that UUID.  Legacy/incomplete rows
        without a UUID may bind one only when Crane's preferences prove exactly
        one non-Default instance with the expected slot name.  This path never
        invokes Anthropic and never falls back to name-only deletion.
        """
        self._bulk_live_status(f"Stall reset {sid}: deleting the stale container.")
        phone = None
        outcome = "not_attempted"
        try:
            row = get_slot(sid)
            if row is None:
                raise PhoneError(
                    f"STALL {sid}: slot row disappeared before container cleanup."
                )
            if row.get("reddit") is True:
                raise PhoneError(
                    f"STALL {sid}: created Reddit account cannot be discarded."
                )

            checkpoint = bulk_reset_cleanup(row)
            if "bulk_reset_cleanup" in row and checkpoint is None:
                raise PhoneError(
                    f"STALL {sid}: malformed bulk-reset cleanup checkpoint; "
                    "refused to infer or delete a container."
                )
            checkpoint_state = bulk_reset_cleanup_state(row)
            if checkpoint_state == "complete":
                self.log(
                    f"STALL {sid}: container cleanup already verified complete "
                    "(skipped)."
                )
                return "complete"

            phone, stack = self._connect(self.bulk_password)
            try:
                stack.cancel_event = self.stop
            except AttributeError:
                pass
            if self.stop.is_set():
                return "stopped"

            instance_id = bulk_reset_cleanup_instance(row)
            if checkpoint_state == "pending":
                self.log(
                    f"STALL {sid}: resuming pending cleanup for exact Crane UUID "
                    f"{instance_id[:8]}; API calls=0."
                )
            else:
                instance_id = str(
                    row.get("ghost_container_instance") or ""
                ).strip().upper()
                if not instance_id:
                    self.log(
                        f"STALL {sid}: no recorded UUID; invalidating Ghost's cache "
                        "and requiring a fresh complete inventory to prove one exact "
                        "matching user container; API calls=0."
                    )
                    invalidate_ghost_container_cache(stack, self.log)
                    _resolved_name, instance_id = resolve_sole_user_container(
                        stack,
                        phone,
                        self.log,
                        stop=self.stop,
                        expected_name=sid,
                    )
                try:
                    mark_bulk_reset_cleanup_pending(
                        sid, instance_id, self.log
                    )
                except ValueError as exc:
                    raise PhoneError(
                        f"STALL {sid}: could not persist exact cleanup intent: {exc}"
                    ) from exc
                self.log(
                    f"STALL {sid}: exact Crane UUID {instance_id[:8]} was "
                    "checkpointed before deletion; API calls=0."
                )

            # Stop after the durable intent intentionally leaves it pending so
            # Retry / Resume can target the same UUID without any new inference.
            if self.stop.is_set():
                return "stopped"
            outcome = str(
                delete_named_instance(
                    stack,
                    phone,
                    self.log,
                    sid,
                    instance_id,
                    stop=self.stop,
                )
                or "deleted"
            )
            if outcome not in {"deleted", "absent"}:
                raise PhoneError(
                    f"Stalled-container deletion returned unverified outcome {outcome!r}."
                )
            try:
                mark_bulk_reset_cleanup_complete(sid, self.log)
            except ValueError as exc:
                raise PhoneError(
                    f"STALL {sid}: deletion was verified but its cleanup "
                    f"checkpoint could not be completed: {exc}"
                ) from exc
            self.log(f"STALL {sid}: verified container outcome={outcome}.")
            return outcome
        except (RecoveryStopped, OperationCancelled):
            self.log(
                f"STALL {sid}: Stop ended deterministic container cleanup; "
                "queue and slot were preserved."
            )
            return "stopped"
        except Exception as exc:
            outcome = f"failed ({type(exc).__name__})"
            self.log(
                f"STALL {sid}: stale container deletion could not be locally "
                f"verified: {exc}."
            )
        finally:
            if phone is not None:
                try:
                    phone.close()
                except Exception as exc:
                    self.log(f"STALL {sid}: PC connection close warning: {exc}")
            self.phone = None
        return outcome

    def _stall_reset(
        self, sid: str, entry: dict, reason: str, *, immediate: bool = False
    ) -> bool:
        """Perform the verified stalled-attempt reset and continue the batch.

        Ordering: delete the stale container, retire the slot, then drop the
        queue head *without* archiving its credentials.  The normal loop that
        follows rotates the IP and creates plus selects a brand-new container
        for the next identity.  Returning True means the caller may continue.
        """
        if self.stop.is_set():
            return False
        if immediate:
            self.log(
                f"STALL {sid}: typed recovery failure requires immediate verified "
                "reset; skipped the 180-second grace window."
            )
        else:
            self.log(
                f"STALL {sid}: the automatic recovery window expired; starting "
                "the full reset."
            )
        self._set_diag(
            "set_disposition", f"stall reset for {sid}; the attempt is discarded"
        )
        slot = get_slot(sid)
        if slot_bulk_skipped(slot):
            # Crash between the durable abandoned-slot record and the queue
            # write: the container was already deleted before that record.
            # Finish the same reset exactly once and never mark it twice.
            recorded = dict(slot.get("bulk_skip") or {})
            if not reason:
                reason = str(recorded.get("reason") or "")
            self.log(
                f"STALL {sid}: abandoned-slot record already saved; finishing "
                "the same queue head exactly once."
            )
        else:
            deletion_outcome = self._discard_stalled_container(sid)
            if str(deletion_outcome).startswith("failed ("):
                self._pause_bulk(
                    "creation",
                    f"Stalled container {sid} could not be safely deleted: "
                    f"{deletion_outcome}",
                    code="stall_reset_container_delete_failed",
                )
                return False
            if deletion_outcome == "stopped":
                return False
            if self.stop.is_set():
                # User Stop always preserves the queue head and the slot.
                self.log(
                    f"STALL {sid}: Stop requested during the reset; the queue "
                    "head and slot were preserved."
                )
                return False
            try:
                mark_bulk_skipped(
                    sid,
                    STALL_RESET_FAILURE_CODE,
                    reason or "No verified creation progress for three minutes.",
                    self.log,
                )
            except Exception as exc:
                self._pause_bulk(
                    "creation",
                    f"Stalled attempt {sid} could not be retired: {exc}",
                    code="stall_reset_persist_failed",
                )
                return False

        # Stop may arrive while the durable retired-slot record is being
        # written, or while a crash-resume run is reconciling an existing one.
        # Retirement is safe to keep, but consuming the queue head is a
        # separate state transition and must wait for an explicit Resume.
        if self.stop.is_set():
            self.log(
                f"STALL {sid}: Stop requested after the slot was retired; "
                "the retirement checkpoint is preserved and the queue head "
                "was not consumed."
            )
            self._set_diag(
                "set_disposition",
                "user Stop after slot retirement; reset waits for manual resume",
            )
            self._set_diag(
                "set_queue", "queue preserved; stalled head was not consumed"
            )
            return False

        ready = datetime.now(timezone.utc) + timedelta(
            seconds=float(entry["delay_seconds"])
        )
        try:
            self._persist_bulk(
                consume_stalled(
                    dict(self.bulk_state),
                    sid,
                    ready_at_utc=ready.isoformat().replace("+00:00", "Z"),
                )
            )
        except (BulkError, OSError) as exc:
            self._pause_bulk(
                "creation",
                f"Stalled attempt {sid} could not be dropped from the queue: {exc}",
                code="stall_reset_queue_failed",
            )
            return False
        if self.bulk_state.get("stage") == "idle":
            self.log(
                "BATCH complete: the queue was drained by stall resets; waiting "
                "for a new queue before creating again."
            )
            status = getattr(self, "status", None)
            if status is not None:
                status.set("Batch complete — waiting for a new queue")
        else:
            self.log(
                f"STALL {sid}: attempt discarded; its queue item is gone and its "
                "identity is never reused. The next item starts with a fresh "
                "container, a rotated IP and a new identity."
            )
        self._set_diag(
            "set_queue", "stalled queue head dropped exactly once"
        )
        self._set_diag(
            "set_preservation",
            "stalled attempt discarded: its identity and queue item are never reused",
        )
        return True

    def _finish_bulk_after_skip(self) -> bool:
        """Report completion when a preserved skip emptied the queue."""
        if self.bulk_state.get("stage") != "idle":
            return False
        failures = len(self.bulk_state.get("failures") or [])
        self.log(
            f"BATCH complete with failures: {failures} item(s) preserved and skipped."
        )
        self._set_diag(
            "set_disposition",
            "batch completed with warnings; failed items were preserved and skipped",
        )
        self._set_diag("set_queue", "queue drained; every item consumed or preserved")
        status = getattr(self, "status", None)
        if status is not None:
            status.set("Batch complete with failures")
        return True

    def _bulk_proxy_scope(self) -> str:
        return configured_scope(self.falcon_proxy_url, SECRETS_PATH)

    def _bulk_slot_and_entry(self, state: dict) -> tuple[str, dict, date]:
        if not state.get("queue") or int(state.get("remaining") or 0) < 1:
            raise BulkError("The batch queue is empty.")
        entry = dict(state["queue"][0])
        birth = parse_slot_birth_date(entry["birth_date"])
        sid = str(state.get("slot_id") or "")
        if not sid:
            sid, slot, _is_new = _resolve_creation_pipeline_target(
                load_slots(), retired_indices()
            )
            saved_birth = str((slot or {}).get("reddit_birth_date") or "").strip()
            if saved_birth and saved_birth != entry["birth_date"]:
                raise BulkError("The next unfinished slot birth date does not match the queue head.")
            changed = dict(state, slot_id=sid)
            self._persist_bulk(changed)
            state = self.bulk_state
        ensure_ghost_container_needed(sid, self.log)
        return sid, entry, birth

    def _bulk_progress(self, event: dict) -> None:
        status = str(event.get("status") or "Checking proxy IP…")
        self.log("BATCH IP: " + status)
        try:
            self.root.after(0, lambda value=status: self.ip_cycle_status_var.set(value))
        except (tk.TclError, AttributeError):
            pass

    def _run_bulk_ip_check(self, state: dict) -> tuple[dict, list[dict]]:
        observations: list[dict] = []
        scope = self._bulk_proxy_scope()

        def checkpoint(attempt: dict) -> None:
            current = dict(self.bulk_state)
            if current.get("stage") == "stopped":
                raise BulkError("Batch stopped before IP checkpoint.")
            baseline = attempt.get("baseline")
            current.update(
                ip_attempt=dict(attempt),
                baseline_ip=(baseline or {}).get("ip") or current.get("baseline_ip"),
            )
            self._persist_bulk(current)

        stage = state.get("stage")
        if stage == "ip_pending":
            operation_id = str(uuid.uuid4())

            def before_trigger(payload: dict) -> None:
                current = dict(self.bulk_state)
                current.update(
                    stage="may_have_triggered",
                    baseline_ip=payload.get("baseline_ip"),
                    trigger_deadline_utc=payload.get("deadline_utc"),
                    ip_attempt=payload.get("attempt"),
                    failure_phase=None,
                )
                self._persist_bulk(current)

            result = run_macrodroid_change(
                self.macrodroid_webhook,
                self.falcon_proxy_url,
                public_ip_url=DEFAULT_PUBLIC_IP_URL,
                cancel_event=self.stop,
                progress=self._bulk_progress,
                on_observation=lambda item: observations.append(dict(item)),
                before_trigger=before_trigger,
                on_checkpoint=checkpoint,
                proxy_scope=scope,
                require_baseline=True,
                # A transient proxy-auth outage has taken roughly one minute
                # in production.  Keep observing for a bounded extended window
                # and never submit a reset until a comparison IP is durable.
                baseline_cycles=12,
                operation_id=operation_id,
            )
            if result.get("outcome") == "verified":
                result["trigger_attempted"] = True
            return result, observations

        baseline = str(state.get("baseline_ip") or "")
        if not baseline:
            return {
                "outcome": "verification_failed",
                "error": "The submitted reset has no saved comparison IP.",
            }, observations
        attempt = state.get("ip_attempt")
        if attempt and attempt.get("baseline"):
            observations.append(dict(attempt["baseline"]))
        result = observe_proxy_without_trigger(
            self.falcon_proxy_url,
            baseline,
            require_change=True,
            cancel_event=self.stop,
            attempt=attempt,
            proxy_scope=scope,
            public_ip_url=DEFAULT_PUBLIC_IP_URL,
            progress=self._bulk_progress,
            on_observation=lambda item: observations.append(dict(item)),
            on_checkpoint=checkpoint,
        )
        if result.get("outcome") == "verified":
            result["observation_only"] = True
        return result, observations

    def _record_bulk_ip(self, result: dict, observations: list[dict]) -> str:
        validate_pair(result)
        proof = result["verification_evidence"]
        evidence = [proof["baseline"], *proof["confirmations"]]
        for item in evidence:
            if item not in observations:
                observations.append(dict(item))
        confirmed_at = str(proof["confirmations"][-1]["observed_at_utc"])
        operation_id = str(result.get("operation_id") or proof["baseline"]["operation_id"])
        record_configured_operation(
            IP_HISTORY_PATH,
            proxy_url=self.falcon_proxy_url,
            secrets_path=SECRETS_PATH,
            operation_id=operation_id,
            observations=observations,
            result=result,
        )
        old_ip = str(result["old_ip"])
        new_ip = str(result["new_ip"])
        try:
            self.root.after(0, lambda: (
                self.ip_previous_var.set(old_ip),
                self.ip_verified_new_var.set(new_ip),
                self.ip_verified_at_var.set(self._display_utc(confirmed_at)),
                self.ip_cycle_status_var.set("Batch IP change verified"),
            ))
        except (tk.TclError, AttributeError):
            pass
        return confirmed_at

    @staticmethod
    def _seconds_until(value: str | None) -> float:
        if not value:
            return 0.0
        try:
            target = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return 0.0

    def _run_bulk_batch(self) -> None:
        """Run one queue head at a time: verified IP, creation, delay."""
        try:
            while self._run_bulk_batch_once():
                pass
        finally:
            try:
                self.root.after(0, self._after_job)
            except (tk.TclError, AttributeError):
                pass

    def _run_bulk_batch_once(self) -> bool:
        """Run until completion or one recovered outer-loop exception.

        True asks the non-recursive wrapper to re-enter from durable state.
        Normal in-loop recoveries continue locally and ordinary completion
        returns a false-y value through the existing bare ``return`` paths.
        """
        restart_after_failure = False
        try:
            while not self._refresh_macrodroid_setup():
                if not self._retry_ip_after_failure(
                    self.macrodroid_config_error,
                    code="macrodroid_setup_failed",
                ):
                    return
            while not self.stop.is_set() and int(self.bulk_state.get("remaining") or 0) > 0:
                state = dict(self.bulk_state)
                if state.get("stage") in {"paused", "stopped", "idle"}:
                    return
                if state.get("stage") == "between":
                    if self.stop.wait(self._seconds_until(state.get("ready_at_utc"))):
                        return
                    state.update(stage="ip_pending", ready_at_utc=None)
                    self._persist_bulk(state)
                    state = dict(self.bulk_state)

                sid, entry, birth = self._bulk_slot_and_entry(state)
                state = dict(self.bulk_state)
                normalized = self._normalize_bulk_queue_usernames(state, sid)
                if normalized != state:
                    state = normalized
                    sid, entry, birth = self._bulk_slot_and_entry(state)
                    state = dict(self.bulk_state)
                slot = get_slot(sid)
                if slot_bulk_skipped(slot):
                    recorded = dict(slot.get("bulk_skip") or {})
                    if str(recorded.get("code") or "") == STALL_RESET_FAILURE_CODE:
                        # A crash can land between the durable abandoned-slot
                        # record and the queue write.  Finish that same reset
                        # exactly once instead of pausing on a dead slot.
                        if self._stall_reset(
                            sid, entry, str(recorded.get("reason") or "")
                        ):
                            continue
                        return
                    # Historical versions could persist an automatic skip before
                    # consuming the queue head.  Strict sequential batches never
                    # advance such a row automatically: keep the same slot and
                    # queue item for explicit recovery/review.
                    self._pause_bulk(
                        "creation",
                        str(
                            recorded.get("reason")
                            or f"{sid} has a historical preserved-skip record."
                        ),
                        code=str(recorded.get("code") or "legacy_bulk_skip"),
                    )
                    return
                self._assert_bulk_requested_username(sid, entry, slot)
                if state.get("stage") in {"creating", "retry_creation"} and _pipeline_slot_complete(slot):
                    if _pipeline_deletion_pending(slot, bool(state.get("delete_containers"))):
                        # The account is fully verified; only its required
                        # local-container deletion remains.  Resume exactly
                        # that cleanup without replaying signup or the Reddit
                        # audit, then advance once it is durably recorded.
                        if not self._run_deletion_only(self.bulk_password, sid):
                            reason = str(
                                getattr(self, "_last_pipeline_failure_reason", "")
                                or f"Verified account {sid} still requires container deletion."
                            )
                            if self._retry_completed_cleanup_after_failure(sid, reason):
                                continue
                            return
                        slot = get_slot(sid)
                    self._assert_bulk_requested_username(sid, entry, slot, require_saved=True)
                    ready = datetime.now(timezone.utc) + timedelta(seconds=float(entry["delay_seconds"]))
                    self._persist_bulk(consume_completed(
                        state, sid, ready_at_utc=ready.isoformat().replace("+00:00", "Z")
                    ))
                    if self.bulk_state.get("stage") == "idle":
                        failures = len(self.bulk_state.get("failures") or [])
                        if failures:
                            self.log(
                                f"BATCH complete with failures: {failures} slot(s) failed "
                                "after exhausting replacement aliases."
                            )
                            status = getattr(self, "status", None)
                            if status is not None:
                                status.set("Batch complete with failures")
                        else:
                            self.log("BATCH complete: every requested item finished.")
                            status = getattr(self, "status", None)
                            if status is not None:
                                status.set("Batch complete")
                        return
                    continue

                freshly_verified = False
                reusing_batch_ip = False
                # Legacy/non-deleting batches can retain the IP verified at
                # fresh-batch start.  Delete-enabled creation batches clear IP
                # evidence when each head is consumed, so every successor
                # reaches the reset path below exactly once.
                if (
                    state.get("stage") == "ip_pending"
                    and state.get("verified_ip")
                    and state.get("verified_at_utc")
                ):
                    state.update(
                        stage="ip_verified",
                        trigger_deadline_utc=None,
                        ip_attempt=None,
                    )
                    self._persist_bulk(state)
                    state = dict(self.bulk_state)
                    reusing_batch_ip = True
                    self.log(
                        "BATCH IP: reusing the IP verified at fresh-batch start; "
                        "no reset trigger was submitted for this item."
                    )
                if state.get("stage") in {"ip_pending", "may_have_triggered", "retry_ip"}:
                    result, observations = self._run_bulk_ip_check(state)
                    if self.stop.is_set():
                        return
                    if result.get("outcome") != "verified":
                        if self._retry_ip_after_failure(
                            str(result.get("error") or "IP verification failed."),
                            code="ip_verification_failed",
                        ):
                            continue
                        return
                    try:
                        confirmed_at = self._record_bulk_ip(result, observations)
                        set_slot_falcon_proxy_ip(sid, str(result["new_ip"]), confirmed_at)
                    except (HistoryError, ValueError, OSError, TypeError) as exc:
                        if self._retry_ip_after_failure(
                            f"Verified IP could not be persisted: {exc}",
                            code="ip_persist_failed",
                        ):
                            continue
                        return
                    state = dict(self.bulk_state)
                    state.update(
                        stage="ip_verified",
                        baseline_ip=str(result["old_ip"]),
                        verified_ip=str(result["new_ip"]),
                        verified_at_utc=confirmed_at,
                        failure_phase=None,
                    )
                    self._persist_bulk(state)
                    freshly_verified = True
                    state = dict(self.bulk_state)

                if state.get("stage") in {"ip_verified", "creating", "retry_creation"} and not freshly_verified:
                    expected = str(state.get("verified_ip") or "")
                    recheck = observe_proxy_without_trigger(
                        self.falcon_proxy_url,
                        expected,
                        require_change=False,
                        cancel_event=self.stop,
                        proxy_scope=self._bulk_proxy_scope(),
                        progress=self._bulk_progress,
                    )
                    if recheck.get("outcome") != "verified":
                        if self._retry_ip_after_failure(
                            "Verified proxy IP could not be reconfirmed.",
                            code="proxy_recheck_failed",
                            already_elapsed=True,
                        ):
                            continue
                        return
                    # Every slot still records the batch IP it actually used,
                    # even though later items do not rotate it again.
                    if reusing_batch_ip:
                        try:
                            confirmed_at = str(state.get("verified_at_utc") or "")
                            set_slot_falcon_proxy_ip(sid, expected, confirmed_at)
                        except (ValueError, OSError, TypeError) as exc:
                            if self._retry_ip_after_failure(
                                f"Reused batch IP could not be persisted: {exc}",
                                code="ip_persist_failed",
                                already_elapsed=True,
                            ):
                                continue
                            return

                state = dict(self.bulk_state)
                resume_failure_code = str(state.get("failure_code") or "")
                state.update(
                    stage="creating",
                    failure_phase=None,
                    failure_reason=None,
                )
                self._persist_bulk(state)
                success = self._run_pipeline_with_bridge_recovery(
                    self.bulk_password,
                    sid,
                    birth,
                    int(entry["swipes"]),
                    requested_username=str(entry.get("username") or ""),
                    requested_password=str(entry.get("password") or ""),
                    resume_failure_code=resume_failure_code,
                    manage_worker=False,
                    delete_container_required=bool(state.get("delete_containers")),
                )
                if self.stop.is_set():
                    return
                # The persisted completion checkpoints are the durable
                # authority. A successful-looking return is never enough to
                # consume the queue head: the slot must contain every verified
                # account checkpoint. Conversely, a late teardown error cannot
                # downgrade a slot whose checkpoints are already complete.
                if not _pipeline_slot_complete(get_slot(sid)):
                    reason = str(
                        getattr(self, "_last_pipeline_failure_reason", "") or
                        "Creator pipeline did not reach all completion checkpoints."
                    )
                    failure_code = str(
                        getattr(self, "_last_pipeline_failure_code", "") or ""
                    )
                    # Shared credential/modal failures and an unconfirmed
                    # exact-container selection must never be converted into
                    # a destructive stall reset when AI recovery was disabled
                    # or could not safely start.  With AI enabled these visual
                    # failures normally stay inside the supervisor and do not
                    # reach this branch at all.
                    pause_only_codes = {
                        ICloudPasswordEntryError.failure_code,
                        ICloudModalBlockingError.failure_code,
                        ContainerProvisioningReconcileError.failure_code,
                        ContainerSelectionError.failure_code,
                        AI_RECOVERY_BUDGET_EXHAUSTED_CODE,
                        AI_DISCARD_REFUSED_CODE,
                        "ai_discard_restart",
                        "bridge_retries_exhausted",
                        "icloud_password_rejected",
                        "icloud_sheet_dismiss_failed",
                    }
                    if (
                        bool(getattr(self, "_last_pipeline_failure_shared", False))
                        or failure_code in pause_only_codes
                    ):
                        self._pause_bulk(
                            "creation",
                            reason,
                            code=failure_code or "shared_phone_ui_failure",
                        )
                        return
                    # Every incomplete creation failure now has the same bounded
                    # policy. Explicit failures get a three-minute grace window;
                    # the inactivity watchdog has already spent that window.
                    # After it expires, delete/retire only this incomplete
                    # attempt and continue through the normal fresh-IP path.
                    if self._reset_failed_creation(
                        sid,
                        entry,
                        reason,
                        already_elapsed=bool(
                            getattr(self, "_last_pipeline_attempt_timed_out", False)
                        ) or failure_code == CONTAINER_RECREATE_FAILURE_CODE,
                    ):
                        continue
                    return
                verified_slot = get_slot(sid)
                if (
                    _pipeline_slot_complete(verified_slot)
                    and _pipeline_deletion_pending(
                        verified_slot, bool(dict(self.bulk_state).get("delete_containers"))
                    )
                ):
                    # A verified account whose required local-container deletion
                    # is still pending is never discarded or advanced. Retry
                    # only that cleanup after the same bounded grace window;
                    # no successful-looking return may bypass required deletion.
                    reason = str(
                        getattr(self, "_last_pipeline_failure_reason", "") or
                        f"Verified account {sid} still requires its local container deletion."
                    )
                    if self._retry_completed_cleanup_after_failure(sid, reason):
                        continue
                    return
                latest_state = dict(self.bulk_state)
                latest_queue = list(latest_state.get("queue") or [])
                if (
                    str(latest_state.get("slot_id") or "") != sid
                    or not latest_queue
                ):
                    self._pause_bulk(
                        "creation",
                        f"Completed slot {sid} no longer matches the durable queue head.",
                        code="username_variant_queue_mismatch",
                    )
                    return
                latest_entry = dict(latest_queue[0])
                self._assert_bulk_requested_username(
                    sid, latest_entry, get_slot(sid), require_saved=True
                )

                ready = datetime.now(timezone.utc) + timedelta(
                    seconds=float(latest_entry["delay_seconds"])
                )
                self._persist_bulk(consume_completed(
                    latest_state, sid,
                    ready_at_utc=ready.isoformat().replace("+00:00", "Z"),
                ))
                if (
                    self.bulk_state.get("stage") == "between"
                    and bool(self.bulk_state.get("delete_containers"))
                ):
                    self.log(
                        f"BATCH {sid}: container deletion verified; next queue "
                        "item will start with a fresh IP reset."
                    )
                self._set_diag(
                    "set_disposition",
                    f"slot {sid} completed and verified; the batch advances",
                )
                self._set_diag("set_queue", "queue head consumed as completed")
                if self.bulk_state.get("stage") == "idle":
                    failures = len(self.bulk_state.get("failures") or [])
                    if failures:
                        self.log(
                            f"BATCH complete with failures: {failures} slot(s) failed "
                            "after exhausting replacement aliases."
                        )
                    else:
                        self.log("BATCH complete: every requested item finished.")
                    status = getattr(self, "status", None)
                    if status is not None:
                        status.set(
                            "Batch complete with failures" if failures else "Batch complete"
                        )
                    return
        except BulkSaveError as exc:
            if not self.stop.is_set():
                restart_after_failure = self._recover_bulk_loop_failure(
                    str(exc), code="bulk_save_failed"
                )
        except (BulkError, HistoryError, IpChangeError, PhoneError, OSError, TypeError, ValueError) as exc:
            if not self.stop.is_set():
                restart_after_failure = self._recover_bulk_loop_failure(
                    str(exc), code="local_failure"
                )
        except Exception as exc:
            if not self.stop.is_set():
                restart_after_failure = self._recover_bulk_loop_failure(
                    f"Unexpected local failure ({type(exc).__name__}).",
                    code="unexpected_local_failure",
                )
        return bool(restart_after_failure and not self.stop.is_set())

    def _probe_usb_idle(self) -> None:
        def work() -> None:
            try:
                report = diagnose_usb()
                self.status.set(usb_status_line(report))
                self.log(usb_status_line(report))
                if report.pnp_names:
                    self.log("PnP: " + "; ".join(report.pnp_names))
                if not report.usbmux_devices:
                    self.log("usbmux has no device yet.")
            except Exception as e:
                self.log(f"USB idle probe: {type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _busy(self, on: bool) -> None:
        self.stop_btn.configure(state="normal" if on else "disabled")
        if hasattr(self, "qa_btn"):
            self.qa_btn.configure(state="disabled" if on else "normal")
        if hasattr(self, "qa_options_btn"):
            self.qa_options_btn.configure(state="disabled" if on else "normal")
        if hasattr(self, "birth_date_btn"):
            self.birth_date_btn.configure(state="disabled" if on else "normal")
        if hasattr(self, "post_stop_btn"):
            try:
                self.post_stop_btn.configure(state="normal" if on else "disabled")
            except (tk.TclError, AttributeError):
                pass
        self._sync_row_buttons()

    def _start_worker(self, target, *args) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.stop.clear()
        self._busy(True)
        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()

    def on_new_ghost(self) -> None:
        try:
            n = int(self.count.get())
        except (tk.TclError, ValueError, TypeError):
            n = 1
        n = max(1, min(n, 10))
        # A selected slot anchors the next container(s).  This makes a selected
        # a0008 + count 1 mean exactly a0009, even when a prior PC-only delete
        # left a retired allocation reservation behind.
        self._start_worker(
            self._run_ghost, self.pw.get(), n, self._selected_id()
        )

    def on_start_creation_reddit(self) -> None:
        """Prompt once, then run the resumable Ghost -> HME -> Reddit flow."""
        if getattr(self, "worker", None) and self.worker.is_alive():
            return
        try:
            rows = load_slots()
            sid, slot, is_new = _resolve_creation_pipeline_target(
                rows, retired_indices()
            )
            cleanup_only = slot_reddit_cleanup_pending(slot)
            persisted_swipes: int | None = None
            persisted_completed = 0
            if slot and (
                "creation_feed_swipes_target" in slot
                or "creation_feed_swipes_completed" in slot
            ):
                persisted_swipes, persisted_completed = creation_feed_swipe_progress(slot)
                if persisted_swipes < 1:
                    raise PhoneError(
                        f"PIPELINE {sid} has no valid persisted swipe target."
                    )

            if cleanup_only:
                saved_birth = str((slot or {}).get("reddit_birth_date") or "").strip()
                birth_date_override = (
                    parse_slot_birth_date(saved_birth) if saved_birth else None
                )
                if persisted_swipes is None:
                    raise PhoneError(
                        f"PIPELINE {sid} cleanup has no persisted swipe target."
                    )
                feed_swipes = persisted_swipes
                feed_pause_count = 0
                if persisted_completed < feed_swipes:
                    options = self._prompt_creation_options(
                        dict(slot or {"id": sid}),
                        persist_birth_date=False,
                        fixed_feed_swipes=feed_swipes,
                    )
                    if options is None:
                        return
                    birth_date_override, feed_swipes, feed_pause_count = (
                        _unpack_creation_options(options)
                    )
                self.log(
                    f"PIPELINE {sid}: resuming with {feed_swipes} persisted swipes "
                    f"and {feed_pause_count} random pauses this session."
                )
            else:
                prompt_slot = dict(slot or {"id": sid})
                options = self._prompt_creation_options(
                    prompt_slot,
                    persist_birth_date=not is_new,
                    fixed_feed_swipes=persisted_swipes,
                )
                if options is None:
                    return
                birth_date_override, feed_swipes, feed_pause_count = (
                    _unpack_creation_options(options)
                )
                self.log(
                    f"PIPELINE {sid}: confirmed with {feed_swipes} feed swipes "
                    f"and {feed_pause_count} random pauses."
                )
        except (PhoneError, OSError, TypeError, ValueError) as exc:
            self.log(f"FAILED PIPELINE: {exc}")
            self.status.set("Failed — see Log")
            return

        self.refresh_slot_id = sid
        worker_args = [
            self._run_creation_pipeline,
            self.pw.get(),
            sid,
            birth_date_override,
            feed_swipes,
        ]
        if feed_pause_count:
            worker_args.append(feed_pause_count)
        self._start_worker(*worker_args)

    def on_create_email(self) -> None:
        sid = self._selected_id()
        if not sid:
            return
        slot = get_slot(sid)
        if not slot:
            self.log(f"Slot {sid} no longer exists.")
            return
        if slot.get("email"):
            self.log(f"{sid} already has {slot['email']}.")
            return
        self.refresh_slot_id = sid
        self._start_worker(self._run_email, self.pw.get(), sid)
    def on_copy_email(self) -> None:
        sid = self._selected_id()
        slot = get_slot(sid) if sid else None
        addr = str((slot or {}).get("email") or "")
        if not addr:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(addr)
        self.log(f"Copied {addr}")

    def on_delete_slot(self) -> None:
        """PC-only slot delete. Phone containers untouched (Crane by hand)."""
        sid = self._selected_id()
        if not sid:
            return
        try:
            ok = messagebox.askyesno(
                "Delete slot",
                "Delete %s from slots.json (+ map entry)?\n"
                "PC only — phone containers untouched. used/%s/ is kept."
                % (sid, sid),
            )
        except (tk.TclError, AttributeError):
            return
        if not ok:
            return
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            slots_path = os.path.join(LOG_DIR, "slots.json")
            shutil.copyfile(
                slots_path, os.path.join(LOG_DIR, "slots.backup-%s.json" % stamp)
            )
            rows = load_slots()
            # Reserve the id before removing its PC row.  Phone containers and
            # used/<slot>/ stay untouched, but automatic allocation can never
            # silently reuse their retained name.
            retire_slot_id(sid)
            save_slots([r for r in rows if str(r.get("id") or "") != sid])
            map_path = os.path.join(LOG_DIR, "slot_community_map.json")
            try:
                with open(map_path, "r", encoding="utf-8") as f:
                    mapping = json.load(f)
                if isinstance(mapping, dict) and sid in mapping:
                    del mapping[sid]
                    with open(map_path, "w", encoding="utf-8") as f:
                        json.dump(mapping, f, indent=2)
            except (OSError, ValueError):
                pass
            state_path = os.path.join(LOG_DIR, "creator_state.json")
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if isinstance(state, dict) and state.get("container") == sid:
                    state["container"] = ""
                    with open(state_path, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2)
            except (OSError, ValueError):
                pass
        except Exception as e:
            self.log(f"ERROR deleting {sid}: {e}")
            return
        self._refresh_slots()
        self.log(f"Deleted {sid} (PC only, backup taken, used/ kept)")
        try:
            self.status.set(f"Deleted {sid}")
        except (tk.TclError, AttributeError):
            pass

    def on_open_profile(self) -> None:
        sid = self._selected_id()
        slot = get_slot(sid) if sid else None
        try:
            user = str((slot or {}).get("username") or "").strip()
        except Exception:
            user = ""
        if not user:
            return
        url = "https://www.reddit.com/user/" + user + "/"
        try:
            webbrowser.open(url)
        except Exception as e:
            self.log(f"ERROR could not open browser: {e}")
            return
        self.log(f"Opened {url}")

    def on_create_reddit(self) -> None:
        if getattr(self, "worker", None) and self.worker.is_alive():
            return
        sid = self._selected_id()
        slot = get_slot(sid) if sid else None
        if slot_container_deleted(slot):
            # The local container was permanently removed after a verified
            # creation; never rebuild it or reopen the account by hand.
            self.log(
                f"{sid}: its local container was already deleted and is no "
                "longer available for creation."
            )
            return
        cleanup_pending = slot_reddit_cleanup_pending(slot)
        ghost_creation_ready = bool(slot) and (
            bool(slot.get("ghost"))
            or ghost_container_state(slot) in ("needed", "provisioning")
        )
        if not cleanup_pending and (
            not slot or not ghost_creation_ready or not slot.get("email")
        ):
            self.log("Create Reddit needs Ghost + email on this slot.")
            return
        if not cleanup_pending and slot_reddit_ready(slot):
            self.log(f"{sid} already has a created Reddit account and completed cleanup.")
            return
        if not cleanup_pending and slot_reddit_done(slot):
            self.log(
                f"{sid} has an unsupported cleanup state "
                f"({reddit_cleanup_state(slot)}); creation was not restarted."
            )
            return
        self._continue_create_reddit(self.pw.get(), sid)

    def _continue_create_reddit(self, password: str, sid: str) -> None:
        slot = get_slot(sid)
        if slot_reddit_cleanup_pending(slot):
            target, completed = creation_feed_swipe_progress(slot)
            feed_pause_count = 0
            birth_date_override = None
            feed_swipes: int | None = None
            if completed < target:
                options = self._prompt_creation_options(
                    dict(slot or {"id": sid}),
                    persist_birth_date=False,
                    fixed_feed_swipes=target,
                )
                if options is None:
                    return
                birth_date_override, feed_swipes, feed_pause_count = (
                    _unpack_creation_options(options)
                )
                self.log(
                    f"{sid}: resuming at swipe {completed + 1}/{target} with "
                    f"{feed_pause_count} random pauses this session."
                )
            else:
                self.log(f"{sid}: resuming verified Reddit cleanup only.")
            self.refresh_slot_id = sid
            worker_args = [
                self._run_create_reddit,
                password,
                sid,
                birth_date_override,
                feed_swipes,
            ]
            if feed_pause_count:
                worker_args.append(feed_pause_count)
            self._start_worker(*worker_args)
            return
        if not slot:
            self.log(f"{sid}: slot disappeared before Reddit creation started.")
            return
        options = self._prompt_creation_options(slot)
        if options is None:
            return
        birth_date_override, feed_swipes, feed_pause_count = (
            _unpack_creation_options(options)
        )
        self.log(
            f"{sid}: creation will run {feed_swipes} feed swipes with "
            f"{feed_pause_count} random pauses."
        )
        self.refresh_slot_id = sid
        worker_args = [
            self._run_create_reddit,
            password,
            sid,
            birth_date_override,
            feed_swipes,
        ]
        if feed_pause_count:
            worker_args.append(feed_pause_count)
        self._start_worker(*worker_args)

    def _prompt_creation_options(
        self,
        slot: dict,
        *,
        persist_birth_date: bool = True,
        fixed_feed_swipes: int | None = None,
    ) -> tuple[date, int, int] | None:
        """Ask for the birthday, swipe total, and random pauses for this session."""
        sid = str(slot.get("id") or "").strip().lower()
        saved_birth_date = str(slot.get("reddit_birth_date") or "").strip()
        parsed_saved: date | None = None
        if saved_birth_date:
            try:
                parsed_saved = parse_slot_birth_date(saved_birth_date)
            except (PhoneError, TypeError, ValueError):
                saved_birth_date = ""

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Create Reddit — {sid}")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")

        row_index = 0
        ttk.Label(frame, text=f"Account: {sid}").grid(
            row=row_index, column=0, columnspan=2, sticky="w"
        )
        row_index += 1

        birth_value = tk.StringVar(value=saved_birth_date)
        birth_entry = None
        if parsed_saved is None:
            ttk.Label(frame, text="Birthday (DD.MM.YYYY):").grid(
                row=row_index, column=0, columnspan=2, sticky="w", pady=(10, 0)
            )
            row_index += 1
            birth_entry = ttk.Entry(frame, textvariable=birth_value, width=24)
            birth_entry.grid(row=row_index, column=0, columnspan=2, sticky="ew")
            row_index += 1
        else:
            ttk.Label(frame, text=f"Birthday: {saved_birth_date} (saved)").grid(
                row=row_index, column=0, columnspan=2, sticky="w", pady=(10, 0)
            )
            row_index += 1

        swipe_value = tk.StringVar(
            value="" if fixed_feed_swipes is None else str(fixed_feed_swipes)
        )
        swipe_entry = None
        if fixed_feed_swipes is None:
            ttk.Label(frame, text="Feed swipes after creation:").grid(
                row=row_index, column=0, columnspan=2, sticky="w", pady=(10, 0)
            )
            row_index += 1
            swipe_entry = ttk.Entry(frame, textvariable=swipe_value, width=24)
            swipe_entry.grid(row=row_index, column=0, columnspan=2, sticky="ew")
            row_index += 1
        else:
            ttk.Label(
                frame, text=f"Feed swipes: {fixed_feed_swipes} (saved)"
            ).grid(
                row=row_index, column=0, columnspan=2, sticky="w", pady=(10, 0)
            )
            row_index += 1

        pause_value = tk.StringVar(value="")
        ttk.Label(frame, text="Random pauses this session:").grid(
            row=row_index, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        row_index += 1
        pause_entry = ttk.Entry(frame, textvariable=pause_value, width=24)
        pause_entry.grid(row=row_index, column=0, columnspan=2, sticky="ew")
        row_index += 1

        error = tk.StringVar(value="")
        ttk.Label(frame, textvariable=error, wraplength=420, justify="left").grid(
            row=row_index, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        row_index += 1
        result: dict[str, tuple[date, int, int] | None] = {"value": None}

        def cancel() -> None:
            dialog.destroy()

        def save() -> None:
            try:
                parsed_birth = parsed_saved or parse_slot_birth_date(
                    birth_value.get().strip()
                )
                swipe_count = parse_creation_swipe_count(swipe_value.get())
                pause_count = parse_creation_pause_count(
                    pause_value.get(), swipe_count
                )
            except (PhoneError, TypeError, ValueError):
                error.set(
                    "Enter a real adult birthday in DD.MM.YYYY format and a "
                    "positive whole-number swipe count. Pauses must be a whole "
                    "number from 0 through the swipe count."
                )
                (birth_entry or swipe_entry or pause_entry or dialog).focus_set()
                return
            if parsed_saved is None and persist_birth_date:
                canonical = parsed_birth.strftime("%d.%m.%Y")
                try:
                    set_slot_birth_date(sid, canonical)
                except (OSError, TypeError, ValueError):
                    error.set("Could not save this account's birthday; nothing started.")
                    return
            result["value"] = (parsed_birth, swipe_count, pause_count)
            dialog.destroy()

        ttk.Button(frame, text="Cancel", command=cancel).grid(
            row=row_index, column=0, sticky="e", padx=(0, 6), pady=(12, 0)
        )
        ttk.Button(frame, text="Start creation", command=save).grid(
            row=row_index, column=1, sticky="w", pady=(12, 0)
        )
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.bind("<Return>", lambda _event: save())
        (birth_entry or swipe_entry or pause_entry or dialog).focus_set()
        dialog.wait_visibility()
        dialog.grab_set()
        dialog.lift()
        self.root.wait_window(dialog)
        if result["value"] is not None:
            try:
                self.tree.set(
                    sid,
                    "birthday",
                    result["value"][0].strftime("%d.%m.%Y"),
                )
            except (tk.TclError, AttributeError):
                pass
        return result["value"]

    def on_run_combined_qa(self) -> None:
        """Run the isolated combined QA workflow through its single launcher."""
        if self.worker and self.worker.is_alive():
            return
        sid = self._selected_id()
        slot = get_slot(sid) if sid else None
        if not sid or not slot_reddit_ready(slot):
            self.log("Combined QA requires a selected, created, cleanup-complete slot.")
            return
        self.nb.select(self.log_tab)
        swipes = load_qa_swipes()
        self.log(f"=== Run Combined QA for {sid}: {swipes} swipes ===")
        self._start_worker(self._run_combined_qa, sid, swipes)

    def on_qa_options(self) -> None:
        """Edit the feed-swipe count shared by creation and Combined QA."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Feed Swipe Options")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(
            frame,
            text="Feed swipe count for new Reddit creation and Combined QA (14–250):",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        value = tk.IntVar(value=load_qa_swipes())
        spinbox = ttk.Spinbox(
            frame, from_=14, to=250, width=8, textvariable=value
        )
        spinbox.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 4))
        error = tk.StringVar(value="")
        ttk.Label(frame, textvariable=error).grid(
            row=2, column=0, columnspan=2, sticky="w"
        )

        def save() -> None:
            try:
                count = int(value.get())
            except (tk.TclError, ValueError, TypeError):
                count = 0
            if not 14 <= count <= 250:
                error.set("Enter a whole number from 14 through 250.")
                return
            try:
                save_qa_swipes(count)
            except OSError as exc:
                error.set(f"Could not save feed swipe options: {exc}")
                return
            self.log(f"Feed Swipe Options saved: {count} swipes.")
            dialog.destroy()

        ttk.Button(frame, text="Cancel", command=dialog.destroy).grid(
            row=3, column=0, sticky="e", padx=(0, 6), pady=(12, 0)
        )
        ttk.Button(frame, text="Save", command=save).grid(
            row=3, column=1, sticky="w", pady=(12, 0)
        )
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind("<Return>", lambda _event: save())
        spinbox.focus_set()
        dialog.wait_visibility()
        dialog.grab_set()
        dialog.lift()

    def _run_combined_qa(self, sid: str, swipes: int) -> None:
        launcher = os.path.join(PROJECT_DIR, "run.creator.bat")
        try:
            if not os.path.isfile(launcher):
                raise OSError(f"Missing combined QA launcher: {launcher}")
            command = [
                "cmd.exe", "/d", "/c", "call", launcher,
                "QA_MODE", "--slot", sid, "--swipes", str(swipes),
            ]
            self.status.set("Combined QA running — see Log")
            self.qa_process = subprocess.Popen(
                command,
                cwd=PROJECT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert self.qa_process.stdout is not None
            for line in self.qa_process.stdout:
                message = line.rstrip("\r\n")
                if message:
                    self.log(message)
            exit_code = self.qa_process.wait()
            if self.stop.is_set():
                self.log("Stopped. Combined QA process was terminated.")
                self.status.set("Stopped")
            elif exit_code:
                self.log(f"ERROR Combined QA failed with exit code {exit_code}.")
                self.status.set("Combined QA failed — see Log")
            else:
                self.log("Combined QA completed successfully.")
                self.status.set("Combined QA complete")
        except OSError as exc:
            self.log(f"ERROR Combined QA launcher failed: {exc}")
            self.status.set("Combined QA failed — see Log")
        finally:
            self.qa_process = None
            self.root.after(0, self._after_job)

    def on_browse_image(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Image(s) for Reddit (multi-select allowed for My Profile)",
            filetypes=[
                ("Images", "*.jpg *.jpeg *.png *.JPG *.JPEG *.PNG"),
                ("All", "*.*"),
            ],
        )
        if paths:
            self.post_images = list(paths)
            names = [os.path.basename(p) for p in self.post_images]
            shown = ", ".join(names[:3]) + (" …" if len(names) > 3 else "")
            self.post_image.set(f"{len(names)} file(s): {shown}")

    def _gen_done(self, title: str) -> None:
        # Main thread only (called from _drain_log): re-enable + fill.
        try:
            self.gen_btn.configure(state="normal")
        except (tk.TclError, AttributeError):
            pass
        if title:
            try:
                self.post_title.delete(0, "end")
                self.post_title.insert(0, title)
            except (tk.TclError, AttributeError):
                pass

    def on_generate_title(self) -> None:
        self.log("GENERATE CLICKED")
        try:
            self.gen_btn.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass
        community = self.post_community.get().strip()
        images = [p for p in self.post_images if str(p or "").strip()]
        try:
            niche = self.post_niche.get().strip()
        except (tk.TclError, AttributeError):
            niche = ""

        def live_log(line: str) -> None:
            # Route through the shared diagnostic stream so generation output is
            # sanitized, persisted and rendered like every other message.  The
            # stream only queues for the Tk thread — it never touches Tk from
            # the worker (after() there raises or silently drops).
            try:
                self.log(line)
            except Exception:
                pass

        def work() -> None:
            title = ""
            try:
                title = generate_title(
                    community, images[0] if images else "",
                    log=live_log, niche=niche)
            except BaseException as e:
                try:
                    self.log("Generate ERROR  %s: %s"
                             % (type(e).__name__, str(e)[:150]))
                except Exception:
                    pass
            finally:
                try:
                    self.log_q.put(("__gen_done__", title))
                except Exception:
                    pass

        self._gen_run = getattr(self, "_gen_run", 0) + 1

        def watchdog(run: int = self._gen_run) -> None:
            # Grey-forever is impossible: force re-enable if this run's
            # worker never returned after 180s.
            try:
                if run == self._gen_run and self.gen_btn.instate(["disabled"]):
                    self.log("WATCHDOG: worker never returned")
                    self.gen_btn.configure(state="normal")
            except (tk.TclError, AttributeError, RuntimeError):
                pass

        threading.Thread(target=work, daemon=True).start()
        try:
            self.after(180000, watchdog)
        except (tk.TclError, AttributeError, RuntimeError):
            pass

    def _post_state(self) -> dict:
        """Current Post-tab values for the agent state file (no secrets)."""
        try:
            dest = self.post_dest.get()
        except (tk.TclError, AttributeError):
            dest = "community"
        images = [p for p in getattr(self, "post_images", []) if str(p or "").strip()]
        if not images:
            try:
                single = self.post_image.get().strip()
            except (tk.TclError, AttributeError):
                single = ""
            if single and os.path.isfile(single):
                images = [single]
        try:
            community = self.post_community.get().strip()
        except (tk.TclError, AttributeError):
            community = ""
        try:
            niche = self.post_niche.get().strip()
        except (tk.TclError, AttributeError):
            niche = ""
        try:
            flair = self.post_flair.get().strip()[:64]
        except (tk.TclError, AttributeError):
            flair = ""
        try:
            title = self.post_title.get().strip()
        except (tk.TclError, AttributeError):
            title = ""
        return {
            "container": self._selected_container_id(),
            "dest": dest,
            "community": community,
            "niche": niche,
            "flair": flair,
            "title": title,
            "images": images,
        }

    def on_save_state(self) -> None:
        path = write_creator_state(self._post_state())
        if path:
            self.log(f"Saved state for agent -> {path}")
            self.status.set("State saved for agent")
        else:
            self.log("ERROR could not write creator_state.json")

    def on_post(self) -> None:
        name = self._selected_container_id()
        title = self.post_title.get().strip()
        images = [p for p in self.post_images if str(p or "").strip()]
        if not images:
            single = self.post_image.get().strip()
            if single and os.path.isfile(single):
                images = [single]
        profile = False
        try:
            profile = self.post_dest.get() == "profile"
        except (tk.TclError, AttributeError):
            profile = False
        if not name:
            self.log("Pick a container from the dropdown (needs a created Reddit account).")
            return
        if not title:
            self.log("Post needs a title.")
            return
        if not images:
            self.log("Post needs JPG/PNG file(s) from this PC (Browse…).")
            return
        for fp in images:
            if not os.path.isfile(fp):
                self.log(f"Post image not on this PC: {fp}")
                return
        community = ""
        if not profile:
            community = self.post_community.get().strip()
            try:
                community = community_name(community)
            except PhoneError as e:
                self.log("ERROR  " + str(e))
                return
            save_post_community(community)
        slot = get_slot(name)
        if not slot or not slot.get("ghost"):
            self.log(f"{name} has no Ghost/Crane container.")
            return
        if slot_container_deleted(slot):
            self.log(
                f"{name}: its local container was deleted; it is not available "
                "for posting."
            )
            return
        if not slot_reddit_ready(slot):
            if slot_reddit_cleanup_pending(slot):
                self.log(f"{name} cleanup is incomplete; finish cleanup before posting.")
            else:
                self.log(f"{name} does not have a completed Reddit account.")
            return
        password = self.pw.get()
        if profile:
            captured_images = list(images)
            self._start_worker(
                self._run_profile_post, password, name, title, captured_images
            )
        else:
            if len(images) > 1:
                self.log(f"Community post takes one image; using {images[0]}.")
            try:
                flair = self.post_flair.get().strip()[:64]
            except (tk.TclError, AttributeError):
                flair = ""
            image = images[0]
            self._start_worker(
                self._run_post, password, name, community, title, image, flair
            )

    def _run_post(
        self, password: str, name: str, community: str, title: str, image: str,
        flair: str = "",
    ) -> None:
        phone = None
        try:
            phone, stack = self._connect(password)
            if self.stop.is_set():
                raise PhoneError("Stopped by user.")
            self.log(f"=== Post {name} / {community} ===")
            run_post(
                phone,
                stack,
                self.log,
                name,
                community,
                title,
                image,
                stop=self.stop,
                flair=flair,
            )
            if self.stop.is_set():
                raise PhoneError("Stopped by user.")
            self.status.set(f"{name}: posted {community}")
        except PhoneError as e:
            if self.stop.is_set():
                self.log("Stopped. Phone remains on its current screen.")
                self.status.set("Stopped")
            else:
                self.log("ERROR  " + str(e))
                self.status.set("Failed — see Log")
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
            self.status.set("Failed — see Log")
        finally:
            if phone:
                try:
                    phone.close()
                except Exception:
                    pass
            self.phone = None
            self.root.after(0, self._after_job)

    def _run_profile_post(
        self, password: str, name: str, title: str, images: list[str]
    ) -> None:
        phone = None
        try:
            phone, stack = self._connect(password)
            if self.stop.is_set():
                raise PhoneError("Stopped by user.")
            self.log(f"=== Profile post {name} ({len(images)} image(s)) ===")
            run_profile_post(
                phone,
                stack,
                self.log,
                name,
                title,
                images,
                stop=self.stop,
            )
            if self.stop.is_set():
                raise PhoneError("Stopped by user.")
            self.status.set(f"{name}: posted to profile")
        except PhoneError as e:
            if self.stop.is_set():
                self.log("Stopped. Phone remains on its current screen.")
                self.status.set("Stopped")
            else:
                self.log("ERROR  " + str(e))
                self.status.set("Failed — see Log")
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
            self.status.set("Failed — see Log")
        finally:
            if phone:
                try:
                    phone.close()
                except Exception:
                    pass
            self.phone = None
            self.root.after(0, self._after_job)

    def _replace_verification_email(
        self,
        sid: str,
        phone: Phone,
        stack: TapStack,
        expected_old: str,
        replacement_index: int,
        label: str,
        *,
        cancel_event=None,
    ) -> str:
        """Reconcile or create exactly one pending HME label, then activate it."""
        slot = get_slot(sid)
        try:
            checkpoint = email_verification_state(slot)
        except ValueError as exc:
            raise PhoneError(f"{sid} email verification checkpoint is malformed.") from exc
        if not checkpoint or checkpoint.get("phase") != "replacement_pending":
            raise PhoneError(
                f"{sid} has no durable pending replacement; did not create an alias."
            )
        if (
            int(checkpoint.get("replacement_index")) != int(replacement_index)
            or str(checkpoint.get("label") or "").lower() != str(label).lower()
        ):
            raise PhoneError(
                f"{sid} pending replacement identity changed; did not create an alias."
            )

        rows = load_slots()
        recovered = _reconcile_pipeline_email(
            sid,
            rows,
            _load_pipeline_email_records(),
            label=label,
        )
        if recovered is not None:
            address = recovered
            self.log(f"EMAIL {sid}: replacement {replacement_index} READY (reconciled)")
        else:
            self.log(f"EMAIL {sid}: replacement {replacement_index} IN_PROGRESS")
            address = create_one_hide_my_email(
                phone,
                stack,
                self.log,
                stop=cancel_event or self.stop,
                label=label,
            )
        if not _valid_pipeline_email(address):
            raise PhoneError(f"EMAIL {sid} replacement returned a malformed address.")
        if str(address).strip().lower() == str(expected_old).strip().lower():
            raise PhoneError(
                f"EMAIL {sid} replacement reused the active address; did not activate it."
            )
        replace_slot_email(
            sid,
            expected_old,
            address,
            label=label,
            replacement_index=replacement_index,
            log=self.log,
        )
        self.log(f"EMAIL {sid}: replacement {replacement_index} READY")
        return str(address).strip().lower()

    def _run_create_reddit(
        self,
        password: str,
        sid: str,
        birth_date_override: date | None = None,
        feed_swipes: int | None = None,
        feed_pause_count: int = 0,
    ) -> None:
        phone = None
        try:
            phone, stack = self._connect(password)
            if self.stop.is_set():
                raise PhoneError("Stopped by user.")
            cleanup_only = slot_reddit_cleanup_pending(get_slot(sid))
            self.log(
                f"=== Finish Reddit cleanup for {sid} ==="
                if cleanup_only
                else f"=== Create Reddit for {sid} ==="
            )
            slot = get_slot(sid)
            if not cleanup_only and ghost_container_state(slot) in (
                "needed",
                "provisioning",
            ):
                email = str(slot.get("email") or "").strip()
                if not email:
                    raise PhoneError(f"{sid} needs Hide My Email first.")
                self.log(
                    f"{sid}: recover the existing Crane container through "
                    "Reddit; no Ghost container will be created."
                )
                recover_existing_container(
                    stack,
                    phone,
                    self.log,
                    sid,
                    email,
                )
                mark_ghost_container_ready_from_crane(sid, self.log)
            create_kwargs = {
                "birth_date_override": birth_date_override,
                "log_fn": self.log,
                "stop": self.stop,
            }
            if feed_swipes is not None:
                create_kwargs["feed_swipes"] = feed_swipes
            create_kwargs["feed_pause_count"] = feed_pause_count
            create_kwargs["email_replacer"] = (
                lambda old, index, label: self._replace_verification_email(
                    sid, phone, stack, old, index, label
                )
            )
            run_create_reddit(sid, phone, stack, **create_kwargs)
            if self.stop.is_set():
                self.log("Stopped. Reddit remains on its current screen.")
                self.status.set("Stopped")
                return
            self.status.set(
                f"{sid}: cleanup complete; Reddit stopped and phone returned Home"
                if cleanup_only
                else f"{sid}: Reddit created; cleanup complete; phone returned Home"
            )
        except PhoneError as e:
            if self.stop.is_set():
                self.log("Stopped. Reddit remains on its current screen.")
                self.status.set("Stopped")
            else:
                self.log("ERROR  " + str(e))
                self.status.set("Failed — see Log")
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
            self.status.set("Failed — see Log")
        finally:
            birth_date_override = None
            if phone:
                try:
                    phone.close()
                except Exception:
                    pass
            self.phone = None
            self.root.after(0, self._after_job)

    def _recorded_container_identity(self, sid: str) -> str:
        """The exact local-container UUID recorded for one slot, if any."""
        row = get_slot(sid)
        return (
            container_deletion_instance(row)
            or crane_container_instance(row)
            or str(
            (row or {}).get("ghost_container_instance") or ""
            ).strip().upper()
        )

    def _bind_crane_container_identity(
        self,
        phone: Phone,
        sid: str,
        *,
        allow_completed_rebind: bool = False,
    ) -> str:
        """Prove and persist one exact-name full Crane UUID from fresh prefs."""
        row = get_slot(sid) or {}
        _status, full_uuid = resolve_crane_container_identity(
            phone,
            self.log,
            sid,
            "",
            allow_absent=False,
        )
        # Ghost's displayed identifier and Crane's persisted UUID are separate
        # namespaces.  The only safe correlation is the unique exact slot name
        # proven by the fresh Crane preferences inventory above; the short Ghost
        # value must never be treated as a prefix of the full Crane UUID.
        ghost_identifier = str(
            row.get("ghost_container_instance") or ""
        ).strip().upper()
        if ghost_identifier and ghost_identifier != full_uuid:
            self.log(
                f"CONTAINER {sid}: keeping the Ghost display identifier separate "
                "from the unique exact-name Crane UUID."
            )
        try:
            reconcile_crane_container_identity(
                sid,
                full_uuid,
                self.log,
                allow_pending_rebind=allow_completed_rebind,
            )
        except ValueError as exc:
            raise PhoneError(f"CONTAINER {sid}: {exc}") from exc
        return full_uuid

    def _ensure_container_deleted(
        self,
        phone: Phone,
        stack: TapStack,
        sid: str,
        attempt_stop=None,
    ) -> str:
        """Durably delete one account's exact local container, idempotently."""
        row = get_slot(sid)
        if row is None:
            raise PhoneError(f"DELETE {sid}: slot row disappeared before deletion.")
        if container_deletion_state(row) == "complete":
            self.log(f"DELETE {sid}: already verified complete (skipped).")
            self._bulk_live_status(f"Deleting {sid}: already complete.")
            return "complete"
        if not row.get("reddit"):
            raise PhoneError(
                f"DELETE {sid}: no created Reddit account; refused container deletion."
            )
        # Always refresh Crane's own preferences before deletion.  Ghost's UI
        # identifier is only a short display prefix and is never deletion
        # authority.  A completed legacy slot may migrate its pending prefix
        # only when exactly one live Crane row has this exact canonical name.
        existing_full = crane_container_instance(row)
        if existing_full:
            resolved_state, resolved_id = resolve_crane_container_identity(
                phone,
                self.log,
                sid,
                existing_full,
                allow_absent=True,
            )
            if resolved_state == "absent":
                try:
                    mark_container_deletion_complete(sid, self.log)
                except ValueError as exc:
                    raise PhoneError(
                        f"DELETE {sid}: verified absent but could not persist "
                        f"completion: {exc}"
                    ) from exc
                self.log(
                    f"DELETE {sid}: exact full Crane UUID is already absent; "
                    "recorded complete without another delete gesture."
                )
                return "complete"
            instance_id = resolved_id
        else:
            instance_id = self._bind_crane_container_identity(
                phone,
                sid,
                allow_completed_rebind=True,
            )
        self._bulk_live_status(
            f"Deleting {sid}: scanning for container {instance_id[:4]}."
        )
        try:
            mark_container_deletion_pending(sid, instance_id, self.log)
        except ValueError as exc:
            raise PhoneError(f"DELETE {sid}: {exc}") from exc
        if attempt_stop is not None and attempt_stop.is_set():
            raise PhoneError("Stopped.")
        self.log(
            f"DELETE {sid}: deterministic Crane Settings cleanup for recorded "
            f"UUID {instance_id[:8]}."
        )
        outcome = str(
            delete_named_instance(
                stack,
                phone,
                self.log,
                sid,
                instance_id,
                stop=attempt_stop,
            )
            or "deleted"
        )
        if outcome not in {"deleted", "absent"}:
            raise PhoneError(
                f"DELETE {sid}: exact UUID cleanup returned unverified outcome "
                f"{outcome!r}."
            )
        try:
            mark_container_deletion_complete(sid, self.log)
        except ValueError as exc:
            raise PhoneError(f"DELETE {sid}: could not persist deletion: {exc}") from exc
        self.log(f"DELETE {sid}: local container {(outcome or 'deleted')} and recorded complete.")
        self._bulk_live_status(f"Deleting {sid}: verified container deleted.")
        return "complete"

    def _run_deletion_only(self, password: str, sid: str) -> bool:
        phone = None
        try:
            phone, stack = self._connect(password)
            try:
                stack.cancel_event = self.stop
            except AttributeError:
                pass
            self._bulk_live_status(f"Deleting {sid}: deletion recovery starting.")
            if self.stop.is_set():
                return False
            result = self._ensure_container_deleted(phone, stack, sid, self.stop)
            if result != "complete":
                raise PhoneError(
                    "Exact container cleanup did not reach its durable complete checkpoint."
                )
            self._bulk_live_status(
                f"Deleting {sid}: deterministic deletion recovery complete."
            )
            return True
        except (RecoveryStopped, OperationCancelled):
            self.log(
                f"BATCH {sid}: Stop ended deterministic container cleanup; "
                "the pending checkpoint was preserved."
            )
            return False
        except PhoneError as exc:
            self.log(f"BATCH {sid}: required container deletion failed: {exc}")
            if not self.stop.is_set():
                self._pause_bulk(
                    "creation",
                    f"Verified account {sid} still needs its local container deleted: {exc}",
                    code="container_deletion_incomplete",
                )
            return False
        except Exception as exc:
            self.log(
                f"BATCH {sid}: required container deletion error "
                f"{type(exc).__name__}: {exc}"
            )
            if not self.stop.is_set():
                self._pause_bulk(
                    "creation",
                    f"Verified account {sid} container deletion failed "
                    f"({type(exc).__name__}).",
                    code="container_deletion_failed",
                )
            return False
        finally:
            if phone is not None:
                try:
                    phone.close()
                except Exception as exc:
                    # The verified deletion already succeeded; a teardown
                    # warning must not turn it back into a failure.
                    self.log(
                        f"BATCH {sid}: container deletion; PC connection close "
                        f"warning: {exc}"
                    )
            self.phone = None

    def _ai_recovery_enabled(self) -> bool:
        enabled = getattr(self, "anthropic_recovery_enabled", None)
        if isinstance(enabled, bool):
            return enabled
        variable = getattr(self, "anthropic_recovery_enabled_var", None)
        getter = getattr(variable, "get", None)
        if callable(getter):
            try:
                return bool(getter())
            except (tk.TclError, RuntimeError):
                return False
        # Headless/unit-test App doubles do not build Tk variables.  Recovery
        # stays opt-in there unless the double explicitly supplies a bool.
        return bool(variable) if isinstance(variable, bool) else False

    def _on_anthropic_recovery_toggle(self) -> None:
        """Keep recovery enabled whenever a configured key is available."""
        variable = getattr(self, "anthropic_recovery_enabled_var", None)
        enabled = recovery_configured()
        setter = getattr(variable, "set", None)
        if callable(setter):
            try:
                setter(enabled)
            except (tk.TclError, RuntimeError):
                pass
        self.anthropic_recovery_enabled = enabled
        if enabled:
            self._set_ai_recovery_status("AI recovery always on")
        else:
            self._set_ai_recovery_status(
                "AI recovery unavailable — add anthropic_api_key to secrets.json"
            )

    def _set_ai_recovery_status(self, message: str) -> None:
        value = str(message).strip()[:300]
        variable = getattr(self, "ai_recovery_status_var", None)
        setter = getattr(variable, "set", None)
        if callable(setter):
            try:
                setter(value)
            except (tk.TclError, RuntimeError):
                pass
        status = getattr(self, "status", None)
        status_setter = getattr(status, "set", None)
        if callable(status_setter):
            try:
                status_setter(value)
            except (tk.TclError, RuntimeError):
                pass

    def _run_creation_pipeline(
        self,
        password: str,
        sid: str,
        birth_date_override: date | None,
        feed_swipes: int,
        feed_pause_count: int = 0,
        *,
        requested_username: str = "",
        requested_password: str = "",
        resume_failure_code: str = "",
        manage_worker: bool = True,
        delete_container_required: bool = False,
        recovery_budget: RecoveryAttemptBudget | None = None,
    ) -> bool:
        """Repeat a recovered attempt; durable checkpoints decide real success."""
        attempt_budget = recovery_budget or RecoveryAttemptBudget()
        try:
            while True:
                result = self._run_creation_pipeline_attempt(
                    password,
                    sid,
                    birth_date_override,
                    feed_swipes,
                    feed_pause_count,
                    requested_username=requested_username,
                    requested_password=requested_password,
                    resume_failure_code=resume_failure_code,
                    manage_worker=False,
                    delete_container_required=delete_container_required,
                    recovery_budget=attempt_budget,
                )
                if result is _PIPELINE_RECOVERY_RETRY:
                    # A deterministic username availability check may atomically
                    # replace the queue head before a later UI recovery asks us
                    # to reconnect.  Do not replay the stale name captured by
                    # the outer batch loop: refresh the active credentials from
                    # the durable in-memory state that the committer just saved.
                    with self.bulk_mutex:
                        current = dict(self.bulk_state)
                        queue_rows = list(current.get("queue") or [])
                    if (
                        str(current.get("slot_id") or "") == str(sid or "")
                        and queue_rows
                    ):
                        refreshed_username = str(
                            (queue_rows[0] or {}).get("username") or ""
                        ).strip()
                        refreshed_password = str(
                            (queue_rows[0] or {}).get("password") or ""
                        )
                        if refreshed_username:
                            requested_username = refreshed_username
                        if refreshed_password:
                            requested_password = refreshed_password
                    self.log(
                        f"AI RECOVERY {sid}: reconnecting and replaying the "
                        "deterministic pipeline from saved checkpoints."
                    )
                    continue
                if result:
                    agent = getattr(self, "_pending_ui_recovery_agent", None)
                    session = getattr(self, "_pending_ui_recovery", None)
                    chain = getattr(self, "_pending_ui_recovery_chain", None)
                    pending = list(getattr(chain, "pending_sessions", ()) or ())
                    if not pending and session is not None:
                        pending = [session]
                    if agent is not None:
                        seen_sessions: set[int] = set()
                        for pending_session in pending:
                            if id(pending_session) in seen_sessions:
                                continue
                            seen_sessions.add(id(pending_session))
                            try:
                                agent.confirm_success(pending_session)
                            except Exception as cache_exc:
                                # A cache write can never downgrade a verified account.
                                self.log(
                                    "AI RECOVERY recipe save warning: "
                                    f"{type(cache_exc).__name__}: {cache_exc}"
                                )
                    self._pending_ui_recovery = None
                    self._pending_ui_recovery_agent = None
                    self._pending_ui_recovery_chain = None
                else:
                    self._pending_ui_recovery = None
                    self._pending_ui_recovery_agent = None
                    self._pending_ui_recovery_chain = None
                return bool(result)
        finally:
            if manage_worker:
                self.root.after(0, self._after_job)

    def _run_creation_pipeline_attempt(
        self,
        password: str,
        sid: str,
        birth_date_override: date | None,
        feed_swipes: int,
        feed_pause_count: int = 0,
        *,
        requested_username: str = "",
        requested_password: str = "",
        resume_failure_code: str = "",
        manage_worker: bool = True,
        delete_container_required: bool = False,
        recovery_budget: RecoveryAttemptBudget | None = None,
    ) -> bool | object:
        """Run all three existing stages through one iPhone connection."""
        recovery_budget = recovery_budget or RecoveryAttemptBudget()
        phone = None
        stack = None
        success = False
        self._last_pipeline_failure_reason = None
        self._last_pipeline_failure_code = None
        self._last_pipeline_failure_shared = False
        self._last_pipeline_attempt_timed_out = False
        # One fresh cancellation view and inactivity watchdog per attempt.  The
        # timeout event is never cleared while an old worker could still hold it.
        timed_out = threading.Event()
        attempt_stop = _AttemptCancel(self.stop, timed_out)
        CREATION_PROGRESS.reset()
        # Run/attempt identifiers + slot context so every event in this attempt
        # can be correlated even before a structured message is emitted.
        diag = getattr(self, "_diag", None)
        if diag is not None:
            self._diag_attempt = int(getattr(self, "_diag_attempt", 0)) + 1
            diag.set_context(
                attempt=self._diag_attempt,
                slot_id=sid,
                stage="pipeline",
                operation="creation_pipeline",
            )
            heartbeat = self._make_heartbeat(sid)
        else:
            heartbeat = None
        watcher = _InactivityWatch(
            timed_out, BULK_INACTIVITY_SECONDS, CREATION_PROGRESS, self.log, sid,
            heartbeat=heartbeat,
        )

        def attempt_failure(kind, *, exc=None, code="", disposition, note="", **extra):
            """Record one clearly marked failure report (never raises)."""
            self._report_attempt_failure(
                sid,
                kind=kind,
                exception=exc,
                code=code,
                disposition=disposition,
                note=note,
                **extra,
            )

        def run_ai_recovery(exc, *, failure_code: str = ""):
            """Run scoped phone-workflow recovery from a sanitized live frame.

            Credential sheets remain visible to the model as sanitized layouts:
            field roles are named, while their values stay local.  Prepared-value
            and destructive operations are exposed only by the narrower workflow
            that owns and deterministically verifies them.  A result merely asks
            the deterministic pipeline to reconnect and resume its durable checkpoint.
            """
            watcher.stop()
            self.log(
                f"AI RECOVERY {sid}: eligible UI failure detected; "
                "checking the local recipe cache."
            )
            self._set_ai_recovery_status("AI recovery — inspecting current screen")
            try:
                workflow_scope = "creation_pipeline"
                workflow_tools = {}
                resume_guard = None
                recovery_context = {
                    "stage": workflow_scope,
                    "failure_code": failure_code,
                }
                current_slot = get_slot(sid) or {}
                container_ready_now = bool(
                    current_slot.get("ghost")
                    and ghost_container_state(current_slot) == "ready"
                    and str(
                        current_slot.get("ghost_container_instance") or ""
                    ).strip()
                    and slot_container_available(current_slot)
                )
                if failure_code == ContainerSelectionError.failure_code:
                    workflow_scope = "container_selection"
                    recovery_context = {
                        "stage": workflow_scope,
                        "failure_code": failure_code,
                        "target_slot": sid,
                        "required_result": (
                            f"exact {sid} selected and Reddit foreground locally proven"
                        ),
                    }
                    self.log(
                        f"AI RECOVERY {sid}: scope=container_selection; "
                        f"controller-bound target slot={sid}."
                    )
                    selection_verified = False
                    selection_proof = None
                    allow_existing_sparse_feed = slot_reddit_done(current_slot)
                    self.log(
                        f"AI RECOVERY {sid}: existing-account sparse-feed "
                        f"verification={'enabled' if allow_existing_sparse_feed else 'disabled'}."
                    )

                    def verified_select_existing(_payload):
                        nonlocal selection_proof, selection_verified
                        selection_proof = select_existing_container_from_compact_menu(
                            stack,
                            phone,
                            self.log,
                            sid,
                            allow_existing_sparse_feed=allow_existing_sparse_feed,
                        )
                        recovered_slot = get_slot(sid) or {}
                        if ghost_container_state(recovered_slot) in {
                            "needed",
                            "provisioning",
                        }:
                            mark_ghost_container_ready_from_crane(sid, self.log)
                        selection_verified = True
                        self.log(
                            f"AI RECOVERY {sid}: exact compact-menu selection and "
                            "Reddit launch were locally verified."
                        )
                        return RecoveryDirective(
                            "resume_pipeline",
                            f"exact {sid} selection and Reddit foreground verified",
                        )

                    workflow_tools[
                        "verified_select_existing_container"
                    ] = RecoveryWorkflowTool(
                        description=(
                            f"Select the already-existing exact Crane slot {sid} "
                            "from the currently expanded compact menu and prove "
                            "that Reddit launches. The target is locally bound and "
                            "cannot be changed by tool input."
                        ),
                        handler=verified_select_existing,
                    )

                    def container_resume_guard(observation):
                        nonlocal selection_proof
                        state = post_selection_state(observation.nodes, sid)
                        if selection_verified and state == "reddit":
                            return (
                                True,
                                f"exact {sid} selection and Reddit foreground proven",
                            )
                        if selection_verified and isinstance(
                            selection_proof, FeedCleanupProof
                        ):
                            advanced_proof = advance_feed_cleanup_proof(
                                selection_proof, observation.nodes
                            )
                            if isinstance(advanced_proof, FeedCleanupProof):
                                selection_proof = advanced_proof
                                return (
                                    True,
                                    f"exact {sid} selection and sparse Reddit "
                                    "feed continuity locally proven",
                                )
                        if not selection_verified:
                            return (
                                False,
                                f"exact {sid} has not been locally verified; use "
                                "verified_select_existing_container after opening "
                                "the compact Crane list",
                            )
                        return (
                            False,
                            f"exact {sid} was selected but Reddit is not yet proven "
                            f"foreground (state={state})",
                        )

                    resume_guard = container_resume_guard
                elif not container_ready_now:
                    workflow_scope = "container_setup"
                    recovery_context["stage"] = workflow_scope

                    def verified_create_select(_payload):
                        apply_reddit_containers(
                            phone,
                            stack,
                            self.log,
                            count=1,
                            stop=attempt_stop,
                            close=True,
                            names=[sid],
                        )
                        verified_slot = get_slot(sid) or {}
                        if not (
                            verified_slot.get("ghost")
                            and ghost_container_state(verified_slot) == "ready"
                            and str(
                                verified_slot.get("ghost_container_instance") or ""
                            ).strip()
                            and slot_container_available(verified_slot)
                        ):
                            raise ContainerProvisioningReconcileError(
                                "Container create/select tool did not reach its "
                                "verified ready checkpoint; preserved the container, "
                                "slot and queue without another Anthropic recovery."
                            )
                        return "container create/select checkpoint verified"

                    workflow_tools[
                        "verified_create_select_container"
                    ] = RecoveryWorkflowTool(
                        description=(
                            "Create/select the requested Reddit Crane container "
                            "through the existing deterministic flow and verify "
                            "its durable ready checkpoint."
                        ),
                        handler=verified_create_select,
                    )
                elif str(failure_code).startswith("icloud_"):
                    workflow_scope = "icloud_unlock"
                    recovery_context["stage"] = workflow_scope

                    def verified_dismiss_icloud_sheet(_payload):
                        if not self._dismiss_icloud_sheet_once(phone, stack):
                            raise PhoneError(
                                "iCloud sheet dismissal could not be verified "
                                "from a fresh frame."
                            )
                        return "iCloud sheet dismissal verified"

                    workflow_tools[
                        "verified_dismiss_icloud_sheet"
                    ] = RecoveryWorkflowTool(
                        description=(
                            "Tap the currently verified iCloud Cancel control "
                            "and confirm from a fresh frame that the sheet closed."
                        ),
                        handler=verified_dismiss_icloud_sheet,
                    )

                    def retry_icloud_unlock(_payload):
                        verified_slot = get_slot(sid) or {}
                        saved = str(verified_slot.get("email") or "").strip().lower()
                        if saved and _valid_pipeline_email(saved):
                            return "Hide My Email checkpoint already verified"
                        address = create_one_hide_my_email(
                            phone,
                            stack,
                            self.log,
                            stop=attempt_stop,
                            label=sid,
                        )
                        if not _valid_pipeline_email(address):
                            raise PhoneError(
                                "Deterministic iCloud retry returned an invalid address."
                            )
                        set_slot_email(sid, address, self.log, label=sid)
                        if not _valid_pipeline_email(
                            (get_slot(sid) or {}).get("email")
                        ):
                            raise PhoneError(
                                "Deterministic iCloud retry did not persist its checkpoint."
                            )
                        return "Hide My Email checkpoint verified and saved"

                    if failure_code != "icloud_password_rejected":
                        workflow_tools[
                            "retry_icloud_unlock"
                        ] = RecoveryWorkflowTool(
                            description=(
                                "Retry the local deterministic iCloud unlock/Hide My "
                                "Email operation. Stored credentials are typed locally "
                                "and are never returned to the model."
                            ),
                            handler=retry_icloud_unlock,
                        )
                chain = getattr(self, "_pending_ui_recovery_chain", None)
                if not isinstance(chain, RecoveryChain):
                    chain = RecoveryChain()
                self.log(
                    f"AI RECOVERY {sid}: recovery chain active; "
                    f"semantic_actions={len(chain.action_pairs)} "
                    f"history={len(chain.history)} "
                    f"pending_verifications={len(chain.pending_sessions)}."
                )
                agent = UIRecoveryAgent(
                    stack,
                    phone,
                    stop=self.stop,
                    log=self.log,
                    status=self._set_ai_recovery_status,
                    workflow_scope=workflow_scope,
                    workflow_tools=workflow_tools,
                    context=recovery_context,
                    resume_guard=resume_guard,
                    chain=chain,
                    attempt_budget=recovery_budget,
                )
                session = agent.recover(
                    exc,
                    previous=getattr(self, "_pending_ui_recovery", None),
                )
                if getattr(session, "directive", "") == "discard_restart_current_attempt":
                    # AI never has authority to delete or retire an identity.
                    # No recovery scope exposes this directive; keep this guard
                    # for stale recipes, malformed responses, and custom agent
                    # implementations that attempt to synthesize it anyway.
                    self._pending_ui_recovery = None
                    self._pending_ui_recovery_agent = None
                    self._pending_ui_recovery_chain = None
                    self._last_pipeline_failure_shared = True
                    self._last_pipeline_failure_code = AI_DISCARD_REFUSED_CODE
                    self._last_pipeline_failure_reason = (
                        "Anthropic requested a destructive attempt reset; the "
                        "controller refused it because only typed local recovery "
                        "code may authorize deletion or queue retirement."
                    )
                    self.log(
                        f"AI RECOVERY {sid}: destructive reset directive refused "
                        f"in scope={workflow_scope}; container, slot and queue preserved."
                    )
                    attempt_failure(
                        "attempt_failure",
                        code=AI_DISCARD_REFUSED_CODE,
                        disposition=(
                            "AI discard refused in every scope; container, slot and "
                            "queue item preserved"
                        ),
                    )
                    return False
                if getattr(session, "directive", "") == "resume_pipeline":
                    # The scoped workflow tool has already completed and
                    # verified this operation's deterministic postcondition.
                    # It may therefore become a candidate immediately; an AI
                    # assertion alone never reaches this branch.
                    try:
                        agent.confirm_success(session)
                    except Exception as cache_exc:
                        self.log(
                            "AI RECOVERY recipe save warning after verified "
                            f"container selection: {type(cache_exc).__name__}: "
                            f"{cache_exc}"
                        )
                    self.log(
                        f"AI RECOVERY {sid}: verified container-selection "
                        "postcondition accepted; recipe confirmation completed."
                    )
                    self._pending_ui_recovery = None
                else:
                    if all(
                        existing is not session
                        for existing in chain.pending_sessions
                    ):
                        chain.pending_sessions.append(session)
                    self.log(
                        f"AI RECOVERY {sid}: recovery chain advanced; "
                        f"semantic_actions={len(chain.action_pairs)} "
                        f"history={len(chain.history)} "
                        f"pending_verifications={len(chain.pending_sessions)}."
                    )
                    self._pending_ui_recovery = session
                self._pending_ui_recovery_agent = agent
                self._pending_ui_recovery_chain = chain
                self._last_pipeline_failure_reason = None
                self._last_pipeline_failure_code = None
                return _PIPELINE_RECOVERY_RETRY
            except (RecoveryStopped, OperationCancelled) as recovery_stop:
                if timed_out.is_set() and not self.stop.is_set():
                    self._record_pipeline_timeout(sid)
                    attempt_failure(
                        "inactivity_timeout",
                        exc=recovery_stop,
                        code=BULK_INACTIVITY_TIMEOUT_CODE,
                        disposition=(
                            "inactivity timeout: attempt reset with a fresh "
                            "container and IP"
                        ),
                        note=(
                            "The inactivity watchdog cancelled the scoped AI "
                            "workflow; it was not reported as an Anthropic failure."
                        ),
                    )
                elif self.stop.is_set() or isinstance(
                    recovery_stop, RecoveryStopped
                ):
                    self.log(
                        f"CANCELLED PIPELINE {sid}: AI recovery stopped; "
                        "current checkpoints preserved."
                    )
                    self.status.set("Stopped")
                    attempt_failure(
                        "user_stop",
                        exc=recovery_stop,
                        code="user_stop",
                        disposition=(
                            "user Stop: AI recovery ended and the attempt was preserved"
                        ),
                        note="The user pressed Stop during Anthropic UI recovery.",
                    )
                else:
                    self._last_pipeline_failure_reason = (
                        "AI UI recovery operation was cancelled before its "
                        f"postcondition: {recovery_stop}"
                    )
                    self._last_pipeline_failure_code = "ai_ui_recovery_cancelled"
                    self.log(
                        f"FAILED PIPELINE {sid}: "
                        f"{self._last_pipeline_failure_reason}"
                    )
                    self.status.set("Failed — AI UI recovery cancelled")
                    attempt_failure(
                        "attempt_failure",
                        exc=recovery_stop,
                        code="ai_ui_recovery_cancelled",
                        disposition=(
                            "AI recovery cancelled before a safe result; "
                            "queue item preserved"
                        ),
                    )
                return False
            except RecoveryBudgetExhausted as recovery_exc:
                # The budget belongs to the complete attempt.  Exhaustion is an
                # uncertain recovery result, never proof that an incomplete
                # identity should be destroyed.  Mark it pause-only for manual
                # and bulk runs alike; the separately typed, locally proven
                # container_recreate_required path remains unchanged.
                failure = AI_RECOVERY_BUDGET_EXHAUSTED_CODE
                disposition = (
                    "shared AI budget exhausted; preserve the container, slot, "
                    "checkpoints and queue item"
                )
                self._last_pipeline_failure_shared = True
                self._last_pipeline_failure_code = failure
                self._last_pipeline_failure_reason = str(recovery_exc)
                self.log(
                    f"FAILED PIPELINE {sid}: shared Anthropic budget exhausted "
                    f"({recovery_exc.used_calls}/{recovery_exc.max_calls}); "
                    f"failure_code={failure}; no destructive reset allowed."
                )
                self.status.set("Failed — AI recovery budget exhausted")
                attempt_failure(
                    "attempt_failure",
                    exc=recovery_exc,
                    code=failure,
                    disposition=disposition,
                )
                return False
            except Exception as recovery_exc:
                if is_recoverable_bridge_failure(recovery_exc):
                    raise BridgeTransportError(str(recovery_exc)) from recovery_exc
                preserved_code = str(
                    failure_code
                    or self._last_pipeline_failure_code
                    or "ai_ui_recovery_failed"
                )
                self._last_pipeline_failure_reason = (
                    "AI UI recovery could not inspect or control the live screen: "
                    f"{type(recovery_exc).__name__}: {recovery_exc}"
                )
                self._last_pipeline_failure_code = preserved_code
                if workflow_scope == "container_setup":
                    # Any unresolved post-Create/provisioning recovery remains
                    # pause-only.  A generic model or transport failure is not
                    # local proof that the just-created container is disposable.
                    self._last_pipeline_failure_shared = True
                self.log(
                    f"FAILED PIPELINE {sid}: "
                    f"{self._last_pipeline_failure_reason}"
                )
                self.status.set("Failed — AI UI recovery unavailable")
                attempt_failure(
                    "attempt_failure",
                    exc=recovery_exc,
                    code=preserved_code,
                    disposition=(
                        "AI recovery failed before a safe navigation result; "
                        "queue item preserved"
                    ),
                )
                return False

        def require_not_stopped() -> None:
            if attempt_stop.is_set():
                raise PhoneError("Stopped by user.")

        try:
            watcher.start()
            self.log(f"PIPELINE {sid}: IN_PROGRESS")
            require_not_stopped()

            slot = get_slot(sid)
            if slot is None:
                ensure_ghost_container_needed(sid, self.log)
                slot = get_slot(sid)
            if slot is None:
                raise PhoneError(f"PIPELINE {sid} checkpoint was not persisted.")
            if slot_container_deleted(slot):
                # The account's local container was permanently removed after a
                # verified creation.  Rebuilding it (or reopening the account)
                # would silently undo that; fail closed instead.  The caller
                # advances on the durable account+deletion state, not on a
                # pipeline replay.
                raise PhoneError(
                    f"PIPELINE {sid}: its local container was already verified "
                    "deleted; refusing to rebuild it or reopen the account."
                )
            requested_username = str(requested_username or "").strip()
            saved_username = str(slot.get("username") or "").strip()
            if requested_username and saved_username and saved_username != requested_username:
                raise PhoneError(
                    f"PIPELINE {sid} saved username differs from its requested batch username. "
                    "Did not open Reddit or change the account."
                )

            saved_birth = str(slot.get("reddit_birth_date") or "").strip()
            if birth_date_override is not None:
                canonical_birth = birth_date_override.strftime("%d.%m.%Y")
                if saved_birth and saved_birth != canonical_birth:
                    raise PhoneError(
                        f"PIPELINE {sid} birthday does not match its saved checkpoint."
                    )
                if not saved_birth:
                    set_slot_birth_date(sid, canonical_birth)
            elif not slot_reddit_cleanup_pending(slot):
                raise PhoneError(f"PIPELINE {sid} has no validated birthday.")
            require_not_stopped()

            phone, stack = self._connect(password)
            # Rebind the tap stack's cancellation to this attempt's view so an
            # inactivity timeout reaches every stack wait, without changing the
            # existing _connect() signature.
            try:
                stack.cancel_event = attempt_stop
            except AttributeError:
                pass
            # Liveness, not just checkpoints: every completed device round-trip
            # (dump, tap, flick, screenshot, app probe, container step) proves
            # this attempt is alive.  A slow-but-working phase - birthday wheel,
            # interests, "Customize your feed", sign-up waits, feed swipes,
            # cleanup or container deletion - can therefore never trip the
            # three-minute reset while the iPhone keeps answering.  It is set on
            # this attempt's private Phone only, so the poster/commenter
            # controllers can never mask a real creation stall.
            phone.progress_note = CREATION_PROGRESS.note
            require_not_stopped()

            slot = get_slot(sid)
            ghost_ready = bool(
                slot
                and slot.get("ghost")
                and ghost_container_state(slot) == "ready"
                and (
                    str(slot.get("ghost_container_instance") or "").strip()
                    or str(slot.get("crane_selection_policy") or "").strip()
                )
                and slot_container_available(slot)
            )
            container_ready = bool(ghost_ready and crane_container_instance(slot))
            if ghost_ready and not container_ready:
                self._bind_crane_container_identity(phone, sid)
                slot = get_slot(sid)
                container_ready = bool(crane_container_instance(slot))
            if container_ready:
                self.log(f"CONTAINER {sid}: READY (skipped)")
            else:
                self.log(f"CONTAINER {sid}: IN_PROGRESS")
                apply_reddit_containers(
                    phone,
                    stack,
                    self.log,
                    count=1,
                    stop=attempt_stop,
                    close=True,
                    names=[sid],
                )
                require_not_stopped()
                slot = get_slot(sid)
                if not (
                    slot
                    and slot.get("ghost")
                    and ghost_container_state(slot) == "ready"
                    and str(slot.get("ghost_container_instance") or "").strip()
                ):
                    raise ContainerProvisioningReconcileError(
                        f"CONTAINER {sid} did not reach its verified ready checkpoint; "
                        "preserved the container, slot and queue with Anthropic calls=0."
                    )
                self._bind_crane_container_identity(phone, sid)
                slot = get_slot(sid)
                if not crane_container_instance(slot):
                    raise ContainerProvisioningReconcileError(
                        f"CONTAINER {sid} has no locally verified full Crane UUID; "
                        "preserved the container, slot and queue."
                    )
                self.log(f"CONTAINER {sid}: READY")
                self.log(
                    f"CONTAINER {sid}: ghost stage complete; continuing to email stage."
                )
            require_not_stopped()

            slot = get_slot(sid)
            saved_email = str((slot or {}).get("email") or "").strip().lower()
            if saved_email:
                if not _valid_pipeline_email(saved_email):
                    raise PhoneError(f"EMAIL {sid} has a malformed saved address.")
                self.log(f"EMAIL {sid}: READY (skipped)")
            else:
                rows = load_slots()
                recovered = _reconcile_pipeline_email(
                    sid, rows, _load_pipeline_email_records()
                )
                if recovered is not None:
                    set_slot_email(sid, recovered, self.log, label=sid)
                    self.log(f"EMAIL {sid}: READY (reconciled)")
                else:
                    self.log(f"EMAIL {sid}: IN_PROGRESS")
                    address = create_one_hide_my_email(
                        phone,
                        stack,
                        self.log,
                        stop=attempt_stop,
                        label=sid,
                    )
                    require_not_stopped()
                    if not _valid_pipeline_email(address):
                        raise PhoneError(
                            f"EMAIL {sid} returned a malformed iCloud address."
                        )
                    set_slot_email(sid, address, self.log, label=sid)
                    self.log(f"EMAIL {sid}: READY")
                require_not_stopped()
                slot = get_slot(sid)
                if not _valid_pipeline_email((slot or {}).get("email")):
                    raise PhoneError(
                        f"EMAIL {sid} did not reach its saved address checkpoint."
                    )

            require_not_stopped()
            self.log(f"REDDIT {sid}: IN_PROGRESS")
            accepted_username = requested_username

            def commit_username_variant(expected, candidate):
                nonlocal accepted_username
                self._commit_bulk_username_variant(sid, expected, candidate)
                accepted_username = candidate

            reddit_kwargs = dict(
                birth_date_override=birth_date_override,
                log_fn=self.log,
                stop=attempt_stop,
                feed_swipes=feed_swipes,
                feed_pause_count=feed_pause_count,
                email_replacer=(
                    lambda old, index, label: self._replace_verification_email(
                        sid,
                        phone,
                        stack,
                        old,
                        index,
                        label,
                        cancel_event=attempt_stop,
                    )
                ),
                rotate_before_poll=(resume_failure_code == "reddit_code_timeout"),
            )
            if requested_username:
                reddit_kwargs["requested_username"] = requested_username
                reddit_kwargs["username_committer"] = commit_username_variant
                reddit_kwargs["username_candidate_allowed"] = (
                    lambda candidate: self._bulk_username_candidate_available(
                        sid, candidate
                    )
                )
            if requested_password:
                reddit_kwargs["requested_password"] = requested_password
            run_create_reddit(sid, phone, stack, **reddit_kwargs)
            require_not_stopped()

            slot = get_slot(sid)
            if not slot:
                raise PhoneError(f"REDDIT {sid} slot checkpoint disappeared.")
            target, completed = creation_feed_swipe_progress(slot)
            if not (
                str(slot.get("username") or "").strip()
                and (
                    not accepted_username
                    or slot.get("username") == accepted_username
                )
                and str(slot.get("password") or "").strip()
                and (not requested_password or slot.get("password") == requested_password)
                and slot.get("reddit") is True
                and slot_reddit_ready(slot)
                and target == feed_swipes
                and completed == feed_swipes
            ):
                raise PhoneError(
                    f"REDDIT {sid} did not reach its verified creation, swipe, "
                    "and cleanup checkpoints."
                )
            self.log(f"REDDIT {sid}: READY")
            if delete_container_required:
                if _pipeline_deletion_satisfied(get_slot(sid), True):
                    self.log(f"DELETE {sid}: READY (already verified)")
                else:
                    self.log(f"DELETE {sid}: IN_PROGRESS")
                    self._ensure_container_deleted(
                        phone, stack, sid, attempt_stop
                    )
                    require_not_stopped()
                    if not _pipeline_deletion_satisfied(get_slot(sid), True):
                        raise PhoneError(
                            f"DELETE {sid} did not reach its verified deletion checkpoint."
                        )
                    self.log(f"DELETE {sid}: READY")
            self.log(f"COMPLETED PIPELINE {sid}")
            self.status.set(
                f"{sid}: container, email, Reddit, swipes, and cleanup complete"
            )
            success = True
        except EmailReplacementExhausted as exc:
            self._last_pipeline_failure_code = exc.failure_code
            self._last_pipeline_failure_reason = str(exc)
            if (
                phone is not None
                and stack is not None
                and not self.stop.is_set()
                and not timed_out.is_set()
                and self._ai_recovery_enabled()
                and is_ui_recoverable_error(exc)
            ):
                return run_ai_recovery(exc, failure_code=exc.failure_code)
            self.log(f"FAILED PIPELINE {sid}: {exc}")
            self.status.set("Failed — replacement email attempts exhausted")
            attempt_failure(
                "expected",
                exc=exc,
                code=exc.failure_code,
                disposition="attempt failed; batch pauses with the same queue item",
                note="Replacement email attempts were exhausted (expected failure).",
            )
        except EmailFieldInvalid as exc:
            self._last_pipeline_failure_code = exc.failure_code
            self._last_pipeline_failure_reason = str(exc)
            if (
                phone is not None
                and stack is not None
                and not self.stop.is_set()
                and not timed_out.is_set()
                and self._ai_recovery_enabled()
                and is_ui_recoverable_error(exc)
            ):
                return run_ai_recovery(exc, failure_code=exc.failure_code)
            self.log(f"FAILED PIPELINE {sid}: {exc}")
            self.status.set("Failed — see Log")
            attempt_failure(
                "expected",
                exc=exc,
                code=exc.failure_code,
                disposition="attempt failed; batch pauses with the same queue item",
                note="The generated email field was invalid (expected failure).",
            )
        except SSHAuthError as exc:
            # SSH authentication failure prevents every account from working.
            self._last_pipeline_failure_shared = True
            self._last_pipeline_failure_code = "ssh_auth_failed"
            self._last_pipeline_failure_reason = f"iPhone SSH authentication failed: {exc}"
            self.log(f"FAILED PIPELINE {sid}: iPhone SSH authentication failed.")
            self.status.set("Failed — iPhone SSH authentication failed")
            attempt_failure(
                "infrastructure",
                exc=exc,
                code="ssh_auth_failed",
                disposition="shared infrastructure failure; the batch pauses",
                note="SSH authentication failed; no further slot can proceed.",
            )
        except ICloudPasswordEntryError as exc:
            # The saved iCloud password could not be entered on the re-auth
            # sheet.  This is account-specific: record its dedicated code and
            # let the batch skip this slot and continue.  Every verified
            # checkpoint (including the READY Ghost container) stays preserved,
            # so a resume reuses the container and continues from the email
            # stage instead of recreating it.
            if self.stop.is_set():
                self.log(f"CANCELLED PIPELINE {sid}: current checkpoints preserved.")
                self.status.set("Stopped")
                attempt_failure(
                    "user_stop",
                    exc=exc,
                    code="user_stop",
                    disposition="user Stop: attempt preserved and not retried",
                    note="The user pressed Stop; checkpoints stay preserved.",
                )
            elif timed_out.is_set():
                self._record_pipeline_timeout(sid)
                attempt_failure(
                    "inactivity_timeout",
                    code=BULK_INACTIVITY_TIMEOUT_CODE,
                    disposition="inactivity timeout: attempt reset with a fresh container and IP",
                    note="No verified progress before the inactivity deadline.",
                )
            else:
                rejected = bool(getattr(exc, "credentials_rejected", False))
                recovery_code = (
                    "icloud_password_rejected"
                    if rejected
                    else exc.failure_code
                )
                self._last_pipeline_failure_code = recovery_code
                self._last_pipeline_failure_reason = str(exc)
                if (
                    phone is not None
                    and stack is not None
                    and self._ai_recovery_enabled()
                    and is_ui_recoverable_error(exc)
                ):
                    # Start recovery while the sign-in sheet is still present.
                    # Anthropic sees its sanitized structure and may request a
                    # locally typed deterministic unlock, verified dismissal,
                    # navigation, retry, or scoped full reset.
                    self.log(
                        f"AI RECOVERY {sid}: iCloud sign-in screen remains "
                        "visible; handing its sanitized live frame and scoped "
                        "iCloud controls directly to Anthropic."
                    )
                    return run_ai_recovery(
                        exc, failure_code=recovery_code
                    )

                # When AI recovery is deliberately disabled, keep the bounded
                # legacy Cancel path rather than leaving a modal over the next
                # operation.
                dismissed = (not rejected) and self._dismiss_icloud_sheet_once(
                    phone, stack
                )
                if rejected or not dismissed:
                    self._last_pipeline_failure_shared = True
                    self._last_pipeline_failure_code = (
                        "icloud_password_rejected"
                        if rejected
                        else "icloud_sheet_dismiss_failed"
                    )
                    self._last_pipeline_failure_reason = str(exc)
                    self.log(f"FAILED PIPELINE {sid}: {exc}")
                    self.status.set("Failed — see Log")
                    attempt_failure(
                        "infrastructure",
                        exc=exc,
                        code=self._last_pipeline_failure_code,
                        disposition=(
                            "shared iCloud failure; the batch pauses (queue kept)"
                        ),
                        note=(
                            "iCloud rejected the saved password; it will not be "
                            "resubmitted."
                            if rejected
                            else "The iCloud password sheet could not be dismissed "
                            "after a verified Cancel, so it would block every "
                            "remaining account."
                        ),
                    )
                else:
                    self._last_pipeline_failure_code = exc.failure_code
                    self._last_pipeline_failure_reason = str(exc)
                    self.log(f"FAILED PIPELINE {sid}: {exc} (sheet dismissed)")
                    self.status.set("Failed — see Log")
                    attempt_failure(
                        "attempt_failure",
                        exc=exc,
                        code=exc.failure_code,
                        disposition="attempt failed; batch pauses with the same queue item",
                        note="The saved iCloud password could not be entered on the "
                        "re-auth sheet; the verified Cancel dismissed the sheet and "
                        "the Ghost container checkpoint is preserved.",
                    )
        except ICloudModalBlockingError as exc:
            # An iCloud re-auth modal is overlaying the screen; it will block
            # every account, so this is a shared failure.  Pause without
            # consuming the queue and keep every checkpoint.
            if self.stop.is_set():
                self.log(f"CANCELLED PIPELINE {sid}: current checkpoints preserved.")
                self.status.set("Stopped")
                attempt_failure(
                    "user_stop",
                    exc=exc,
                    code="user_stop",
                    disposition="user Stop: attempt preserved and not retried",
                    note="The user pressed Stop; checkpoints stay preserved.",
                )
            elif timed_out.is_set():
                self._record_pipeline_timeout(sid)
                attempt_failure(
                    "inactivity_timeout",
                    code=BULK_INACTIVITY_TIMEOUT_CODE,
                    disposition="inactivity timeout: attempt reset with a fresh container and IP",
                    note="No verified progress before the inactivity deadline.",
                )
            else:
                self._last_pipeline_failure_code = exc.failure_code
                self._last_pipeline_failure_reason = str(exc)
                if (
                    phone is not None
                    and stack is not None
                    and self._ai_recovery_enabled()
                    and is_ui_recoverable_error(exc)
                ):
                    return run_ai_recovery(
                        exc, failure_code=exc.failure_code
                    )
                self._last_pipeline_failure_shared = True
                self.log(f"FAILED PIPELINE {sid}: {exc}")
                self.status.set("Failed — iCloud sheet is blocking the screen")
                attempt_failure(
                    "infrastructure",
                    exc=exc,
                    code=exc.failure_code,
                    disposition="shared iCloud blocking modal; the batch pauses (queue kept)",
                    note="An iCloud 'Sign in to iCloud' sheet overlays Ghost and "
                    "would block every remaining account; it must be dismissed "
                    "in Settings first.",
                )
        except ContainerProvisioningReconcileError as exc:
            if self.stop.is_set():
                self.log(f"CANCELLED PIPELINE {sid}: current checkpoints preserved.")
                self.status.set("Stopped")
                attempt_failure(
                    "user_stop",
                    exc=exc,
                    code="user_stop",
                    disposition="user Stop: attempt preserved and not retried",
                    note="The user pressed Stop during container reconciliation.",
                )
            else:
                # Ghost inventory reconciliation is local and deterministic.
                # Zero/ambiguous/unproven matches must never be handed to AI or
                # converted into the generic incomplete-attempt deletion path.
                self._last_pipeline_failure_shared = True
                self._last_pipeline_failure_code = exc.failure_code
                self._last_pipeline_failure_reason = str(exc)
                self.log(
                    f"FAILED PIPELINE {sid}: container provisioning reconciliation "
                    "was not locally proven; Anthropic calls=0; preserving the "
                    "container, slot and queue item."
                )
                self.status.set("Failed — container provisioning needs review")
                attempt_failure(
                    "attempt_failure",
                    exc=exc,
                    code=exc.failure_code,
                    disposition=(
                        "local container provisioning reconciliation failed; "
                        "container, slot and queue item preserved"
                    ),
                    note=(
                        "No Anthropic request or destructive reset is allowed for "
                        "this deterministic provisioning state."
                    ),
                )
            return False
        except CraneDoorwayUnavailable as exc:
            if self.stop.is_set():
                self.log(f"CANCELLED PIPELINE {sid}: current checkpoints preserved.")
                self.status.set("Stopped")
                attempt_failure(
                    "user_stop",
                    exc=exc,
                    code="user_stop",
                    disposition="user Stop: attempt preserved and not retried",
                    note="The user pressed Stop; checkpoints stay preserved.",
                )
            elif timed_out.is_set():
                self._record_pipeline_timeout(sid)
                attempt_failure(
                    "inactivity_timeout",
                    code=BULK_INACTIVITY_TIMEOUT_CODE,
                    disposition=(
                        "inactivity timeout: attempt reset with a fresh container and IP"
                    ),
                    note="No verified progress before the inactivity deadline.",
                )
            else:
                current_slot = get_slot(sid) or {}
                bulk_state_now = getattr(self, "bulk_state", {})
                discard_incomplete = bool(
                    isinstance(bulk_state_now, dict)
                    and bulk_state_now.get("stage")
                    in {"creating", "retry_creation", "ip_verified"}
                    and str(bulk_state_now.get("slot_id") or sid) == sid
                    and not current_slot.get("reddit")
                )
                if discard_incomplete:
                    self._last_pipeline_failure_code = (
                        CONTAINER_RECREATE_FAILURE_CODE
                    )
                    self._last_pipeline_failure_reason = str(exc)
                    self.log(
                        f"FAILED PIPELINE {sid}: the proven stock Reddit menu "
                        "had no Crane doorway and exact-UUID local recovery failed; "
                        "Anthropic calls=0; requesting immediate verified recreation."
                    )
                    self.status.set(
                        "Crane doorway unavailable — verified local recreation"
                    )
                    attempt_failure(
                        "attempt_failure",
                        exc=exc,
                        code=CONTAINER_RECREATE_FAILURE_CODE,
                        disposition=(
                            "typed missing-Crane-doorway failure on an incomplete "
                            "bulk attempt; run verified delete/retire/new-IP replacement"
                        ),
                        note=(
                            "No Anthropic request was made for this deterministic state."
                        ),
                    )
                else:
                    # A manual run or any slot that already has a Reddit account
                    # is never auto-deleted by this recovery path.
                    self._last_pipeline_failure_code = exc.failure_code
                    self._last_pipeline_failure_reason = str(exc)
                    self.log(f"FAILED PIPELINE {sid}: {exc}")
                    self.status.set("Failed — Crane doorway unavailable")
                    attempt_failure(
                        "attempt_failure",
                        exc=exc,
                        code=exc.failure_code,
                        disposition=(
                            "account/slot preserved; manual Retry / Resume remains available"
                        ),
                    )
                return False
        except ContainerSelectionError as exc:
            if self.stop.is_set():
                self.log(f"CANCELLED PIPELINE {sid}: current checkpoints preserved.")
                self.status.set("Stopped")
                attempt_failure(
                    "user_stop",
                    exc=exc,
                    code="user_stop",
                    disposition="user Stop: attempt preserved and not retried",
                    note="The user pressed Stop; checkpoints stay preserved.",
                )
            elif timed_out.is_set():
                self._record_pipeline_timeout(sid)
                attempt_failure(
                    "inactivity_timeout",
                    code=BULK_INACTIVITY_TIMEOUT_CODE,
                    disposition=(
                        "inactivity timeout: attempt reset with a fresh container and IP"
                    ),
                    note="No verified progress before the inactivity deadline.",
                )
            elif (
                phone is not None
                and stack is not None
                and self._ai_recovery_enabled()
                and is_ui_recoverable_error(exc)
            ):
                return run_ai_recovery(exc, failure_code=exc.failure_code)
            else:
                self._last_pipeline_failure_code = exc.failure_code
                self._last_pipeline_failure_reason = str(exc)
                self.log(f"FAILED PIPELINE {sid}: {exc}")
                self.status.set("Failed — container selection not confirmed")
                attempt_failure(
                    "attempt_failure",
                    exc=exc,
                    code=exc.failure_code,
                    disposition=(
                        "container selection failed; batch pauses with the same "
                        "slot and queue item"
                    ),
                    note=(
                        "Reddit was not allowed to continue because the exact "
                        "Crane container was not confirmed active."
                    ),
                )
        except PhoneError as exc:
            if (
                not self.stop.is_set()
                and not timed_out.is_set()
                and is_recoverable_bridge_failure(exc)
            ):
                raise BridgeTransportError(str(exc)) from exc
            if self.stop.is_set():
                self.log(f"CANCELLED PIPELINE {sid}: current checkpoints preserved.")
                self.status.set("Stopped")
                attempt_failure(
                    "user_stop",
                    exc=exc,
                    code="user_stop",
                    disposition="user Stop: attempt preserved and not retried",
                    note="The user pressed Stop; checkpoints stay preserved.",
                )
            elif timed_out.is_set():
                self._record_pipeline_timeout(sid)
                attempt_failure(
                    "inactivity_timeout",
                    code=BULK_INACTIVITY_TIMEOUT_CODE,
                    disposition="inactivity timeout: attempt reset with a fresh container and IP",
                    note="No verified progress before the inactivity deadline.",
                )
            elif (
                phone is not None
                and stack is not None
                and self._ai_recovery_enabled()
                and is_ui_recoverable_error(exc)
            ):
                # The ordinary inactivity timer must not race an intentionally
                # unlimited Anthropic/API recovery loop.  Stop remains wired to
                # both the stack and HTTP wait throughout recovery.
                return run_ai_recovery(
                    exc,
                    failure_code=self._last_pipeline_failure_code or "",
                )
            else:
                # Generic PhoneError is deterministic/local by default.  It is
                # never authority to spend AI budget, wait 180 seconds, delete
                # a container or consume a queue head.  Only explicitly typed
                # reset paths above may opt into destructive replacement.
                self._last_pipeline_failure_shared = True
                if not self._last_pipeline_failure_code:
                    self._last_pipeline_failure_code = "deterministic_phone_error"
                self._last_pipeline_failure_reason = str(exc)
                self.log(f"FAILED PIPELINE {sid}: {exc}")
                self.status.set("Failed — see Log")
                attempt_failure(
                    "attempt_failure",
                    exc=exc,
                    code=self._last_pipeline_failure_code,
                    disposition="attempt failed; batch pauses with the same queue item",
                )
        except Exception as exc:
            if (
                not self.stop.is_set()
                and not timed_out.is_set()
                and is_recoverable_bridge_failure(exc)
            ):
                raise BridgeTransportError(str(exc)) from exc
            if timed_out.is_set() and not self.stop.is_set():
                self._record_pipeline_timeout(sid)
                attempt_failure(
                    "inactivity_timeout",
                    code=BULK_INACTIVITY_TIMEOUT_CODE,
                    disposition="inactivity timeout: attempt reset with a fresh container and IP",
                    note="No verified progress before the inactivity deadline.",
                )
            else:
                # An unexpected programming error must never silently drop a
                # queue item; pause instead of preserving-and-skipping.
                self._last_pipeline_failure_shared = True
                self._last_pipeline_failure_code = "unexpected_pipeline_error"
                self._last_pipeline_failure_reason = f"{type(exc).__name__}: {exc}"
                self.log(f"FAILED PIPELINE {sid}: {type(exc).__name__}: {exc}")
                self.status.set("Failed — see Log")
                attempt_failure(
                    "unexpected",
                    exc=exc,
                    code="unexpected_pipeline_error",
                    disposition="unexpected error: the batch pauses for a human",
                    note="A programming error escaped the pipeline; it was reported, "
                    "not swallowed.",
                )
        finally:
            watcher.stop()
            birth_date_override = None
            if phone:
                phone.progress_note = None
                try:
                    phone.close()
                except Exception as exc:
                    if success:
                        # Every creation checkpoint was already verified before
                        # this teardown error; a failed phone.close() must not
                        # downgrade a finished account to a failure.
                        self.log(
                            f"PIPELINE {sid}: created; PC connection close "
                            f"warning: {exc}"
                        )
                        self.status.set(f"{sid}: created; PC connection close warning")
                        self._report_cleanup_error(
                            f"phone.close() after a verified creation for {sid}",
                            exception=exc,
                        )
                    else:
                        # Never erase an already-classified account-specific or
                        # shared failure: the teardown error is only a fallback
                        # reason when nothing more precise was recorded.
                        if (
                            not self._last_pipeline_failure_code
                            and not self._last_pipeline_failure_reason
                            and not self._last_pipeline_failure_shared
                        ):
                            self._last_pipeline_failure_reason = (
                                f"PC connection close: {exc}"
                            )
                        self.log(f"FAILED PIPELINE {sid}: PC connection close: {exc}")
                        self.status.set("Failed — see Log")
                        # Reported separately: the original failure stays intact.
                        self._report_cleanup_error(
                            f"phone.close() after a failed attempt for {sid}",
                            exception=exc,
                        )
                        success = False
            self.phone = None
        return success

    def _dismiss_icloud_sheet_once(self, phone, stack) -> bool:
        """Permit ONE verified Cancel to clear a stuck iCloud sheet.

        Returns True only when the Cancel control was verified from a fresh
        frame and the sheet's dismissal was confirmed.  A Stop, a missing tap
        backend, or an unverifiable sheet returns False (never a blind retry).
        """
        if phone is None or stack is None:
            return False
        try:
            return bool(
                dismiss_icloud_password_sheet(stack, phone, self.log, self.stop)
            )
        except PhoneError as exc:
            self.log(f"iCloud sheet dismissal failed: {exc}")
            return False
        except Exception as exc:
            self.log(f"iCloud sheet dismissal error: {type(exc).__name__}: {exc}")
            return False

    def _record_pipeline_timeout(self, sid: str) -> None:
        """Record the automatic inactivity cancellation for the current attempt."""
        self._last_pipeline_failure_code = BULK_INACTIVITY_TIMEOUT_CODE
        self._last_pipeline_attempt_timed_out = True
        self._last_pipeline_failure_reason = (
            f"No verified creation progress for {int(BULK_INACTIVITY_SECONDS)} "
            "seconds; the attempt was cancelled and will be reset."
        )
        self.log(f"TIMEOUT PIPELINE {sid}: {self._last_pipeline_failure_reason}")
        self.status.set("Timed out — resetting the attempt")

    def _bridge_health_check(self, password: str) -> None:
        """Reconnect to the configured device and take read-only fresh evidence."""
        phone = None
        try:
            phone, stack = self._connect(password)
            phone.exec("printf BRIDGE_OK", timeout=15)
            stack.dump_xml()
            if stack.screenshot_png() is None:
                raise BridgeTransportError("XXTouch fresh screenshot was unavailable.")
        except SSHAuthError:
            raise
        except PhoneError as exc:
            if is_recoverable_bridge_failure(exc):
                raise BridgeTransportError(str(exc)) from exc
            raise
        except Exception as exc:
            if is_recoverable_bridge_failure(exc):
                raise BridgeTransportError(str(exc)) from exc
            raise
        finally:
            if phone is not None:
                try:
                    phone.close()
                except Exception:
                    pass
            self.phone = None

    def _run_pipeline_with_bridge_recovery(self, *args, **kwargs) -> bool:
        """Retry only typed infrastructure loss; each replay starts at reconciliation."""
        # One paid-request budget belongs to the complete outer attempt, not to
        # an individual connection or UIRecoveryAgent.  Reconnects therefore
        # keep spending from the same counter instead of silently receiving six
        # fresh Anthropic requests.
        budget = kwargs.get("recovery_budget")
        if not isinstance(budget, RecoveryAttemptBudget):
            budget = RecoveryAttemptBudget()
            kwargs["recovery_budget"] = budget
        try:
            return self._run_creation_pipeline(*args, **kwargs)
        except BridgeTransportError as original:
            last_error = str(original)

        for attempt, delay in enumerate(RECOVERY_DELAYS_SECONDS, 1):
            if self.stop.wait(delay):
                return False
            next_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            state = dict(self.bulk_state)
            state["recovery"] = {
                "status": "recovering",
                "attempt": attempt,
                "last_error": last_error[:300],
                "next_retry_at_utc": next_at,
            }
            self._persist_bulk(state)
            self.log(
                f"BRIDGE recovery {attempt}/5 after {delay}s: reconnecting the configured iPhone."
            )
            try:
                self._bridge_health_check(str(args[0] if args else self.bulk_password))
                result = self._run_creation_pipeline(*args, **kwargs)
                state = dict(self.bulk_state)
                state["recovery"] = None
                self._persist_bulk(state)
                return result
            except BridgeTransportError as exc:
                last_error = str(exc)
                continue
            except (PhoneError, BulkError):
                raise

        state = dict(self.bulk_state)
        state["recovery"] = {
            "status": "bridge_retries_exhausted",
            "attempt": 5,
            "last_error": last_error[:300],
            "next_retry_at_utc": None,
        }
        self._persist_bulk(state)
        self._last_pipeline_failure_code = "bridge_retries_exhausted"
        self._last_pipeline_failure_reason = (
            "Bridge recovery failed after five attempts; Retry / Resume remains available."
        )
        return False

    def on_stop(self) -> None:
        bulk_state = getattr(self, "bulk_state", {})
        bulk_active = (
            isinstance(bulk_state, dict)
            and bulk_state.get("stage") not in {None, "idle", "stopped"}
        )
        if self.stop.is_set() and not bulk_active:
            return
        if bulk_active:
            self._stop_bulk_batch()
        else:
            self.stop.set()
        ip_cancel_event = getattr(self, "ip_cancel_event", None)
        if ip_cancel_event is not None:
            ip_cancel_event.set()
        status = getattr(self, "status", None)
        if status is not None:
            try:
                status.set("Stopping…")
            except (tk.TclError, AttributeError):
                pass
        for button_name in ("stop_btn", "post_stop_btn"):
            button = getattr(self, button_name, None)
            if button is not None:
                try:
                    button.configure(state="disabled")
                except (tk.TclError, AttributeError):
                    pass
        process = self.qa_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self.log("Stop requested; waiting for the current bounded device call.")

    def on_close(self) -> None:
        self.closing = True
        try:
            diag = getattr(self, "_diag", None)
            if diag is not None:
                diag.unsubscribe(self._on_diag_event)
            crash_remove_sink(self._crash_sink)
        except Exception:
            pass
        self.ip_operation_generation = int(
            getattr(self, "ip_operation_generation", 0)
        ) + 1
        self._save_post_community()
        draft_after_id = getattr(self, "bulk_draft_after_id", None)
        if draft_after_id is not None:
            try:
                self.root.after_cancel(draft_after_id)
            except (tk.TclError, ValueError):
                pass
        self._save_bulk_draft_now()
        try:
            bulk_state = load_bulk_state()
            self.bulk_state = dict(bulk_state)
        except BulkError:
            bulk_state = {}
        owns_run = (
            isinstance(bulk_state, dict)
            and bulk_state.get("writer_id") == getattr(self, "bulk_writer_id", None)
        )
        if (
            isinstance(bulk_state, dict)
            and bulk_state.get("stage") not in {None, "idle", "stopped"}
            and owns_run
        ):
            try:
                self._stop_bulk_batch()
            except (BulkError, OSError):
                self.stop.set()
        else:
            self.stop.set()
        ip_cancel_event = getattr(self, "ip_cancel_event", None)
        if ip_cancel_event is not None:
            ip_cancel_event.set()
        process = self.qa_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if self.phone:
            try:
                self.phone.close()
            except Exception:
                pass
        allow_sleep()
        self.root.destroy()

    def _required_jailbroken_iphone_udid(self) -> str:
        udid = str(getattr(self, "jailbroken_iphone_udid", "") or "").strip()
        if not udid:
            try:
                udid = str(load_secrets().get("jailbroken_iphone_udid") or "").strip()
            except Exception:
                udid = ""
        if not udid:
            raise PhoneError(
                "The jailbroken iPhone X USB identity is not configured; "
                "Creator will not fall back to another connected iPhone."
            )
        return udid

    def _connect(self, password: str) -> tuple[Phone, TapStack]:
        phone = Phone(
            log=self.log,
            requested_serial=self._required_jailbroken_iphone_udid(),
        )
        self.phone = phone
        self.log("USB SSH…")
        # Forward WDA when available so TapStack can use a real XCUITest/XPath
        # hierarchy as a read-only observer. XXTouch remains the tap backend.
        phone.connect(password, also_wda=True)
        self.status.set(f"SSH up  serial={phone.serial}  device_port={phone.ssh_remote_port}")
        stack = TapStack(phone, self.log, cancel_event=self.stop)
        self.log("BACKEND " + stack.attach())
        return phone, stack

    def _run_ghost(
        self, password: str, count: int, anchor_sid: str | None = None
    ) -> None:
        phone = None
        names: list[str] = []
        try:
            phone, stack = self._connect(password)
            if self.stop.is_set():
                raise PhoneError("Stopped by user.")
            requested_names = None
            anchor_index = name_to_index(str(anchor_sid or ""))
            if anchor_index is not None:
                requested_names = [
                    index_to_name(anchor_index + offset)
                    for offset in range(1, count + 1)
                ]
                self.log(
                    f"New Ghost anchored after {anchor_sid}: "
                    + ", ".join(requested_names)
                )
            names = apply_reddit_containers(
                phone, stack, self.log, count=count, stop=self.stop, close=True,
                names=requested_names,
            )
            if self.stop.is_set():
                raise PhoneError("Stopped by user.")
            if names:
                self.refresh_slot_id = names[-1]
            self.status.set("Ghost done: " + ", ".join(names))
        except PhoneError as e:
            if self.stop.is_set():
                self.log("Stopped. Phone remains on its current screen.")
                self.status.set("Stopped")
            else:
                self.log("ERROR  " + str(e))
                self.status.set("Failed — see Log")
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
            self.status.set("Failed — see Log")
        finally:
            if phone:
                try:
                    phone.close()
                except Exception as exc:
                    self.log(f"ERROR  USB shutdown did not complete cleanly: {exc}")
                    self.status.set("Failed — USB cleanup; see Log")
            self.phone = None
            self.root.after(0, self._after_job)

    def _run_email(self, password: str, sid: str) -> None:
        phone = None
        try:
            phone, stack = self._connect(password)
            if self.stop.is_set():
                raise PhoneError("Stopped by user.")
            # HME label: stored email_label (legacy rN) or the padded slot id.
            slot = get_slot(sid) or {}
            label = str(slot.get("email_label") or sid).lower()
            self.log(f"=== Hide My Email for {sid} label={label!r} ===")
            addr = create_one_hide_my_email(
                phone, stack, self.log, stop=self.stop, label=label
            )
            if self.stop.is_set():
                raise PhoneError("Stopped by user.")
            set_slot_email(sid, addr, self.log, label=label)
            self.status.set(f"{sid}: {addr} ({label})")
        except PhoneError as e:
            if self.stop.is_set():
                self.log("Stopped. Phone remains on its current screen.")
                self.status.set("Stopped")
            else:
                self.log("ERROR  " + str(e))
                self.status.set("Failed — see Log")
        except Exception as e:
            self.log(f"ERROR  {type(e).__name__}: {e}")
            self.status.set("Failed — see Log")
        finally:
            if phone:
                try:
                    phone.close()
                except Exception:
                    pass
            self.phone = None
            self.root.after(0, self._after_job)

    def _after_job(self) -> None:
        self.worker = None
        self._busy(False)
        select = str(getattr(self, "refresh_slot_id", "") or "")
        self.refresh_slot_id = ""
        self._refresh_slots(select=select)

    def run(self) -> None:
        self.root.mainloop()


def run_cli(password: str, count: int = 1) -> int:
    def log(msg: str) -> None:
        print(msg, flush=True)

    phone = Phone(log=log)
    try:
        phone.connect(password, also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        apply_reddit_containers(phone, stack, log, count=count)
        return 0
    except PhoneError as e:
        log("ERROR  " + str(e))
        return 1
    finally:
        phone.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--cli" in argv:
        argv.remove("--cli")
        count = 1
        pw = ""
        if "--count" in argv:
            i = argv.index("--count")
            count = int(argv[i + 1])
            argv = [a for j, a in enumerate(argv) if j not in (i, i + 1)]
        if argv:
            pw = argv[0]
        return run_cli(pw, count)
    if not _acquire_creator_single_instance():
        ctypes.windll.user32.MessageBoxW(
            None,
            "Creator is already running. Use the existing Creator window.",
            "Creator already open",
            0x40,
        )
        return 2
    resume_batch = "--resume-batch" in argv
    watchdog_child = "--watchdog-child" in argv
    unknown = set(argv) - {"--resume-batch", "--watchdog-child"}
    if unknown:
        raise SystemExit("Unknown Creator option: " + ", ".join(sorted(unknown)))
    # Process-wide crash capture (idempotent): uncaught main-thread and worker
    # exceptions are written to the crash file and forwarded to the Log tab.
    crash_install()
    app = App()
    if (resume_batch or watchdog_child) and hasattr(app, "nb") and hasattr(app, "log_tab"):
        app.nb.select(app.log_tab)
    if resume_batch:
        app.root.after(0, app.retry_bulk_batch)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
