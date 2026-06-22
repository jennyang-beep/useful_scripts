#!/usr/bin/env python3
"""Update or clone dpdk-tests on a remote server over SSH."""

import argparse
import getpass
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PASSWORD = "3tango"
DEFAULT_GITHUB_USER = "jenny-beep"
DEFAULT_REMOTE_PATH = "/root/jenny/dpdk-tests"
DEFAULT_REPO_URL = "https://github.com/jennyang-beep/dpdk-tests.git"
TOKEN_FILE = Path(__file__).with_name("sync_dpdk_tests.token")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SSH to a server and sync /root/jenny/dpdk-tests from GitHub."
    )
    parser.add_argument(
        "server_dns",
        help="Server DNS name, for example host.example.com.",
    )
    parser.add_argument(
        "--github-user",
        default=DEFAULT_GITHUB_USER,
        help=f"GitHub username for HTTPS auth. Default: {DEFAULT_GITHUB_USER}.",
    )
    parser.add_argument(
        "--github-token",
        help="GitHub token. Prefer GITHUB_PAT or an interactive prompt instead of this flag.",
    )
    parser.add_argument(
        "--save-github-token",
        action="store_true",
        help=f"Save a prompted GitHub token to {TOKEN_FILE.name} for future runs.",
    )
    parser.add_argument(
        "--remote-path",
        default=DEFAULT_REMOTE_PATH,
        help=f"Remote repo path. Default: {DEFAULT_REMOTE_PATH}.",
    )
    parser.add_argument(
        "--repo-url",
        default=DEFAULT_REPO_URL,
        help=f"Repository URL. Default: {DEFAULT_REPO_URL}.",
    )
    return parser.parse_args()


def read_saved_github_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def save_github_token(token: str) -> None:
    TOKEN_FILE.write_text(f"{token}\n", encoding="utf-8")
    TOKEN_FILE.chmod(0o600)


def get_github_token(args: argparse.Namespace) -> str:
    token = (
        args.github_token
        or os.environ.get("GITHUB_PAT")
        or os.environ.get("GITHUB_TOKEN")
        or read_saved_github_token()
    )
    if token:
        return token

    token = getpass.getpass(
        "GitHub token for cloning/pulling dpdk-tests (input hidden): "
    ).strip()
    if token and args.save_github_token:
        save_github_token(token)
        print(f"Saved GitHub token to {TOKEN_FILE}")
    return token


def build_ssh_command(args: argparse.Namespace) -> list[str]:
    server_dns = args.server_dns.rsplit("@", 1)[-1]
    ssh_target = f"{DEFAULT_SSH_USER}@{server_dns}"
    return [
        "ssh",
        "-o",
        "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=1",
        ssh_target,
        "bash",
        "-s",
    ]


def run_ssh(command: list[str], remote_script: str) -> subprocess.CompletedProcess:
    askpass_path = ""
    try:
        fd, askpass_path = tempfile.mkstemp(prefix="sync-dpdk-ssh-askpass-")
        with os.fdopen(fd, "w") as askpass:
            askpass.write(
                "#!/bin/sh\n"
                'printf "%s\\n" "$SYNC_DPDK_SSH_PASSWORD"\n'
            )
        os.chmod(askpass_path, 0o700)

        env = os.environ.copy()
        env["SYNC_DPDK_SSH_PASSWORD"] = os.environ.get(
            "SYNC_DPDK_SSH_PASSWORD", DEFAULT_SSH_PASSWORD
        )
        env["SSH_ASKPASS"] = askpass_path
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["DISPLAY"] = env.get("DISPLAY") or "sync-dpdk"

        return subprocess.run(
            command,
            input=remote_script,
            text=True,
            env=env,
            start_new_session=True,
        )
    finally:
        if askpass_path:
            try:
                os.unlink(askpass_path)
            except FileNotFoundError:
                pass


def build_remote_script(args: argparse.Namespace, github_token: str) -> str:
    return f"""\
set -euo pipefail

REPO_URL={shlex.quote(args.repo_url)}
REMOTE_PATH={shlex.quote(args.remote_path)}
GITHUB_USERNAME={shlex.quote(args.github_user)}
GITHUB_TOKEN={shlex.quote(github_token)}

if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git is not installed on the remote server." >&2
    exit 2
fi

parent_dir="$(dirname "$REMOTE_PATH")"
askpass="$(mktemp)"

cleanup() {{
    rm -f "$askpass"
}}
trap cleanup EXIT

cat > "$askpass" <<'ASKPASS'
#!/bin/sh
case "$1" in
    *Username*) printf '%s\\n' "$GITHUB_USERNAME" ;;
    *Password*) printf '%s\\n' "$GITHUB_TOKEN" ;;
    *) printf '\\n' ;;
esac
ASKPASS
chmod 700 "$askpass"

export GITHUB_USERNAME
export GITHUB_TOKEN
export GIT_ASKPASS="$askpass"
export GIT_TERMINAL_PROMPT=0

if [ -d "$REMOTE_PATH/.git" ]; then
    echo "Found existing repo: $REMOTE_PATH"
    git -C "$REMOTE_PATH" pull
    echo "SUCCESS: updated $REMOTE_PATH"
elif [ -e "$REMOTE_PATH" ]; then
    echo "ERROR: $REMOTE_PATH exists but is not a git repository." >&2
    exit 3
else
    echo "Repo not found. Cloning into: $REMOTE_PATH"
    mkdir -p "$parent_dir"
    git clone "$REPO_URL" "$REMOTE_PATH"
    echo "SUCCESS: cloned $REPO_URL into $REMOTE_PATH"
fi
"""


def main() -> int:
    args = parse_args()
    github_token = get_github_token(args)
    if not github_token:
        print("ERROR: a GitHub token is required.", file=sys.stderr)
        return 1

    command = build_ssh_command(args)
    remote_script = build_remote_script(args, github_token)

    result = run_ssh(command, remote_script)
    if result.returncode == 0:
        print("dpdk-tests sync completed successfully.")
    else:
        print(f"dpdk-tests sync failed with exit code {result.returncode}.", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
