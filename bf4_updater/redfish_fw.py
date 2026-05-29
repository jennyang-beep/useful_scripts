"""Push a BF4 PLDM ``.fwpkg`` to the BMC via Redfish UpdateService.

This module mirrors the working manual command::

    curl -k -u admin:'Nvidia_12345!' \\
        https://<bmc>/redfish/v1/UpdateService/update-multipart \\
        -F 'UpdateParameters={};type=application/octet-stream' \\
        -F UpdateFile=@<file>.fwpkg

i.e. a multipart POST to ``/redfish/v1/UpdateService/update-multipart``
with two parts:

  * ``UpdateParameters`` : literal ``{}``, ``Content-Type:
    application/octet-stream``, no ``filename`` in Content-Disposition.
  * ``UpdateFile``       : the ``.fwpkg``, ``Content-Type:
    application/octet-stream``, ``filename="<name>.fwpkg"``.

A successful submission returns ``202 Accepted`` and a ``Location``
header pointing at a Task in ``/redfish/v1/TaskService/Tasks/<id>``.
We poll the task until it transitions out of ``Running`` / ``New`` /
``Pending``.

TLS verification is intentionally disabled because BMCs ship with
self-signed certificates; we suppress the resulting urllib3 warning
but log a one-time notice so it's still visible.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
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

# How many consecutive 404s on the task URI before we give up. A completed
# task usually lingers (CompletedTaskOverWritePolicy=Oldest), so a 404 means
# either it was reaped or we polled the wrong URI.
MAX_TASK_NOT_FOUND = 3


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

    update_uri = urljoin(base, "/redfish/v1/UpdateService/update-multipart")
    fwpkg_path = Path(fwpkg_path)
    if not fwpkg_path.is_file():
        raise RedfishError(f"Firmware file not found: {fwpkg_path}")

    log.info("Pushing %s to %s (%.1f MiB)",
             fwpkg_path.name, update_uri, fwpkg_path.stat().st_size / (1024 * 1024))

    task_uri = _post_multipart(session, update_uri, fwpkg_path, show_progress)

    log.info("Task started: %s", task_uri)
    return _poll_task(session, base, task_uri, poll_interval, overall_timeout)


def _post_multipart(
    session: requests.Session,
    update_uri: str,
    fwpkg_path: Path,
    show_progress: bool,
) -> str:
    # Match the working curl exactly:
    #   -F 'UpdateParameters={};type=application/octet-stream'
    #   -F UpdateFile=@<file>.fwpkg
    fh = open(fwpkg_path, "rb")
    try:
        encoder = MultipartEncoder(
            fields=[
                ("UpdateParameters", (None, "{}", "application/octet-stream")),
                ("UpdateFile", (fwpkg_path.name, fh, "application/octet-stream")),
            ]
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

    # Prefer the Task resource (@odata.id in the body) over the Location
    # header. On BlueField the Location points at the TaskMonitor
    # (.../Tasks/<id>/Monitor), which does NOT expose TaskState /
    # PercentComplete / Messages, so polling it spins until timeout.
    # Strip a trailing /Monitor as a safety net if we fall back to Location.
    odata_id = None
    try:
        odata_id = resp.json().get("@odata.id")
    except ValueError:
        odata_id = None

    task_uri = odata_id or resp.headers.get("Location")
    if task_uri:
        task_uri = _strip_monitor(task_uri)

    if not task_uri:
        raise RedfishError(
            f"Firmware POST returned {resp.status_code} but no task Location/@odata.id.\n"
            f"Body: {_safe_body(resp)}"
        )

    return task_uri


def _strip_monitor(uri: str) -> str:
    """Turn a TaskMonitor URI (.../Tasks/<id>/Monitor) into the Task URI."""
    trimmed = uri.rstrip("/")
    if trimmed.endswith("/Monitor"):
        return trimmed[: -len("/Monitor")]
    return uri


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
    not_found = 0

    while True:
        try:
            resp = session.get(full_uri, timeout=(15, 60))
            if resp.status_code == 404:
                not_found += 1
                log.warning(
                    "Task %s returned 404 (%d/%d); it may have completed and "
                    "been reaped by the BMC.",
                    _short_task_id(full_uri), not_found, MAX_TASK_NOT_FOUND,
                )
                if not_found >= MAX_TASK_NOT_FOUND:
                    raise RedfishError(
                        f"Task {full_uri} vanished (404) before a terminal "
                        f"state was observed. The update may have completed; "
                        f"verify via FirmwareInventory."
                    )
                time.sleep(poll_interval)
                continue
            resp.raise_for_status()
            body = resp.json()
            not_found = 0
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
                _print_task_log(result)
                raise RedfishError(
                    f"Firmware task ended in state={state} status={status}. "
                    f"See log for Redfish messages."
                )
            log.info("Firmware update completed successfully (%s).", status or "OK")
            _log_messages(messages, level=logging.INFO)
            _print_task_log(result)
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
        resolution = (m.get("Resolution") or "").strip()
        if resolution and resolution not in ("None.", "None"):
            log.log(level, "  Redfish msg: [%s] %s -- %s  (Resolution: %s)",
                    sev, msg_id, text, resolution)
        else:
            log.log(level, "  Redfish msg: [%s] %s -- %s", sev, msg_id, text)


def _print_task_log(result: "TaskResult") -> None:
    """Print a human-readable summary of the task's Messages once it ends.

    Highlights which components were updated and what activation action
    (e.g. AC/DC power cycle, system reboot) each one still needs, parsed
    from the BMC's ``Update.1.0.*`` messages.
    """
    updated = []        # (device, image)
    activations = {}    # device -> resolution (power-cycle instructions)
    for m in result.messages:
        msg_id = m.get("MessageId", "")
        args = m.get("MessageArgs") or []
        if msg_id == "Update.1.0.UpdateSuccessful" and len(args) >= 2:
            updated.append((args[0], args[1]))
        elif msg_id == "Update.1.0.AwaitToActivate" and len(args) >= 2:
            # args: [image, device]; Resolution holds the activation action.
            activations[args[1]] = (m.get("Resolution") or "").strip()

    sys.stderr.flush()
    bar = "=" * 60
    print()
    print(bar)
    print(f"  Firmware task {result.task_id}: {result.state} "
          f"({result.status or '?'}), {result.percent if result.percent is not None else '?'}%")
    if updated:
        print("  Components updated:")
        for device, image in updated:
            print(f"    - {device}: {image}")
    if activations:
        print("  Activation required (staged, not yet live):")
        for device, action in activations.items():
            print(f"    - {device}: {action or 'power cycle'}")
        print("  >>> Power-cycle / reboot the BF4 to activate the new firmware.")
    print(bar)
    print(flush=True)


def _warn_tls_once() -> None:
    global _TLS_WARNED
    if _TLS_WARNED:
        return
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    log.warning("BMC TLS certificate verification is DISABLED (lab self-signed certs).")
    _TLS_WARNED = True


