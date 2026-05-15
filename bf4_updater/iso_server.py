"""Serve the downloaded DOCA OS ISO over HTTP from the runner.

Spawns a ``ThreadingHTTPServer`` rooted at the directory that holds the
ISO, detects the runner's outward-facing LAN IP, HEAD-verifies the
resulting URL (so we don't lie to the user about reachability), and
then blocks in the foreground so the user can complete the install via
the BMC's virtual-media UI.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import requests


log = logging.getLogger(__name__)


class IsoServerError(Exception):
    """Raised when the HTTP server can't be started or its URL isn't reachable."""


@dataclass
class ServedIso:
    iso_path: Path
    url: str
    bind_host: str
    port: int
    server: ThreadingHTTPServer
    thread: threading.Thread

    def shutdown(self) -> None:
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()


class _RootedHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler rooted at a fixed directory.

    Subclassing lets us avoid changing the process CWD (which would
    interfere with downloads still in flight).
    """

    serve_root: Path = Path(".")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.serve_root), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        log.info("http: %s - %s", self.address_string(), fmt % args)


def serve_iso(
    iso_path: Path,
    *,
    bind: str = "0.0.0.0",
    port: int = 8000,
    advertise_host: Optional[str] = None,
    head_verify_timeout: float = 10.0,
) -> ServedIso:
    """Start an HTTP server serving the directory containing ``iso_path``.

    The published URL is built from ``advertise_host`` if given, else
    auto-detected via :func:`detect_lan_ip`. After starting the server
    the function performs a ``HEAD`` request against the URL to confirm
    it returns ``200`` with a ``Content-Length`` matching the file on
    disk; raises :class:`IsoServerError` otherwise.
    """
    iso_path = Path(iso_path).resolve()
    if not iso_path.is_file():
        raise IsoServerError(f"ISO not found: {iso_path}")

    serve_root = iso_path.parent

    handler_cls = type(
        "BoundHandler",
        (_RootedHandler,),
        {"serve_root": serve_root},
    )

    try:
        server = ThreadingHTTPServer((bind, port), handler_cls)
    except OSError as exc:
        raise IsoServerError(
            f"Failed to bind HTTP server to {bind}:{port}: {exc}"
        ) from exc

    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name=f"iso-http-{port}",
        daemon=True,
    )
    thread.start()

    advertise = advertise_host or detect_lan_ip()
    url = f"http://{advertise}:{port}/{iso_path.name}"

    try:
        _verify_url(url, expected_size=iso_path.stat().st_size, timeout=head_verify_timeout)
    except IsoServerError:
        server.shutdown()
        server.server_close()
        raise

    log.info("ISO available at: %s", url)
    return ServedIso(
        iso_path=iso_path,
        url=url,
        bind_host=bind,
        port=port,
        server=server,
        thread=thread,
    )


def detect_lan_ip() -> str:
    """Best-effort guess of the runner's outward-facing IP.

    Opens a UDP socket toward a non-routed address; the OS picks the
    interface it would use to reach it, which is exactly what we want.
    Falls back to ``socket.gethostname()`` resolution if that fails.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def _verify_url(url: str, *, expected_size: int, timeout: float) -> None:
    # Allow the server thread a moment to be ready to accept().
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None

    while time.monotonic() < deadline:
        try:
            resp = requests.head(url, timeout=(2, 5), allow_redirects=True)
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(0.25)
            continue

        if resp.status_code != 200:
            raise IsoServerError(
                f"ISO URL {url} returned HTTP {resp.status_code} (expected 200)"
            )

        cl_raw = resp.headers.get("Content-Length")
        if cl_raw is None:
            raise IsoServerError(f"ISO URL {url} did not return a Content-Length header")
        try:
            cl = int(cl_raw)
        except ValueError as exc:
            raise IsoServerError(
                f"ISO URL {url} returned invalid Content-Length: {cl_raw!r}"
            ) from exc

        if cl != expected_size:
            raise IsoServerError(
                f"ISO URL {url} reports {cl} bytes but file on disk is {expected_size}"
            )
        return

    raise IsoServerError(
        f"ISO URL {url} did not become reachable within {timeout:.1f}s: {last_error}"
    )
