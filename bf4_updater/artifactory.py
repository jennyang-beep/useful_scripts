"""Discover the BF4 PLDM ``.fwpkg`` inside an Artifactory directory URL.

Artifactory exposes a JSON listing for any folder via the ``?list`` query
parameter, e.g.

    GET https://urm.nvidia.com/artifactory/api/storage/<repo>/<path>?list&deep=0

We translate the user-facing browse URL (``/artifactory/<repo>/<path>/``)
into the matching ``/artifactory/api/storage/<repo>/<path>`` URL, fetch
the JSON, and pick the single file that ends in ``.fwpkg``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

import requests

from .downloader import DEFAULT_TIMEOUT, make_session


log = logging.getLogger(__name__)


class ArtifactoryError(Exception):
    """Raised when the Artifactory listing fails or no fwpkg can be picked."""


@dataclass(frozen=True)
class ArtifactoryFile:
    name: str
    download_url: str
    size: Optional[int] = None
    sha256: Optional[str] = None


def find_fwpkg(
    dir_url: str,
    *,
    session: Optional[requests.Session] = None,
    suffix: str = ".fwpkg",
) -> ArtifactoryFile:
    """Return the single ``*.fwpkg`` in ``dir_url`` (raises if 0 or >1)."""

    sess = session or make_session(verify=True)
    files = _list_directory(sess, dir_url)

    matches = [f for f in files if f.name.lower().endswith(suffix)]
    if not matches:
        names = ", ".join(f.name for f in files) or "(empty directory)"
        raise ArtifactoryError(
            f"No '*{suffix}' file found in {dir_url}. Directory contains: {names}"
        )
    if len(matches) > 1:
        names = ", ".join(f.name for f in matches)
        raise ArtifactoryError(
            f"Expected exactly one '*{suffix}' in {dir_url}, found {len(matches)}: {names}"
        )

    chosen = matches[0]
    log.info("Selected firmware: %s (%s)", chosen.name, chosen.download_url)
    return chosen


def _list_directory(session: requests.Session, dir_url: str) -> List[ArtifactoryFile]:
    api_url = _to_storage_api_url(dir_url)
    log.debug("Listing Artifactory: %s", api_url)
    try:
        resp = session.get(
            api_url,
            params={"list": "", "deep": "0", "listFolders": "0"},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise ArtifactoryError(f"Failed to list {api_url}: {exc}") from exc
    except ValueError as exc:
        raise ArtifactoryError(f"Artifactory did not return JSON for {api_url}: {exc}") from exc

    base_browse = _ensure_trailing_slash(dir_url)
    out: List[ArtifactoryFile] = []
    for item in data.get("files", []):
        if item.get("folder"):
            continue
        # The "uri" field is "/foo.fwpkg" (with leading slash).
        rel = item.get("uri", "").lstrip("/")
        if not rel:
            continue
        out.append(
            ArtifactoryFile(
                name=rel,
                download_url=base_browse + rel,
                size=item.get("size"),
                sha256=(item.get("sha2") or None),
            )
        )
    return out


def _to_storage_api_url(browse_url: str) -> str:
    """Convert /artifactory/<repo>/<path>/ -> /artifactory/api/storage/<repo>/<path>."""
    parsed = urlparse(browse_url)
    parts = [p for p in parsed.path.split("/") if p]

    if not parts or parts[0] != "artifactory":
        raise ArtifactoryError(
            f"URL does not look like an Artifactory path: {browse_url}"
        )

    if len(parts) >= 3 and parts[1] == "api" and parts[2] == "storage":
        # User already passed an api/storage URL.
        new_path = "/" + "/".join(parts)
    else:
        new_path = "/" + "/".join(["artifactory", "api", "storage"] + parts[1:])

    return urlunparse(parsed._replace(path=new_path, query="", fragment=""))


def _ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"
