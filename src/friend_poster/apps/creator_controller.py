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
import uuid
import webbrowser
import ctypes
from datetime import date, datetime, timedelta, timezone
from tkinter import filedialog, messagebox, scrolledtext, ttk

from friend_poster.core.paths import LEGACY_MISC_DIR, PROJECT_ROOT

MISC_DIR = str(LEGACY_MISC_DIR)
PROJECT_DIR = str(PROJECT_ROOT)

from friend_poster.core.bulk_creation import (
    BulkError,
    RECOVERY_DELAYS_SECONDS,
    archive_state as archive_bulk_state,
    consume_completed,
    consume_failed,
    empty_draft as empty_bulk_draft,
    empty_state as empty_bulk_state,
    extend_queue as extend_bulk_queue,
    load_draft as load_bulk_draft,
    load_state as load_bulk_state,
    parse_queue as parse_bulk_queue,
    prepare_retry as prepare_bulk_retry,
    claim_state as claim_bulk_state,
    reconcile_stopped_start,
    save_draft as save_bulk_draft,
    save_state as save_bulk_state,
    save_state_cas as save_bulk_state_cas,
)
from friend_poster.core.config import LOG_DIR, REDDIT_BUNDLE, SECRETS_PATH
from friend_poster.device.ghost_live import apply_reddit_containers
from friend_poster.device.hidemyemail import create_one_hide_my_email
from friend_poster.reddit.reddit_about import BIRTH_YEAR_MAX, BIRTH_YEAR_MIN
from friend_poster.reddit.reddit_create_day1 import (
    EmailReplacementExhausted,
    run_slot as run_create_reddit,
)
from friend_poster.reddit.reddit_post import community_name, run_post
from friend_poster.reddit.reddit_profile_post import run_profile_post
from friend_poster.reddit.reddit_open import recover_existing_container
from friend_poster.core.slots import (
    creation_feed_swipe_progress,
    email_verification_state,
    ensure_ghost_container_needed,
    get_slot,
    ghost_container_state,
    index_to_name,
    load_slots,
    mark_creation_terminal_failure,
    mark_ghost_container_ready_from_crane,
    reddit_cleanup_state,
    replace_slot_email,
    retired_indices,
    retire_slot_id,
    save_slots,
    set_slot_falcon_proxy_ip,
    set_slot_birth_date,
    set_slot_email,
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
from friend_poster.device.ui_tree import TapStack
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
        raise PhoneError(
            f"PIPELINE found {index_to_name(highest)} both active and retired."
        )
    if (
        newest is not None
        and not _pipeline_slot_complete(newest)
        and not slot_creation_terminal_failure(newest)
    ):
        return index_to_name(highest), newest, False
    sid = index_to_name(highest + 1)
    return sid, None, True


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
        self.log_q: queue.Queue[str] = queue.Queue()
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

        self.log_box = scrolledtext.ScrolledText(
            self.log_tab, wrap="word", height=24, font=("Consolas", 10),
            bg="#0c1a30", fg="#dce9fc", insertbackground="#eaf2ff",
            selectbackground="#285783", relief="flat", padx=10, pady=10,
        )
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_box.configure(state="disabled")

        self._refresh_slots()
        self.root.after(200, self._probe_usb_idle)
        self.root.after(120, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
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
        return {
            "version": 1,
            "count_text": str(count_text or len(rows) or 1),
            "births_text": "\n".join(str(row["birth_date"]) for row in rows),
            "swipes_text": "\n".join(str(row["swipes"]) for row in rows),
            "delays_text": "\n".join(str(row["delay_seconds"]) for row in rows),
            "usernames_text": "\n".join(str(row.get("username") or "") for row in rows),
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
        ttk.Label(
            frame,
            text=(
                "The first Items to process rows are used; extra rows stay saved for later. "
                "Valid unique usernames are used exactly; missing, invalid, or repeated rows "
                "use Reddit suggestions. Each item verifies a fresh proxy IP."
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
        headings = ("Birth dates (DD.MM.YYYY)", "Swipe counts", "Delay (2–7s)", "Usernames (optional)")
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
        self.bulk_stop_btn = ttk.Button(actions, text="Stop batch", command=self.stop_bulk_batch, style="Stop.TButton")
        self.bulk_stop_btn.pack(side="left")
        ttk.Label(actions, text="Uses the SSH password entered above.").pack(side="left", padx=12)

        self.bulk_status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.bulk_status_var, wraplength=900).grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(10, 0)
        )
        self.bulk_draft_status_var = tk.StringVar(
            value=self.bulk_draft_reconcile_error or "Draft saved locally"
        )
        ttk.Label(
            frame, textvariable=self.bulk_draft_status_var, wraplength=900,
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(4, 0))
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
        if hasattr(self, "bulk_status_var"):
            self.bulk_status_var.set(
                f"Stage: {stage}  |  Completed: {completed}  |  Remaining: {remaining}  |  "
                f"Failed: {failed}  |  Current: {slot_id}  |  "
                f"Verified IP: {verified}  |  Failure: {failure}"
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
                    "Use Retry / Resume batch."
                )
            elif stage == "stopped":
                self.ip_cycle_status_var.set("Batch stopped. Use Retry / Resume batch.")
            elif resumable:
                self.ip_cycle_status_var.set("Batch interrupted. Use Retry / Resume batch.")
        if hasattr(self, "bulk_start_btn"):
            self.bulk_start_btn.configure(state="disabled" if active or paused else "normal")
        if hasattr(self, "bulk_retry_btn"):
            self.bulk_retry_btn.configure(state="normal" if resumable else "disabled")
        if hasattr(self, "bulk_extend_btn"):
            can_extend = (
                not self.bulk_draft_reconcile_error
                and stage != "idle"
                and remaining > 0
            )
            self.bulk_extend_btn.configure(state="normal" if can_extend else "disabled")
        if hasattr(self, "bulk_quick_resume_btn"):
            self.bulk_quick_resume_btn.configure(state="normal" if resumable else "disabled")
        if hasattr(self, "bulk_stop_btn"):
            self.bulk_stop_btn.configure(state="normal" if active or paused else "disabled")

    def _on_start_bulk_batch(self) -> None:
        try:
            requested, items = parse_bulk_queue(
                self.bulk_count_var.get(),
                self.bulk_births_text.get("1.0", "end-1c"),
                self.bulk_swipes_text.get("1.0", "end-1c"),
                self.bulk_delays_text.get("1.0", "end-1c"),
                parse_slot_birth_date,
                usernames_text=self.bulk_usernames_text.get("1.0", "end-1c"),
            )
            _planned, reconciliation = self._plan_bulk_start(items, requested)
            if reconciliation is not None:
                sid = reconciliation["slot_id"]
                confirmed = messagebox.askokcancel(
                    f"Resume unfinished {sid}?",
                    (
                        f"{sid} is unfinished and will be completed first.\n\n"
                        f"Saved recovery item: birth date {reconciliation['birth_date']}, "
                        f"{reconciliation['swipes']} swipes, "
                        f"{reconciliation['delay_seconds']:g}-second delay.\n"
                        f"New rows after recovery: {requested}.\n"
                        f"Final remaining count: {requested + 1}.\n\n"
                        f"The previous unused tail ({reconciliation['old_tail_count']} row(s)) "
                        "will be replaced after a timestamped local backup is saved."
                    ),
                    parent=self.root,
                )
                if not confirmed:
                    self.log("BATCH start cancelled; stopped queue was not changed.")
                    self.status.set("Batch start cancelled")
                    return
            if not self.start_bulk_batch(items, requested, self.pw.get()):
                raise BulkError("A Creator or batch task is already active.")
        except (BulkError, PhoneError, TypeError, ValueError) as exc:
            self.log(f"BATCH not started: {exc}")
            self.status.set("Batch not started — see Log")

    def _show_post_tab(self) -> None:
        self.nb.select(self.post_tab)
        sid = self._selected_id()
        self._refresh_container_choices(select=sid)

    def log(self, msg: str) -> None:
        self.log_q.put(msg)

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self.log_q.get_nowait()
                if isinstance(msg, tuple) and msg[:1] == ("__gen_done__",):
                    self._gen_done(msg[1] if len(msg) > 1 else "")
                    continue
                stamp = datetime.now().strftime("%H:%M:%S")
                self.log_box.configure(state="normal")
                self.log_box.insert("end", f"{stamp}  {msg}\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
                try:
                    with open(CREATOR_LOG_PATH, "a", encoding="utf-8") as f:
                        f.write(f"{stamp}  {msg}\n")
                except OSError:
                    pass
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

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
            # Keep their isolated persistence seam while every real panel uses CAS.
            if not hasattr(self, "bulk_writer_id"):
                if getattr(save_bulk_state, "__module__", "") != "bulk_creation":
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
                allow_writer_takeover=allow_from_stopped,
            )
            self.bulk_state = dict(saved)
        try:
            self.root.after(0, self._sync_row_buttons)
        except (tk.TclError, AttributeError):
            pass

    def _check_bulk_resume_compatibility(self, state: dict) -> None:
        if not state.get("queue") or int(state.get("remaining") or 0) < 1:
            raise BulkError("The batch has no queued item to process.")
        item = state["queue"][0]
        expected_birth = str(item["birth_date"])
        slot = get_slot(str(state.get("slot_id") or "")) if state.get("slot_id") else None
        if slot is None:
            _sid, slot, _is_new = _resolve_creation_pipeline_target(
                load_slots(), retired_indices()
            )
        saved_birth = str((slot or {}).get("reddit_birth_date") or "").strip()
        if saved_birth and saved_birth != expected_birth:
            raise BulkError("The next unfinished slot birth date does not match the queue head.")
        self._assert_bulk_requested_username(str((slot or {}).get("id") or state.get("slot_id") or ""), item, slot)

    @staticmethod
    def _assert_bulk_requested_username(
        sid: str, entry: dict, slot: dict | None, *, require_saved: bool = False,
    ) -> None:
        requested = str(entry.get("username") or "").strip()
        saved = str((slot or {}).get("username") or "").strip()
        if requested and ((saved and saved != requested) or (require_saved and saved != requested)):
            raise BulkError(
                f"{sid or 'Current slot'} has saved username {saved!r}, but this batch "
                f"requests {requested!r}. Did not reuse the slot or change its username."
            )

    def _plan_bulk_start(
        self, items: list[dict], requested: int
    ) -> tuple[dict, dict | None]:
        """Build a start state without writing it, including stopped-head recovery."""
        current = dict(getattr(self, "bulk_state", empty_bulk_state()))
        sid = str(current.get("slot_id") or "")
        if (
            current.get("stage") == "stopped"
            and int(current.get("remaining") or 0) > 0
            and sid
        ):
            slot = get_slot(sid)
            if slot is None:
                raise BulkError(
                    f"Stopped batch points to {sid}, but that slot is missing. "
                    "No queue data was changed."
                )
            saved_birth = str(slot.get("reddit_birth_date") or "").strip()
            state = reconcile_stopped_start(
                current,
                items,
                int(requested),
                slot_id=sid,
                saved_birth=saved_birth,
            )
            head = state["queue"][0]
            return state, {
                "slot_id": sid,
                "birth_date": head["birth_date"],
                "swipes": int(head["swipes"]),
                "delay_seconds": float(head["delay_seconds"]),
                "old_tail_count": max(
                    0,
                    min(
                        int(current.get("remaining") or 0),
                        len(current.get("queue") or []),
                    ) - 1,
                ),
            }

        state = empty_bulk_state()
        state.update(
            queue=[dict(item) for item in items],
            requested=int(requested),
            remaining=int(requested),
            stage="ip_pending",
        )
        self._check_bulk_resume_compatibility(state)
        return state, None

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
            additions = editor_rows[len(baseline):]
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

    def start_bulk_batch(self, items: list[dict], requested: int, password: str) -> bool:
        """Persist and asynchronously start an IP-first local batch."""
        current = getattr(self, "bulk_state", empty_bulk_state())
        if (
            getattr(self, "worker", None) and self.worker.is_alive()
            or getattr(self, "ip_reset_busy", False)
            or getattr(self, "pending_ip_action", None)
            or current.get("stage") not in {"idle", "stopped"}
        ):
            return False
        state, reconciliation = self._plan_bulk_start(items, requested)
        self.bulk_password = str(password or "")
        archive_path = None
        if reconciliation is not None:
            archive_path = archive_bulk_state(current)
        self._persist_bulk(state, allow_from_stopped=True)
        # Preserve the raw editor draft, including surplus rows that were not
        # scheduled in this run. Only the requested prefix belongs to the
        # durable execution queue.
        self.bulk_editor_baseline = [dict(row) for row in state.get("queue") or []]
        self._save_bulk_draft_now()
        if reconciliation is not None:
            self.log(
                f"BATCH reconciled: {reconciliation['slot_id']} recovery head + "
                f"{requested} new item(s); previous stopped state archived as "
                f"{os.path.basename(str(archive_path))}."
            )
        self.log(
            f"BATCH started: {state['remaining']} item(s), "
            "IP verification before each new item."
        )
        self._start_bulk_worker()
        return True

    def retry_bulk_batch(self, password: str | None = None) -> bool:
        if getattr(self, "worker", None) and self.worker.is_alive():
            return False
        try:
            if hasattr(self, "bulk_writer_id"):
                current = dict(load_bulk_state())
                self.bulk_state = dict(current)
            else:
                current = dict(self.bulk_state)
            stage = current.get("stage")
            if stage == "paused":
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
        except BulkError as exc:
            self.log(f"BATCH retry unavailable: {exc}")
            return False
        if password is not None:
            self.bulk_password = str(password)
        elif hasattr(self, "pw"):
            self.bulk_password = str(self.pw.get() or "")
        self.log(f"BATCH retrying {state.get('failure_phase') or 'saved'} stage.")
        self._start_bulk_worker()
        return True

    def stop_bulk_batch(self) -> None:
        self._stop_bulk_batch()

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
        latest = load_bulk_state()
        writer_id = str(getattr(self, "bulk_writer_id", "") or "")
        if latest.get("writer_id") not in (None, writer_id):
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
        try:
            self.status.set(f"Batch paused — {phase}; use Retry / Resume")
        except (tk.TclError, AttributeError):
            pass

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
            if not self._refresh_macrodroid_setup():
                self._pause_bulk("ip", self.macrodroid_config_error)
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
                slot = get_slot(sid)
                self._assert_bulk_requested_username(sid, entry, slot)
                if state.get("stage") in {"creating", "retry_creation"} and _pipeline_slot_complete(slot):
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
                if state.get("stage") in {"ip_pending", "may_have_triggered", "retry_ip"}:
                    result, observations = self._run_bulk_ip_check(state)
                    if self.stop.is_set():
                        return
                    if result.get("outcome") != "verified":
                        self._pause_bulk("ip", str(result.get("error") or "IP verification failed."))
                        return
                    try:
                        confirmed_at = self._record_bulk_ip(result, observations)
                        set_slot_falcon_proxy_ip(sid, str(result["new_ip"]), confirmed_at)
                    except (HistoryError, ValueError, OSError, TypeError) as exc:
                        self._pause_bulk("ip", f"Verified IP could not be persisted: {exc}")
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
                        self._pause_bulk("creation", "Verified proxy IP could not be reconfirmed.")
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
                    resume_failure_code=resume_failure_code,
                    manage_worker=False,
                )
                if self.stop.is_set():
                    return
                if not success or not _pipeline_slot_complete(get_slot(sid)):
                    reason = str(
                        getattr(self, "_last_pipeline_failure_reason", "") or
                        "Creator pipeline did not reach all completion checkpoints."
                    )
                    failure_code = str(
                        getattr(self, "_last_pipeline_failure_code", "") or ""
                    )
                    if failure_code == EmailReplacementExhausted.failure_code:
                        mark_creation_terminal_failure(
                            sid, failure_code, reason, self.log
                        )
                        ready = datetime.now(timezone.utc) + timedelta(
                            seconds=float(entry["delay_seconds"])
                        )
                        self._persist_bulk(
                            consume_failed(
                                dict(self.bulk_state),
                                sid,
                                code=failure_code,
                                reason=reason,
                                ready_at_utc=ready.isoformat().replace("+00:00", "Z"),
                            )
                        )
                        self.log(
                            f"BATCH {sid}: terminal email-verification failure recorded; "
                            "the saved queue will continue with a fresh proxy IP."
                        )
                        if self.bulk_state.get("stage") == "idle":
                            failures = len(self.bulk_state.get("failures") or [])
                            self.log(
                                f"BATCH complete with failures: {failures} slot(s) failed "
                                "after exhausting replacement aliases."
                            )
                            status = getattr(self, "status", None)
                            if status is not None:
                                status.set("Batch complete with failures")
                            return
                        continue
                    if failure_code == "bridge_retries_exhausted":
                        self._pause_bulk("creation", reason, code=failure_code)
                        return
                    if failure_code:
                        self._pause_bulk("creation", reason, code=failure_code)
                    else:
                        self._pause_bulk("creation", reason)
                    return
                self._assert_bulk_requested_username(sid, entry, get_slot(sid), require_saved=True)

                ready = datetime.now(timezone.utc) + timedelta(seconds=float(entry["delay_seconds"]))
                self._persist_bulk(consume_completed(
                    dict(self.bulk_state), sid,
                    ready_at_utc=ready.isoformat().replace("+00:00", "Z"),
                ))
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
        except (BulkError, HistoryError, IpChangeError, PhoneError, OSError, TypeError, ValueError) as exc:
            if not self.stop.is_set():
                phase = "creation" if self.bulk_state.get("stage") in {"creating", "retry_creation"} else "ip"
                self._pause_bulk(phase, str(exc))
        except Exception as exc:
            if not self.stop.is_set():
                phase = "creation" if self.bulk_state.get("stage") in {"creating", "retry_creation"} else "ip"
                self._pause_bulk(phase, f"Unexpected local failure ({type(exc).__name__}).")
        finally:
            try:
                self.root.after(0, self._after_job)
            except (tk.TclError, AttributeError):
                pass

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
            # Queue only — _drain_log (main thread) writes the widget.
            # Never touch Tk from the worker: after() there raises or
            # silently drops, which greyed Generate forever.
            try:
                self.log_q.put(line)
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
                    self.log_q.put("Generate ERROR  %s: %s"
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
                stop=self.stop,
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

    def _run_creation_pipeline(
        self,
        password: str,
        sid: str,
        birth_date_override: date | None,
        feed_swipes: int,
        feed_pause_count: int = 0,
        *,
        requested_username: str = "",
        resume_failure_code: str = "",
        manage_worker: bool = True,
    ) -> bool:
        """Run all three existing stages through one iPhone connection."""
        phone = None
        success = False
        self._last_pipeline_failure_reason = None
        self._last_pipeline_failure_code = None

        def require_not_stopped() -> None:
            if self.stop.is_set():
                raise PhoneError("Stopped by user.")

        try:
            self.log(f"PIPELINE {sid}: IN_PROGRESS")
            require_not_stopped()

            slot = get_slot(sid)
            if slot is None:
                ensure_ghost_container_needed(sid, self.log)
                slot = get_slot(sid)
            if slot is None:
                raise PhoneError(f"PIPELINE {sid} checkpoint was not persisted.")
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
            require_not_stopped()

            slot = get_slot(sid)
            container_ready = bool(
                slot
                and slot.get("ghost")
                and ghost_container_state(slot) == "ready"
                and str(slot.get("ghost_container_instance") or "").strip()
            )
            if container_ready:
                self.log(f"CONTAINER {sid}: READY (skipped)")
            else:
                self.log(f"CONTAINER {sid}: IN_PROGRESS")
                apply_reddit_containers(
                    phone,
                    stack,
                    self.log,
                    count=1,
                    stop=self.stop,
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
                    raise PhoneError(
                        f"CONTAINER {sid} did not reach its verified ready checkpoint."
                    )
                self.log(f"CONTAINER {sid}: READY")
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
                        stop=self.stop,
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
            reddit_kwargs = dict(
                birth_date_override=birth_date_override,
                log_fn=self.log,
                stop=self.stop,
                feed_swipes=feed_swipes,
                feed_pause_count=feed_pause_count,
                email_replacer=(
                    lambda old, index, label: self._replace_verification_email(
                        sid, phone, stack, old, index, label
                    )
                ),
                rotate_before_poll=(resume_failure_code == "reddit_code_timeout"),
            )
            if requested_username:
                reddit_kwargs["requested_username"] = requested_username
            run_create_reddit(sid, phone, stack, **reddit_kwargs)
            require_not_stopped()

            slot = get_slot(sid)
            if not slot:
                raise PhoneError(f"REDDIT {sid} slot checkpoint disappeared.")
            target, completed = creation_feed_swipe_progress(slot)
            if not (
                str(slot.get("username") or "").strip()
                and (not requested_username or slot.get("username") == requested_username)
                and str(slot.get("password") or "").strip()
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
            self.log(f"COMPLETED PIPELINE {sid}")
            self.status.set(
                f"{sid}: container, email, Reddit, swipes, and cleanup complete"
            )
            success = True
        except EmailReplacementExhausted as exc:
            self._last_pipeline_failure_code = exc.failure_code
            self._last_pipeline_failure_reason = str(exc)
            self.log(f"FAILED PIPELINE {sid}: {exc}")
            self.status.set("Failed — replacement email attempts exhausted")
        except PhoneError as exc:
            if is_recoverable_bridge_failure(exc) and not self.stop.is_set():
                raise BridgeTransportError(str(exc)) from exc
            if self.stop.is_set():
                self.log(f"CANCELLED PIPELINE {sid}: current checkpoints preserved.")
                self.status.set("Stopped")
            else:
                self._last_pipeline_failure_reason = str(exc)
                self.log(f"FAILED PIPELINE {sid}: {exc}")
                self.status.set("Failed — see Log")
        except Exception as exc:
            if is_recoverable_bridge_failure(exc) and not self.stop.is_set():
                raise BridgeTransportError(str(exc)) from exc
            self._last_pipeline_failure_reason = f"{type(exc).__name__}: {exc}"
            self.log(f"FAILED PIPELINE {sid}: {type(exc).__name__}: {exc}")
            self.status.set("Failed — see Log")
        finally:
            birth_date_override = None
            if phone:
                try:
                    phone.close()
                except Exception as exc:
                    self._last_pipeline_failure_code = None
                    self._last_pipeline_failure_reason = f"PC connection close: {exc}"
                    self.log(f"FAILED PIPELINE {sid}: PC connection close: {exc}")
                    self.status.set("Failed — see Log")
                    success = False
            self.phone = None
            if manage_worker:
                self.root.after(0, self._after_job)
        return success

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
        phone.connect(password, also_wda=False)
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
    app = App()
    if (resume_batch or watchdog_child) and hasattr(app, "nb") and hasattr(app, "log_tab"):
        app.nb.select(app.log_tab)
    if resume_batch:
        app.root.after(0, app.retry_bulk_batch)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
