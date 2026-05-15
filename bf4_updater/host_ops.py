"""SSH operations on a bench host (used by --first-install).

Provides a thin wrapper around paramiko for the host-side steps of a
first-time BF4 install:

    1. mkdir -p ~/jenny  (cd into it)
    2. wget -nc <fw_bin_url>
    3. flint -d <mst_device> -i <bin> b
    4. ask the user to confirm the host has been power-cycled
       (the new firmware doesn't take effect until that happens)

The bench's ``host_user`` / ``host_pass`` from ``benches.yaml`` are used
for password authentication; SSH host-key verification is intentionally
disabled (lab hosts, fresh installs, etc.).
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

import paramiko


log = logging.getLogger(__name__)


DEFAULT_WORKDIR = "~/jenny"
DEFAULT_MST_DEVICE = "/dev/mst/mt4133_pciconf0"
DEFAULT_SSH_TIMEOUT = 30  # seconds for the initial connect

# Long enough for a flint burn (~minutes for a BF4 firmware image).
FLINT_BURN_TIMEOUT = 30 * 60


class HostOpsError(Exception):
    """Raised on any unrecoverable host-side failure."""


@dataclass
class CommandResult:
    cmd: str
    exit_code: int
    stdout: str
    stderr: str

    def check(self) -> "CommandResult":
        if self.exit_code != 0:
            raise HostOpsError(
                f"Remote command failed (exit {self.exit_code}): {self.cmd}\n"
                f"--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"
            )
        return self


@contextmanager
def ssh_session(host: str, user: str, password: str, port: int = 22):
    """Yield a connected paramiko ``SSHClient``.

    Closes the client on exit. Host-key checking is disabled because
    lab hosts may have rotating keys (especially after a fresh install).
    """
    log.info("Connecting to host %s as %s ...", host, user)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            allow_agent=False,
            look_for_keys=False,
            timeout=DEFAULT_SSH_TIMEOUT,
            banner_timeout=DEFAULT_SSH_TIMEOUT,
            auth_timeout=DEFAULT_SSH_TIMEOUT,
        )
    except (paramiko.SSHException, OSError) as exc:
        raise HostOpsError(f"SSH connection to {user}@{host} failed: {exc}") from exc

    try:
        yield client
    finally:
        client.close()


def run(
    client: paramiko.SSHClient,
    cmd: str,
    *,
    timeout: Optional[int] = 60,
    stream: bool = True,
) -> CommandResult:
    """Run ``cmd`` on the remote host and capture (and optionally stream) output."""
    log.info("$ %s", cmd)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    stdin.close()

    out_chunks: list = []
    err_chunks: list = []
    if stream:
        # Stream stdout live so long commands (flint burn, wget) show progress.
        channel = stdout.channel
        while True:
            if channel.recv_ready():
                data = channel.recv(4096).decode("utf-8", errors="replace")
                out_chunks.append(data)
                sys.stderr.write(data)
                sys.stderr.flush()
            if channel.recv_stderr_ready():
                data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                err_chunks.append(data)
                sys.stderr.write(data)
                sys.stderr.flush()
            if channel.exit_status_ready() and not (
                channel.recv_ready() or channel.recv_stderr_ready()
            ):
                break
        exit_code = channel.recv_exit_status()
    else:
        out_chunks.append(stdout.read().decode("utf-8", errors="replace"))
        err_chunks.append(stderr.read().decode("utf-8", errors="replace"))
        exit_code = stdout.channel.recv_exit_status()

    return CommandResult(
        cmd=cmd,
        exit_code=exit_code,
        stdout="".join(out_chunks),
        stderr="".join(err_chunks),
    )


def burn_dpu_personality(
    host: str,
    user: str,
    password: str,
    *,
    fw_bin_url: str,
    workdir: str = DEFAULT_WORKDIR,
    mst_device: str = DEFAULT_MST_DEVICE,
) -> None:
    """Run the host-side DPU-personality firmware burn.

    Steps (mirrors the manual workflow):

        cd ~/jenny           # mkdir -p first
        wget -nc <fw_bin_url>
        flint -d <mst>  -i <bin> b

    On success this function returns; the caller is expected to prompt
    the user for the subsequent power cycle (which is required for the
    new firmware to take effect).
    """
    bin_name = os.path.basename(fw_bin_url.split("?")[0])
    if not bin_name:
        raise HostOpsError(f"Cannot infer bin filename from URL: {fw_bin_url}")

    quoted_workdir = shlex.quote(workdir)
    quoted_url = shlex.quote(fw_bin_url)
    quoted_bin = shlex.quote(bin_name)
    quoted_mst = shlex.quote(mst_device)
    remote_bin_path = f"{workdir.rstrip('/')}/{bin_name}"
    quoted_remote_bin = shlex.quote(remote_bin_path)

    with ssh_session(host, user, password) as client:
        log.info("=== Step 1/3: ensure %s exists ===", workdir)
        run(client, f"mkdir -p {quoted_workdir}", timeout=30).check()

        log.info("=== Step 2/3: download DPU-personality firmware ===")
        # Skip the download entirely if the .bin is already staged in workdir.
        existing = run(
            client,
            f"test -f {quoted_remote_bin}",
            timeout=30,
            stream=False,
        )
        if existing.exit_code == 0:
            log.info(
                "%s already present in %s; skipping wget.",
                bin_name,
                workdir,
            )
        else:
            wget_cmd = (
                f"cd {quoted_workdir} && "
                f"wget --no-clobber --progress=dot:giga {quoted_url}"
            )
            result = run(client, wget_cmd, timeout=60 * 30)
            # `wget -nc` exits 1 when the file already exists; treat that as success.
            if result.exit_code != 0 and "already there" not in result.stderr.lower():
                raise HostOpsError(
                    f"wget failed (exit {result.exit_code}):\n{result.stderr}"
                )

        log.info("=== Step 3/3: burn firmware via flint ===")
        flint_cmd = (
            f"cd {quoted_workdir} && "
            f"flint -d {quoted_mst} -i {quoted_bin} b"
        )
        run(client, flint_cmd, timeout=FLINT_BURN_TIMEOUT).check()

    log.info("DPU-personality firmware burn completed on %s.", host)


def confirm_power_cycle(host: str) -> None:
    """Prompt the user to power-cycle the host before continuing.

    A power cycle is required for the freshly burned firmware to take
    effect. We deliberately don't trigger the reboot ourselves so the
    user stays in control of timing (workloads, peers, etc.).
    """
    bar = "=" * 70
    print()
    print(bar, file=sys.stderr)
    print(f"  Host: {host}", file=sys.stderr)
    print("  POWER-CYCLE REQUIRED", file=sys.stderr)
    print("  The new DPU-personality firmware will not take effect until", file=sys.stderr)
    print("  the host is power-cycled. Please power-cycle now (out-of-band", file=sys.stderr)
    print("  or via your usual method).", file=sys.stderr)
    print(bar, file=sys.stderr)
    sys.stderr.flush()

    while True:
        try:
            answer = input(
                "Type 'done' once the host has been power-cycled and is back up, "
                "or 'abort' to stop: "
            ).strip().lower()
        except EOFError:
            raise HostOpsError("No tty available to confirm power cycle.")
        if answer in ("done", "d", "y", "yes"):
            log.info("Power-cycle confirmed; continuing with BMC steps.")
            return
        if answer in ("abort", "a", "n", "no"):
            raise HostOpsError("Aborted by user before BMC password change.")
        print("Please type 'done' or 'abort'.", file=sys.stderr)
