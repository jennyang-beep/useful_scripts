#!/usr/bin/env python3
"""CLI: update BF4 firmware (PLDM) via BMC and serve a DOCA OS ISO over HTTP.

Two roles, both resolved from ``benches.yaml``:

  * ``--bf4 <bench>``                 : whose BMC receives the firmware
  * ``--http_server_bench <bench>``   : whose host runs the local HTTP server
                                       (its ``host`` becomes the URL the BMC
                                       reaches for the ISO)

Either flag may be omitted when the matching step is skipped via
``--skip-firmware`` / ``--skip-os``. The two flags may name the same bench.

Run ``python update_bf4.py --help`` for full usage.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from bf4_updater import (
        artifactory, config, downloader, host_ops, iso_server,
        redfish_account, redfish_fw,
    )
else:
    from . import (
        artifactory, config, downloader, host_ops, iso_server,
        redfish_account, redfish_fw,
    )


log = logging.getLogger("bf4_updater")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="update_bf4",
        description=(
            "Update BF4 firmware (PLDM via BMC Redfish) and stage a DOCA OS "
            "ISO on a local HTTP server for manual install via the BMC."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--bf4",
        dest="bf4",
        help=(
            "Bench whose BMC receives the firmware update "
            "(key under 'benches:' in benches.yaml). "
            "Required unless --skip-firmware."
        ),
    )
    p.add_argument(
        "--http_server_bench",
        "--http-server-bench",
        dest="http_server_bench",
        help=(
            "Bench whose host runs the local HTTP server for the ISO "
            "(its 'host' is advertised as the URL). "
            "Required unless --skip-os."
        ),
    )
    p.add_argument(
        "--pldm-url",
        help=(
            "Artifactory directory URL containing the BF4 .fwpkg "
            "(e.g. https://urm.nvidia.com/artifactory/.../MT_0000001775-01/). "
            "Required unless --skip-firmware."
        ),
    )
    p.add_argument(
        "--iso-url",
        help="Direct URL to the DOCA OS .iso. Required unless --skip-os.",
    )
    p.add_argument(
        "--workdir",
        type=Path,
        default=Path(__file__).resolve().parent / "downloads",
        help="Local directory for downloaded firmware and ISO files.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to benches.yaml (defaults to benches.yaml next to this script).",
    )
    p.add_argument(
        "--http-port",
        type=int,
        default=8000,
        help="Port the local HTTP server listens on for the ISO.",
    )
    p.add_argument(
        "--http-bind",
        default="0.0.0.0",
        help="Bind address for the HTTP server.",
    )
    p.add_argument(
        "--http-advertise",
        default=None,
        help=(
            "Override the hostname/IP used in the printed ISO URL. "
            "Defaults to the --http_server_bench's host."
        ),
    )
    p.add_argument(
        "--skip-firmware",
        action="store_true",
        help="Skip the PLDM firmware update step.",
    )
    p.add_argument(
        "--skip-os",
        action="store_true",
        help="Skip the OS ISO download + HTTP-serve step.",
    )
    # --- First-time install -----------------------------------------------
    p.add_argument(
        "--first-install",
        action="store_true",
        help=(
            "Treat the BF4 as never-installed: SSH into the bench host to "
            "burn a DPU-personality firmware via flint, prompt for a power "
            "cycle, then rotate the BMC default credentials BEFORE the "
            "regular .fwpkg push. Requires --bf4 and --host-fw-url."
        ),
    )
    p.add_argument(
        "--host-fw-url",
        help=(
            "URL of the DPU-personality .bin to wget on the host (used "
            "by --first-install). Build-specific, e.g. "
            "http://nbu-nfs.mellanox.com/.../fw-ConnectX9-rel-...-DPU_Ax-...bin"
        ),
    )
    p.add_argument(
        "--default-bmc-user",
        default="service",
        help="BMC default username, used only by --first-install.",
    )
    p.add_argument(
        "--default-bmc-pass",
        default="0penBmc",
        help="BMC default password, used only by --first-install.",
    )
    p.add_argument(
        "--first-install-rotate-users",
        default="service,root",
        help=(
            "Comma-separated list of BMC accounts whose password should be "
            "rotated to the bench's bmc_pass during --first-install. The "
            "configured bmc_user is always rotated in addition to these."
        ),
    )
    p.add_argument(
        "--first-install-host",
        default=None,
        help=(
            "Bench whose host runs the flint burn (the x86 server that "
            "has the BF4 PCIe card). If omitted, derived from --bf4 by "
            "stripping the '-bf4' suffix (e.g. --bf4 sw-mtx-065-bf4 "
            "implies --first-install-host sw-mtx-065)."
        ),
    )
    p.add_argument(
        "--host-workdir",
        default=host_ops.DEFAULT_WORKDIR,
        help="Remote directory on the burn host for the FW bin (--first-install).",
    )
    p.add_argument(
        "--mst-device",
        default=host_ops.DEFAULT_MST_DEVICE,
        help="MST device path used by `flint -d` on the burn host (--first-install).",
    )
    p.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable tqdm progress bars (useful in CI logs).",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging (includes Redfish payloads).",
    )
    return p


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    if args.skip_firmware and args.skip_os and not args.first_install:
        log.error("Both --skip-firmware and --skip-os given; nothing to do.")
        return 2

    # --first-install does work BEFORE _do_firmware, so it implies the
    # firmware step (--skip-firmware is incompatible with it).
    if args.first_install:
        if args.skip_firmware:
            log.error("--first-install cannot be combined with --skip-firmware.")
            return 2
        if not args.bf4:
            log.error("--first-install requires --bf4.")
            return 2
        if not args.host_fw_url:
            log.error("--first-install requires --host-fw-url <DPU-personality .bin URL>.")
            return 2

    if not args.skip_firmware:
        if not args.bf4:
            log.error("--bf4 is required unless --skip-firmware is set.")
            return 2
        if not args.pldm_url:
            log.error("--pldm-url is required unless --skip-firmware is set.")
            return 2
    if not args.skip_os:
        if not args.http_server_bench:
            log.error("--http_server_bench is required unless --skip-os is set.")
            return 2
        if not args.iso_url:
            log.error("--iso-url is required unless --skip-os is set.")
            return 2

    # Resolve the two bench roles. Reuse the loaded object when both flags
    # name the same bench so we only parse / validate it once.
    bf4_bench: Optional[config.Bench] = None
    http_bench: Optional[config.Bench] = None

    try:
        if not args.skip_firmware:
            bf4_bench = config.load_bench(args.bf4, path=args.config, require_bmc=True)
            log.info(
                "BF4 (firmware target): %s -> bmc=%s host=%s",
                bf4_bench.name, bf4_bench.bmc_target(), bf4_bench.host_target(),
            )
        if not args.skip_os:
            if bf4_bench is not None and args.http_server_bench == args.bf4:
                http_bench = bf4_bench
            else:
                http_bench = config.load_bench(
                    args.http_server_bench, path=args.config, require_bmc=False,
                )
            log.info(
                "HTTP server bench: %s -> host=%s",
                http_bench.name, http_bench.host_target(),
            )
    except config.ConfigError as exc:
        log.error("%s", exc)
        return 2

    burn_bench: Optional[config.Bench] = None
    if args.first_install:
        assert bf4_bench is not None
        burn_bench_name = args.first_install_host
        if not burn_bench_name:
            if bf4_bench.name.endswith("-bf4"):
                burn_bench_name = bf4_bench.name[: -len("-bf4")]
            else:
                log.error(
                    "Could not auto-derive --first-install-host from --bf4=%s "
                    "(name doesn't end in '-bf4'). Pass --first-install-host explicitly.",
                    bf4_bench.name,
                )
                return 2
        try:
            burn_bench = config.load_bench(
                burn_bench_name, path=args.config, require_bmc=False,
            )
        except config.ConfigError as exc:
            log.error(
                "Could not resolve --first-install-host bench '%s': %s",
                burn_bench_name, exc,
            )
            return 2
        if not (burn_bench.host_user and burn_bench.host_pass):
            log.error(
                "--first-install needs SSH credentials on bench '%s'. "
                "Add 'host_user' and 'host_pass' to it in benches.yaml.",
                burn_bench.name,
            )
            return 2
        log.info(
            "First-install burn host: %s -> %s (user=%s)",
            burn_bench.name, burn_bench.host_target(), burn_bench.host_user,
        )

    show_progress = not args.no_progress
    args.workdir.mkdir(parents=True, exist_ok=True)

    if args.first_install:
        assert bf4_bench is not None and burn_bench is not None
        rc = _do_first_install(bf4_bench, burn_bench, args)
        if rc != 0:
            return rc

    if not args.skip_firmware:
        assert bf4_bench is not None
        rc = _do_firmware(bf4_bench, args, show_progress)
        if rc != 0:
            return rc

    if not args.skip_os:
        assert http_bench is not None
        return _do_os(http_bench, args, show_progress)

    log.info("All requested steps completed.")
    return 0


def _do_first_install(
    bf4: config.Bench,
    burn: config.Bench,
    args: argparse.Namespace,
) -> int:
    """Host-side DPU personality burn + BMC default-credential rotation.

    Runs BEFORE _do_firmware so that the subsequent .fwpkg push uses the
    rotated BMC password (the bench's ``bmc_pass``).

    Two distinct benches are involved:

      * ``burn`` -- the x86 host that has the BF4 PCIe card and runs
        the ``flint`` burn (uses its host_user / host_pass for SSH).
      * ``bf4``  -- the BF4 itself, whose BMC gets its default
        credentials rotated.
    """
    log.info("=== --first-install for BF4 %s (burn host: %s) ===", bf4.name, burn.name)
    log.info(
        "Plan: host burn on %s -> power-cycle prompt -> BMC password rotation on %s",
        burn.host_target(), bf4.bmc_target(),
    )

    # 1) Host: mkdir + wget + flint b
    try:
        host_ops.burn_dpu_personality(
            host=burn.host_target(),
            user=burn.host_user,  # type: ignore[arg-type]
            password=burn.host_pass,  # type: ignore[arg-type]
            fw_bin_url=args.host_fw_url,
            workdir=args.host_workdir,
            mst_device=args.mst_device,
        )
    except host_ops.HostOpsError as exc:
        log.error("Host burn failed: %s", exc)
        return 7

    # 2) Power-cycle the host (manual, with confirmation prompt)
    try:
        host_ops.confirm_power_cycle(burn.host_target())
    except host_ops.HostOpsError as exc:
        log.error("%s", exc)
        return 7

    # 3) BMC: rotate default creds to bf4.bmc_pass
    rotate_users = [u.strip() for u in args.first_install_rotate_users.split(",") if u.strip()]
    if bf4.bmc_user not in rotate_users:
        rotate_users.append(bf4.bmc_user)  # type: ignore[arg-type]

    log.info(
        "Rotating BMC passwords on %s for users: %s",
        bf4.bmc_target(), ", ".join(rotate_users),
    )
    try:
        result = redfish_account.first_install_rotate(
            bmc_host=bf4.bmc_target(),
            default_user=args.default_bmc_user,
            default_pass=args.default_bmc_pass,
            new_password=bf4.bmc_pass,  # type: ignore[arg-type]
            target_users=rotate_users,
        )
    except redfish_account.RedfishAccountError as exc:
        log.error("BMC password rotation failed: %s", exc)
        return 8

    log.info(
        "BMC password rotation: rotated=%s skipped=%s failed=%s",
        result.rotated,
        [u for u, _ in result.skipped],
        [u for u, _ in result.failed],
    )

    # 4) Sanity: verify we can log in as bf4.bmc_user with bf4.bmc_pass
    try:
        redfish_account.verify_login(
            bmc_host=bf4.bmc_target(),
            user=bf4.bmc_user,  # type: ignore[arg-type]
            password=bf4.bmc_pass,  # type: ignore[arg-type]
        )
    except redfish_account.RedfishAccountError as exc:
        log.error(
            "Configured bmc_user '%s' cannot log in to %s after rotation: %s",
            bf4.bmc_user, bf4.bmc_target(), exc,
        )
        return 8

    log.info("=== --first-install completed; proceeding to fwpkg push ===")
    return 0


def _do_firmware(bench: config.Bench, args: argparse.Namespace, show_progress: bool) -> int:
    fw_dir = args.workdir / "firmware"
    log.info("=== Firmware update for BF4 %s (BMC %s) ===", bench.name, bench.bmc_target())
    sess = downloader.make_session(verify=True)

    try:
        chosen = artifactory.find_fwpkg(args.pldm_url, session=sess)
    except artifactory.ArtifactoryError as exc:
        log.error("%s", exc)
        return 3

    try:
        result = downloader.download(
            chosen.download_url,
            fw_dir,
            filename=chosen.name,
            expected_sha256=chosen.sha256,
            session=sess,
            verify_tls=True,
            show_progress=show_progress,
        )
    except downloader.DownloadError as exc:
        log.error("Firmware download failed: %s", exc)
        return 3

    log.info("Firmware ready: %s (%d bytes)", result.path, result.size)

    try:
        redfish_fw.push_firmware(
            bmc_host=bench.bmc_target(),
            bmc_user=bench.bmc_user,
            bmc_pass=bench.bmc_pass,
            fwpkg_path=result.path,
            show_progress=show_progress,
        )
    except redfish_fw.RedfishError as exc:
        log.error("Firmware update failed on BMC %s: %s", bench.bmc_target(), exc)
        return 4

    log.info("=== Firmware update OK ===")
    return 0


def _do_os(http_bench: config.Bench, args: argparse.Namespace, show_progress: bool) -> int:
    iso_dir = args.workdir / "iso"
    log.info("=== OS ISO staging on %s ===", http_bench.name)

    sess = downloader.make_session(verify=True)
    try:
        result = downloader.download(
            args.iso_url,
            iso_dir,
            session=sess,
            verify_tls=True,
            show_progress=show_progress,
        )
    except downloader.DownloadError as exc:
        log.error("ISO download failed: %s", exc)
        return 5

    log.info("ISO ready: %s (%d bytes)", result.path, result.size)

    advertise = args.http_advertise or http_bench.host_target()

    try:
        served = iso_server.serve_iso(
            result.path,
            bind=args.http_bind,
            port=args.http_port,
            advertise_host=advertise,
        )
    except iso_server.IsoServerError as exc:
        log.error("Could not serve ISO: %s", exc)
        return 6

    _print_banner(served.url, served.iso_path, http_bench.name)

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Shutting down ISO HTTP server...")
        served.shutdown()
        return 0


def _print_banner(url: str, iso_path: Path, bench_name: str) -> None:
    bar = "=" * max(len(url), 40) + "===="
    sys.stderr.flush()
    print()
    print(bar)
    print(f"  ISO ready on bench: {bench_name}")
    print(f"  File   : {iso_path.name}")
    print(f"  URL    : {url}")
    print(f"  Verify : curl -I {url}")
    print()
    print("  Use this URL in the BMC virtual-media UI to install BF4 OS.")
    print("  Press Ctrl+C to stop the HTTP server when you're done.")
    print(bar)
    print(flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
