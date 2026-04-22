#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
#
# [project]
# name = "gt-paxlab"
# ///
"""
gt-paxlab.py — Automate connecting to the Georgia Tech Linux server
and launching Jupyter Lab via an SSH tunnel.

Steps performed automatically:
  1. Open a background SSH master connection (authenticates once — you may be
     prompted for your password here and only here).
  2. Run `update-paxlab.py` on the remote host to install or upgrade
     PassengerSim if a newer version is available in /opt/pax-installers.
  3. Check whether a Jupyter Lab server is already running on the remote host
     by calling `jupyter lab list`.  If one is found, its token and port are
     reused and no new server is started.
  4. If no server is running, start one by calling `~/server-paxlab` via nohup
     so that it keeps running even if the SSH connection later drops.  The
     server's output is redirected to ~/.paxlab-jupyter.log on the server, and
     the script tails that file until the token appears.
  5. Open an SSH port-forward tunnel (server port → localhost:LOCAL_PORT).
  6. Open the browser at http://localhost:LOCAL_PORT/lab?token=<TOKEN>.

Usage:
  python gt-paxlab.py [GA_TECH_ID]

If GA_TECH_ID is not given on the command line the script falls back to
the GA_TECH_ID environment variable, then to a cached value from a previous
run, and finally prompts the user.

The server hostname and GT ID are cached between runs in a platform-specific
config file so that you are not prompted on every invocation:
  macOS  : ~/Library/Application Support/paxlab/config.json
  Linux  : $XDG_CONFIG_HOME/paxlab/config.json  (~/.config/paxlab/config.json)
  Windows: %APPDATA%\\paxlab\\config.json

The server hostname can also be overridden at any time via the
PAXLAB_SERVER_HOST environment variable.

Password handling
-----------------
SSH connection multiplexing (ControlMaster / ControlPath) is used so that
all SSH connections in this script share a single authenticated session.
You are prompted for your password at most once (when the master connection
is established in step 1).  All later connections reuse the open socket and
never ask again.  The socket lives in a per-run temporary directory that is
deleted when the script exits, so nothing persists between runs and your
existing ~/.ssh/authorized_keys is never modified.

Jupyter server lifetime
-----------------------
The remote Jupyter server is started with nohup and runs in the background,
so it keeps running after this script exits (or if the SSH connection drops).
On the next run the script will detect the still-running server and connect
to it directly without starting a new one.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import tempfile
import shutil
import webbrowser
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

LOCAL_PORT = 8999  # local port forwarded to the remote Jupyter port

# Path on the remote server where server startup output is logged.
# Using a fixed, predictable name lets subsequent runs tail the same log.
REMOTE_LOG = "~/.paxlab-jupyter.log"

# Token regex: matches the full Jupyter URL printed in `jupyter lab list`
# output and in the server startup log, capturing (port, token).
TOKEN_RE = re.compile(r"http://localhost:([0-9]+)/.*\?token=([a-zA-Z0-9]{48})")

# How long to wait (seconds) for the Jupyter token to appear in the log.
TOKEN_TIMEOUT = 120

# How long to wait (seconds) for the SSH master socket to be created.
MASTER_TIMEOUT = 60

# How long to wait (seconds) after the tunnel is opened before launching
# the browser, giving SSH time to finish negotiating the port forward.
TUNNEL_SETTLE = 2


# --------------------------------------------------------------------------- #
# Persistent config (hostname + GT ID cached between runs)
# --------------------------------------------------------------------------- #


def get_config_path() -> Path:
    """
    Return the platform-appropriate path for the paxlab config file.

    - macOS   : ~/Library/Application Support/paxlab/config.json
    - Windows : %APPDATA%/paxlab/config.json
    - Linux   : $XDG_CONFIG_HOME/paxlab/config.json  (default: ~/.config/…)
    """
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "paxlab"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / "paxlab" if appdata else Path.home() / "AppData" / "Roaming" / "paxlab"
    else:
        # Linux / other POSIX: respect the XDG Base Directory specification.
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg) / "paxlab" if xdg else Path.home() / ".config" / "paxlab"
    return base / "config.json"


def load_config() -> dict:
    """Load the cached config from disk, returning an empty dict on any error."""
    path = get_config_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass  # corrupt file — start fresh
    return {}


def save_config(config: dict) -> None:
    """Persist *config* to disk, creating parent directories as needed."""
    path = get_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except Exception as exc:
        # Non-fatal: warn but don't abort the script.
        print(f"  Warning: could not save config to {path}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def get_gt_id(config: dict) -> str:
    """
    Resolve the Georgia Tech ID, in priority order:
      1. CLI argument (sys.argv[1])
      2. GA_TECH_ID environment variable
      3. Cached value from a previous run (user may accept or override)
      4. Interactive prompt
    """
    if len(sys.argv) > 1:
        return sys.argv[1]
    env_id = os.environ.get("GA_TECH_ID", "").strip()
    if env_id:
        return env_id
    cached = config.get("gt_id", "").strip()
    if cached:
        response = input(f"Georgia Tech ID [{cached}]: ").strip()
        return response if response else cached
    return input("Enter your Georgia Tech ID: ").strip()


def get_server_host(config: dict) -> str:
    """
    Resolve the remote server hostname, in priority order:
      1. PAXLAB_SERVER_HOST environment variable
      2. Cached value from a previous run (user may accept or override)
      3. Interactive prompt
    """
    env_host = os.environ.get("PAXLAB_SERVER_HOST", "").strip()
    if env_host:
        return env_host
    cached = config.get("server_host", "").strip()
    if cached:
        response = input(f"Server hostname [{cached}]: ").strip()
        return response if response else cached
    return input("Enter the server hostname: ").strip()


def follower_opts(control_path: str) -> list[str]:
    """Return SSH options that reuse an existing ControlMaster socket."""
    return ["-o", "ControlMaster=no", "-o", f"ControlPath={control_path}"]


def run_remote(args_ssh: list[str], cmd: str, *, timeout: int = 30) -> str:
    """
    Run *cmd* on the remote host via the existing master connection and return
    combined stdout+stderr as a string.  Raises RuntimeError on non-zero exit.
    """
    result = subprocess.run(
        [*args_ssh, cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return result.stdout.decode(errors="replace")


def parse_token(text: str) -> tuple[str | None, str | None]:
    """
    Search *text* for a Jupyter Lab URL and return (token, port) or
    (None, None) if none is found.
    """
    match = TOKEN_RE.search(text)
    if match:
        return match.group(2), match.group(1)  # token, port
    return None, None


# --------------------------------------------------------------------------- #
# Main connection steps
# --------------------------------------------------------------------------- #


def establish_master(control_path: str, user_host: str) -> subprocess.Popen:
    """
    Start a background SSH master connection (-M -N) and wait until the
    ControlPath socket is created, indicating successful authentication.

    The returned process must be kept alive for as long as follower connections
    are needed; terminate it when finished.
    """
    master_proc = subprocess.Popen(
        # -M  : become the ControlMaster (create the shared socket)
        # -N  : do not execute a remote command; just keep the connection open
        # stdin is inherited so a password prompt (if any) is shown to the user
        ["ssh", "-M", "-N", "-o", f"ControlPath={control_path}", user_host],
    )

    print("      Waiting for SSH master connection …", end="\r\n")
    deadline = time.monotonic() + MASTER_TIMEOUT
    while not os.path.exists(control_path):
        if master_proc.poll() is not None:
            raise RuntimeError(
                "SSH master connection exited before the socket was created.\n"
                "Check your GT ID, VPN connection, and credentials."
            )
        if time.monotonic() > deadline:
            master_proc.terminate()
            raise RuntimeError("Timed out waiting for the SSH master connection.")
        time.sleep(0.25)

    print("      Master connection established.", end="\r\n")
    return master_proc


def check_existing_server(ssh_follower: list[str]) -> tuple[str | None, str | None]:
    """
    Ask the remote host for a list of running Jupyter Lab servers and return
    (token, port) of the first one found, or (None, None) if none is running.
    """
    print("  Running `jupyter lab list` on the server …", end="\r\n")
    try:
        # Use a login shell (-l) so that ~/.bash_profile / ~/.profile is sourced
        # and uv (typically in ~/.local/bin) is on PATH, even in a non-interactive
        # SSH session.
        output = run_remote(ssh_follower, "bash -lc 'uv run -p .paxlab jupyter lab list'", timeout=30)
    except Exception as exc:
        print(f"  Warning: `jupyter lab list` failed ({exc}); assuming no server.", end="\r\n")
        return None, None

    for line in output.splitlines():
        print(f"  [list] {line}", end="\r\n")

    return parse_token(output)


def start_server_and_get_token(ssh_follower: list[str]) -> tuple[str | None, str | None]:
    """
    Start ~/server-paxlab on the remote host via nohup so that it survives
    SSH disconnection, then poll `jupyter lab list` until the server appears.

    Returns (token, port) or (None, None) on failure.
    """
    # Launch the server in the background.  nohup prevents the process from
    # receiving SIGHUP when the SSH session ends, keeping it alive after exit.
    start_cmd = f"nohup ~/server-paxlab >> {REMOTE_LOG} 2>&1 &"
    print("  Starting ~/server-paxlab …", end="\r\n")
    try:
        run_remote(ssh_follower, start_cmd, timeout=30)
    except Exception as exc:
        print(f"  Error starting server: {exc}", file=sys.stderr, end="\r\n")
        return None, None

    # Poll `jupyter lab list` until the server registers itself, or we time out.
    print(f"  Waiting for Jupyter server to start (up to {TOKEN_TIMEOUT}s) …", end="\r\n")
    deadline = time.monotonic() + TOKEN_TIMEOUT
    poll_interval = 5  # seconds between retries
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        try:
            output = run_remote(
                ssh_follower,
                "bash -lc 'uv run -p .paxlab jupyter lab list'",
                timeout=30,
            )
        except Exception:
            continue  # transient error; try again
        print(f"  [list] {output.strip()}", end="\r\n")
        token, port = parse_token(output)
        if token:
            return token, port

    print("  Timed out waiting for Jupyter server.", file=sys.stderr, end="\r\n")
    return None, None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> None:
    # Load cached settings (GT ID and server hostname) from the previous run.
    config = load_config()

    gt_id = get_gt_id(config)
    if not gt_id:
        print("Error: Georgia Tech ID is required.", file=sys.stderr, end="\r\n")
        sys.exit(1)

    server_host = get_server_host(config)
    if not server_host:
        print("Error: server hostname is required.", file=sys.stderr, end="\r\n")
        sys.exit(1)

    # Persist both values so the user is not prompted on the next run.
    config["gt_id"] = gt_id
    config["server_host"] = server_host
    save_config(config)

    user_host = f"{gt_id}@{server_host}"

    # Per-run temporary directory for the SSH multiplexing socket.
    # mkdtemp creates it with mode 0700 (owner-only), keeping the socket private.
    # The directory is removed in the outer finally block no matter how we exit.
    socket_dir = tempfile.mkdtemp(prefix="ssh-paxlab-")
    control_path = os.path.join(socket_dir, "ctrl")

    # SSH args prepended to every follower invocation.
    ssh_follower = ["ssh", *follower_opts(control_path), user_host]

    master_proc = None
    tunnel_proc = None

    try:
        # ------------------------------------------------------------------ #
        # Step 1 — Authenticate once via the master connection
        # ------------------------------------------------------------------ #
        print(f"\n[1] Connecting to {user_host} …")
        print("    (You may be prompted for your password once.)\n")
        master_proc = establish_master(control_path, user_host)

        # ------------------------------------------------------------------ #
        # Step 2 — Update PassengerSim on the remote host (if a newer
        #           installer is available in /opt/pax-installers).
        # ------------------------------------------------------------------ #
        print("\n[2] Checking for PassengerSim updates …", end="\r\n")
        try:
            # Run in a login shell so that ~/.bash_profile / ~/.profile is
            # sourced and uv (typically in ~/.local/bin) is on PATH.
            update_output = run_remote(
                ssh_follower,
                "bash -lc 'uv run python /opt/pax-installers/update-paxlab.py'",
                timeout=300,  # installer download can take a while
            )
            for line in update_output.splitlines():
                print(f"  [update] {line}", end="\r\n")
        except Exception as exc:
            print(f"  Warning: update check failed ({exc}); continuing anyway.", end="\r\n")

        # ------------------------------------------------------------------ #
        # Step 3 — Check for an already-running Jupyter server
        # ------------------------------------------------------------------ #
        print("\n[3] Checking for an existing Jupyter Lab server …", end="\r\n")
        token, remote_port = check_existing_server(ssh_follower)

        if token:
            print(f"\n  ✓ Found existing server on port {remote_port}.", end="\r\n")
            print("    Will reuse it — no new server will be started.", end="\r\n")
        else:
            # ---------------------------------------------------------------- #
            # Step 2b — No server found: start one that survives disconnection
            # ---------------------------------------------------------------- #
            print("\n  No running server found.", end="\r\n")
            print("  Starting a new one (it will keep running after this script exits) …\n", end="\r\n")
            token, remote_port = start_server_and_get_token(ssh_follower)

        if not token:
            print("\nFailed to obtain a Jupyter token.  See output above.", file=sys.stderr, end="\r\n")
            sys.exit(1)

        print(f"\n  Token : {token}", end="\r\n")
        print(f"  Port  : {remote_port}", end="\r\n")

        # ------------------------------------------------------------------ #
        # Step 4 — Port-forward tunnel (localhost:LOCAL_PORT → server:remote_port)
        # ------------------------------------------------------------------ #
        print(f"\n[4] Opening tunnel  localhost:{LOCAL_PORT} → {server_host}:{remote_port} …", end="\r\n")
        print("    (Reusing the authenticated session — no password needed.)", end="\r\n")

        tunnel_proc = subprocess.Popen(
            [
                "ssh",
                "-N",  # no remote command; just forward the port
                "-L",
                f"{LOCAL_PORT}:localhost:{remote_port}",
                *follower_opts(control_path),
                user_host,
            ]
        )

        time.sleep(TUNNEL_SETTLE)

        if tunnel_proc.poll() is not None:
            print("SSH tunnel exited unexpectedly.  Check your VPN / credentials.", file=sys.stderr, end="\r\n")
            sys.exit(1)

        # ------------------------------------------------------------------ #
        # Step 5 — Open the browser
        # ------------------------------------------------------------------ #
        url = f"http://localhost:{LOCAL_PORT}/lab?token={token}"
        print(f"\n[5] Opening browser at:\n    {url}\n", end="\r\n")
        webbrowser.open(url)

        # ------------------------------------------------------------------ #
        # Keep the tunnel alive until the user presses Ctrl-C
        # ------------------------------------------------------------------ #
        print(
            "\033[1;31m Press Ctrl-C to close the SSH tunnel (the Jupyter server will keep running). \033[0m\n",
            end="\r\n",
        )

        try:
            while True:
                if master_proc.poll() is not None:
                    print("\nSSH master connection has closed.", end="\r\n")
                    break
                if tunnel_proc.poll() is not None:
                    print("\nSSH tunnel has closed.", end="\r\n")
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down tunnel …", end="\r\n")

    except RuntimeError as exc:
        print(f"\nError: {exc}", file=sys.stderr, end="\r\n")
        sys.exit(1)

    finally:
        # Close the tunnel and master connection, then remove the socket dir.
        for proc in (tunnel_proc, master_proc):
            if proc is None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(socket_dir, ignore_errors=True)

    print("Done.  (Jupyter server is still running on the remote host.)", end="\r\n")


if __name__ == "__main__":
    main()
