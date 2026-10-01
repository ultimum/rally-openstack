"""Twelve independent acceptance workflows, invoked only by Rally plugins."""

import base64
import collections
import concurrent.futures
import json
import os
import pathlib
import time

import yaml

from .cloud import wait_for
from .config import InvalidError
from .guest import Continuity
from .guest import q
from .prepare import exclusive_host
from .prepare import host_service
from .resources import Resources


class Scenarios(Resources):
    def run(self, name):
        self.options = self.cfg["scenarios"][name]
        with self.step("ultimum." + name):
            getattr(self, name.replace("-", "_"))()

    def placement(self):
        s = self.options
        self.access()
        distributions = []

        def batch(count, group=None):
            servers = self.server(
                count=count,
                group=group,
                az=s["availability_zone"],
                label="placement",
            )
            return servers if isinstance(servers, list) else [servers]

        def inspect(servers, mode, policy):
            hosts = [
                self.describe(vm)["OS-EXT-SRV-ATTR:host"] for vm in servers
            ]
            if s["allowed_hosts"] and not set(hosts) <= set(
                s["allowed_hosts"]
            ):
                raise AssertionError(
                    "Placement used a host outside allowed_hosts"
                )
            if policy == "affinity" and len(set(hosts)) != 1:
                raise AssertionError(
                    "Affinity group was distributed across hosts"
                )
            if policy == "anti-affinity" and len(set(hosts)) != len(hosts):
                raise AssertionError("Anti-affinity instances share a host")
            distributions.append(
                {
                    "mode": mode,
                    "policy": policy,
                    "hosts": dict(collections.Counter(hosts)),
                    "servers": [vm["id"] for vm in servers],
                }
            )
            self.ledger.evidence("placement", distributions)

        for mode in s["submission_modes"]:
            with self.step("Placement Count batches: " + mode):
                group = (
                    self.group(s["group_policy"])
                    if s["group_policy"]
                    else None
                )
                if mode == "parallel":
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=len(s["batch_sizes"])
                    ) as executor:
                        futures = [
                            executor.submit(batch, count, group)
                            for count in s["batch_sizes"]
                        ]
                        servers = [
                            vm for future in futures for vm in future.result()
                        ]
                    inspect(servers, mode, s["group_policy"])
                    for vm in servers:
                        self.delete_server(vm)
                else:
                    # Each batch remains together; resources are released
                    # before next batch.
                    for count in s["batch_sizes"]:
                        servers = batch(count, group)
                        inspect(servers, mode, s["group_policy"])
                        for vm in servers:
                            self.delete_server(vm)
        if s["anti_affinity_count"]:
            with self.step("Placement: explicit anti-affinity group"):
                inspect(
                    batch(
                        s["anti_affinity_count"], self.group("anti-affinity")
                    ),
                    "anti-affinity",
                    "anti-affinity",
                )

    def delete_server(self, server):
        self.cloud.delete("compute", "/servers/" + server["id"])
        wait_for(
            lambda: self.cloud.absent("compute", "/servers/" + server["id"]),
            bool,
            self.timeout,
            description="server deletion",
        )
        for item in self.ledger.data["resources"]:
            if item["kind"] == "server" and item["id"] == server["id"]:
                item["deleted"] = True
        self.ledger.save()

    def nova_live_migration(self):
        with self.step("Create VM, floating IP and establish SSH"):
            server, _, _, guest = self.vm()
        with self.step("Migrate while measuring ping and persistent SSH"):
            with Continuity(guest, self.options, self.ledger):
                self.migrate(server, self.options["target_host"])
        guest.command("true")

    def nova_live_migration_tpm(self):
        s = self.options
        server, _, _, guest = self.vm(flavor=self.runtime["tpm_flavor_id"])
        guest.tools("tpm2-tools")
        payload = os.urandom(s["payload_bytes"])
        index = str(int(s["nv_index"], 0))
        with self.step("Define TPM NV index and write exact binary payload"):
            guest.write("/tmp/ultimum-tpm-value", payload)
            guest.command(
                f"sudo -n tpm2_nvdefine {q(index)} "
                f"-C o -s {int(s['payload_bytes'])}"
            )
            guest.command(
                f"sudo -n tpm2_nvwrite {q(index)} "
                "-C o -i /tmp/ultimum-tpm-value"
            )
        self.migrate(server, s["target_host"])
        with self.step(
            "Compare TPM bytes after migration and create a new TPM key"
        ):
            encoded = guest.command(
                f"sudo -n tpm2_nvread {q(index)} "
                f"-C o -s {int(s['payload_bytes'])} "
                "-o /tmp/ultimum-tpm-read && "
                "sudo -n base64 -w0 /tmp/ultimum-tpm-read"
            )
            if base64.b64decode(encoded, validate=True) != payload:
                raise AssertionError(
                    "TPM NV payload changed after live migration"
                )
            if s["verify_key_creation"]:
                guest.command(
                    "sudo -n tpm2_createprimary -C o -G rsa -c "
                    "/tmp/ultimum-primary.ctx && sudo -n "
                    "tpm2_create -C /tmp/ultimum-primary.ctx "
                    "-G rsa -u /tmp/ultimum-key.pub -r "
                    "/tmp/ultimum-key.priv"
                )
            self.ledger.evidence(
                "tpm",
                {
                    "bytes_compared": len(payload),
                    "identical": True,
                    "new_key_created": s["verify_key_creation"],
                },
            )

    def nova_drain(self):
        s = self.options
        exclusive_host(self.admin, s["host"])
        active_vms = [
            self.vm(host=s["host"], label="drain-active")
            for _ in range(s["active_instances"])
        ]
        stopped_vms = [
            self.vm(host=s["host"], label="drain-stopped")
            for _ in range(s["stopped_instances"])
        ]
        active = [vm[0] for vm in active_vms]
        stopped = [vm[0] for vm in stopped_vms]
        for vm, _, _, guest in stopped_vms:
            guest.write("/var/tmp/ultimum-drain", self.run_id)
            guest.command("sync")
            guest.close()
            self.action(vm, {"os-stop": None})
            self.cloud.wait_status(
                "compute",
                "/servers/" + vm["id"],
                "server",
                {"SHUTOFF"},
                self.timeout,
            )
        for vm in active + stopped:
            if self.describe(vm)["OS-EXT-SRV-ATTR:host"] != s["host"]:
                raise AssertionError(
                    "Requested placement did not land on dedicated host"
                )
        exclusive_host(
            self.admin, s["host"], [vm["id"] for vm in active + stopped]
        )
        self.save_service(s["host"])
        try:
            with self.step(
                "Disable dedicated compute, then drain only this run's VMs"
            ):
                service = host_service(self.admin, s["host"])
                self.admin.request(
                    "compute",
                    "PUT",
                    "/os-services/" + str(service["id"]),
                    {
                        "status": "disabled",
                        "disabled_reason": "Ultimum " + self.run_id,
                    },
                )
                for vm, _, _, guest in active_vms:
                    with Continuity(
                        guest,
                        self.cfg["scenarios"]["nova-live-migration"],
                        self.ledger,
                    ):
                        self.migrate(vm)
                for vm, _, floating, _ in stopped_vms:
                    self.migrate(vm, cold=True)
                    state = self.describe(vm)
                    if state["status"] != "SHUTOFF":
                        raise AssertionError(
                            "Cold migration changed stopped VM power state"
                        )
                    self.action(vm, {"os-start": None})
                    self.cloud.wait_status(
                        "compute",
                        "/servers/" + vm["id"],
                        "server",
                        {"ACTIVE"},
                        self.timeout,
                    )
                    guest = self.connect(vm, floating)
                    if (
                        guest.command("cat /var/tmp/ultimum-drain")
                        != self.run_id
                    ):
                        raise AssertionError("Cold migration lost VM data")
                remaining = exclusive_host(
                    self.admin,
                    s["host"],
                    [vm["id"] for vm in active + stopped],
                )
                if remaining:
                    raise AssertionError(
                        "Dedicated host still has instances after drain"
                    )
                self.ledger.evidence(
                    "drain",
                    {
                        "active_migrated": len(active),
                        "stopped_migrated": len(stopped),
                        "remaining": 0,
                    },
                )
        finally:
            self.restore_services()

    def nova_cloud_init(self):
        s = self.options
        userdata = "#cloud-config\n" + yaml.safe_dump(
            {
                "users": [
                    "default",
                    {
                        "name": s["username"],
                        "groups": "sudo",
                        "shell": "/bin/bash",
                        "sudo": ["ALL=(ALL) NOPASSWD:ALL"],
                    },
                ],
                "write_files": [
                    {"path": s["file_path"], "content": s["file_content"]}
                ],
            }
        )
        server, _, _, guest = self.vm(userdata=userdata)
        with self.step(
            "Verify cloud-init user, file and network metadata endpoint"
        ):
            guest.command("id " + q(s["username"]))
            value = guest.command("sudo -n cat " + q(s["file_path"]))
            if value != s["file_content"].strip():
                raise AssertionError("cloud-init file content differs")
            if s["verify_metadata_endpoint"]:
                guest.tools("curl")
                meta = json.loads(
                    guest.command(
                        "curl --noproxy '*' --fail --max-time 15 "
                        "http://169.254.169.254/openstack/latest/me"
                        "ta_data.json"
                    )
                )
                if meta["uuid"] != server["id"]:
                    raise AssertionError(
                        "Metadata endpoint returned a different instance UUID"
                    )
            datasource = guest.command(
                "sudo -n cat /run/cloud-init/ds-identify.log"
            )
            self.ledger.evidence(
                "cloud_init",
                {
                    "user": s["username"],
                    "file_content": value,
                    "metadata_verified": s["verify_metadata_endpoint"],
                    "datasource_log": datasource,
                },
            )

    def neutron_dhcp(self):
        s = self.options
        subnet = self.cloud.get(
            "network", "/subnets/" + self.runtime["subnet_id"]
        )["subnet"]
        if not subnet["enable_dhcp"]:
            raise InvalidError("DHCP is disabled on the configured subnet")
        port = self.port(fixed_ip=s["fixed_ip"])
        _, _, _, guest = self.vm(port=port, label="net-test1")
        with self.step("Verify fixed IP, MAC and DHCP lease inside guest"):
            addresses = json.loads(guest.command("ip -j address show"))
            matches = [
                i
                for i in addresses
                if i.get("address", "").lower() == port["mac_address"].lower()
            ]
            if len(matches) != 1 or not any(
                a["local"] == s["fixed_ip"] for a in matches[0]["addr_info"]
            ):
                raise AssertionError(
                    "Guest interface does not have the port's "
                    "configured fixed IP"
                )
            leases = guest.command(
                "sudo -n sh -c 'cat "
                "/run/systemd/netif/leases/* "
                "/var/lib/dhcp/*.leases "
                "/var/lib/NetworkManager/*.lease "
                "2>/dev/null || true'"
            )
            if s["require_dhcp_lease"]:
                import re

                patterns = [
                    r"(?m)^ADDRESS=" + re.escape(s["fixed_ip"]) + r"$",
                    r"fixed-address\s+" + re.escape(s["fixed_ip"]) + r";",
                    r"(?m)^ADDRESS='?" + re.escape(s["fixed_ip"]) + r"'?$",
                ]
                if not any(re.search(pattern, leases) for pattern in patterns):
                    raise AssertionError(
                        "No DHCP lease for the fixed IP found in guest"
                    )
            self.ledger.evidence(
                "dhcp",
                {
                    "port_id": port["id"],
                    "interface": matches[0],
                    "lease": leases,
                },
            )

    def neutron_security_group(self):
        s = self.options
        group = self.security_group("http")
        port = self.port(groups=[group])
        _, _, floating, guest = self.vm(port=port)
        marker = "ultimum-http-" + self.run_id
        self.webserver(guest, marker, s["http_port"])
        rule = self.rule(
            group,
            "tcp",
            s["http_port"],
            self.cfg["network"]["probe_source_cidr"],
        )
        ip = floating["floating_ip_address"]
        with self.step("Allow HTTP: verify new TCP connections"):
            self.wait_http(
                ip, s["http_port"], marker, s["propagation_timeout_seconds"]
            )
        with self.step(
            "Delete HTTP ingress rule: verify new connections are blocked"
        ):
            self.cloud.delete("network", "/security-group-rules/" + rule)
            self.wait_http(
                ip,
                s["http_port"],
                marker,
                s["propagation_timeout_seconds"],
                reachable=False,
            )
            # Verify the guest/application is still healthy via the separate
            # SSH SG.
            guest.tools("curl")
            if (
                guest.command(
                    "curl --noproxy '*' --fail --max-time 10 http://127.0.0.1:"
                    + str(s["http_port"])
                )
                != marker
            ):
                raise AssertionError(
                    "HTTP service stopped while testing deny rule"
                )
        if s["verify_allow_after_deny"]:
            with self.step("Re-add HTTP ingress rule and verify recovery"):
                self.rule(
                    group,
                    "tcp",
                    s["http_port"],
                    self.cfg["network"]["probe_source_cidr"],
                )
                self.wait_http(
                    ip,
                    s["http_port"],
                    marker,
                    s["propagation_timeout_seconds"],
                )
        self.ledger.evidence(
            "security_group",
            {
                "allow": True,
                "deny": True,
                "allow_again": s["verify_allow_after_deny"],
            },
        )

    def octavia_vip(self):
        s = self.options
        group = self.security_group("backend")
        for cidr in (
            self.cfg["network"]["subnet"]["cidr"],
            self.cfg["network"]["probe_source_cidr"],
        ):
            self.rule(group, "tcp", s["backend_port"], cidr)
        backends = []
        with self.step(
            "Create independent HTTP backends, "
            "configure over temporary SSH floating IPs"
        ):
            for index in range(s["backend_count"]):
                port = self.port(groups=[group])
                _, _, floating, guest = self.vm(port=port, label="backend")
                marker = "VM" + str(index + 1)
                self.webserver(guest, marker, s["backend_port"])
                self.wait_http(
                    floating["floating_ip_address"], s["backend_port"], marker
                )
                guest.close()
                self.cloud.delete("network", "/floatingips/" + floating["id"])
                for item in self.ledger.data["resources"]:
                    if (
                        item["kind"] == "floatingip"
                        and item["id"] == floating["id"]
                    ):
                        item["deleted"] = True
                self.ledger.save()
                backends.append((port["fixed_ips"][0]["ip_address"], marker))
        lb = self.create(
            "lb",
            "/lbaas/loadbalancers",
            "loadbalancer",
            {
                "name": self.name("lb"),
                "vip_subnet_id": self.runtime["subnet_id"],
                "provider": s["provider"],
            },
        )

        def ready():
            return self.cloud.wait_status(
                "lb",
                "/lbaas/loadbalancers/" + lb["id"],
                "loadbalancer",
                {"ACTIVE"},
                s["provisioning_timeout_seconds"],
            )

        lb = ready()
        listener = self.cloud.post(
            "lb",
            "/lbaas/listeners",
            {
                "listener": {
                    "name": self.name("listener"),
                    "loadbalancer_id": lb["id"],
                    "protocol": s["protocol"],
                    "protocol_port": s["vip_port"],
                }
            },
        )["listener"]
        ready()
        pool = self.cloud.post(
            "lb",
            "/lbaas/pools",
            {
                "pool": {
                    "name": self.name("pool"),
                    "listener_id": listener["id"],
                    "protocol": s["protocol"],
                    "lb_algorithm": s["algorithm"],
                }
            },
        )["pool"]
        ready()
        for address, _ in backends:
            self.cloud.post(
                "lb",
                "/lbaas/pools/" + pool["id"] + "/members",
                {
                    "member": {
                        "address": address,
                        "protocol_port": s["backend_port"],
                        "subnet_id": self.runtime["subnet_id"],
                    }
                },
            )
            ready()
        vip_group = self.security_group("vip")
        self.rule(
            vip_group,
            "tcp",
            s["vip_port"],
            self.cfg["network"]["probe_source_cidr"],
        )
        vip_port = self.cloud.get("network", "/ports/" + lb["vip_port_id"])[
            "port"
        ]
        self.cloud.request(
            "network",
            "PUT",
            "/ports/" + vip_port["id"],
            {
                "port": {
                    "security_groups": vip_port["security_groups"]
                    + [vip_group]
                }
            },
        )
        floating = self.floating(vip_port["id"])
        markers = {marker for _, marker in backends}
        with self.step(
            "Probe VIP with fresh TCP connections and "
            "verify backend identities"
        ):
            wait_for(
                lambda: self._vip_available(floating, s["vip_port"], markers),
                bool,
                s["provisioning_timeout_seconds"],
                description="VIP data plane",
            )
            observed = collections.Counter()
            for _ in range(s["probe_connections"]):
                value = self.http(
                    floating["floating_ip_address"], s["vip_port"]
                )
                if value not in markers:
                    raise AssertionError(
                        "VIP returned an unknown backend response"
                    )
                observed[value] += 1
            self.ledger.evidence(
                "octavia",
                {
                    "provider": s["provider"],
                    "protocol": s["protocol"],
                    "algorithm": s["algorithm"],
                    "responses": dict(observed),
                },
            )
            if len(observed) < s["required_backends_seen"]:
                raise AssertionError(
                    "VIP did not reach the required number of "
                    "distinct backends"
                )

    def _vip_available(self, floating, port, markers):
        import requests

        try:
            return self.http(floating["floating_ip_address"], port) in markers
        except (requests.ConnectionError, requests.Timeout):
            return False

    def disk_setup(self, size, mount, content):
        server, _, _, guest = self.vm()
        guest.tools("e2fsprogs")
        volume = self.volume(size)
        self.attach(server, volume)
        device = guest.device(volume["id"], unmounted=True)
        guest.command(
            "sudo -n mkfs.ext4 -F "
            + q(device)
            + " && sudo -n mkdir -p "
            + q(mount)
            + " && sudo -n mount "
            + q(device)
            + " "
            + q(mount)
        )
        guest.write(mount + "/test.txt", content)
        guest.command("sync")
        return server, volume, guest, device

    def cinder_snapshot_revert(self):
        s = self.options
        server, volume, guest, device = self.disk_setup(
            s["volume_size_gib"], s["mount_path"], s["before_content"]
        )
        with self.step(
            "Unmount and detach disk, create a consistent latest snapshot"
        ):
            guest.command("sudo -n umount " + q(s["mount_path"]))
            self.detach(server, volume)
            snapshot = self.create(
                "volume",
                "/snapshots",
                "snapshot",
                {
                    "name": self.name("snapshot"),
                    "volume_id": volume["id"],
                    "force": False,
                    "metadata": {"ultimum_run_id": self.run_id},
                },
            )
            self.cloud.wait_status(
                "volume",
                "/snapshots/" + snapshot["id"],
                "snapshot",
                {"available"},
                self.timeout,
            )
        self.attach(server, volume)
        device = guest.device(volume["id"])
        guest.command("sudo -n mount " + q(device) + " " + q(s["mount_path"]))
        guest.write(s["mount_path"] + "/test.txt", s["after_content"])
        if (
            guest.command("sudo -n cat " + q(s["mount_path"] + "/test.txt"))
            != s["after_content"].strip()
        ):
            raise AssertionError(
                "Failed to establish changed data before revert"
            )
        guest.command("sync && sudo -n umount " + q(s["mount_path"]))
        self.detach(server, volume)
        with self.step(
            "Revert original detached volume through Cinder 3.40 action"
        ):
            self.cloud.post(
                "volume",
                "/volumes/" + volume["id"] + "/action",
                {"revert": {"snapshot_id": snapshot["id"]}},
            )
            self.cloud.wait_status(
                "volume",
                "/volumes/" + volume["id"],
                "volume",
                {"available"},
                self.timeout,
            )
        self.attach(server, volume)
        device = guest.device(volume["id"])
        guest.command("sudo -n mount " + q(device) + " " + q(s["mount_path"]))
        value = guest.command(
            "sudo -n cat " + q(s["mount_path"] + "/test.txt")
        )
        if value != s["before_content"].strip():
            raise AssertionError(
                "Snapshot revert did not restore original file"
            )
        self.ledger.evidence(
            "snapshot_revert",
            {
                "volume_id": volume["id"],
                "snapshot_id": snapshot["id"],
                "restored_content": value,
            },
        )

    def cinder_volume_extend(self):
        s = self.options
        server, volume, guest, device = self.disk_setup(
            s["initial_size_gib"], s["mount_path"], s["file_content"]
        )

        def fs_size():
            return int(
                guest.command(
                    "df -B1 --output=size "
                    + q(s["mount_path"])
                    + " | tail -n1"
                )
            )

        before = fs_size()
        with self.step(
            "Extend attached volume, verify kernel "
            "capacity and grow ext4 online"
        ):
            self.cloud.post(
                "volume",
                "/volumes/" + volume["id"] + "/action",
                {"os-extend": {"new_size": s["target_size_gib"]}},
            )
            wait_for(
                lambda: self.cloud.get("volume", "/volumes/" + volume["id"])[
                    "volume"
                ],
                lambda v: (
                    v["size"] == s["target_size_gib"]
                    and v["status"] == "in-use"
                ),
                self.timeout,
                description="attached volume extension",
            )
            target = s["target_size_gib"] * 1024**3

            def capacity():
                guest.command(
                    "if test -e /sys/class/block/"
                    + q(pathlib.Path(device).name)
                    + (
                        "/device/rescan; then echo 1 | sudo -n tee "
                        "/sys/class/block/"
                    )
                    + q(pathlib.Path(device).name)
                    + "/device/rescan >/dev/null; fi"
                )
                return int(
                    guest.command("sudo -n blockdev --getsize64 " + q(device))
                )

            actual = wait_for(
                capacity,
                lambda n: n >= target,
                self.timeout,
                description="guest block device growth",
            )
            guest.command(
                "sudo -n resize2fs " + q(device), timeout=self.timeout
            )
            after = fs_size()
            value = guest.command(
                "sudo -n cat " + q(s["mount_path"] + "/test.txt")
            )
            if (
                after <= before
                or after < target * 0.9
                or value != s["file_content"].strip()
            ):
                raise AssertionError(
                    "Filesystem did not grow to target or file data changed"
                )
            self.ledger.evidence(
                "volume_extend",
                {
                    "before_bytes": before,
                    "after_bytes": after,
                    "block_bytes": actual,
                    "file_content": value,
                },
            )

    def masakari_host_failure(self):
        s = self.options
        exclusive_host(self.admin, s["host"])
        vms = [
            self.vm(host=s["host"], label="masakari")
            for _ in range(s["instance_count"])
        ]
        for server, _, _, guest in vms:
            if self.describe(server)["OS-EXT-SRV-ATTR:host"] != s["host"]:
                raise AssertionError("Masakari test VM is on the wrong host")
            guest.write("/var/tmp/ultimum-masakari", self.run_id)
            guest.command("sync")
        owned = [vm[0]["id"] for vm in vms]
        exclusive_host(self.admin, s["host"], owned)
        path = (
            "/segments/"
            + self.runtime["masakari_segment_id"]
            + "/hosts/"
            + self.runtime["masakari_host_id"]
        )
        original = self.admin.get("ha", path)["host"]
        self.save_service(
            s["host"],
            {"path": path, "on_maintenance": original["on_maintenance"]},
        )
        marker = self.ledger.path.parent / "fault-start.json"
        self.ledger.update(status="WAITING_FOR_FAULT")
        self.ledger.event(
            f"READY: dedicated host {s['host']}. In another terminal run: "
            f"ultimum-rally --config {q(self.ledger.data['config_path'])} "
            f"fault-start {self.run_id}; "
            "then HARD POWER OFF that host."
        )

        def armed():
            exclusive_host(self.admin, s["host"], owned)
            return marker.exists()

        wait_for(
            armed,
            bool,
            self.timeout,
            description="operator fault-start marker",
        )
        started = json.loads(marker.read_text())["epoch"]
        self.ledger.update(status="RUNNING")
        deadline = started + s["recovery_timeout_seconds"]
        observed_down, observed_maintenance = False, False

        def recovered():
            nonlocal observed_down, observed_maintenance
            observed_down |= (
                host_service(self.admin, s["host"])["state"] == "down"
            )
            observed_maintenance |= bool(
                self.admin.get("ha", path)["host"]["on_maintenance"]
            )
            current = [self.describe(vm[0]) for vm in vms]
            self.ledger.evidence(
                "masakari_progress",
                {
                    "observed_down": observed_down,
                    "observed_maintenance": observed_maintenance,
                    "servers": [
                        {
                            "id": vm["id"],
                            "host": vm["OS-EXT-SRV-ATTR:host"],
                            "status": vm["status"],
                        }
                        for vm in current
                    ],
                },
            )
            return (
                observed_down
                and observed_maintenance
                and all(
                    vm["status"] == "ACTIVE"
                    and vm["OS-EXT-SRV-ATTR:host"] != s["host"]
                    and not vm.get("OS-EXT-STS:task_state")
                    for vm in current
                )
            )

        wait_for(
            recovered,
            bool,
            max(0, deadline - time.time()),
            description="automatic Masakari recovery",
        )
        for server, _, floating, old_guest in vms:
            old_guest.close()
            guest = self.connect(server, floating)
            if guest.command("cat /var/tmp/ultimum-masakari") != self.run_id:
                raise AssertionError(
                    "Recovered VM lost its persistent test data"
                )
        self.ledger.evidence(
            "masakari_recovery_seconds", time.time() - started
        )
        if time.time() > deadline:
            raise AssertionError(
                "Masakari recovery including guest access "
                "exceeded the configured deadline"
            )
        self.ledger.event(
            "Recovery verified. Power the source host "
            "back on; cleanup will restore saved "
            "Nova/Masakari flags once it is up."
        )

    def nova_shelve_unshelve(self):
        s = self.options
        server, port, floating, guest = self.vm(az=s["source_az"])
        if (
            self.describe(server)["OS-EXT-AZ:availability_zone"]
            != s["source_az"]
        ):
            raise AssertionError("VM did not boot in requested source AZ")
        guest.write(s["data_path"], s["data_content"])
        guest.command("sync")
        guest.close()
        with self.step(
            "Shelve, explicitly offload, then unshelve to target AZ"
        ):
            self.action(server, {"shelve": None})
            shelved = self.cloud.wait_status(
                "compute",
                "/servers/" + server["id"],
                "server",
                {"SHELVED", "SHELVED_OFFLOADED"},
                self.timeout,
            )
            actor = (
                self.admin
                if self.cfg["compute"]["migration_actor"] == "admin"
                else self.cloud
            )
            if shelved["status"] != "SHELVED_OFFLOADED":
                self.action(server, {"shelveOffload": None}, actor)
                self.cloud.wait_status(
                    "compute",
                    "/servers/" + server["id"],
                    "server",
                    {"SHELVED_OFFLOADED"},
                    self.timeout,
                )
            self.action(
                server,
                {"unshelve": {"availability_zone": s["target_az"]}},
                actor,
            )
            self.cloud.wait_status(
                "compute",
                "/servers/" + server["id"],
                "server",
                {"ACTIVE"},
                self.timeout,
            )
        actual = self.describe(server)["OS-EXT-AZ:availability_zone"]
        self.ledger.evidence(
            "unshelve", {"requested_az": s["target_az"], "actual_az": actual}
        )
        if actual != s["target_az"]:
            raise AssertionError("Unshelve did not place VM in target AZ")
        current = self.cloud.get("network", "/floatingips/" + floating["id"])[
            "floatingip"
        ]
        if current.get("port_id") != port["id"]:
            self.cloud.request(
                "network",
                "PUT",
                "/floatingips/" + floating["id"],
                {"floatingip": {"port_id": port["id"]}},
            )
        guest = self.connect(server, floating)
        if (
            guest.command("sudo -n cat " + q(s["data_path"]))
            != s["data_content"].strip()
        ):
            raise AssertionError("VM data changed after shelve/unshelve")
