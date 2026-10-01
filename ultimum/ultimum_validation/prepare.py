"""Idempotent identity/network preparation and read-only capability checks."""

from .cloud import APIError
from .cloud import Cloud
from .cloud import session_from_rc
from .cloud import unique
from .config import InvalidError
from .config import UnsupportedError
from .config import validate


def clouds(cfg, rc):
    return (
        Cloud(session_from_rc(rc), rc),
        Cloud(session_from_rc(rc, cfg["identity"]), rc),
    )


def ensure_identity(cfg, admin):
    identities = cfg["identity"]
    domains = admin.list("identity", "/domains", "domains")
    project_domain = unique(
        domains, identities["project"]["domain"], "domain"
    )["id"]
    user_domain = unique(domains, identities["user"]["domain"], "domain")["id"]
    objects = {}
    for kind, domain in (("project", project_domain), ("user", user_domain)):
        options = identities[kind]
        matches = admin.list(
            "identity",
            "/" + kind + "s",
            kind + "s",
            domain_id=domain,
            name=options["name"],
        )
        matches = [
            m
            for m in matches
            if m["name"] == options["name"] and m["domain_id"] == domain
        ]
        if matches:
            obj = unique(matches, options["name"], kind)
            if not obj.get("enabled", True):
                raise InvalidError(f"Configured {kind} is disabled")
            if kind == "user" and options["update_existing_password"]:
                admin.request(
                    "identity",
                    "PATCH",
                    "/users/" + obj["id"],
                    {"user": {"password": options["password"]}},
                )
        else:
            if not cfg["create_" + kind]:
                raise InvalidError(
                    f"Configured {kind} does not exist "
                    "and creation is disabled"
                )
            body = {
                "name": options["name"],
                "domain_id": domain,
                "enabled": True,
                "description": "Ultimum acceptance testing",
            }
            if kind == "user":
                body["password"] = options["password"]
            obj = admin.post("identity", "/" + kind + "s", {kind: body})[kind]
        objects[kind] = obj
    roles = admin.list("identity", "/roles", "roles")
    for name in identities["user"]["roles"]:
        role = unique(roles, name, "role")
        admin.request(
            "identity",
            "PUT",
            "/projects/{}/users/{}/roles/{}".format(
                objects["project"]["id"], objects["user"]["id"], role["id"]
            ),
        )
    return objects["project"]["id"]


def set_quotas(cfg, admin, project_id):
    quotas = cfg["quotas"]
    if not quotas["apply"]:
        return
    targets = {
        "nova": ("compute", "/os-quota-sets/", "quota_set"),
        "cinder": ("volume", "/os-quota-sets/", "quota_set"),
        "neutron": ("network", "/quotas/", "quota"),
        "octavia": ("lb", "/lbaas/quotas/", "quota"),
    }
    for name, (service, path, key) in targets.items():
        if quotas[name]:
            if not isinstance(quotas[name], dict) or any(
                type(v) is not int or v < -1 for v in quotas[name].values()
            ):
                raise InvalidError(
                    f"quotas.{name} values must be integers >= -1"
                )
            admin.request(
                service, "PUT", path + project_id, {key: quotas[name]}
            )


def resolve_base(cfg, cloud):
    image = unique(
        cloud.list("image", "/images", "images"),
        cfg["compute"]["image"],
        "image",
    )
    if image.get("status") != "active":
        raise InvalidError("Configured image is not active")
    flavor = unique(
        cloud.list("compute", "/flavors/detail", "flavors"),
        cfg["compute"]["flavor"],
        "flavor",
    )
    external = unique(
        cloud.list("network", "/networks", "networks"),
        cfg["network"]["external_network"],
        "external network",
    )
    if not external.get("router:external"):
        raise InvalidError(
            "network.external_network is not an external network"
        )
    return {
        "image_id": image["id"],
        "flavor_id": flavor["id"],
        "external_network_id": external["id"],
    }


