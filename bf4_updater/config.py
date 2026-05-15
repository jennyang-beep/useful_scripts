"""Load and validate the bench inventory (benches.yaml).

A bench config maps short names (e.g. ``sw-mtx-perf-003-bf4``) to the
network coordinates and credentials used by the updater. The file is
intentionally kept out of git; users start from
``benches.example.yaml``.

Two kinds of bench entries are supported:

  * **BF4 benches** — have a connected BlueField-4 and therefore expose
    a BMC. They can be used as ``--bf4`` (firmware target) and/or as
    ``--http_server_bench`` (ISO server).
  * **Host-only benches** — plain x86 hosts with no BF4. They can only
    be used as ``--http_server_bench``. Loading them with
    ``require_bmc=True`` raises :class:`ConfigError`.

The script never SSHes into a bench today (it just uses ``host`` to
build the ISO URL), but ``host_user`` / ``host_pass`` may be recorded
for documentation / future use.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml


HOST_REQUIRED_FIELDS = ("host",)
BMC_REQUIRED_FIELDS = ("bmc_host", "bmc_user", "bmc_pass")
PLACEHOLDER_HINT = "<fill in benches.yaml>"


class ConfigError(Exception):
    """Raised when benches.yaml is missing, malformed, or incomplete."""


@dataclass(frozen=True)
class Bench:
    """Resolved coordinates for a single bench."""

    name: str
    host: str
    bmc_host: Optional[str] = None
    bmc_user: Optional[str] = None
    bmc_pass: Optional[str] = None
    host_fqdn: Optional[str] = None
    bmc_fqdn: Optional[str] = None
    host_user: Optional[str] = None
    host_pass: Optional[str] = None

    @property
    def has_bmc(self) -> bool:
        return all([self.bmc_host, self.bmc_user, self.bmc_pass])

    def host_target(self) -> str:
        """Preferred address for reaching the host (FQDN if available)."""
        return self.host_fqdn or self.host

    def bmc_target(self) -> str:
        """Preferred address for reaching the BMC.

        Raises :class:`ConfigError` if the bench has no BMC info.
        """
        if not self.has_bmc:
            raise ConfigError(
                f"Bench '{self.name}' has no BMC information; "
                f"it cannot be used as a firmware target."
            )
        return self.bmc_fqdn or self.bmc_host  # type: ignore[return-value]


def default_config_path() -> Path:
    """Path to ``benches.yaml`` next to this module."""
    return Path(__file__).resolve().parent / "benches.yaml"


def load_bench(
    name: str,
    path: Optional[Path] = None,
    *,
    require_bmc: bool = True,
) -> Bench:
    """Load ``benches.yaml`` and resolve the requested bench.

    Set ``require_bmc=False`` when the caller only needs host info
    (e.g. an ISO HTTP server location); host-only benches will then be
    accepted.

    Raises :class:`ConfigError` with a user-actionable message if the
    file is missing, the bench name is unknown, or any required field
    is empty / still set to the placeholder string.
    """

    cfg_path = path or default_config_path()

    if not cfg_path.exists():
        example = cfg_path.with_name("benches.example.yaml")
        raise ConfigError(
            f"Bench config not found: {cfg_path}\n"
            f"Copy {example.name} to {cfg_path.name} and fill in credentials."
        )

    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse {cfg_path}: {exc}") from exc

    benches = raw.get("benches")
    if not isinstance(benches, dict) or not benches:
        raise ConfigError(
            f"{cfg_path} must contain a non-empty top-level 'benches:' mapping."
        )

    if name not in benches:
        known = ", ".join(sorted(benches)) or "(none)"
        raise ConfigError(
            f"Bench '{name}' not found in {cfg_path}. Known benches: {known}"
        )

    entry = benches[name]
    if not isinstance(entry, dict):
        raise ConfigError(f"Bench '{name}' in {cfg_path} must be a mapping.")

    required: List[str] = list(HOST_REQUIRED_FIELDS)
    if require_bmc:
        required.extend(BMC_REQUIRED_FIELDS)

    missing: List[str] = []
    placeholder: List[str] = []
    for field in required:
        value = entry.get(field)
        if value is None or value == "":
            missing.append(field)
        elif isinstance(value, str) and value.strip() == PLACEHOLDER_HINT:
            placeholder.append(field)

    if missing or placeholder:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if placeholder:
            details.append(f"still placeholder: {', '.join(placeholder)}")
        role = "BF4 (firmware target)" if require_bmc else "host"
        raise ConfigError(
            f"Bench '{name}' is incomplete for use as a {role} in {cfg_path} "
            f"({'; '.join(details)})."
        )

    return Bench(
        name=name,
        host=str(entry["host"]),
        bmc_host=_optional_str(entry.get("bmc_host")),
        bmc_user=_optional_str(entry.get("bmc_user")),
        bmc_pass=_optional_str(entry.get("bmc_pass")),
        host_fqdn=_optional_str(entry.get("host_fqdn")),
        bmc_fqdn=_optional_str(entry.get("bmc_fqdn")),
        host_user=_optional_str(entry.get("host_user")),
        host_pass=_optional_str(entry.get("host_pass")),
    )


def list_benches(path: Optional[Path] = None) -> Dict[str, dict]:
    """Return the raw ``benches:`` mapping (without validation).

    Useful for CLI helpers like ``--list-benches``.
    """
    cfg_path = path or default_config_path()
    if not cfg_path.exists():
        return {}
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    benches = raw.get("benches") or {}
    return benches if isinstance(benches, dict) else {}


def _optional_str(value) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)
