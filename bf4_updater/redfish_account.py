"""Redfish AccountService helpers used by --first-install.

When a BF4 BMC ships with default credentials (``service`` / ``0penBmc``
on NVIDIA OpenBMC), the standard pre-flight is:

  1. PATCH ``/redfish/v1/AccountService/Accounts/service`` with
     ``{"Password": "<new>"}`` using HTTP Basic ``service:0penBmc``.
     The BMC frequently *requires* this on first login, so we do it
     unconditionally.
  2. PATCH ``/redfish/v1/AccountService/Accounts/root`` with the same
     new password, now authenticated with the new ``service`` creds.
  3. PATCH the bench's configured ``bmc_user`` (e.g. ``admin``) too,
     so the regular update flow keeps working with the same password.
  4. Verify by GETting ``/redfish/v1/AccountService/Accounts/<bmc_user>``
     with the new credentials.

Accounts that don't exist (404) are logged at WARNING and skipped so
that BMCs without an ``admin`` account don't make the whole flow fail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry


log = logging.getLogger(__name__)


_TLS_WARNED = False


class RedfishAccountError(Exception):
    """Raised on any unrecoverable AccountService failure."""


@dataclass
class RotateResult:
    rotated: List[str]   # accounts whose password was successfully changed
    skipped: List[Tuple[str, str]]  # (account, reason) for non-existent accounts
    failed: List[Tuple[str, str]]   # (account, reason) for hard failures


def first_install_rotate(
    bmc_host: str,
    *,
    default_user: str,
    default_pass: str,
    new_password: str,
    target_users: Optional[List[str]] = None,
) -> RotateResult:
    """Rotate passwords on a fresh BMC.

    The first user in ``target_users`` is patched using
    ``default_user`` / ``default_pass`` (Basic Auth). Subsequent users
    are patched using ``default_user`` with the new password (assuming
    ``default_user`` is one of the target_users — which is the common
    case where ``service`` has its own password rotated first).

    Returns a :class:`RotateResult`. Raises
    :class:`RedfishAccountError` only when the very first PATCH (the
    one that proves we can talk to the BMC at all) fails.
    """
    _warn_tls_once()
    if not target_users:
        target_users = [default_user, "root"]

    # Always rotate the default_user first so that we can use the new
    # password for everything that follows. Dedupe while preserving order.
    ordered: List[str] = []
    seen = set()
    for u in [default_user] + target_users:
        if u not in seen:
            ordered.append(u)
            seen.add(u)

    session = _session()
    base = _base_url(bmc_host)
    rotated: List[str] = []
    skipped: List[Tuple[str, str]] = []
    failed: List[Tuple[str, str]] = []

    current_pass_for_default = default_pass

    for idx, user in enumerate(ordered):
        # Use default creds for the first user; the new password for the rest
        # (default_user is always first thanks to the dedupe above).
        auth = HTTPBasicAuth(default_user, current_pass_for_default)
        url = urljoin(base, f"/redfish/v1/AccountService/Accounts/{user}")
        try:
            resp = session.patch(
                url,
                json={"Password": new_password},
                auth=auth,
                timeout=(15, 60),
            )
        except requests.RequestException as exc:
            msg = f"PATCH {url} raised {type(exc).__name__}: {exc}"
            if idx == 0:
                raise RedfishAccountError(msg) from exc
            failed.append((user, msg))
            log.error("%s", msg)
            continue

        if resp.status_code in (200, 204):
            log.info("Rotated password for BMC account '%s'.", user)
            rotated.append(user)
            if user == default_user:
                # All subsequent PATCHes use the new password.
                current_pass_for_default = new_password
            continue

        if resp.status_code == 404:
            log.warning("BMC account '%s' does not exist (404); skipping.", user)
            skipped.append((user, "404 Not Found"))
            continue

        # Anything else is a real failure.
        body = _safe_body(resp)
        msg = f"PATCH {url} returned HTTP {resp.status_code}\n{body}"
        if idx == 0:
            raise RedfishAccountError(msg)
        failed.append((user, msg))
        log.error("%s", msg)

    return RotateResult(rotated=rotated, skipped=skipped, failed=failed)


def verify_login(bmc_host: str, user: str, password: str) -> None:
    """Confirm ``user``/``password`` can read ``AccountService/Accounts/<user>``."""
    _warn_tls_once()
    session = _session()
    url = urljoin(_base_url(bmc_host), f"/redfish/v1/AccountService/Accounts/{user}")
    try:
        resp = session.get(url, auth=HTTPBasicAuth(user, password), timeout=(15, 30))
    except requests.RequestException as exc:
        raise RedfishAccountError(
            f"Login verification GET {url} raised {type(exc).__name__}: {exc}"
        ) from exc

    if resp.status_code == 200:
        log.info("Login verification OK for '%s' on %s.", user, bmc_host)
        return
    raise RedfishAccountError(
        f"Login verification failed for '{user}': HTTP {resp.status_code}\n{_safe_body(resp)}"
    )


def _session() -> requests.Session:
    s = requests.Session()
    s.verify = False  # BMC self-signed certs
    retry = Retry(
        total=3,
        connect=5,
        read=3,
        backoff_factor=2.0,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _base_url(bmc_host: str) -> str:
    if bmc_host.startswith("http://") or bmc_host.startswith("https://"):
        return bmc_host.rstrip("/") + "/"
    return f"https://{bmc_host}/"


def _safe_body(resp: requests.Response) -> str:
    try:
        import json
        return json.dumps(resp.json(), indent=2)
    except ValueError:
        return resp.text[:2000]


def _warn_tls_once() -> None:
    global _TLS_WARNED
    if _TLS_WARNED:
        return
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    log.warning("BMC TLS certificate verification is DISABLED (lab self-signed certs).")
    _TLS_WARNED = True
