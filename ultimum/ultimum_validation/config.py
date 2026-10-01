"""Configuration and local validation. Importing this module needs no cloud."""

import copy
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import subprocess

import yaml


SCENARIOS = (
    "placement",
    "nova-live-migration",
    "nova-live-migration-tpm",
    "nova-drain",
    "nova-cloud-init",
    "neutron-dhcp",
    "neutron-security-group",
    "octavia-vip",
    "cinder-snapshot-revert",
    "cinder-volume-extend",
    "masakari-host-failure",
    "nova-shelve-unshelve",
    "nova-evacuate",
)


class InvalidError(Exception):
    "InvalidError input or unmet prerequisite (BLOCKED, never a passing test)."


class UnsupportedError(InvalidError):
    """A required API capability is unavailable."""


def fingerprint(config):
    return hashlib.sha256(
        json.dumps(config, sort_keys=True).encode()
    ).hexdigest()


def upgrade_keys(value):
    """Accept the initial runner schema without conflicting duplicate knobs."""
    if not isinstance(value, dict):
        raise InvalidError("Configuration must be a mapping")
    value = copy.deepcopy(value)

    def alias(key, old):
        if key in value and (
            type(value[key]) is not type(old) or value[key] != old
        ):
            raise InvalidError(f"Conflicting old/new setting for {key}")
        value[key] = old

    identity = value.get("identity", {})
    if isinstance(identity, dict):
        for kind in ("project", "user"):
            options = identity.get(kind, {})
            if isinstance(options, dict) and "create_if_missing" in options:
                alias("create_" + kind, options.pop("create_if_missing"))
    network = value.get("network", {})
    if isinstance(network, dict) and "mode" in network:
        mode = network.pop("mode")
        if mode not in ("managed", "existing"):
            raise InvalidError("network.mode must be managed or existing")
        for kind in ("network", "subnet", "router"):
            alias("create_" + kind, mode == "managed")
    execution = value.get("execution", {})
    if isinstance(execution, dict) and "resource_prefix" in execution:
        alias("resources_prefix", execution.pop("resource_prefix"))
    scenarios = value.get("scenarios", {})
    if isinstance(scenarios, dict):
        drain = scenarios.get("nova-drain", {})
        if isinstance(drain, dict) and "max_parallel_migrations" in drain:
            old_limit = drain.pop("max_parallel_migrations")
            if type(old_limit) is not int or old_limit != 1:
                raise InvalidError(
                    "Legacy nova-drain.max_parallel_migrations must be 1"
                )
    return value


def resource_names(cfg):
    """Generate missing names only when creation is allowed."""
    specs = (
        ("project", cfg["identity"]["project"], "identity.project", "project"),
        ("user", cfg["identity"]["user"], "identity.user", "user"),
        (
            "network",
            cfg["network"]["tenant_network"],
            "network.tenant_network",
            "net",
        ),
        ("subnet", cfg["network"]["subnet"], "network.subnet", "subnet"),
        ("router", cfg["network"]["router"], "network.router", "router"),
        ("ssh_key", cfg["ssh_key"], "ssh_key", "key"),
    )
    for kind, options, path, suffix in specs:
        create = cfg["create_" + kind]
        if type(create) is not bool:
            raise InvalidError(f"create_{kind} must be a YAML boolean")
        name, identifier = options.get("name"), options.get("id")
        if name is not None and (
            not isinstance(name, str) or not name.strip()
        ):
            raise InvalidError(f"{path}.name must be nonempty or null")
        if identifier is not None and not identifier.strip():
            raise InvalidError(f"{path}.id must be nonempty or null")
        if create:
            if identifier:
                raise InvalidError(f"{path}.id requires create_{kind}=false")
            if not name:
                options["name"] = cfg["resources_prefix"] + "-" + suffix
        elif not (name or identifier):
            raise InvalidError(
                f"create_{kind}=false requires explicit {path}.name"
                + (" or id" if kind in ("network", "subnet", "router") else "")
            )
    key_name = cfg["ssh_key"]["name"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,255}", key_name):
        raise InvalidError(
            "ssh_key.name must be 1-255 letters/digits/underscores/hyphens"
        )
    directory = pathlib.Path(cfg["ssh_key"]["directory"])
    if not directory.is_absolute():
        raise InvalidError("ssh_key.directory must be an absolute path")
    guest = cfg["guest"]
    if guest["ssh_private_key"] is None:
        guest["ssh_private_key"] = str(directory / key_name)
    if guest["ssh_public_key"] is None:
        guest["ssh_public_key"] = guest["ssh_private_key"] + ".pub"
    for field in ("ssh_private_key", "ssh_public_key"):
        if not pathlib.Path(guest[field]).is_absolute():
            raise InvalidError(f"guest.{field} must be an absolute path")
    if guest["ssh_private_key"] == guest["ssh_public_key"]:
        raise InvalidError("SSH private and public key paths must differ")


