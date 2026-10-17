# UTF-8
"""Purge iCloud Hide My Email aliases outside an explicitly preserved slot range.

This is intentionally separate from the address-creation module.  Every
destructive tap is preceded by fresh UI evidence for the exact label and email.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass

from friend_poster.device.hidemyemail import EMAIL_RE, _icloud_main_hme_row
from friend_poster.device.ssh_device import Phone, PhoneError
from friend_poster.device.ui_tree import (
    Node,
    TapStack,
    collapsed_text,
    dump_named,
    find_label_exact,
)

KEEP_FIRST = 24
KEEP_LAST = 64
_SLOT_LABEL_RE = re.compile(r"^(?:a|r)?0*(\d{1,4})(?:-\d+)?$", re.I)
_CURSOR_EMAIL_RE = re.compile(r"[|¦](?=@icloud\.com)", re.I)


@dataclass(frozen=True)
class AliasRow:
    label: str
    email: str
    node: Node


def slot_index(label: str) -> int | None:
    compact = "".join(str(label or "").split())
    match = _SLOT_LABEL_RE.fullmatch(compact)
    return int(match.group(1)) if match else None


def keep_label(label: str, first: int = KEEP_FIRST, last: int = KEEP_LAST) -> bool:
    index = slot_index(label)
    return index is not None and first <= index <= last


def _email(text: str) -> str | None:
    normalized = _CURSOR_EMAIL_RE.sub("", str(text or ""))
    match = EMAIL_RE.search(normalized)
    return match.group(0).lower() if match else None


def visible_alias_rows(nodes: list[Node]) -> list[AliasRow]:
    """Pair each visible list email with the nearest title immediately above."""
    visible = [n for n in nodes if n.ntype != "ocr-line" and n.on_screen()]
    rows: list[AliasRow] = []
    for email_node in visible:
        address = _email(email_node.labels())
        if address is None or "from settings" not in email_node.labels().casefold():
            continue
        ey = email_node.center()[1]
        candidates = [
            n
            for n in visible
            if n is not email_node
            and n.x < 360
            and n.y > 180
            and 12 <= ey - n.center()[1] <= 70
            and _email(n.labels()) is None
            and collapsed_text(n.labels()) not in {"hide my email", "inactive addresses"}
        ]
        if not candidates:
            continue
        title = min(candidates, key=lambda n: ey - n.center()[1])
        label = collapsed_text(title.labels()).strip()
        if label and all(row.email != address for row in rows):
            rows.append(AliasRow(label=label, email=address, node=title))
    return sorted(rows, key=lambda row: row.node.y)


def _active_list(nodes: list[Node]) -> bool:
    title = find_label_exact(nodes, ("Hide My Email",))
    search = find_label_exact(nodes, ("Search", "Q Search"))
    inactive = find_label_exact(nodes, ("Inactive Addresses", "Inactive addresses"))
    list_evidence = bool(search and search.y < 230) or bool(inactive) or bool(
        visible_alias_rows(nodes)
    )
    return bool(title and title.y < 140 and list_evidence)


def _inactive_list(nodes: list[Node]) -> bool:
    title = find_label_exact(nodes, ("Inactive Addresses", "Inactive addresses"))
    return bool(title and title.y < 140)


def _detail_labels(nodes: list[Node]) -> list[str]:
    """Return label evidence from a Hide My Email detail screen.

    The centered navigation title is sometimes OCRed incorrectly.  The value in
    the explicit Label row is independent evidence and is usually more reliable.
    """
    labels: list[str] = []
    for node in nodes:
        if not node.on_screen():
            continue
        is_title = node.y < 140 and node.x > 180
        is_label_value = 540 <= node.y <= 680 and node.x > 300
        if not (is_title or is_label_value):
            continue
        value = collapsed_text(node.labels()).strip().rstrip(">").strip()
        if value and value.casefold() not in {item.casefold() for item in labels}:
            labels.append(value)
    return labels


def _detail_label_values(nodes: list[Node]) -> list[str]:
    """Return only values from the explicit Label row on a detail screen."""
    values: list[str] = []
    for node in nodes:
        if not node.on_screen() or not (540 <= node.y <= 680 and node.x > 300):
            continue
        value = collapsed_text(node.labels()).strip().rstrip(">").strip()
        if value and value.casefold() not in {item.casefold() for item in values}:
            values.append(value)
    return values


class Purger:
    def __init__(self, phone: Phone, stack: TapStack, log=print):
        self.phone = phone
        self.stack = stack
        self.log = log
        self.sequence = 0
        self.deactivated = 0
        self.deleted = 0
        self.failed_active: set[str] = set()
        self.failed_inactive: set[str] = set()
        self.protected_active: set[str] = set()
        self.protected_inactive: set[str] = set()

    def dump(self, note: str) -> list[Node]:
        self.sequence += 1
        _blob, nodes = dump_named(
            self.stack,
            self.phone,
            f"hme_purge_{self.sequence:04d}_{note}",
            self.log,
        )
        return nodes

    def _tap(self, node: Node) -> None:
        self.stack.tap_xy(*node.center())

    def _back(self, nodes: list[Node]) -> None:
        backs = [
            n
            for n in nodes
            if n.on_screen()
            and n.x < 180
            and n.y < 180
            and "back" in collapsed_text(n.labels())
        ]
        if not backs:
            raise PhoneError("A verified top-left Back control was unavailable.")
        self._tap(min(backs, key=lambda n: n.x))
        time.sleep(1.0)

    def ensure_active_list(self) -> list[Node]:
        nodes = self.dump("ensure_active")
        for _ in range(4):
            if _active_list(nodes):
                return nodes
            overview_row = _icloud_main_hme_row(nodes)
            if overview_row is not None:
                self._tap(overview_row)
                time.sleep(1.2)
                nodes = self.dump("after_hme_row")
                continue
            if find_label_exact(nodes, ("Deactivate Email Address?",)):
                cancel = find_label_exact(nodes, ("Cancel",))
                if cancel is None:
                    raise PhoneError("Deactivation prompt has no exact Cancel control.")
                self._tap(cancel)
                time.sleep(0.8)
                nodes = self.dump("after_prompt_cancel")
                continue
            if _inactive_list(nodes):
                self._back(nodes)
            elif find_label_exact(nodes, ("Deactivate Email Address", "Delete Address")):
                self._back(nodes)
            else:
                raise PhoneError("Could not prove the active Hide My Email list.")
            nodes = self.dump("after_back")
        raise PhoneError("Could not return to the active Hide My Email list.")

    def deactivate(self, row: AliasRow) -> list[Node]:
        self.log(f"HME PURGE deactivate {row.label!r}")
        self.stack.tap_row(row.node)
        time.sleep(1.0)
        nodes = self.dump("active_detail")
        detail_labels = _detail_labels(nodes)
        detail_values = _detail_label_values(nodes)
        expected = collapsed_text(row.label)
        protected = [label for label in detail_labels if keep_label(label)]
        if protected:
            actual = protected[0]
            self.log(
                f"HME PURGE preserve {actual!r}; list OCR read it as "
                f"{row.label!r}."
            )
            self.protected_active.add(row.email)
            self._back(nodes)
            time.sleep(1.0)
            active = self.dump("after_protected_back")
            if not _active_list(active):
                raise PhoneError("Protected alias did not return to the active list.")
            return active
        if not detail_values and not any(
            label.casefold() == expected.casefold() for label in detail_labels
        ):
            raise PhoneError(f"Alias detail did not prove exact label {row.label!r}.")
        button = find_label_exact(nodes, ("Deactivate Email Address",))
        if button is None:
            raise PhoneError(f"Exact Deactivate Email Address control missing for {row.label!r}.")
        self._tap(button)
        time.sleep(0.8)
        nodes = self.dump("deactivate_prompt")
        prompt = find_label_exact(nodes, ("Deactivate Email Address?",))
        choices = [n for n in nodes if n.on_screen() and collapsed_text(n.labels()) == "deactivate"]
        if prompt is None or len(choices) != 1:
            raise PhoneError(f"Exact deactivation confirmation missing for {row.label!r}.")
        self._tap(choices[0])
        time.sleep(7.0)
        nodes = self.dump("after_deactivate")
        failure = find_label_exact(nodes, ("Stop email failed",))
        if failure is not None:
            ok = find_label_exact(nodes, ("OK",))
            if ok is None:
                raise PhoneError("Apple rejected deactivation and the exact OK control is missing.")
            self._tap(ok)
            time.sleep(0.8)
            detail = self.dump("deactivate_failed")
            self._back(detail)
            time.sleep(1.0)
            self.failed_active.add(row.email)
            self.log(f"HME PURGE Apple rejected active alias {row.label!r}; skipped.")
            nodes = self.dump("after_failed_back")
            if not _active_list(nodes):
                raise PhoneError("Rejected deactivation did not return to the active list.")
            return nodes
        if not _active_list(nodes):
            time.sleep(2.0)
            nodes = self.dump("after_deactivate_settled")
        if not _active_list(nodes):
            raise PhoneError(f"Deactivation of {row.label!r} did not return to the active list.")
        if any(alias.email == row.email for alias in visible_alias_rows(nodes)):
            raise PhoneError(f"Deactivated alias {row.label!r} is still visible in the active list.")
        self.deactivated += 1
        self.log(f"HME PURGE deactivated {row.label!r} ({self.deactivated} total)")
        return nodes

    def purge_active(self) -> list[Node]:
        nodes = self.ensure_active_list()
        last_signature: tuple[str, ...] | None = None
        stuck = 0
        for _ in range(500):
            rows = visible_alias_rows(nodes)
            candidate = next(
                (
                    row for row in rows
                    if not keep_label(row.label)
                    and row.email not in self.failed_active
                    and row.email not in self.protected_active
                ),
                None,
            )
            if candidate is not None:
                nodes = self.deactivate(candidate)
                continue
            inactive = find_label_exact(nodes, ("Inactive Addresses", "Inactive addresses"))
            if inactive is not None:
                return nodes
            signature = tuple(row.email for row in rows)
            stuck = stuck + 1 if signature == last_signature else 0
            if stuck >= 3:
                raise PhoneError("Active alias list stopped moving before Inactive Addresses.")
            last_signature = signature
            self.stack.interp_flick(375, 1080, 375, 280, steps=14, ms=16)
            time.sleep(0.35)
            nodes = self.dump("active_scroll")
        raise PhoneError("Active alias purge exceeded its bounded iteration limit.")

    def enter_inactive(self, nodes: list[Node] | None = None) -> list[Node]:
        if nodes is None or not _active_list(nodes):
            nodes = self.ensure_active_list()
        inactive = find_label_exact(nodes, ("Inactive Addresses", "Inactive addresses"))
        for _ in range(22):
            if inactive is not None:
                break
            self.stack.interp_flick(375, 1080, 375, 260, steps=12, ms=14)
            time.sleep(0.18)
            nodes = self.dump("find_inactive")
            inactive = find_label_exact(nodes, ("Inactive Addresses", "Inactive addresses"))
        if inactive is None:
            raise PhoneError("Inactive Addresses was not found at the active-list bottom.")
        self.stack.tap_row(inactive)
        time.sleep(1.0)
        nodes = self.dump("inactive_list")
        if not _inactive_list(nodes):
            raise PhoneError("Inactive Addresses did not open its proven list.")
        return nodes

    def delete_inactive(self, row: AliasRow) -> None:
        self.log(f"HME PURGE permanently delete {row.label!r}")
        self.stack.tap_row(row.node)
        time.sleep(1.0)
        nodes = self.dump("inactive_detail")
        # Settings occasionally yields a split transition frame where both the
        # outgoing list and incoming controls are visible.  Always verify a
        # second, settled snapshot rather than trusting that mixed frame.
        time.sleep(2.0)
        nodes = self.dump("inactive_detail_settled")
        if _inactive_list(nodes):
            same_rows = [item for item in visible_alias_rows(nodes) if item.email == row.email]
            if len(same_rows) != 1:
                raise PhoneError(
                    f"Inactive row tap for {row.label!r} missed and the exact address "
                    "was not uniquely visible for one safe retry."
                )
            self.stack.tap_xy(650, same_rows[0].node.center()[1])
            time.sleep(3.0)
            nodes = self.dump("inactive_detail_retry")
        detail_labels = _detail_labels(nodes)
        detail_values = _detail_label_values(nodes)
        expected = collapsed_text(row.label)
        protected = [label for label in detail_labels if keep_label(label)]
        if protected:
            actual = protected[0]
            self.log(
                f"HME PURGE preserve inactive {actual!r}; list OCR read it as "
                f"{row.label!r}."
            )
            self.protected_inactive.add(row.email)
            self._back(nodes)
            time.sleep(1.0)
            return
        if not detail_values and not any(
            label.casefold() == expected.casefold() for label in detail_labels
        ):
            raise PhoneError(f"Inactive detail did not prove exact label {row.label!r}.")
        if find_label_exact(nodes, ("Reactivate Address",)) is None:
            raise PhoneError(f"{row.label!r} was not proven inactive; refused Delete Address.")
        button = find_label_exact(nodes, ("Delete Address",))
        if button is None:
            raise PhoneError(f"Exact Delete Address control missing for {row.label!r}.")
        self._tap(button)
        time.sleep(0.8)
        nodes = self.dump("delete_prompt")
        prompt = find_label_exact(nodes, ("Delete Email Address?",))
        choices = [n for n in nodes if n.on_screen() and collapsed_text(n.labels()) == "delete"]
        if prompt is None or len(choices) != 1:
            raise PhoneError(f"Exact permanent-delete confirmation missing for {row.label!r}.")
        self._tap(choices[0])
        time.sleep(7.0)
        nodes = self.dump("after_delete")
        if find_label_exact(nodes, ("Delete email failed", "Delete address failed")):
            raise PhoneError(f"Apple rejected permanent deletion of {row.label!r}.")
        if not _active_list(nodes):
            time.sleep(2.0)
            nodes = self.dump("after_delete_settled")
        if not _active_list(nodes):
            raise PhoneError(f"Permanent deletion of {row.label!r} did not return safely.")
        self.deleted += 1
        self.log(f"HME PURGE permanently deleted {row.label!r} ({self.deleted} total)")

    def purge_inactive(self, nodes: list[Node]) -> None:
        last_signature: tuple[str, ...] | None = None
        stuck = 0
        for _ in range(500):
            rows = visible_alias_rows(nodes)
            candidate = next(
                (
                    row for row in rows
                    if not keep_label(row.label)
                    and row.email not in self.failed_inactive
                    and row.email not in self.protected_inactive
                ),
                None,
            )
            if candidate is not None:
                self.delete_inactive(candidate)
                nodes = self.enter_inactive()
                last_signature = None
                stuck = 0
                continue
            signature = tuple(row.email for row in rows)
            stuck = stuck + 1 if signature == last_signature else 0
            if stuck >= 3:
                return
            last_signature = signature
            self.stack.interp_flick(375, 1080, 375, 280, steps=14, ms=16)
            time.sleep(0.35)
            nodes = self.dump("inactive_scroll")
            if not _inactive_list(nodes):
                raise PhoneError("Inactive alias list disappeared while scanning.")
        raise PhoneError("Inactive alias purge exceeded its bounded iteration limit.")

    def run(self) -> tuple[int, int, int]:
        bottom = self.purge_active()
        inactive = self.enter_inactive(bottom)
        self.purge_inactive(inactive)
        return self.deactivated, self.deleted, len(self.failed_active)


def main() -> int:
    def safe_log(message: str) -> None:
        safe = EMAIL_RE.sub("<icloud-email>", str(message))
        try:
            print(safe, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            print(safe.encode(encoding, "backslashreplace").decode(encoding), flush=True)

    phone = Phone(log=safe_log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, safe_log)
        safe_log("BACKEND " + stack.attach())
        deactivated, deleted, failed = Purger(phone, stack, safe_log).run()
        safe_log(
            f"HME_PURGE_DONE deactivated={deactivated} permanently_deleted={deleted} "
            f"apple_rejected={failed} keep=a0024..a0064"
        )
        return 0 if failed == 0 else 2
    except PhoneError as exc:
        safe_log(f"HME_PURGE_STOPPED {exc}")
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
