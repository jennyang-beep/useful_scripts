"""Streaming HTTP downloader with progress, resume, and sha256 verification.

Used both for the firmware ``.fwpkg`` (from Artifactory) and the DOCA OS
``.iso`` (from the NFS mirror). Designed to handle the multi-GB ISOs
without loading them into memory and to survive transient network blips.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry


log = logging.getLogger(__name__)


CHUNK_SIZE = 1024 * 1024  # 1 MiB
DEFAULT_TIMEOUT = (10, 60)  # (connect, read) seconds


class DownloadError(Exception):
    """Raised when a download or checksum verification fails."""


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size: int
    sha256: Optional[str]
    reused: bool  # True if we found an already-complete file on disk


def make_session(verify: bool = True) -> requests.Session:
    """A requests Session with sane retry/backoff defaults."""
    session = requests.Session()
    session.verify = verify
    retry = Retry(
        total=5,
        connect=5,
        read=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def download(
    url: str,
    dest_dir: Path,
    *,
    filename: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    session: Optional[requests.Session] = None,
    verify_tls: bool = True,
    show_progress: bool = True,
) -> DownloadResult:
    """Download ``url`` into ``dest_dir``.

    - Skips the download entirely (no HTTP request at all) if the final
      file already exists locally. When ``expected_sha256`` is given the
      cached file's sha256 is verified; on mismatch it is redownloaded.
    - Resumes via HTTP Range if a partial ``<file>.part`` exists.
    - Verifies sha256 after download when ``expected_sha256`` is set.
    """

    dest_dir.mkdir(parents=True, exist_ok=True)
    name = filename or _basename_from_url(url)
    final_path = dest_dir / name
    part_path = dest_dir / (name + ".part")

    sess = session or make_session(verify=verify_tls)

    # Fast path: trust an already-staged file and skip the network entirely.
    # Saves a HEAD round-trip and avoids re-downloading multi-GB artifacts
    # when HEAD fails (e.g. no Content-Length from the NFS mirror).
    if final_path.exists():
        existing_size = final_path.stat().st_size
        if expected_sha256:
            sha = _sha256_file(final_path)
            if sha == expected_sha256:
                log.info(
                    "Already downloaded (sha256 verified): %s (%d bytes); skipping HTTP fetch.",
                    final_path, existing_size,
                )
                return DownloadResult(final_path, existing_size, sha, reused=True)
            log.warning(
                "Existing %s has wrong sha256 (%s != %s); redownloading.",
                final_path.name, sha, expected_sha256,
            )
            final_path.unlink()
        else:
            log.info(
                "Already downloaded: %s (%d bytes); skipping HTTP fetch.",
                final_path, existing_size,
            )
            return DownloadResult(final_path, existing_size, None, reused=True)

    remote_size = _head_content_length(sess, url)

    headers = {}
    mode = "wb"
    initial = 0
    if part_path.exists():
        initial = part_path.stat().st_size
        if remote_size is not None and initial >= remote_size:
            part_path.unlink()
            initial = 0
        else:
            headers["Range"] = f"bytes={initial}-"
            mode = "ab"
            log.info("Resuming download of %s at byte %d", name, initial)

    log.info("Downloading %s -> %s", url, part_path)
    try:
        with sess.get(url, stream=True, headers=headers, timeout=DEFAULT_TIMEOUT) as resp:
            if resp.status_code == 416:  # range not satisfiable, start over
                part_path.unlink(missing_ok=True)
                initial = 0
                mode = "wb"
                resp = sess.get(url, stream=True, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()

            total = remote_size
            if total is None:
                cl = resp.headers.get("Content-Length")
                total = (int(cl) + initial) if cl is not None else None

            progress = None
            if show_progress:
                progress = tqdm(
                    total=total,
                    initial=initial,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=name,
                )

            try:
                with open(part_path, mode) as fh:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        if progress is not None:
                            progress.update(len(chunk))
            finally:
                if progress is not None:
                    progress.close()
    except requests.RequestException as exc:
        raise DownloadError(f"Failed to download {url}: {exc}") from exc

    os.replace(part_path, final_path)
    final_size = final_path.stat().st_size

    if remote_size is not None and final_size != remote_size:
        raise DownloadError(
            f"Size mismatch for {final_path}: got {final_size}, expected {remote_size}"
        )

    sha = None
    if expected_sha256:
        sha = _sha256_file(final_path)
        if sha != expected_sha256:
            raise DownloadError(
                f"sha256 mismatch for {final_path}: got {sha}, expected {expected_sha256}"
            )
        log.info("sha256 verified: %s", sha)

    return DownloadResult(final_path, final_size, sha, reused=False)


def _head_content_length(session: requests.Session, url: str) -> Optional[int]:
    try:
        resp = session.head(url, allow_redirects=True, timeout=DEFAULT_TIMEOUT)
        if resp.status_code >= 400:
            return None
        cl = resp.headers.get("Content-Length")
        return int(cl) if cl is not None else None
    except (requests.RequestException, ValueError):
        return None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _basename_from_url(url: str) -> str:
    # Strip query string / fragment, then take the last path segment.
    from urllib.parse import urlparse

    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    if not name:
        raise DownloadError(f"Cannot infer filename from URL: {url}")
    return name
