"""Safe, single-attempt MacroDroid IP change and verification helpers."""

from __future__ import annotations

import base64
import copy
import ipaddress
import socket
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests
from friend_poster.services.ip_evidence import ConfirmationTracker, TRANSIENT_ERRORS, utc_stamp, validate_attempt, validate_pair


DEFAULT_PUBLIC_IP_URL = "https://api64.ipify.org?format=json"
TRIGGER_TIMEOUT_SECONDS = 10.0
IP_CHECK_TIMEOUT_SECONDS = 15.0
IP_CHECK_ATTEMPTS = 2
IP_CHECK_RETRY_DELAY_SECONDS = 1.0
POLL_INTERVAL_SECONDS = 4.0
OBSERVATION_SECONDS = 180.0
# A freshly rotated proxy can briefly reject logins while its endpoint
# reconnects. This applies only after a trigger (or to read-only recovery),
# never to the pre-trigger baseline policy. Authentication still invalidates
# any partial confirmation in ConfirmationTracker.
OBSERVATION_RETRY_ERRORS = TRANSIENT_ERRORS | {"invalid_response", "authentication"}


class IpChangeError(RuntimeError):
    """A deliberately secret-free, user-facing network error."""

    def __init__(self, message: str, *, category: str = "unknown"):
        super().__init__(message)
        self.category = category


def _network_error(error: Exception) -> IpChangeError:
    """Inspect exception types only; exception text can contain proxy credentials."""
    pending, seen, errors = [error], set(), []
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        errors.append(item)
        pending.extend(value for value in (
            getattr(item, "__cause__", None), getattr(item, "__context__", None),
            getattr(item, "reason", None), getattr(item, "socket_err", None), *getattr(item, "args", ()),
        ) if isinstance(value, BaseException))
    names = {type(item).__name__ for item in errors}
    if names & {"SOCKS5AuthError", "SOCKS4Error"}:
        return IpChangeError("FalconProxy login failed.", category="authentication")
    if any(isinstance(item, (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema,
                             requests.exceptions.MissingSchema)) for item in errors):
        return IpChangeError("FalconProxy configuration is invalid.", category="configuration")
    if any(isinstance(item, requests.exceptions.SSLError) for item in errors):
        return IpChangeError("FalconProxy TLS verification failed.", category="tls")
    if any(isinstance(item, (TimeoutError, requests.exceptions.Timeout)) for item in errors):
        return IpChangeError("FalconProxy IP check timed out.", category="timeout")
    if any(isinstance(item, (ConnectionError, socket.gaierror)) for item in errors):
        return IpChangeError("FalconProxy connection is temporarily unavailable.", category="connection")
    if any(isinstance(item, requests.exceptions.ProxyError) for item in errors):
        return IpChangeError("FalconProxy connection or login failed.", category="proxy")
    if any(isinstance(item, requests.exceptions.ConnectionError) for item in errors):
        return IpChangeError("FalconProxy connection is temporarily unavailable.", category="connection")
    return IpChangeError("FalconProxy IP check failed safely.", category="unknown")