def load(path):
    with open(path) as stream:
        cfg = upgrade_keys(yaml.safe_load(stream))
    example = (
        pathlib.Path(__file__).resolve().parent.parent / "ultimum.yaml.example"
    )
    with example.open() as stream:
        defaults = yaml.safe_load(stream)

    def merge(base, value, prefix=""):
        if not isinstance(value, dict):
            raise InvalidError(
                f"{prefix or 'configuration'} must be a mapping"
            )
        result = copy.deepcopy(base)
        for key, item in value.items():
            if key not in base:
                raise InvalidError(f"Unknown configuration key: {prefix}{key}")
            if isinstance(base[key], dict) and base[key]:
                result[key] = merge(base[key], item, f"{prefix}{key}.")
            else:
                expected = base[key]
                if prefix + key == "tls.insecure":
                    if item is not None and type(item) is not bool:
                        raise InvalidError(
                            "tls.insecure must be a YAML boolean or null"
                        )
                elif expected is None:
                    if item is not None and not isinstance(item, str):
                        raise InvalidError(
                            f"{prefix}{key} must be a string or null"
                        )
                else:
                    valid_type = (
                        type(item) is type(expected)
                        or isinstance(expected, float)
                        and type(item) in (int, float)
                    )
                    if not valid_type:
                        raise InvalidError(
                            f"{prefix}{key} must have type "
                            f"{type(expected).__name__}"
                        )
                result[key] = item
        return result

    cfg = merge(defaults, cfg)
    if cfg["schema_version"] != 1:
        raise InvalidError("schema_version must be 1")
    if cfg["execution"]["cleanup"] not in ("always", "on-success", "never"):
        raise InvalidError(
            "execution.cleanup must be always, on-success or never"
        )
    if cfg["compute"]["migration_actor"] not in ("admin", "test_user"):
        raise InvalidError(
            "compute.migration_actor must be admin or test_user"
        )
    if not cfg["execution"]["project_lock"]:
        raise InvalidError("project_lock cannot be disabled in this release")
    positive(cfg["execution"]["repetitions"], "execution.repetitions")
    positive(cfg["execution"]["api_timeout_seconds"], "api_timeout_seconds")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", cfg["resources_prefix"]):
        raise InvalidError(
            "resources_prefix must be 1-48 letters/digits/underscores/hyphens"
        )
    resource_names(cfg)
    for name, options in cfg["scenarios"].items():
        for key, value in options.items():
            if key.endswith("_seconds") or key in (
                "payload_bytes",
                "backend_count",
                "probe_connections",
                "volume_size_gib",
                "initial_size_gib",
                "target_size_gib",
                "instance_count",
            ):
                positive(value, f"scenarios.{name}.{key}")
        for key in ("http_port", "vip_port", "backend_port"):
            if key in options and not 1 <= options[key] <= 65535:
                raise InvalidError(f"{name}.{key} must be a valid TCP port")
    if not 1 <= cfg["guest"]["ssh_port"] <= 65535:
        raise InvalidError("guest.ssh_port must be a valid TCP port")
    for key in ("ssh_timeout_seconds", "cloud_init_timeout_seconds"):
        positive(cfg["guest"][key], "guest." + key)
    for name in ("nova", "cinder", "neutron", "octavia"):
        if any(
            type(v) is not int or v < -1 for v in cfg["quotas"][name].values()
        ):
            raise InvalidError(f"quotas.{name} must contain integers >= -1")
    return cfg


def positive(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
    ):
        raise InvalidError(f"{name} must be positive")


