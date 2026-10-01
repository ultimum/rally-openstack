"""Run-owned resources, guest access, migration and retryable cleanup."""

import base64
import contextlib
import time
import uuid

import requests

from .cloud import APIError
from .cloud import wait_for
from .config import InvalidError
from .guest import Guest
from .guest import q
from .prepare import host_service
from .progress import watch


class Resources:
    def __init__(self, cfg, admin, cloud, ledger, runtime, timer=None):
        self.cfg, self.admin, self.cloud, self.ledger, self.runtime = (
            cfg,
            admin,
            cloud,
            ledger,
            runtime,
        )
        self.run_id = ledger.data["run_id"]
        self.timeout = cfg["execution"]["api_timeout_seconds"]
        self.guests = []
        self.timer = timer
        self.keypair = None
        self.ssh_group = None

    @contextlib.contextmanager
    def step(self, message):
        self.ledger.event("START " + message)
        started = time.monotonic()
        try:
            if self.timer:
                with self.timer(message):
                    yield
            else:
                yield
        except BaseException as exc:
            self.ledger.event(f"FAIL {message} ({type(exc).__name__})")
            raise
        else:
            self.ledger.event(
                f"OK {message} ({time.monotonic() - started:.1f}s)"
            )

    def name(self, suffix):
        prefix = self.cfg["resources_prefix"]
        return f"{prefix}-{self.run_id}-{suffix}-{uuid.uuid4().hex[:6]}"

    def create(self, service, collection, key, values):
        self.ledger.event(f"CREATE {key}: {values.get('name', collection)}")
        # Persist a unique searchable marker before POST, covering a lost
        # response.
        if service in ("network", "lb"):
            values = dict(values, description="Ultimum run " + self.run_id)
        marker = {
            k: values[k]
            for k in ("name", "description", "metadata")
            if k in values
        }
        if not marker:
            raise InvalidError(
                "Run resource needs an ownership marker before creation"
            )
        intent = {
            "service": service,
            "collection": collection,
            "key": key,
            "marker": marker,
            "resolved": False,
        }
        with self.ledger.lock:
            self.ledger.data.setdefault("intents", []).append(intent)
            self.ledger.save()
        try:
            obj = self.cloud.post(service, collection, {key: values})[key]
        except APIError as exc:
            if 400 <= exc.status < 500 and exc.status != 408:
                with self.ledger.lock:
                    intent["resolved"] = True
                    self.ledger.save()
            raise
        self.ledger.own(
            service, collection + "/" + obj["id"], obj["id"], kind=key
        )
        with self.ledger.lock:
            intent["resolved"] = True
            self.ledger.save()
        self.ledger.event(f"OK {key}: {obj['id']}")
        return obj

    def access(self):
        if self.keypair:
            return
        self.keypair = self.runtime["keypair_name"]
        self.ledger.event(f"REUSE prepared SSH keypair: {self.keypair}")
        self.ssh_group = self.security_group("access")
        self.rule(
            self.ssh_group,
            "tcp",
            self.cfg["guest"]["ssh_port"],
            self.cfg["network"]["probe_source_cidr"],
        )
        self.rule(
            self.ssh_group,
            "icmp",
            None,
            self.cfg["network"]["probe_source_cidr"],
        )

    def security_group(self, suffix):
        return self.create(
            "network",
            "/security-groups",
            "security_group",
            {
                "name": self.name(suffix),
                "description": "Ultimum run " + self.run_id,
            },
        )["id"]

    def rule(self, group, protocol, port, cidr):
        values = {
            "security_group_id": group,
            "direction": "ingress",
            "ethertype": "IPv4",
            "protocol": protocol,
            "remote_ip_prefix": cidr,
        }
        if port is not None:
            values.update(port_range_min=port, port_range_max=port)
        return self.create(
            "network", "/security-group-rules", "security_group_rule", values
        )["id"]

    def port(self, groups=None, fixed_ip=None):
        self.access()
        fixed = {"subnet_id": self.runtime["subnet_id"]}
        if fixed_ip:
            fixed["ip_address"] = fixed_ip
        return self.create(
            "network",
            "/ports",
            "port",
            {
                "name": self.name("port"),
                "network_id": self.runtime["network_id"],
                "fixed_ips": [fixed],
                "security_groups": [self.ssh_group] + (groups or []),
                "admin_state_up": True,
            },
        )

    def volume(self, size, image=None):
        values = {
            "name": self.name("volume"),
            "size": size,
            "metadata": {"ultimum_run_id": self.run_id},
        }
        if image:
            values["imageRef"] = image
        for key, value in self.cfg["storage"].items():
            if value:
                values[key] = value
        volume = self.create("volume", "/volumes", "volume", values)
        self.ledger.event(
            f"WAIT volume {volume['id']}: available ({size} GiB)"
        )
        result = self.cloud.wait_status(
            "volume",
            "/volumes/" + volume["id"],
            "volume",
            {"available"},
            self.timeout,
        )
        self.ledger.event(f"OK volume {volume['id']}: available")
        return result

    def server(
        self,
        *,
        port=None,
        userdata=None,
        flavor=None,
        az=None,
        host=None,
        group=None,
        count=1,
        label="vm",
    ):
        self.access()
        compute = self.cfg["compute"]
        values = {
            "name": self.name(label),
            "flavorRef": flavor or self.runtime["flavor_id"],
            "key_name": self.keypair,
            "metadata": {"ultimum_run_id": self.run_id},
            "config_drive": False,
            "networks": [{"port": port["id"]}]
            if port
            else [{"uuid": self.runtime["network_id"]}],
            "security_groups": [{"name": self.ssh_group}],
            "min_count": count,
            "max_count": count,
        }
        selected_az = az or compute["availability_zone"]
        if selected_az:
            values["availability_zone"] = selected_az
        if host:
            values["host"] = host
        if userdata:
            values["user_data"] = base64.b64encode(userdata.encode()).decode()
        if compute["boot_from_volume"]:
            block = {
                "boot_index": 0,
                "destination_type": "volume",
                "delete_on_termination": True,
            }
            if count == 1:
                root = self.volume(
                    compute["root_volume_size_gib"], self.runtime["image_id"]
                )
                block.update(source_type="volume", uuid=root["id"])
            else:
                block.update(
                    source_type="image",
                    uuid=self.runtime["image_id"],
                    volume_size=compute["root_volume_size_gib"],
                )
                if self.cfg["storage"]["volume_type"]:
                    block["volume_type"] = self.cfg["storage"]["volume_type"]
            values["block_device_mapping_v2"] = [block]
        else:
            values["imageRef"] = self.runtime["image_id"]
        body = {"server": values}
        if group:
            body["os:scheduler_hints"] = {"group": group}
        if count > 1:
            values["return_reservation_id"] = True
        self.ledger.event(f"CREATE VM: {values['name']}, Count={count}")
        response = self.cloud.post("compute", "/servers", body)
        if count > 1:
            reservation = response["reservation_id"]
            self.ledger.event(f"Nova Count={count}, reservation={reservation}")

            def discover():
                found = self.cloud.list(
                    "compute",
                    "/servers/detail",
                    "servers",
                    reservation_id=reservation,
                )
                found = [
                    vm
                    for vm in found
                    if vm.get("metadata", {}).get("ultimum_run_id")
                    == self.run_id
                    and (
                        vm["name"] == values["name"]
                        or vm["name"].startswith(values["name"] + "-")
                    )
                ]
                for obj in found:
                    self.ledger.own(
                        "compute",
                        "/servers/" + obj["id"],
                        obj["id"],
                        kind="server",
                    )
                return found

            servers = wait_for(
                discover,
                lambda x: len(x) == count,
                self.timeout,
                description="Nova batch discovery",
            )
        else:
            servers = [response["server"]]
            self.ledger.own(
                "compute",
                "/servers/" + servers[0]["id"],
                servers[0]["id"],
                kind="server",
            )
        result = []
        for obj in servers:
            self.ledger.event(f"WAIT VM {obj['id']}: ACTIVE")
            server = self.cloud.wait_status(
                "compute",
                "/servers/" + obj["id"],
                "server",
                {"ACTIVE"},
                self.timeout,
            )
            for volume in server.get(
                "os-extended-volumes:volumes_attached", []
            ):
                self.ledger.own(
                    "volume",
                    "/volumes/" + volume["id"],
                    volume["id"],
                    kind="volume",
                )
            result.append(server)
            self.ledger.event(f"OK VM {obj['id']}: ACTIVE")
        return result if count > 1 else result[0]

    def describe(self, server):
        return self.admin.get("compute", "/servers/" + server["id"])["server"]

    def floating(self, port_id):
        floating = self.create(
            "network",
            "/floatingips",
            "floatingip",
            {
                "floating_network_id": self.runtime["external_network_id"],
                "port_id": port_id,
                "description": "Ultimum run " + self.run_id,
            },
        )
        self.ledger.event(
            f"OK floating IP {floating['floating_ip_address']} "
            f"-> port {port_id}"
        )
        return floating

    def vm(self, **kwargs):
        port = kwargs.pop("port", None) or self.port()
        server = self.server(port=port, **kwargs)
        floating = self.floating(port["id"])
        guest = self.connect(server, floating)
        return server, port, floating, guest

    def connect(self, server, floating):
        guest = Guest(
            floating["floating_ip_address"],
            server["id"],
            self.cfg["guest"],
            self.ledger,
        ).connect()
        self.guests.append(guest)
        self.ledger.event(f"WAIT cloud-init via SSH on VM {server['id']}")
        guest.command(
            "sudo -n cloud-init status --wait",
            self.cfg["guest"]["cloud_init_timeout_seconds"],
        )
        self.ledger.event(f"OK cloud-init on VM {server['id']}")
        return guest

    def action(self, server, body, actor=None):
        self.ledger.event(f"ACTION VM {server['id']}: {', '.join(body)}")
        (actor or self.cloud).post(
            "compute", "/servers/" + server["id"] + "/action", body
        )

    def group(self, policy):
        return self.create(
            "compute",
            "/os-server-groups",
            "server_group",
            {"name": self.name("group"), "policy": policy},
        )["id"]

    def target(self, server, requested=None):
        before = self.describe(server)
        source, az = (
            before["OS-EXT-SRV-ATTR:host"],
            before["OS-EXT-AZ:availability_zone"],
        )
        zones = self.admin.list(
            "compute", "/os-availability-zone/detail", "availabilityZoneInfo"
        )
        zone = next((z for z in zones if z["zoneName"] == az), None)
        if not zone:
            raise InvalidError("Cannot resolve source availability zone")
        candidates = [
            host
            for host, services in (zone.get("hosts") or {}).items()
            if host != source
            and services.get("nova-compute", {}).get("active")
            and services["nova-compute"].get("available")
        ]
        if requested:
            if requested not in candidates:
                raise InvalidError(
                    "Target host must be another enabled/up "
                    "compute in the same AZ"
                )
            return requested, before
        if not candidates:
            raise InvalidError("No other enabled/up compute in source AZ")
        return sorted(candidates)[0], before

    def migrate(self, server, target=None, cold=False):
        target, before = self.target(server, target)
        actor = (
            self.admin
            if self.cfg["compute"]["migration_actor"] == "admin"
            else self.cloud
        )
        self.ledger.event(
            f"{'Cold' if cold else 'Live'} migration "
            f"{server['id']} -> {target}"
        )
        if cold:
            self.action(server, {"migrate": {"host": target}}, actor)
            self.cloud.wait_status(
                "compute",
                "/servers/" + server["id"],
                "server",
                {"VERIFY_RESIZE"},
                self.timeout,
            )
            self.action(server, {"confirmResize": None}, actor)
        else:
            self.action(
                server,
                {"os-migrateLive": {"host": target, "block_migration": False}},
                actor,
            )

        def migrated(obj):
            if obj["status"] == "ERROR":
                raise AssertionError("VM entered ERROR during migration")
            return (
                obj["OS-EXT-SRV-ATTR:host"] == target
                and obj["status"]
                in ({"SHUTOFF", "ACTIVE"} if cold else {"ACTIVE"})
                and not obj.get("OS-EXT-STS:task_state")
            )

        after = wait_for(
            lambda: self.describe(server),
            migrated,
            self.timeout,
            description="host change after migration",
        )
        if (
            after["OS-EXT-AZ:availability_zone"]
            != before["OS-EXT-AZ:availability_zone"]
        ):
            raise AssertionError("Migration changed availability zone")
        self.ledger.evidence(
            "migration_" + server["id"],
            {
                "source": before["OS-EXT-SRV-ATTR:host"],
                "target": after["OS-EXT-SRV-ATTR:host"],
                "az": after["OS-EXT-AZ:availability_zone"],
            },
        )
        self.ledger.event(f"OK migration: VM {server['id']} on {target}")
        return after

    def attach(self, server, volume):
        self.ledger.event(f"ATTACH volume {volume['id']} to VM {server['id']}")
        self.cloud.post(
            "compute",
            "/servers/" + server["id"] + "/os-volume_attachments",
            {"volumeAttachment": {"volumeId": volume["id"]}},
        )
        self.cloud.wait_status(
            "volume",
            "/volumes/" + volume["id"],
            "volume",
            {"in-use"},
            self.timeout,
        )

    def detach(self, server, volume):
        self.ledger.event(
            f"DETACH volume {volume['id']} from VM {server['id']}"
        )
        self.cloud.delete(
            "compute",
            "/servers/"
            + server["id"]
            + "/os-volume_attachments/"
            + volume["id"],
        )
        self.cloud.wait_status(
            "volume",
            "/volumes/" + volume["id"],
            "volume",
            {"available"},
            self.timeout,
        )

    def webserver(self, guest, content, port):
        self.ledger.event(f"START guest HTTP service on port {port}")
        guest.tools("python3")
        guest.command("sudo -n mkdir -p /var/lib/ultimum-http")
        guest.write("/var/lib/ultimum-http/index.html", content)
        guest.command(
            (
                "sudo -n systemd-run --unit=ultimum-http "
                "--property=Restart=on-failure "
                "/usr/bin/python3 -m http.server "
            )
            + q(port)
            + " --bind 0.0.0.0 --directory /var/lib/ultimum-http"
        )

    def http(self, address, port, expected=None):
        # A fresh Session per probe avoids keep-alive/conntrack and proxy
        # settings.
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                f"http://{address}:{port}/",
                timeout=4,
                headers={"Connection": "close"},
                allow_redirects=False,
            )
            response.raise_for_status()
            text = response.text.strip()
            if expected is not None and text != expected:
                raise AssertionError(
                    "HTTP payload differs from expected backend marker"
                )
            return text

    def wait_http(self, address, port, expected, timeout=60, reachable=True):
        consecutive = 0

        def probe():
            nonlocal consecutive
            try:
                self.http(address, port, expected)
                ok = reachable
            except (requests.ConnectionError, requests.Timeout):
                ok = not reachable
            consecutive = consecutive + 1 if ok else 0
            return consecutive >= 3

        wait_for(
            probe,
            bool,
            timeout,
            description="HTTP reachability=" + str(reachable),
        )

    def save_service(self, host, masakari=None):
        service = host_service(self.admin, host)
        restore = {
            "host": host,
            "service_id": service["id"],
            "status": service["status"],
            "disabled_reason": service.get("disabled_reason"),
            "done": False,
        }
        if masakari:
            restore["masakari"] = masakari
        self.ledger.data["restore"].append(restore)
        self.ledger.save()

    def restore_services(self):
        for item in self.ledger.data["restore"]:
            if item["done"]:
                continue
            self.ledger.event(
                f"RESTORE original service state: {item['host']}"
            )
            service = host_service(self.admin, item["host"])
            if service["state"] != "up":
                raise InvalidError(
                    "Host is still down; restore pending. "
                    "Power it on, then run cleanup again"
                )
            if item.get("masakari"):
                saved = item["masakari"]
                self.admin.request(
                    "ha",
                    "PUT",
                    saved["path"],
                    {"host": {"on_maintenance": saved["on_maintenance"]}},
                )
            body = {"status": item["status"]}
            if item["status"] == "disabled":
                body["disabled_reason"] = item["disabled_reason"]
            self.admin.request(
                "compute",
                "PUT",
                "/os-services/" + str(item["service_id"]),
                body,
            )
            item["done"] = True
            self.ledger.save()
        self.ledger.data.pop("restore_error", None)
        self.ledger.save()

    def close(self):
        for guest in self.guests:
            guest.close()

    def reconcile_servers(self):
        # Recover a successful create whose response was lost before ledger
        # write.
        for server in self.cloud.list("compute", "/servers/detail", "servers"):
            if server.get("metadata", {}).get("ultimum_run_id") == self.run_id:
                self.ledger.own(
                    "compute",
                    "/servers/" + server["id"],
                    server["id"],
                    kind="server",
                )
                for volume in server.get(
                    "os-extended-volumes:volumes_attached", []
                ):
                    self.ledger.own(
                        "volume",
                        "/volumes/" + volume["id"],
                        volume["id"],
                        kind="volume",
                    )

    def reconcile_intents(self):
        for intent in self.ledger.data.get("intents", []):
            if intent["resolved"]:
                continue
            collection = intent["collection"]
            list_path = (
                collection + "/detail"
                if intent["service"] == "volume"
                else collection
            )
            objects = self.cloud.list(
                intent["service"], list_path, intent["key"] + "s"
            )
            for obj in objects:
                if all(
                    obj.get(k) == value
                    for k, value in intent["marker"].items()
                ):
                    self.ledger.own(
                        intent["service"],
                        collection + "/" + obj["id"],
                        obj["id"],
                        kind=intent["key"],
                    )
                    intent["resolved"] = True
                    self.ledger.save()
            # Keep unresolved intents searchable on later cleanup attempts:
            # an accepted create may still be asynchronous at this point.

    @watch()
    def cleanup(self):
        self.ledger.event(
            "START cleanup: reconcile and remove run-owned resources"
        )
        if self.cloud.project_id != self.ledger.data["project_id"]:
            raise InvalidError("Cleanup project differs from the run ledger")
        self.close()
        errors = []
        for reconcile in (self.reconcile_intents, self.reconcile_servers):
            try:
                reconcile()
            except Exception as exc:
                errors.append(f"Resource reconciliation: {exc}")
        if (
            not self.cfg["compute"]["boot_from_volume"]
            and self.ledger.data.get("scenario") == "nova-shelve-unshelve"
        ):
            owned = {
                obj["id"]
                for obj in self.ledger.data["resources"]
                if obj["kind"] == "server"
            }
            try:
                for image in self.cloud.list("image", "/images", "images"):
                    if image.get("instance_uuid") in owned:
                        self.ledger.own(
                            "image",
                            "/images/" + image["id"],
                            image["id"],
                            kind="image",
                        )
            except Exception as exc:
                errors.append(f"Shelve image reconciliation: {exc}")
        priority = {
            "floatingip": 0,
            "loadbalancer": 1,
            "server": 2,
            "port": 3,
            "snapshot": 4,
            "image": 4,
            "volume": 5,
            "security_group_rule": 6,
            "security_group": 7,
            "server_group": 8,
            "keypair": 9,
        }
        for obj in sorted(
            self.ledger.data["resources"],
            key=lambda r: priority.get(r["kind"], 5),
        ):
            if obj["deleted"]:
                continue
            self.ledger.event(f"DELETE {obj['kind']}: {obj['id']}")
            try:
                path = obj["path"] + (
                    "?cascade=true" if obj["kind"] == "loadbalancer" else ""
                )
                try:
                    self.cloud.delete(obj["service"], path)
                except APIError as exc:
                    if exc.status != 404:
                        raise
                wait_for(
                    lambda: self.cloud.absent(obj["service"], obj["path"]),
                    bool,
                    self.timeout,
                    description="delete " + obj["path"],
                )
                obj["deleted"] = True
                self.ledger.save()
                self.ledger.event(f"OK deleted {obj['kind']}: {obj['id']}")
            except Exception as exc:
                errors.append(f"{obj['kind']} {obj['id']}: {exc}")
        try:
            self.restore_services()
        except Exception as exc:
            errors.append(str(exc))
        if any(not i["resolved"] for i in self.ledger.data.get("intents", [])):
            errors.append(
                "Unconfirmed create request(s): repeat cleanup after the "
                "cloud settles; inspect unresolved intents if still absent"
            )
        self.ledger.update(cleanup_errors=errors, cleanup_complete=not errors)
        if not errors:
            self.ledger.data.pop("cleanup_error", None)
            self.ledger.save()
        if errors:
            self.ledger.event(
                "FAIL cleanup: some resources or restoration remain"
            )
            raise InvalidError("Cleanup incomplete: " + "; ".join(errors))
        self.ledger.event(
            "OK cleanup complete; shared resources and SSH key retained"
        )