def prepare_network(cfg, cloud, base):
    opts = cfg["network"]
    project_id = cloud.project_id
    managed = "Managed by ultimum-rally prepare"
    parts = {
        "network": "tenant_network",
        "subnet": "subnet",
        "router": "router",
    }

    def find(kind):
        options = opts[parts[kind]]
        collection = "/" + kind + "s"
        if options["id"]:
            try:
                return cloud.get("network", collection + "/" + options["id"])[
                    kind
                ]
            except APIError as exc:
                if exc.status == 404:
                    return None
                raise
        candidates = cloud.list(
            "network",
            collection,
            kind + "s",
            project_id=project_id,
            name=options["name"],
        )
        candidates = [
            obj
            for obj in candidates
            if obj["name"] == options["name"]
            and obj.get("project_id", obj.get("tenant_id")) == project_id
        ]
        if candidates:
            return unique(candidates, options["name"], kind)
        return None

    # Resolve every reference before creating anything in Neutron.
    existing = {}
    for kind in parts:
        obj = find(kind)
        if not cfg["create_" + kind] and obj is None:
            raise InvalidError(
                f"Configured {kind} does not exist and create_{kind}=false"
            )
        if cfg["create_" + kind] and obj is not None:
            if obj.get("description") != managed:
                raise InvalidError(
                    f"Existing {kind} name collides with an unmanaged "
                    f"resource; set create_{kind}=false to use it"
                )
        existing[kind] = obj

    network, subnet, router = (existing[k] for k in parts)
    s = opts["subnet"]
    subnet_values = {
        "name": s["name"],
        "ip_version": 4,
        "cidr": s["cidr"],
        "gateway_ip": s["gateway_ip"],
        "enable_dhcp": s["enable_dhcp"],
        "allocation_pools": [
            {
                "start": s["allocation_pool_start"],
                "end": s["allocation_pool_end"],
            }
        ],
        "dns_nameservers": s["dns_nameservers"],
    }
    if subnet:
        if not network or subnet["network_id"] != network["id"]:
            raise InvalidError(
                "Existing subnet does not belong to the selected network"
            )
        if subnet["cidr"] != s["cidr"]:
            raise InvalidError("Subnet CIDR differs from configuration")
        if cfg["create_subnet"]:
            for key in (
                "gateway_ip",
                "enable_dhcp",
                "allocation_pools",
                "dns_nameservers",
            ):
                if subnet.get(key) != subnet_values[key]:
                    raise InvalidError(
                        f"Existing managed subnet differs in {key}; "
                        "reconcile explicitly"
                    )

    gateway = {
        "network_id": base["external_network_id"],
        "enable_snat": opts["router"]["enable_snat"],
    }
    if opts["router"]["external_subnet"]:
        ext_subnet = unique(
            cloud.list(
                "network",
                "/subnets",
                "subnets",
                network_id=base["external_network_id"],
            ),
            opts["router"]["external_subnet"],
            "external subnet",
        )
        fixed = {"subnet_id": ext_subnet["id"]}
        if opts["router"]["external_fixed_ip"]:
            fixed["ip_address"] = opts["router"]["external_fixed_ip"]
        gateway["external_fixed_ips"] = [fixed]

    if router:
        actual = router.get("external_gateway_info") or {}
        if actual.get("network_id") != gateway["network_id"]:
            raise InvalidError(
                "Router is not connected to configured external network"
            )
        if (
            cfg["create_router"]
            and actual.get("enable_snat") != gateway["enable_snat"]
        ):
            raise InvalidError(
                "Existing managed router has different SNAT configuration"
            )
        for fixed in gateway.get("external_fixed_ips", []):
            if not any(
                all(ip.get(k) == v for k, v in fixed.items())
                for ip in actual.get("external_fixed_ips", [])
            ):
                raise InvalidError(
                    "Router external IP/subnet differs from configuration"
                )

    def connected(router, subnet):
        ports = cloud.list(
            "network",
            "/ports",
            "ports",
            device_id=router["id"],
        )
        return any(
            any(ip["subnet_id"] == subnet["id"] for ip in port["fixed_ips"])
            for port in ports
        )

    # An existing router is read-only: a missing interface is a configuration
    # error, not implicit permission to attach a newly created subnet.
    if not cfg["create_router"]:
        if not subnet or not connected(router, subnet):
            raise InvalidError(
                "create_router=false requires an existing interface to the "
                "selected subnet; no network resources were created"
            )

    def ensure(kind, values):
        if existing[kind]:
            return existing[kind]
        return cloud.post(
            "network",
            "/" + kind + "s",
            {kind: dict(values, description=managed)},
        )[kind]

    network = ensure(
        "network",
        {"name": opts["tenant_network"]["name"], "admin_state_up": True},
    )
    subnet = ensure("subnet", dict(subnet_values, network_id=network["id"]))
    router = ensure(
        "router",
        {
            "name": opts["router"]["name"],
            "admin_state_up": True,
            "external_gateway_info": gateway,
        },
    )
    if cfg["create_router"] and not connected(router, subnet):
        cloud.request(
            "network",
            "PUT",
            "/routers/" + router["id"] + "/add_router_interface",
            {"subnet_id": subnet["id"]},
        )
    if not connected(router, subnet):
        raise InvalidError(
            "Router has no interface in configured tenant subnet"
        )
    return dict(
        base,
        project_id=project_id,
        network_id=network["id"],
        subnet_id=subnet["id"],
        router_id=router["id"],
    )