def validate(cfg, scenario=None):
    resource_names(cfg)
    user = cfg["identity"]["user"]
    if not user["password"] or user["password"] == "CHANGE_ME":
        raise InvalidError("Set identity.user.password")
    for section in ("project", "user"):
        for key in ("name", "domain"):
            if not cfg["identity"][section][key]:
                raise InvalidError(f"Set identity.{section}.{key}")
    if not user["roles"] or not isinstance(user["roles"], list):
        raise InvalidError("identity.user.roles must be a nonempty list")
    net = cfg["network"]
    if not net["external_network"]:
        raise InvalidError("Set network.external_network (name or UUID)")
    pool = net["external_ip_pool"]
    if (pool["start"] is None) != (pool["end"] is None):
        raise InvalidError("external_ip_pool requires both start and end")
    if pool["start"] is None and pool["subnet"] is not None:
        raise InvalidError("external_ip_pool.subnet requires start and end")
    if pool["start"] is not None:
        start, end = (ipaddress.ip_address(pool[k]) for k in ("start", "end"))
        if start.version != 4 or end.version != 4 or start > end:
            raise InvalidError("external_ip_pool needs IPv4 start <= end")
        fixed = net["router"]["external_fixed_ip"]
        if (
            cfg["create_router"]
            and fixed
            and not (
                int(start) <= int(ipaddress.ip_address(fixed)) <= int(end)
            )
        ):
            raise InvalidError(
                "Router external_fixed_ip is outside external_ip_pool"
            )
    if not net["probe_source_cidr"]:
        raise InvalidError(
            "Set network.probe_source_cidr to the runner's actual source CIDR"
        )
    if ipaddress.ip_network(net["probe_source_cidr"]).version != 4:
        raise InvalidError(
            "network.probe_source_cidr must be IPv4 for the SSH/ICMP rules"
        )
    subnet = net["subnet"]
    cidr = ipaddress.ip_network(subnet["cidr"])
    if cidr.version != 4:
        raise InvalidError("This release requires an IPv4 tenant subnet")
    for key in ("gateway_ip", "allocation_pool_start", "allocation_pool_end"):
        if ipaddress.ip_address(subnet[key]) not in cidr:
            raise InvalidError(f"network.subnet.{key} is outside subnet CIDR")
    if ipaddress.ip_address(
        subnet["allocation_pool_start"]
    ) > ipaddress.ip_address(subnet["allocation_pool_end"]):
        raise InvalidError("Allocation pool start must not exceed end")
    if (
        net["router"]["external_fixed_ip"]
        and not net["router"]["external_subnet"]
    ):
        raise InvalidError(
            "external_fixed_ip also requires router.external_subnet"
        )
    for key in ("ssh_private_key", "ssh_public_key"):
        if (
            not cfg["create_ssh_key"]
            and not pathlib.Path(cfg["guest"][key]).is_file()
        ):
            raise InvalidError(f"Missing guest.{key} file")
    if scenario is None:
        return
    if scenario not in SCENARIOS:
        raise InvalidError(f"Unknown scenario: {scenario}")
    s = cfg["scenarios"][scenario]
    if not s["enabled"]:
        raise InvalidError(f"{scenario} is disabled in configuration")
    if scenario in ("nova-drain", "nova-evacuate", "masakari-host-failure"):
        if not s["host"] or not s["require_exclusive_test_host"]:
            raise InvalidError(
                "Host tests require an explicit, exclusive test host"
            )
        if (
            scenario in ("nova-drain", "masakari-host-failure")
            and not s["restore_original_service_state"]
        ):
            raise InvalidError("Host service restoration cannot be disabled")
    if scenario == "nova-drain" and (
        min(s["active_instances"], s["stopped_instances"]) < 0
        or s["active_instances"] + s["stopped_instances"] == 0
    ):
        raise InvalidError(
            "Drain requires at least one instance and nonnegative counts"
        )
    if scenario == "nova-evacuate" and not cfg["compute"]["boot_from_volume"]:
        raise InvalidError(
            "Nova evacuation requires compute.boot_from_volume=true "
            "to verify persistent guest data"
        )
    if scenario == "masakari-host-failure":
        if s["fault_mode"] != "manual" or not s["segment"]:
            raise InvalidError(
                "Masakari requires fault_mode=manual and an explicit segment"
            )
    if scenario == "nova-live-migration":
        if s["block_migration"] or not s["same_availability_zone"]:
            raise InvalidError(
                "Only same-AZ shared-storage live migration is supported"
            )
        if not s["require_same_ssh_connection"]:
            raise InvalidError(
                "The migration test requires a single "
                "persistent SSH connection"
            )
    if (
        scenario == "nova-live-migration-tpm"
        and not cfg["compute"]["tpm_flavor"]
    ):
        raise InvalidError("Set compute.tpm_flavor")
    if scenario == "nova-cloud-init" and s["config_drive"]:
        raise InvalidError("The metadata test requires config_drive=false")
    if scenario == "nova-shelve-unshelve":
        if (
            not s["source_az"]
            or not s["target_az"]
            or s["source_az"] == s["target_az"]
        ):
            raise InvalidError("Set distinct source_az and target_az")
        if not s["require_shelved_offloaded"] or not s["expected_target_az"]:
            raise InvalidError(
                "Unshelve test requires offload and exact target AZ"
            )
    if scenario.startswith("cinder-"):
        if s["filesystem"] != "ext4" or s["device_selection"] != "volume_id":
            raise InvalidError(
                "Storage tests require ext4 and volume_id device selection"
            )
        if not s["mount_path"].startswith("/mnt/"):
            raise InvalidError("Storage mount_path must be below /mnt/")
    if scenario == "cinder-volume-extend":
        if not s["online"] or s["target_size_gib"] <= s["initial_size_gib"]:
            raise InvalidError(
                "Online extend requires target_size_gib > initial_size_gib"
            )
    if scenario == "neutron-dhcp":
        ip = ipaddress.ip_address(s["fixed_ip"])
        if (
            ip not in cidr
            or ip in (cidr.network_address, cidr.broadcast_address)
            or str(ip) == subnet["gateway_ip"]
        ):
            raise InvalidError(
                "DHCP fixed_ip must be a usable address in the tenant subnet"
            )
        if (
            ipaddress.ip_address(subnet["allocation_pool_start"])
            <= ip
            <= ipaddress.ip_address(subnet["allocation_pool_end"])
        ):
            raise InvalidError(
                "DHCP fixed_ip must be outside the allocation pool"
            )
    if scenario == "placement":
        if (
            cfg["compute"]["boot_from_volume"]
            and cfg["storage"]["availability_zone"]
            and any(type(n) is int and n > 1 for n in s["batch_sizes"])
        ):
            raise UnsupportedError(
                "Nova Count cannot set an independent Cinder AZ; "
                "unset storage.availability_zone for placement"
            )
        if (
            not s["batch_sizes"]
            or not s["submission_modes"]
            or not set(s["submission_modes"]) <= {"serial", "parallel"}
        ):
            raise InvalidError(
                "Placement requires batches and "
                "serial/parallel submission modes"
            )
        for count in s["batch_sizes"]:
            positive(count, "placement batch size")
            if type(count) is not int:
                raise InvalidError("Placement batch counts must be integers")
        if s["anti_affinity_count"] < 0:
            raise InvalidError("anti_affinity_count must be nonnegative")
        if s["group_policy"] not in (
            None,
            "affinity",
            "anti-affinity",
            "soft-affinity",
            "soft-anti-affinity",
        ):
            raise InvalidError("Unknown placement group policy")
    if scenario == "octavia-vip":
        if s["provider"] == "ovn" and (
            s["protocol"] != "TCP" or s["algorithm"] != "SOURCE_IP_PORT"
        ):
            raise UnsupportedError(
                "OVN HTTP backend test requires TCP / SOURCE_IP_PORT"
            )
        if not 1 <= s["required_backends_seen"] <= s["backend_count"]:
            raise InvalidError(
                "required_backends_seen must be between 1 and backend_count"
            )


def openrc(path):
    "Source an explicitly trusted operator RC without printing its secrets."
    if not pathlib.Path(path).is_file():
        raise InvalidError("admin.openrc file does not exist")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OS_")}
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            'set +x; set -a; source "$1" >/dev/null 2>&1 || exit; env -0',
            "ultimum",
            str(path),
        ],
        env=env,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise InvalidError("Could not load admin.openrc")
    values = {}
    for entry in result.stdout.split(b"\0"):
        key, sep, value = entry.partition(b"=")
        if sep and key.startswith(b"OS_"):
            values[key.decode()] = value.decode()
    for key in ("OS_AUTH_URL", "OS_USERNAME", "OS_PASSWORD"):
        if not values.get(key):
            raise InvalidError(f"admin.openrc must export {key}")
    if not (values.get("OS_PROJECT_NAME") or values.get("OS_PROJECT_ID")):
        raise InvalidError("admin.openrc must select an admin project")
    return values