class _ObservationCancelled(Exception):
    """A local retry wait was interrupted before another network attempt."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Make redirects observable instead of silently following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


class StrictProxyHandler(urllib.request.ProxyHandler):
    """Use the explicitly selected proxy without consulting NO_PROXY."""

    def proxy_open(self, req, proxy, req_type):  # noqa: ANN001
        del req_type
        parsed = urllib.parse.urlsplit(proxy)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        hostport = f"{host}:{parsed.port}" if parsed.port else host
        if parsed.username is not None:
            user = urllib.parse.unquote(parsed.username)
            password = urllib.parse.unquote(parsed.password or "")
            token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
            req.add_unredirected_header("Proxy-Authorization", "Basic " + token)
        req.set_proxy(hostport, parsed.scheme.lower())
        return None


def _validate_https_url(value: str, *, label: str) -> str:
    text = str(value or "").strip()
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise IpChangeError(f"{label} must be a valid HTTPS URL.", category="configuration")
    if parsed.username or parsed.password:
        raise IpChangeError(f"{label} must not contain embedded credentials.", category="configuration")
    return text


def validate_webhook_url(value: str) -> str:
    return _validate_https_url(value, label="MacroDroid webhook")


def validate_proxy_url(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(text)
        host = parsed.hostname
        port = parsed.port
    except (ValueError, TypeError):
        raise IpChangeError("FalconProxy address or port is invalid.", category="configuration") from None
    if parsed.scheme.lower() not in {"http", "https", "socks5h"} or not host:
        raise IpChangeError("FalconProxy requires an HTTP, HTTPS, or SOCKS5H proxy URL.", category="configuration")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise IpChangeError("FalconProxy URL must not contain a path, query, or fragment.", category="configuration")
    if parsed.scheme.lower() == "socks5h" and port is None:
        raise IpChangeError("FalconProxy SOCKS5H URL requires a port.", category="configuration")
    return text


def build_socks5h_proxy_url(host: str, port: str, username: str, password: str) -> str:
    """Encode setup fields into a private proxy URL without exposing them in errors."""
    host = str(host or "").strip()
    username = str(username or "").strip()
    password = str(password or "")
    if not host or any(char.isspace() or char in "/@?#:" for char in host):
        raise IpChangeError("Enter a valid FalconProxy host.")
    try:
        number = int(str(port or "").strip())
    except (TypeError, ValueError):
        raise IpChangeError("Enter a valid FalconProxy port.") from None
    if not 1 <= number <= 65535:
        raise IpChangeError("Enter a valid FalconProxy port.")
    if not username or not password:
        raise IpChangeError("Enter the FalconProxy username and password.")
    encoded_user = urllib.parse.quote(username, safe="")
    encoded_password = urllib.parse.quote(password, safe="")
    return validate_proxy_url(
        f"socks5h://{encoded_user}:{encoded_password}@{host}:{number}"
    )


def sanitized_endpoint(value: str) -> str:
    """Return host[:port] without user info, path, query, or tokens."""
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        host = parsed.hostname or "unconfigured"
        return f"{host}:{parsed.port}" if parsed.port else host
    except (ValueError, TypeError):
        return "unconfigured"


def build_direct_opener() -> urllib.request.OpenerDirector:
    # An empty ProxyHandler also prevents environment/system proxy inheritance.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )


def build_proxy_opener(proxy_url: str) -> urllib.request.OpenerDirector:
    proxy_url = validate_proxy_url(proxy_url)
    if urllib.parse.urlsplit(proxy_url).scheme.lower() == "socks5h":
        raise IpChangeError("SOCKS5H requires the dedicated proxy transport.")
    return urllib.request.build_opener(
        StrictProxyHandler({"http": proxy_url, "https": proxy_url}),
        NoRedirectHandler(),
    )


def normalize_ip(value: Any) -> str:
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError as exc:
        raise IpChangeError("The public IP service returned an invalid address.", category="invalid_response") from exc


def parse_ip_payload(payload: Any) -> str:
    # Keep this parser local so MacroDroid mode does not inherit the legacy
    # iPhone configuration or USB validation path.
    import json

    value = payload
    for _ in range(4):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
            continue
        if isinstance(value, str):
            stripped = value.strip()
            try:
                return normalize_ip(stripped)
            except IpChangeError as ip_error:
                try:
                    value = json.loads(stripped)
                except (ValueError, TypeError):
                    raise ip_error
                continue
        if isinstance(value, dict):
            for key in ("ip", "result", "output", "value"):
                if key in value:
                    value = value[key]
                    break
            else:
                raise IpChangeError("The public IP response is missing its address.", category="invalid_response")
            continue
        raise IpChangeError("The public IP response is missing its address.", category="invalid_response")
    raise IpChangeError("The public IP response is too deeply nested.", category="invalid_response")


def fetch_proxy_public_ip(
    public_ip_url: str,
    proxy_url: str,
    timeout: float,
    *,
    opener: Any | None = None,
) -> str:
    endpoint = _validate_https_url(public_ip_url, label="Public IP endpoint")
    proxy_url = validate_proxy_url(proxy_url)
    if urllib.parse.urlsplit(proxy_url).scheme.lower() == "socks5h":
        if opener is None:
            try:
                import socks  # noqa: F401 - Requests' optional SOCKS transport.
            except ImportError:
                raise IpChangeError(
                    "SOCKS support is missing; restart Creator to install dependencies.",
                    category="configuration",
                ) from None
        client = opener if opener is not None else requests.Session()
        owned = opener is None
        if owned:
            client.trust_env = False
            client.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        try:
            response = client.get(
                endpoint,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=max(0.1, float(timeout)),
                allow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": "Creator-IP-Check/4"},
            )
            if response.status_code != 200:
                raise IpChangeError(
                    f"Public IP endpoint returned HTTP {int(response.status_code)}.",
                    category="authentication" if response.status_code == 407 else "http",
                )
            return parse_ip_payload(response.content)
        except IpChangeError:
            raise
        except Exception as exc:
            raise _network_error(exc) from None
        finally:
            if owned:
                client.close()
    client = opener if opener is not None else build_proxy_opener(proxy_url)
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json", "User-Agent": "Creator-IP-Check/3"},
        method="GET",
    )
    try:
        with client.open(request, timeout=max(0.1, float(timeout))) as response:
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            if status != 200:
                raise IpChangeError(f"Public IP endpoint returned HTTP {status}.", category="authentication" if status == 407 else "http")
            return parse_ip_payload(response.read())
    except IpChangeError:
        raise
    except urllib.error.HTTPError as exc:
        raise IpChangeError(f"Public IP endpoint returned HTTP {exc.code}.", category="authentication" if exc.code == 407 else "http") from None
    except Exception as exc:
        raise _network_error(exc) from None


def trigger_webhook_once(
    webhook_url: str,
    timeout: float = TRIGGER_TIMEOUT_SECONDS,
    *,
    opener: Any | None = None,
) -> dict[str, Any]:
    """Submit one GET. Returned messages never contain the secret URL."""
    endpoint = validate_webhook_url(webhook_url)
    client = opener if opener is not None else build_direct_opener()
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "text/plain", "User-Agent": "Creator-MacroDroid/1"},
        method="GET",
    )
    try:
        with client.open(request, timeout=max(0.1, float(timeout))) as response:
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            if status == 200:
                return {"acknowledged": True, "uncertain": False, "status": 200, "error": ""}
            return {
                "acknowledged": False,
                "uncertain": False,
                "status": status,
                "error": f"Trigger not acknowledged (HTTP {status}).",
            }
    except urllib.error.HTTPError as exc:
        return {
            "acknowledged": False,
            "uncertain": False,
            "status": int(exc.code),
            "error": f"Trigger not acknowledged (HTTP {exc.code}).",
        }
    except (TimeoutError, socket.timeout):
        return {
            "acknowledged": False,
            "uncertain": True,
            "status": None,
            "error": "Trigger delivery uncertain: request timed out.",
        }
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return {
                "acknowledged": False,
                "uncertain": True,
                "status": None,
                "error": "Trigger delivery uncertain: request timed out.",
            }
        return {
            "acknowledged": False,
            "uncertain": False,
            "status": None,
            "error": "Trigger delivery failed before acknowledgement.",
        }
    except OSError:
        return {
            "acknowledged": False,
            "uncertain": False,
            "status": None,
            "error": "Trigger delivery failed before acknowledgement.",
        }
    except Exception:
        return {
            "acknowledged": False,
            "uncertain": False,
            "status": None,
            "error": "Trigger delivery failed safely.",
        }


def _checked_at(now: Callable[[], datetime]) -> str:
    return now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _result(
    outcome: str,
    *,
    old_ip: str = "",
    new_ip: str = "",
    current_ip: str = "",
    last_success_at: str = "",
    trigger_acknowledged: bool = False,
    trigger_uncertain: bool = False,
    trigger_attempted: bool = False,
    trigger_deadline: float = 0.0,
    elapsed_seconds: float = 0.0,
    error: str = "",
    verification_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "old_ip": old_ip,
        "new_ip": new_ip,
        "current_ip": current_ip,
        "last_success_at": last_success_at,
        "trigger_acknowledged": bool(trigger_acknowledged),
        "trigger_uncertain": bool(trigger_uncertain),
        "trigger_attempted": bool(trigger_attempted),
        "trigger_deadline": float(trigger_deadline),
        "elapsed_seconds": max(0.0, float(elapsed_seconds)),
        "error": str(error or ""),
        "verification_evidence": verification_evidence,
    }


def run_macrodroid_change(
    webhook_url: str,
    proxy_url: str,
    *,
    public_ip_url: str = DEFAULT_PUBLIC_IP_URL,
    cancel_event: Any,
    progress: Callable[[dict[str, Any]], None] | None = None,
    on_observation: Callable[[dict[str, Any]], None] | None = None,
    before_trigger: Callable[[dict[str, Any]], None] | None = None,
    on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
    proxy_scope: str | None = None,
    require_baseline: bool = False,
    baseline_cycles: int = 1,
    operation_id: str | None = None,
    triggerer: Callable[..., dict[str, Any]] = trigger_webhook_once,
    fetcher: Callable[..., str] = fetch_proxy_public_ip,
    monotonic: Callable[[], float] = time.monotonic,
    waiter: Callable[[float], bool] | None = None,
    now: Callable[[], datetime] = datetime.now,
    trigger_timeout: float = TRIGGER_TIMEOUT_SECONDS,
    check_timeout: float = IP_CHECK_TIMEOUT_SECONDS,
    check_attempts: int = IP_CHECK_ATTEMPTS,
    retry_delay: float = IP_CHECK_RETRY_DELAY_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    observation_seconds: float = OBSERVATION_SECONDS,
) -> dict[str, Any]:
    """Try for a baseline, trigger once, then observe proxy egress."""
    emit = progress or (lambda _event: None)
    wait = waiter or cancel_event.wait
    started = monotonic()
    old_ip = ""
    current_ip = ""
    last_success = ""
    trigger_attempted = False
    trigger_deadline = 0.0
    acknowledged = False
    uncertain = False
    operation_id = operation_id or uuid.uuid4().hex
    check_sequence = 0
    baseline_evidence: dict[str, Any] | None = None

    def elapsed() -> float:
        return max(0.0, monotonic() - started)

    def measure(phase: str, deadline: float | None = None) -> tuple[str, bool, dict[str, Any]]:
        """Return (address, retried, evidence); never use the PC route."""
        nonlocal check_sequence
        problem = IpChangeError("FalconProxy IP check failed safely.")
        retried = False
        for attempt in range(max(1, int(check_attempts))):
            if cancel_event.is_set():
                raise _ObservationCancelled()
            remaining = (deadline - monotonic()) if deadline is not None else float(check_timeout)
            if remaining <= 0:
                break
            try:
                check_sequence += 1
                observed = normalize_ip(
                    fetcher(public_ip_url, proxy_url, min(float(check_timeout), remaining))
                )
                if cancel_event.is_set():
                    raise _ObservationCancelled()
                observed_at = now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                evidence = {
                    "operation_id": operation_id,
                    "check_sequence": check_sequence,
                    "phase": phase,
                    "ip": observed,
                    "observed_at_utc": observed_at,
                }
                if on_observation is not None:
                    try:
                        on_observation(dict(evidence))
                    except Exception:
                        # A history collector must not turn a valid IP into a failed check.
                        pass
                return observed, retried, evidence
            except _ObservationCancelled:
                raise
            except IpChangeError as exc:
                problem = exc
            except Exception as exc:
                problem = _network_error(exc)
            if problem.category not in TRANSIENT_ERRORS | {"invalid_response"} or attempt + 1 >= max(1, int(check_attempts)):
                break
            remaining = (deadline - monotonic()) if deadline is not None else float(retry_delay)
            if remaining <= 0:
                break
            retried = True
            emit({
                "phase": f"{phase}_retry",
                "status": f"FalconProxy {phase} check failed ({problem.category}); retrying once.",
                "old_ip": old_ip,
                "current_ip": current_ip,
                "last_success_at": last_success,
                "elapsed_seconds": elapsed(),
            })
            if wait(min(max(0.0, float(retry_delay)), remaining)):
                raise _ObservationCancelled()
        raise problem

    try:
        validate_webhook_url(webhook_url)
        validate_proxy_url(proxy_url)
        _validate_https_url(public_ip_url, label="Public IP endpoint")
    except IpChangeError as exc:
        result = _result("verification_failed", elapsed_seconds=elapsed(), error=str(exc))
        result["error_category"] = exc.category
        return result

    if cancel_event.is_set():
        return _result("cancelled", elapsed_seconds=elapsed(), error="Observation canceled before trigger.")

    baseline_problem = ""
    baseline_category = ""
    for cycle in range(max(1, int(baseline_cycles)) if require_baseline else 1):
        try:
            old_ip, _retried, baseline_evidence = measure("baseline")
            current_ip = old_ip
            last_success = _checked_at(now)
            break
        except _ObservationCancelled:
            return _result("cancelled", elapsed_seconds=elapsed(), error="Observation canceled before trigger.")
        except IpChangeError as exc:
            if cancel_event.is_set():
                return _result("cancelled", elapsed_seconds=elapsed(), error="Observation canceled before trigger.")
            baseline_category = exc.category
            baseline_problem = baseline_category
            another_cycle = (require_baseline and cycle + 1 < max(1, int(baseline_cycles))
                             and baseline_category in TRANSIENT_ERRORS | {"invalid_response"})
            emit({
                "phase": "baseline_warning",
                "status": (
                    f"Baseline unavailable ({baseline_problem}); retrying before reset."
                    if another_cycle else
                    (
                        f"Baseline unavailable ({baseline_problem}); required-baseline mode "
                        "will not submit an unverifiable reset."
                        if require_baseline else
                        f"Baseline unavailable ({baseline_problem}); submitting one trigger anyway. Change verification will be limited."
                    )
                ),
                "old_ip": "", "current_ip": "", "elapsed_seconds": elapsed(),
            })
            if another_cycle and wait(float(poll_interval)):
                return _result("cancelled", elapsed_seconds=elapsed(), error="Observation canceled before trigger.")
            if not another_cycle:
                break
    if require_baseline and not old_ip:
        result = _result(
            "baseline_unavailable",
            elapsed_seconds=elapsed(),
            error=(
                "FalconProxy baseline could not be measured; no reset was submitted. "
                "Retry remains safe because the webhook was not called."
            ),
        )
        result["error_category"] = baseline_category or "connection"
        return result
    emit(
        {
            "phase": "baseline",
            "status": (
                "Baseline measured through FalconProxy; submitting one trigger request…"
                if old_ip else "Baseline unavailable; submitting one trigger request…"
            ),
            "old_ip": old_ip,
            "current_ip": old_ip,
            "current_note": "baseline" if old_ip else "unavailable",
            "last_success_at": last_success,
            "elapsed_seconds": elapsed(),
        }
    )
    if cancel_event.is_set():
        return _result(
            "cancelled",
            old_ip=old_ip,
            current_ip=old_ip,
            last_success_at=last_success,
            elapsed_seconds=elapsed(),
            error="Observation canceled before trigger.",
        )

    trigger_started = monotonic()
    trigger_deadline = trigger_started + max(1.0, float(observation_seconds))
    stamp = now().astimezone(timezone.utc)
    deadline_utc = (stamp + timedelta(seconds=max(1.0, float(observation_seconds)))).isoformat().replace("+00:00", "Z")
    attempt = {
        "operation_id": operation_id, "proxy_scope": proxy_scope,
        "verification_target": public_ip_url, "baseline": baseline_evidence,
        "check_sequence": check_sequence, "candidate": None,
        "confirmation_floor": check_sequence, "verification_evidence": None,
        "observation_deadline_utc": deadline_utc,
    }
    if before_trigger is not None:
        try:
            before_trigger({
                "operation_id": operation_id,
                "baseline_ip": old_ip or None,
                "deadline_utc": deadline_utc,
                "attempt": copy.deepcopy(attempt),
            })
        except Exception:
            return _result(
                "verification_failed", old_ip=old_ip, current_ip=current_ip,
                last_success_at=last_success, elapsed_seconds=elapsed(),
                error="Reset was not submitted because its safety checkpoint could not be saved.",
            )
    if cancel_event.is_set():
        return _result(
            "cancelled", old_ip=old_ip, current_ip=current_ip,
            last_success_at=last_success, elapsed_seconds=elapsed(),
            error="Observation canceled before trigger.",
        )
    trigger_attempted = True
    trigger = triggerer(webhook_url, trigger_timeout)
    acknowledged = bool(trigger.get("acknowledged"))
    uncertain = bool(trigger.get("uncertain"))
    if not acknowledged and not uncertain:
        return _result(
            "verification_failed",
            old_ip=old_ip,
            current_ip=old_ip,
            last_success_at=last_success,
            trigger_attempted=True,
            trigger_deadline=trigger_deadline,
            elapsed_seconds=elapsed(),
            error=str(trigger.get("error") or "Trigger was not acknowledged."),
        )

    trigger_status = (
        "Trigger acknowledged — waiting for FalconProxy egress."
        if acknowledged
        else "Trigger delivery uncertain — observing without resending."
    )
    emit(
        {
            "phase": "trigger_acknowledged" if acknowledged else "trigger_uncertain",
            "status": trigger_status,
            "old_ip": old_ip,
            "current_ip": old_ip,
            "current_note": "baseline" if old_ip else "unavailable",
            "last_success_at": last_success,
            "elapsed_seconds": elapsed(),
        }
    )

    result = _observe_proxy(
        proxy_url, old_ip, attempt=attempt, require_change=True,
        cancel_event=cancel_event, deadline=trigger_deadline, initial_delay=True,
        fetcher=fetcher, monotonic=monotonic, waiter=wait, now=now,
        check_timeout=check_timeout, check_attempts=check_attempts,
        retry_delay=retry_delay, poll_interval=poll_interval,
        progress=emit, on_observation=on_observation, on_checkpoint=on_checkpoint,
    )
    result.update(trigger_attempted=True, trigger_acknowledged=acknowledged,
                  trigger_uncertain=uncertain, trigger_deadline=trigger_deadline,
                  elapsed_seconds=elapsed())
    return result


def _observe_proxy(
    proxy_url, expected_ip, *, attempt, require_change, cancel_event, deadline,
    fetcher, monotonic, waiter, now, check_timeout=IP_CHECK_TIMEOUT_SECONDS,
    check_attempts=IP_CHECK_ATTEMPTS, retry_delay=IP_CHECK_RETRY_DELAY_SECONDS,
    poll_interval=POLL_INTERVAL_SECONDS, initial_delay=False,
    progress=None, on_observation=None, on_checkpoint=None,
):
    """One observation engine for reset, Retry and restart recovery."""
    emit = progress or (lambda _event: None)
    started = monotonic()
    state = copy.deepcopy(attempt)
    state["verification_evidence"] = None
    candidate = state.get("candidate")
    age = None
    if candidate:
        try:
            age = (now().astimezone(timezone.utc) - utc_stamp(candidate["observed_at_utc"])).total_seconds()
        except (KeyError, TypeError, ValueError, AttributeError):
            candidate = None
    tracker = ConfirmationTracker(
        expected_ip, require_change=require_change, candidate=candidate,
        candidate_age=age, monotonic_now=monotonic(), floor=state["confirmation_floor"],
    )
    current = expected_ip
    last_success = ""
    confirmed_current = ""
    saw_unchanged = False
    last_category = ""

    def finish(outcome, error=""):
        result = _result(
            outcome, old_ip=expected_ip, new_ip=current if outcome == "verified" else "",
            current_ip=current, elapsed_seconds=monotonic() - started,
            last_success_at=last_success, error=error,
            verification_evidence=state["verification_evidence"] if outcome == "verified" else None,
        )
        result.update(ip=result["new_ip"], operation_id=state["operation_id"],
                      error_category=last_category, observation_state=copy.deepcopy(state))
        return result

    def checkpoint():
        state["candidate"] = dict(tracker.pair[-1]) if tracker.pair else None
        state["confirmation_floor"] = tracker.floor
        if on_checkpoint is not None:
            on_checkpoint(copy.deepcopy(state))

    if initial_delay and waiter(min(poll_interval, max(0.0, deadline - monotonic()))):
        return finish("cancelled", "Observation canceled; the submitted Android macro may continue.")
    while monotonic() < deadline and not cancel_event.is_set():
        for retry in range(max(1, int(check_attempts))):
            if cancel_event.is_set() or monotonic() >= deadline:
                break
            state["check_sequence"] += 1
            sequence = state["check_sequence"]
            try:
                raw = fetcher(state["verification_target"], proxy_url,
                              min(check_timeout, max(0.1, deadline - monotonic())))
                # A bounded request can still finish late or after Stop.
                if cancel_event.is_set():
                    return finish("cancelled", "Observation canceled; the submitted Android macro may continue.")
                if monotonic() >= deadline:
                    break
                observed = normalize_ip(raw)
            except Exception as exc:
                if cancel_event.is_set():
                    return finish("cancelled", "Observation canceled; the submitted Android macro may continue.")
                if monotonic() >= deadline:
                    break
                problem = exc if isinstance(exc, IpChangeError) else _network_error(exc)
                last_category = problem.category
                tracker.failure(last_category, sequence, monotonic())
                state["verification_evidence"] = None
                try:
                    checkpoint()
                except Exception:
                    last_category = "storage"
                    return finish("verification_failed", "IP observation checkpoint could not be saved.")
                retryable = last_category in OBSERVATION_RETRY_ERRORS
                emit({
                    "phase": "waiting_connectivity" if retryable else "observation_error",
                    "status": (
                        "FalconProxy authentication unavailable; retrying before the verification deadline."
                        if last_category == "authentication" else
                        f"IP observation failed ({last_category}); "
                        + ("candidate retained." if tracker.pair else "waiting for confirmation.")
                    ),
                    "error_category": last_category, "old_ip": expected_ip,
                    "current_ip": tracker.pair[0]["ip"] if tracker.pair else "",
                    "elapsed_seconds": monotonic() - started,
                })
                if not retryable:
                    return finish("verification_failed", f"FalconProxy verification stopped ({last_category}).")
                if retry + 1 < max(1, int(check_attempts)):
                    emit({"phase": "observation_retry", "status": f"Retrying IP observation ({last_category})."})
                    if waiter(min(retry_delay, max(0.0, deadline - monotonic()))):
                        break
                continue

            evidence = {
                "operation_id": state["operation_id"], "check_sequence": sequence,
                "phase": "observation", "ip": observed,
                "observed_at_utc": now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            current = observed
            last_success = _checked_at(now)
            if on_observation is not None:
                try:
                    on_observation(dict(evidence))
                except Exception:
                    pass
            if cancel_event.is_set():
                return finish("cancelled", "Observation canceled; the submitted Android macro may continue.")
            if monotonic() >= deadline:
                break
            last_category = ""
            matched = tracker.observe(evidence, monotonic())
            if expected_ip and observed == expected_ip:
                saw_unchanged = True
            if matched and expected_ip:
                state["verification_evidence"] = {
                    "baseline": state["baseline"], "confirmations": tracker.pair[-2:],
                    "confirmation_floor": tracker.floor,
                }
                try:
                    validate_pair({
                        "outcome": "verified", "old_ip": expected_ip, "new_ip": observed,
                        "verification_evidence": state["verification_evidence"],
                    }, require_change=require_change)
                except ValueError:
                    tracker.clear(sequence)
                    state["verification_evidence"] = None
                    matched = False
            elif matched:
                confirmed_current = observed
                tracker.pair = tracker.pair[-1:]
                tracker.first_at = monotonic()
            try:
                checkpoint()
            except Exception:
                last_category = "storage"
                return finish("verification_failed", "IP observation checkpoint could not be saved.")
            # A Stop arriving during a durable checkpoint must also win.
            if cancel_event.is_set():
                return finish("cancelled", "Observation canceled; the submitted Android macro may continue.")
            if monotonic() >= deadline:
                break
            emit({
                "phase": "checking",
                "status": "IP change verified." if matched and expected_ip else f"Waiting for verification — {tracker.note}.",
                "old_ip": expected_ip, "current_ip": observed, "current_note": tracker.note,
                "last_success_at": last_success, "elapsed_seconds": monotonic() - started,
            })
            if matched and expected_ip:
                return finish("verified")
            break
        if waiter(min(poll_interval, max(0.0, deadline - monotonic()))):
            break
    if cancel_event.is_set():
        return finish("cancelled", "Observation canceled; the submitted Android macro may continue.")
    if last_category == "authentication":
        return finish("verification_failed", "FalconProxy authentication did not recover before the verification deadline.")
    if not expected_ip and confirmed_current:
        current = confirmed_current
        return finish("observed_unverified", "Current FalconProxy egress was observed, but no pre-reset baseline exists.")
    if expected_ip and saw_unchanged and not tracker.pair and require_change:
        return finish("no_change", "No IP change observed before the deadline.")
    return finish("verification_failed", "Verification timed out: two fresh matching observations were not confirmed.")


def observe_proxy_without_trigger(
    proxy_url: str, expected_ip: str, *, require_change: bool, cancel_event: Any,
    observation_seconds: float = OBSERVATION_SECONDS,
    fetcher: Callable[..., str] = fetch_proxy_public_ip,
    monotonic: Callable[[], float] = time.monotonic,
    waiter: Callable[[float], bool] | None = None,
    now: Callable[[], datetime] = datetime.now,
    attempt: dict | None = None, proxy_scope: str | None = None,
    public_ip_url: str = DEFAULT_PUBLIC_IP_URL,
    progress=None, on_observation=None, on_checkpoint=None,
) -> dict[str, Any]:
    """Read-only confirmation with the same evidence rules as a fresh reset."""
    try:
        validate_proxy_url(proxy_url)
        expected = normalize_ip(expected_ip)
        _validate_https_url(public_ip_url, label="Public IP endpoint")
    except IpChangeError as exc:
        result = _result("verification_failed", error="A valid proxy or comparison IP is unavailable.")
        result.update(ip="", error_category=exc.category)
        return result
    if attempt is None:
        operation = uuid.uuid4().hex
        attempt = {
            "operation_id": operation, "proxy_scope": proxy_scope,
            "verification_target": public_ip_url,
            "baseline": {"operation_id": operation, "check_sequence": 0, "phase": "baseline",
                         "ip": expected, "observed_at_utc": None,
                         "provenance": "legacy_saved_baseline" if require_change else "saved_comparison"},
            "check_sequence": 0, "candidate": None, "confirmation_floor": 0,
            "verification_evidence": None,
            "observation_deadline_utc": (now().astimezone(timezone.utc) + timedelta(seconds=observation_seconds)).isoformat().replace("+00:00", "Z"),
        }
    if (attempt.get("proxy_scope") != proxy_scope or attempt.get("verification_target") != public_ip_url
            or not attempt.get("baseline") or attempt["baseline"].get("ip") != expected):
        result = _result("verification_failed", error="Saved IP attempt does not match the configured proxy and baseline.")
        result.update(ip="", error_category="configuration")
        return result
    try:
        validate_attempt(attempt, require_scope=proxy_scope is not None, require_change=require_change)
        remaining = max(0.0, (utc_stamp(attempt["observation_deadline_utc"]) - now().astimezone(timezone.utc)).total_seconds())
    except (ValueError, TypeError, AttributeError):
        result = _result("verification_failed", error="Saved IP observation evidence is malformed.")
        result.update(ip="", error_category="configuration")
        return result
    # Recovery cannot extend a saved window. Explicit Retry must first save a new deadline.
    deadline = monotonic() + min(max(0.0, observation_seconds), remaining)
    if on_checkpoint is not None:
        try:
            on_checkpoint(copy.deepcopy(attempt))
        except Exception:
            result = _result("verification_failed", error="IP observation checkpoint could not be saved.")
            result.update(ip="", error_category="storage")
            return result
    result = _observe_proxy(
        proxy_url, expected, attempt=attempt, require_change=require_change,
        cancel_event=cancel_event, deadline=deadline,
        fetcher=fetcher, monotonic=monotonic, waiter=waiter or cancel_event.wait,
        now=now, progress=progress, on_observation=on_observation, on_checkpoint=on_checkpoint,
    )
    result["observation_only"] = True
    return result