def host_service(admin, host):
    matches = [
        s
        for s in admin.list(
            "compute",
            "/os-services",
            "services",
            host=host,
            binary="nova-compute",
        )
        if s["host"] == host and s["binary"] == "nova-compute"
    ]
    if len(matches) != 1:
        raise InvalidError(
            f"Expected exactly one nova-compute service on {host}"
        )
    return matches[0]


def exclusive_host(admin, host, owned_ids=()):
    servers = admin.list(
        "compute", "/servers/detail", "servers", all_tenants=1, host=host
    )
    foreign = [s["id"] for s in servers if s["id"] not in owned_ids]
    if foreign:
        raise InvalidError(
            f"Host {host} contains {len(foreign)} foreign VM(s); "
            "refusing host operation"
        )
    return servers


def preflight(cfg, scenario, admin, cloud):
    """Read-only checks; no resource creation and no privilege fallback."""
    validate(cfg, scenario)
    info = resolve_base(cfg, cloud)
    s = cfg["scenarios"][scenario]
    compute_version = (
        "2.64"  # Single-policy groups and UUID compute service IDs.
    )
    if cfg["compute"]["boot_from_volume"] and cfg["storage"]["volume_type"]:
        compute_version = (
            "2.67"  # Volume type in image-to-volume block mapping.
        )
    if scenario in ("nova-drain", "masakari-host-failure"):
        compute_version = "2.74"
    if scenario == "nova-shelve-unshelve":
        compute_version = "2.77"
    info["compute_max_version"] = admin.require_version(
        "compute", compute_version
    )
    cloud.require_version("compute", compute_version)
    info["compute_version"] = compute_version
    if cfg["compute"]["boot_from_volume"] or scenario.startswith("cinder-"):
        version = (
            "3.42"
            if scenario == "cinder-volume-extend"
            else "3.40"
            if scenario == "cinder-snapshot-revert"
            else "3.0"
        )
        info["volume_max_version"] = cloud.require_version("volume", version)
        info["volume_version"] = version
    if scenario == "nova-live-migration-tpm":
        flavor = unique(
            cloud.list("compute", "/flavors/detail", "flavors"),
            cfg["compute"]["tpm_flavor"],
            "TPM flavor",
        )
        specs = admin.get(
            "compute", "/flavors/" + flavor["id"] + "/os-extra_specs"
        )["extra_specs"]
        if specs.get("hw:tpm_version") != "2.0":
            raise UnsupportedError(
                "TPM flavor must request hw:tpm_version=2.0"
            )
        info["tpm_flavor_id"] = flavor["id"]
        info["tpm_extra_specs"] = specs
    if scenario in ("nova-drain", "masakari-host-failure"):
        service = host_service(admin, s["host"])
        if service["state"] != "up" or service["status"] != "enabled":
            raise InvalidError(
                "Dedicated host must initially be up and enabled"
            )
        exclusive_host(admin, s["host"])
        if scenario == "masakari-host-failure":
            segment = unique(
                admin.list("ha", "/segments", "segments"),
                s["segment"],
                "Masakari segment",
            )
            host = unique(
                admin.list(
                    "ha", "/segments/" + segment["uuid"] + "/hosts", "hosts"
                ),
                s["host"],
                "Masakari host",
            )
            if host["on_maintenance"]:
                raise InvalidError("Masakari host is already in maintenance")
            info["masakari_segment_id"], info["masakari_host_id"] = (
                segment["uuid"],
                host["uuid"],
            )
    if scenario == "octavia-vip":
        providers = cloud.list("lb", "/lbaas/providers", "providers")
        unique(providers, s["provider"], "Octavia provider")
    return info
