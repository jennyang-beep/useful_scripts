"""Push a BF4 PLDM ``.fwpkg`` to the BMC via Redfish UpdateService.

NVIDIA OpenBMC for BlueField exposes a multipart push endpoint:

    POST https://<bmc>/redfish/v1/UpdateService

with a multipart body of:

    UpdateParameters : application/json with {"Targets": [], "@Redfish.OperationApplyTime": "Immediate"}
    UpdateFile       : application/octet-stream (the .fwpkg)

A successful submission returns ``202 Accepted`` and a ``Location`` header
pointing at a Task in ``/redfish/v1/TaskService/Tasks/<id>``. We poll the
task until it transitions out of ``Running`` / ``New`` / ``Pending``.

TLS verification is intentionally disabled because BMCs ship with
self-signed certificates; we suppress the resulting urllib3 warning but
log a one-time notice so it's still visible.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urljoin

import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from tqdm import tqdm
from urllib3.util.retry import Retry


log = logging.getLogger(__name__)


_TLS_WARNED = False

TERMINAL_STATES = {"Completed", "Exception", "Killed", "Cancelled", "Interrupted"}
SUCCESS_STATES = {"Completed"}


class RedfishError(Exception):
    """Raised on any unrecoverable Redfish failure."""


@dataclass
class TaskResult:
    task_id: str
    state: str
    status: Optional[str]
    percent: Optional[int]
    messages: list


def push_firmware(
    bmc_host: str,
    bmc_user: str,
    bmc_pass: str,
    fwpkg_path: Path,
    *,
    apply_time: str = "Immediate",
    poll_interval: float = 5.0,
    overall_timeout: float = 60 * 60,  # 1 hour
    show_progress: bool = True,
) -> TaskResult:
    """Upload ``fwpkg_path`` and wait until the BMC's task terminates.

    Returns the final :class:`TaskResult`. Raises :class:`RedfishError`
    if the task ends in any non-``Completed`` terminal state, or if
    the overall timeout expires.
    """
    _warn_tls_once()

    base = _base_url(bmc_host)
    session = _session(bmc_user, bmc_pass)

    update_uri = urljoin(base, "/redfish/v1/UpdateService")
    fwpkg_path = Path(fwpkg_path)
    if not fwpkg_path.is_file():
        raise RedfishError(f"Firmware file not found: {fwpkg_path}")

    log.info("Pushing %s to %s (%.1f MiB)",
             fwpkg_path.name, update_uri, fwpkg_path.stat().st_size / (1024 * 1024))

    task_uri = _post_multipart(session, update_uri, fwpkg_path, apply_time, show_progress)

    log.info("Task started: %s", task_uri)
    return _poll_task(session, base, task_uri, poll_interval, overall_timeout)


def _post_multipart(
    session: requests.Session,
    update_uri: str,
    fwpkg_path: Path,
    apply_time: str,
    show_progress: bool,
) -> str:
    params = {
        "Targets": [],
        "@Redfish.OperationApplyTime": apply_time,
    }

    fh = open(fwpkg_path, "rb")
    try:
        encoder = MultipartEncoder(
            fields={
                "UpdateParameters": (
                    "UpdateParameters.json",
                    json.dumps(params),
                    "application/json",
                ),
                "UpdateFile": (
                    fwpkg_path.name,
                    fh,
                    "application/octet-stream",
                ),
            }
        )

        progress = None
        if show_progress:
            progress = tqdm(
                total=encoder.len,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=f"upload {fwpkg_path.name}",
            )

        last_bytes = 0

        def _cb(monitor: MultipartEncoderMonitor) -> None:
            nonlocal last_bytes
            if progress is None:
                return
            delta = monitor.bytes_read - last_bytes
            if delta:
                progress.update(delta)
                last_bytes = monitor.bytes_read

        monitor = MultipartEncoderMonitor(encoder, _cb)
        headers = {"Content-Type": monitor.content_type}

        try:
            resp = session.post(update_uri, data=monitor, headers=headers, timeout=(15, 600))
        finally:
            if progress is not None:
                progress.close()
    finally:
        fh.close()

    if resp.status_code not in (200, 201, 202):
        raise RedfishError(
            f"Firmware POST failed: HTTP {resp.status_code}\n{_safe_body(resp)}"
        )

    task_uri = resp.headers.get("Location")
    if not task_uri:
        # Some implementations return the Task in the body instead.
        try:
            body = resp.json()
            task_uri = body.get("@odata.id")
        except ValueError:
            task_uri = None

    if not task_uri:
        raise RedfishError(
            f"Firmware POST returned {resp.status_code} but no task Location/@odata.id.\n"
            f"Body: {_safe_body(resp)}"
        )

    return task_uri


def _poll_task(
    session: requests.Session,
    base_url: str,
    task_uri: str,
    poll_interval: float,
    overall_timeout: float,
) -> TaskResult:
    full_uri = task_uri if task_uri.startswith("http") else urljoin(base_url, task_uri)
    deadline = time.monotonic() + overall_timeout
    last_percent = -1

    while True:
        try:
            resp = session.get(full_uri, timeout=(15, 60))
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Task poll failed (will retry): %s", exc)
            body = {}

        state = body.get("TaskState", "Unknown")
        status = body.get("TaskStatus")
        percent = body.get("PercentComplete")
        messages = body.get("Messages", []) or []

        if isinstance(percent, int) and percent != last_percent:
            log.info("Task %s: %s (%d%%)", _short_task_id(full_uri), state, percent)
            last_percent = percent
        else:
            log.debug("Task %s: state=%s status=%s pct=%s", full_uri, state, status, percent)

        if state in TERMINAL_STATES:
            result = TaskResult(
                task_id=_short_task_id(full_uri),
                state=state,
                status=status,
                percent=percent,
                messages=messages,
            )
            if state not in SUCCESS_STATES or (status and status not in ("OK", "Ok")):
                _log_messages(messages)
                raise RedfishError(
                    f"Firmware task ended in state={state} status={status}. "
                    f"See log for Redfish messages."
                )
            log.info("Firmware update completed successfully (%s).", status or "OK")
            _log_messages(messages, level=logging.INFO)
            return result

        if time.monotonic() > deadline:
            raise RedfishError(
                f"Timed out after {overall_timeout:.0f}s waiting for task {full_uri} "
                f"(last state={state}, percent={percent})."
            )

        time.sleep(poll_interval)


def _session(user: str, password: str) -> requests.Session:
    s = requests.Session()
    s.verify = False  # BMCs use self-signed certs
    s.auth = HTTPBasicAuth(user, password)
    retry = Retry(
        total=3,
        connect=5,
        read=3,
        backoff_factor=2.0,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["GET"]),  # never retry POST/PATCH
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _base_url(bmc_host: str) -> str:
    if bmc_host.startswith("http://") or bmc_host.startswith("https://"):
        # Strip trailing slash for clean urljoin behaviour.
        return bmc_host.rstrip("/") + "/"
    return f"https://{bmc_host}/"


def _safe_body(resp: requests.Response) -> str:
    try:
        return json.dumps(resp.json(), indent=2)
    except ValueError:
        return resp.text[:2000]


def _short_task_id(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]


def _log_messages(messages, level: int = logging.WARNING) -> None:
    for m in messages:
        msg_id = m.get("MessageId", "?")
        text = m.get("Message", "")
        sev = m.get("Severity") or m.get("MessageSeverity") or "?"
        log.log(level, "  Redfish msg: [%s] %s -- %s", sev, msg_id, text)


def _warn_tls_once() -> None:
    global _TLS_WARNED
    if _TLS_WARNED:
        return
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    log.warning("BMC TLS certificate verification is DISABLED (lab self-signed certs).")
    _TLS_WARNED = True


def _normalize_apply_time(value: str) -> Tuple[str, ...]:  # pragma: no cover - reserved for future
    return (value,)
