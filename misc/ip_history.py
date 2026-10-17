"""Append-only, credential-free FalconProxy egress history (UTF-8 JSON lines)."""

from __future__ import annotations

import csv
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from ip_evidence import validate_pair

SOURCE = "falconproxy_socks5h_egress_ipify"
DEVICE_LABEL = "Xiaomi / MacroDroid"
FIELDS = (
    "version", "event_id", "operation_id", "scope_id", "device_label",
    "proxy_endpoint", "source", "result", "old_ip", "new_ip",
    "old_observed_at_utc", "new_observed_at_utc", "confirmed_at_utc",
    "recorded_at_utc", "imported_at_utc",
)
RESULTS = {"initial_observation", "observed_unverified", "verified_transition"}
_LOCAL_LOCK = threading.RLock()
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class HistoryError(RuntimeError):
    """User-facing history failure without file contents or secrets."""


def _configured_key(secrets_path: str | Path) -> str:
    """Return the private local history key, creating it without exposing it."""
    path = Path(secrets_path)
    temporary = None
    try:
        with _LOCAL_LOCK:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError()
            key = payload.get("ip_history_scope_key")
            if not key:
                key = secrets.token_hex(32)
                payload["ip_history_scope_key"] = key
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(payload, handle, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            # Validate here so callers never receive malformed secret material.
            if not isinstance(key, str) or len(bytes.fromhex(key)) != 32:
                raise ValueError()
            return key
    except (OSError, ValueError, TypeError):
        raise HistoryError("The private proxy scope could not be established.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def configured_scope(proxy_url: str, secrets_path: str | Path) -> str:
    """Return a stable, credential-free identifier for the configured proxy."""
    return history_scope(proxy_url, _configured_key(secrets_path))[0]


def record_configured_operation(
    path: str | Path,
    *,
    proxy_url: str,
    secrets_path: str | Path,
    operation_id: str,
    observations: list[dict[str, Any]],
    result: dict[str, Any],
) -> int:
    """Record one operation without exposing the private history key to UI code."""
    return record_operation(
        path,
        proxy_url=proxy_url,
        key_hex=_configured_key(secrets_path),
        operation_id=operation_id,
        observations=observations,
        result=result,
    )


def latest_configured_transition(
    path: str | Path,
    *,
    proxy_url: str,
    secrets_path: str | Path,
) -> dict[str, Any] | None:
    """Return the newest verified transition for this exact proxy scope."""
    scope_id = configured_scope(proxy_url, secrets_path)
    matches = [
        row for row in read_history(path)
        if row["scope_id"] == scope_id and row["result"] == "verified_transition"
    ]
    if not matches:
        return None
    return dict(max(matches, key=lambda row: (row["confirmed_at_utc"] or "", row["recorded_at_utc"])))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def history_scope(proxy_url: str, key_hex: str) -> tuple[str, str]:
    """Identify a proxy configuration without saving any part of its credentials."""
    try:
        key = bytes.fromhex(key_hex)
        parsed = urllib.parse.urlsplit(proxy_url)
        host = parsed.hostname or ""
        port = parsed.port
        scheme = parsed.scheme.lower()
        if port is None and scheme in {"http", "https"}:
            port = 80 if scheme == "http" else 443
        if (
            len(key) != 32 or not host or not port
            or scheme not in {"http", "https", "socks5h"}
            or not _safe_host(host)
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
        ):
            raise ValueError()
        endpoint = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        scope = hmac.new(key, proxy_url.encode("utf-8"), hashlib.sha256).hexdigest()
        return scope, endpoint
    except (TypeError, ValueError, UnicodeError):
        raise HistoryError("IP history scope could not be established safely.") from None


def _safe_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOST_RE.fullmatch(host))


def _utc_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HistoryError("IP history contains an invalid UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HistoryError("IP history contains an invalid UTC timestamp.") from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise HistoryError("IP history contains an invalid UTC timestamp.")
    return value


def _ip_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        normalized = str(ipaddress.ip_address(value))
    except (TypeError, ValueError):
        raise HistoryError("IP history contains an invalid address.") from None
    if value != normalized:
        raise HistoryError("IP history contains a noncanonical address.")
    return normalized


def validate_event(record: Any) -> dict[str, Any]:
    """Allow only the versioned safe fields; never copy unknown imported data."""
    if not isinstance(record, dict) or set(record) != set(FIELDS):
        raise HistoryError("IP history contains an unsupported record.")
    if (
        record["version"] != 1
        or not isinstance(record["result"], str)
        or record["result"] not in RESULTS
        or record["source"] != SOURCE
    ):
        raise HistoryError("IP history contains an unsupported record.")
    try:
        uuid.UUID(str(record["event_id"]))
        uuid.UUID(str(record["operation_id"]))
    except (TypeError, ValueError, AttributeError):
        raise HistoryError("IP history contains an invalid event identity.") from None
    if not isinstance(record["scope_id"], str) or len(record["scope_id"]) != 64:
        raise HistoryError("IP history contains an invalid proxy identity.")
    try:
        bytes.fromhex(record["scope_id"])
    except ValueError:
        raise HistoryError("IP history contains an invalid proxy identity.") from None
    endpoint = record["proxy_endpoint"]
    if (
        record["device_label"] != DEVICE_LABEL
        or not isinstance(endpoint, str)
        or not endpoint
        or any(ch in endpoint for ch in "/@?#\\ \t\r\n")
    ):
        raise HistoryError("IP history contains an unsafe source label.")
    try:
        parsed_endpoint = urllib.parse.urlsplit("socks5h://" + endpoint)
        host = parsed_endpoint.hostname
        port = parsed_endpoint.port
        expected = f"[{host}]:{port}" if host and ":" in host else f"{host}:{port}"
        if not host or port is None or not _safe_host(host) or endpoint != expected:
            raise ValueError()
    except ValueError:
        raise HistoryError("IP history contains an unsafe proxy endpoint.") from None
    old_ip = _ip_or_none(record["old_ip"])
    new_ip = _ip_or_none(record["new_ip"])
    if new_ip is None:
        raise HistoryError("IP history observation has no address.")
    if record["result"] == "verified_transition":
        if old_ip is None or old_ip == new_ip:
            raise HistoryError("IP history contains an invalid transition.")
        if ipaddress.ip_address(old_ip).version != ipaddress.ip_address(new_ip).version:
            raise HistoryError("IP history contains an address-family mismatch.")
        if _utc_or_none(record["confirmed_at_utc"]) is None:
            raise HistoryError("IP history transition lacks confirmation time.")
    elif old_ip is not None or record["confirmed_at_utc"] is not None:
        raise HistoryError("IP history contains an invalid observation.")
    for field in (
        "old_observed_at_utc", "new_observed_at_utc", "confirmed_at_utc",
        "recorded_at_utc", "imported_at_utc",
    ):
        _utc_or_none(record[field])
    if record["recorded_at_utc"] is None:
        raise HistoryError("IP history record lacks a write time.")
    return {field: record[field] for field in FIELDS}


def _decode(data: bytes) -> list[dict[str, Any]]:
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise HistoryError("IP history has an incomplete final line; no new data was saved.")
    try:
        lines = data.decode("utf-8").splitlines()
        return [validate_event(json.loads(line)) for line in lines]
    except (UnicodeError, json.JSONDecodeError):
        raise HistoryError("IP history has a malformed line; no new data was saved.") from None


@contextmanager
def _locked(handle: Any) -> Iterator[None]:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_history(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    try:
        with _LOCAL_LOCK, target.open("rb") as handle:
            return _decode(handle.read())
    except HistoryError:
        raise
    except OSError:
        raise HistoryError("IP history could not be read.") from None


def append_events(path: str | Path, records: list[dict[str, Any]]) -> int:
    clean = [validate_event(record) for record in records]
    if not clean:
        return 0
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _LOCAL_LOCK, target.open("a+b") as handle, _locked(handle):
            handle.seek(0)
            existing = _decode(handle.read())
            known = {row["event_id"] for row in existing}
            fresh = []
            for row in clean:
                if row["event_id"] not in known:
                    fresh.append(row)
                    known.add(row["event_id"])
            if fresh:
                payload = "".join(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                    for row in fresh
                ).encode("utf-8")
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            return len(fresh)
    except HistoryError:
        raise
    except OSError:
        raise HistoryError("IP history could not be saved.") from None


def _event_id(operation_id: str, kind: str, sequence: int) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"friend-poster-ip:{operation_id}:{kind}:{sequence}").hex


def _valid_evidence(result: dict[str, Any], observations: list[dict[str, Any]]) -> tuple[dict, dict, dict] | None:
    if result.get("outcome") != "verified":
        return None
    if result.get("trigger_attempted") is not True and result.get("observation_only") is not True:
        raise HistoryError("Verified IP result lacks trigger evidence; history was not saved.")
    proof = result.get("verification_evidence")
    if not isinstance(proof, dict) or not isinstance(proof.get("confirmations"), list):
        raise HistoryError("Verified IP result lacks address evidence; history was not saved.")
    baseline = proof.get("baseline")
    pair = proof["confirmations"]
    if not isinstance(baseline, dict) or len(pair) != 2 or not all(isinstance(item, dict) for item in pair):
        raise HistoryError("Verified IP result lacks address evidence; history was not saved.")
    if baseline not in observations or any(item not in observations for item in pair):
        raise HistoryError("Verified IP evidence was not observed; history was not saved.")
    try:
        validate_pair(result)
    except ValueError:
        raise HistoryError("Verified IP evidence is inconsistent; history was not saved.") from None
    # Failed requests can leave sequence gaps; a contradictory successful check cannot.
    if any(pair[0]["check_sequence"] < item["check_sequence"] < pair[1]["check_sequence"]
           and item["ip"] != pair[0]["ip"] for item in observations):
        raise HistoryError("Verified IP evidence is inconsistent; history was not saved.")
    return baseline, pair[0], pair[1]


def record_operation(
    path: str | Path,
    *,
    proxy_url: str,
    key_hex: str,
    operation_id: str,
    observations: list[dict[str, Any]],
    result: dict[str, Any],
) -> int:
    """Persist observed addresses and only evidence-backed transitions."""
    scope_id, endpoint = history_scope(proxy_url, key_hex)
    try:
        uuid.UUID(operation_id)
    except ValueError:
        raise HistoryError("IP history operation identity is invalid.") from None
    clean_observations = []
    for item in observations:
        if not isinstance(item, dict) or item.get("operation_id") != operation_id:
            raise HistoryError("IP history observation identity is invalid.")
        ip = _ip_or_none(item.get("ip"))
        stamp = _utc_or_none(item.get("observed_at_utc"))
        seq = item.get("check_sequence")
        legacy = item.get("phase") == "baseline" and item.get("provenance") == "legacy_saved_baseline"
        if ip is None or (stamp is None and not legacy) or type(seq) is not int or seq < (0 if legacy else 1) or item.get("phase") not in {"baseline", "observation"}:
            raise HistoryError("IP history observation is invalid.")
        clean_observations.append(dict(item))
    clean_observations.sort(key=lambda row: row["check_sequence"])
    if len({row["check_sequence"] for row in clean_observations}) != len(clean_observations):
        raise HistoryError("IP history observation sequence is duplicated.")
    proof = _valid_evidence(result, clean_observations)
    existing = read_history(path)
    scoped = [row for row in existing if row["scope_id"] == scope_id and row["source"] == SOURCE]
    latest = max(
        scoped,
        key=lambda row: (
            row["new_observed_at_utc"] is not None,
            row["new_observed_at_utc"] or "",
            row["recorded_at_utc"],
        ),
        default=None,
    )
    last_ip = latest["new_ip"] if latest else None
    fresh = []
    confirmed_seq = proof[1]["check_sequence"] if proof else None
    for item in clean_observations:
        ip = item["ip"]
        seq = item["check_sequence"]
        if proof and ip == result["new_ip"] and item["phase"] == "observation":
            continue
        if ip == last_ip:
            continue
        kind = "initial_observation" if last_ip is None else "observed_unverified"
        fresh.append({
            "version": 1,
            "event_id": _event_id(operation_id, "observation", seq),
            "operation_id": operation_id,
            "scope_id": scope_id,
            "device_label": DEVICE_LABEL,
            "proxy_endpoint": endpoint,
            "source": SOURCE,
            "result": kind,
            "old_ip": None,
            "new_ip": ip,
            "old_observed_at_utc": None,
            "new_observed_at_utc": item["observed_at_utc"],
            "confirmed_at_utc": None,
            "recorded_at_utc": utc_now(),
            "imported_at_utc": None,
        })
        last_ip = ip
    if proof:
        baseline, first, second = proof
        fresh.append({
            "version": 1,
            "event_id": _event_id(operation_id, "transition", confirmed_seq),
            "operation_id": operation_id,
            "scope_id": scope_id,
            "device_label": DEVICE_LABEL,
            "proxy_endpoint": endpoint,
            "source": SOURCE,
            "result": "verified_transition",
            "old_ip": baseline["ip"],
            "new_ip": first["ip"],
            "old_observed_at_utc": baseline["observed_at_utc"],
            "new_observed_at_utc": first["observed_at_utc"],
            "confirmed_at_utc": second["observed_at_utc"],
            "recorded_at_utc": utc_now(),
            "imported_at_utc": None,
        })
    return append_events(path, fresh)


def import_history(path: str | Path, source_path: str | Path) -> int:
    """Merge only this application's versioned TXT records by stable event ID."""
    if not Path(source_path).is_file():
        raise HistoryError("The selected IP history TXT could not be read.")
    source = read_history(source_path)
    existing_ids = {row["event_id"] for row in read_history(path)}
    imported_at = utc_now()
    fresh = [
        {**row, "imported_at_utc": imported_at}
        for row in source if row["event_id"] not in existing_ids
    ]
    return append_events(path, fresh)


def export_csv(path: str | Path, destination: str | Path) -> int:
    rows = read_history(path)
    target = Path(destination)
    if target.resolve() == Path(path).resolve():
        raise HistoryError("CSV export must not replace the IP history TXT file.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", delete=False,
            dir=target.parent, prefix=".ip-history-export-", suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        os.replace(temporary, target)
        return len(rows)
    except OSError:
        raise HistoryError("IP history CSV could not be exported.") from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)
