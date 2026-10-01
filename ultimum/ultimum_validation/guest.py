"""Guest assertions and continuous, non-reconnecting migration probes."""

import base64
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time

import paramiko

from .cloud import wait_for
from .config import InvalidError


def q(value):
    return shlex.quote(str(value))


class Guest:
    def __init__(self, address, server_id, cfg, ledger):
        self.address, self.server_id, self.cfg, self.ledger = (
            address,
            server_id,
            cfg,
            ledger,
        )
        self.client = None

    def connect(self):
        cfg, ledger, server_id = self.cfg, self.ledger, self.server_id

        class RunHostKey(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                fingerprint = key.get_name() + " " + key.get_base64()
                known = ledger.data["evidence"].get("ssh_host_keys", {})
                if server_id in known and known[server_id] != fingerprint:
                    raise InvalidError(
                        "SSH host key changed for this run's VM"
                    )
                known[server_id] = fingerprint
                ledger.evidence("ssh_host_keys", known)
                client.get_host_keys().add(hostname, key.get_name(), key)

        def attempt():
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(RunHostKey())
            try:
                client.connect(
                    self.address,
                    port=cfg["ssh_port"],
                    username=cfg["ssh_username"],
                    key_filename=cfg["ssh_private_key"],
                    timeout=10,
                    banner_timeout=10,
                    auth_timeout=10,
                    allow_agent=False,
                    look_for_keys=False,
                )
                self.client = client
                return True
            except paramiko.AuthenticationException:
                client.close()
                # sshd can precede cloud-init's authorized_keys installation.
                return False
            except (OSError, EOFError, paramiko.SSHException):
                client.close()
                return False

        wait_for(
            attempt, bool, cfg["ssh_timeout_seconds"], description="guest SSH"
        )
        return self

    def close(self):
        if self.client:
            self.client.close()

    def command(self, command, timeout=120):
        channel = self.client.get_transport().open_session(timeout=10)
        channel.exec_command(command)
        out, err = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            while True:
                while channel.recv_ready():
                    out.extend(channel.recv(65536))
                while channel.recv_stderr_ready():
                    err.extend(channel.recv_stderr(65536))
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Guest command timed out")
                time.sleep(0.03)
            status = channel.recv_exit_status()
            if status:
                detail = bytes(err).decode(errors="replace")[-1000:]
                raise AssertionError(
                    f"Guest command failed with exit {status}: {detail}"
                )
            return bytes(out).decode(errors="replace").strip()
        finally:
            channel.close()

    def tools(self, *packages):
        names = {
            "e2fsprogs": "resize2fs",
            "tpm2-tools": "tpm2_nvdefine",
            "python3": "python3",
            "curl": "curl",
        }
        for package in packages:
            try:
                self.command("command -v " + q(names[package]))
            except AssertionError:
                if not self.cfg["install_missing_packages"]:
                    raise InvalidError(
                        f"Image lacks {package}; preinstall or enable "
                        "guest.install_missing_packages"
                    ) from None
                self.command(
                    (
                        "sudo -n apt-get update && sudo -n env "
                        "DEBIAN_FRONTEND=noninteractive apt-get "
                        "install -y "
                    )
                    + q(package),
                    timeout=600,
                )

    def write(self, path, content):
        payload = base64.b64encode(
            content.encode() if isinstance(content, str) else content
        ).decode()
        self.command(
            "printf %s "
            + q(payload)
            + " | base64 -d | sudo -n tee "
            + q(path)
            + " >/dev/null"
        )

    def device(self, volume_id, unmounted=False, timeout=120):
        """Find a unique volume serial; reject root/mounted disks."""
        normalized = volume_id.replace("-", "").lower()

        def find():
            data = json.loads(
                self.command(
                    "sudo -n lsblk --json -o PATH,SERIAL,TYPE,MOUNTPOINTS"
                )
            )
            matches = []

            def walk(items):
                for item in items:
                    raw_serial = (item.get("serial") or "").strip().lower()
                    serial = raw_serial.replace("-", "")
                    if (
                        item.get("type") == "disk"
                        and len(raw_serial) >= 20
                        and normalized.startswith(serial)
                    ):
                        matches.append(item)
                    walk(item.get("children", []))

            walk(data["blockdevices"])
            if not matches:
                return None
            if len(matches) != 1:
                raise InvalidError(
                    "Volume serial matches multiple guest disks"
                )
            disk = matches[0]

            def mounts(node):
                return [m for m in node.get("mountpoints", []) if m] + [
                    m for c in node.get("children", []) for m in mounts(c)
                ]

            mounted = mounts(disk)
            if "/" in mounted or "/boot" in mounted or (unmounted and mounted):
                raise InvalidError(
                    "Refusing root or mounted disk for volume test"
                )
            return disk["path"]

        return wait_for(
            find, bool, timeout, description="volume disk serial in guest"
        )


class Continuity:
    """One SSH channel and one ping process, maintained across migration."""

    def __init__(self, guest, options, ledger):
        self.guest, self.options, self.ledger = guest, options, ledger
        self.samples, self.ping_lines = [], []
        self.stop_event = threading.Event()
        self.error = None
        self.channel = None
        self.ping = None
        self.threads = []

    def __enter__(self):
        interval = float(self.options["ssh_heartbeat_interval_seconds"])
        self.channel = self.guest.client.get_transport().open_session()
        self.channel.exec_command(
            "while true; do printf 'heartbeat\\n'; sleep "
            + q(interval)
            + "; done"
        )

        def heartbeat():
            pending = b""
            try:
                while not self.stop_event.is_set():
                    if self.channel.recv_ready():
                        chunk = self.channel.recv(4096)
                        if not chunk:
                            raise AssertionError(
                                "Persistent SSH channel closed"
                            )
                        pending += chunk
                        while b"\n" in pending:
                            _, pending = pending.split(b"\n", 1)
                            self.samples.append(time.monotonic())
                    elif (
                        self.channel.exit_status_ready()
                        or not self.guest.client.get_transport().is_active()
                    ):
                        raise AssertionError(
                            "Persistent SSH connection interrupted"
                        )
                    time.sleep(0.01)
            except Exception as exc:
                self.error = str(exc)

        try:
            self.ping = subprocess.Popen(
                [
                    "ping",
                    "-n",
                    "-D",
                    "-i",
                    str(self.options["ping_interval_seconds"]),
                    self.guest.address,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=dict(os.environ, LC_ALL="C"),
            )

            def read_ping():
                self.ping_lines.extend(self.ping.stdout)

            self.threads = [
                threading.Thread(target=heartbeat, daemon=True),
                threading.Thread(target=read_ping, daemon=True),
            ]
            for thread in self.threads:
                thread.start()
            wait_for(
                lambda: len(self.samples),
                lambda n: n >= 3,
                15,
                interval=0.1,
                description="SSH heartbeat baseline",
            )
            time.sleep(1)
            if self.ping.poll() is not None:
                raise InvalidError(
                    "ping could not start (check iputils-ping "
                    "and ICMP permissions)"
                )
            return self
        except BaseException:
            self.finish()
            raise

    def finish(self):
        self.stop_event.set()
        if self.channel:
            self.channel.close()
        if self.ping and self.ping.poll() is None:
            self.ping.send_signal(signal.SIGINT)
            try:
                self.ping.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.ping.kill()
                self.ping.wait()
        for thread in self.threads:
            thread.join(timeout=5)
        if self.ping and self.ping.stdout:
            self.ping.stdout.close()

    def __exit__(self, exc_type, exc, traceback):
        time.sleep(1)
        end = time.monotonic()
        self.finish()
        text = "".join(self.ping_lines)
        match = re.search(r"(\d+) packets transmitted, (\d+) received", text)
        sent, received = map(int, match.groups()) if match else (0, 0)
        gaps = [b - a for a, b in zip(self.samples, self.samples[1:])]
        if self.samples:
            gaps.append(end - self.samples[-1])
        evidence = {
            "sent": sent,
            "received": received,
            "lost": sent - received,
            "max_ssh_gap_seconds": max(gaps, default=end),
            "heartbeat_count": len(self.samples),
            "ssh_error": self.error,
            "ping_output": text,
        }
        self.ledger.evidence("continuity_" + self.guest.server_id, evidence)
        if exc_type is None:
            if self.error or sent == 0 or received == 0:
                raise AssertionError(
                    "Continuity probes failed or produced no usable evidence"
                )
            if evidence["lost"] > self.options["max_lost_packets"]:
                raise AssertionError(
                    "Migration exceeded allowed ping packet loss"
                )
            if (
                evidence["max_ssh_gap_seconds"]
                > self.options["max_ssh_gap_seconds"]
            ):
                raise AssertionError(
                    "Migration exceeded allowed persistent SSH heartbeat gap"
                )
