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
    from bf4_updater import artifactory, config, downloader, iso_server, redfish_fw
else:
    from . import artifactory, config, downloader, iso_server, redfish_fw


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
    p.add_argument(
        "--apply-time",
        default="Immediate",
        choices=["Immediate", "OnReset"],
        help="Redfish OperationApplyTime for the firmware update.",
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

    if args.skip_firmware and args.skip_os:
        log.error("Both --skip-firmware and --skip-os given; nothing to do.")
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

    show_progress = not args.no_progress
    args.workdir.mkdir(parents=True, exist_ok=True)

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
            apply_time=args.apply_time,
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
