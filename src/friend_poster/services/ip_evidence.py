"""Shared, transport-independent rules for FalconProxy confirmation evidence."""

from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import datetime, timezone

CONFIRMATION_MAX_AGE = 90.0
TRANSIENT_ERRORS = frozenset({"timeout", "connection"})
IP_CHECK_TARGET = "https://api64.ipify.org?format=json"


def utc_stamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not value.endswith("Z") or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise ValueError("IP evidence timestamp must be UTC.")
    return stamp


def validate_pair(result: dict, *, require_change: bool = True) -> tuple[dict, dict, dict]:
    """Validate actual addresses, distinct checks and freshness, including legacy baselines."""
    try:
        if result.get("outcome") != "verified":
            raise ValueError()
        old = ipaddress.ip_address(result["old_ip"])
        new = ipaddress.ip_address(result["new_ip"])
        if old.version != new.version or (old != new) != require_change:
            raise ValueError()
        proof = result["verification_evidence"]
        baseline = proof["baseline"]
        first, second = proof["confirmations"]
        op = baseline["operation_id"]
        if not isinstance(op, str) or not op or baseline["phase"] != "baseline" or baseline["ip"] != str(old):
            raise ValueError()
        if result.get("operation_id", op) != op:
            raise ValueError()
        seq = baseline["check_sequence"]
        if type(seq) is not int or seq < 0:
            raise ValueError()
        stamp = baseline.get("observed_at_utc")
        if stamp is None:
            if baseline.get("provenance") not in {"legacy_saved_baseline", "saved_comparison"}:
                raise ValueError()
        else:
            utc_stamp(stamp)
        floor = proof.get("confirmation_floor", seq)
        if type(floor) is not int or floor < seq:
            raise ValueError()
        for item in (first, second):
            if (item["operation_id"] != op or item["phase"] != "observation"
                    or item["ip"] != str(new) or type(item["check_sequence"]) is not int
                    or item["check_sequence"] <= max(seq, floor)):
                raise ValueError()
            seq = item["check_sequence"]
            utc_stamp(item["observed_at_utc"])
        gap = (utc_stamp(second["observed_at_utc"]) - utc_stamp(first["observed_at_utc"])).total_seconds()
        if not 0 <= gap <= CONFIRMATION_MAX_AGE:
            raise ValueError()
        if stamp is not None and utc_stamp(stamp) > utc_stamp(first["observed_at_utc"]):
            raise ValueError()
        return baseline, first, second
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("IP confirmation evidence is missing, inconsistent, or expired.") from None


def validate_attempt(attempt: dict, *, require_scope: bool = True, require_change: bool = True) -> None:
    """Persist only known, credential-free checkpoint fields."""
    fields = {"operation_id", "proxy_scope", "verification_target", "baseline", "check_sequence",
              "candidate", "confirmation_floor", "verification_evidence", "observation_deadline_utc"}
    try:
        if not isinstance(attempt, dict) or set(attempt) != fields:
            raise ValueError()
        uuid.UUID(attempt["operation_id"])
        scope = attempt["proxy_scope"]
        if not (scope is None and not require_scope) and (not isinstance(scope, str) or not re.fullmatch(r"[a-f0-9]{64}", scope)):
            raise ValueError()
        if attempt["verification_target"] != IP_CHECK_TARGET:
            raise ValueError()
        utc_stamp(attempt["observation_deadline_utc"])
        seq, floor = attempt["check_sequence"], attempt["confirmation_floor"]
        if type(seq) is not int or type(floor) is not int or not 0 <= floor <= seq:
            raise ValueError()
        proof = attempt["verification_evidence"]
        items = [(attempt["baseline"], "baseline"), (attempt["candidate"], "observation")]
        if proof is not None:
            if (set(proof) != {"baseline", "confirmations", "confirmation_floor"}
                    or proof["baseline"] != attempt["baseline"] or proof["confirmation_floor"] != floor):
                raise ValueError()
            items.extend((item, "observation") for item in proof["confirmations"])
            validate_pair({"outcome": "verified", "old_ip": proof["baseline"]["ip"],
                           "new_ip": proof["confirmations"][-1]["ip"], "verification_evidence": proof}, require_change=require_change)
        for item, phase in items:
            if item is None:
                continue
            base_fields = {"operation_id", "check_sequence", "phase", "ip", "observed_at_utc"}
            if not isinstance(item, dict) or set(item) not in (base_fields, base_fields | {"provenance"}):
                raise ValueError()
            if item["operation_id"] != attempt["operation_id"] or item["phase"] != phase:
                raise ValueError()
            if str(ipaddress.ip_address(item["ip"])) != item["ip"] or type(item["check_sequence"]) is not int or not 0 <= item["check_sequence"] <= seq:
                raise ValueError()
            provenance = "legacy_saved_baseline" if require_change else "saved_comparison"
            if item["observed_at_utc"] is None:
                if phase != "baseline" or item.get("provenance") != provenance:
                    raise ValueError()
            else:
                utc_stamp(item["observed_at_utc"])
            if "provenance" in item and (phase != "baseline" or item["provenance"] != provenance):
                raise ValueError()
        baseline, candidate = attempt["baseline"], attempt["candidate"]
        if baseline and baseline["check_sequence"] > floor:
            raise ValueError()
        if candidate and (candidate["check_sequence"] <= floor or (baseline and (
                (candidate["ip"] != baseline["ip"]) != require_change or ipaddress.ip_address(candidate["ip"]).version != ipaddress.ip_address(baseline["ip"]).version))):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        raise ValueError("Saved IP attempt is malformed.") from None


class ConfirmationTracker:
    """Temporary transport gaps are neutral; contradictory evidence clears the pair."""

    def __init__(self, baseline: str, *, require_change: bool = True, candidate: dict | None = None,
                 candidate_age: float | None = None, monotonic_now: float = 0.0, floor: int = 0):
        self.baseline = baseline
        self.require_change = require_change
        self.family = ipaddress.ip_address(baseline).version if baseline else None
        self.pair: list[dict] = []
        self.first_at = 0.0
        self.floor = floor
        self.note = "waiting for an address"
        if candidate is not None and candidate_age is not None and 0 <= candidate_age <= CONFIRMATION_MAX_AGE:
            self.pair = [dict(candidate)]
            self.first_at = monotonic_now - candidate_age

    def clear(self, sequence: int) -> None:
        self.pair = []
        self.floor = max(self.floor, sequence)

    def failure(self, category: str, sequence: int, at: float) -> None:
        if category not in TRANSIENT_ERRORS or (self.pair and at - self.first_at > CONFIRMATION_MAX_AGE):
            self.clear(sequence)

    def observe(self, evidence: dict, at: float) -> bool:
        address = evidence["ip"]
        sequence = evidence["check_sequence"]
        if self.pair and (at - self.first_at > CONFIRMATION_MAX_AGE or at < self.first_at):
            self.clear(sequence - 1)
        if self.family is not None and ipaddress.ip_address(address).version != self.family:
            self.clear(sequence)
            self.note = "family mismatch"
            return False
        if self.baseline and ((address != self.baseline) != self.require_change):
            self.clear(sequence)
            self.note = "unchanged" if self.require_change else "different from saved IP"
            return False
        if not self.pair or self.pair[0]["ip"] != address:
            self.clear(sequence - 1)
            self.pair = [dict(evidence)]
            self.first_at = at
        elif sequence > self.pair[-1]["check_sequence"]:
            self.pair.append(dict(evidence))
        self.note = f"candidate {min(len(self.pair), 2)}/2"
        return len(self.pair) >= 2
