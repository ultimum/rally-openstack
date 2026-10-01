"""Allocate exact external IPs inside an operator-owned inclusive range."""

import ipaddress

from .cloud import APIError
from .cloud import unique
from .config import InvalidError


def resolve_pool(cfg, cloud, network_id):
    options = cfg["network"]["external_ip_pool"]
    if options["start"] is None:
        return None
    start, end = (ipaddress.ip_address(options[k]) for k in ("start", "end"))
    subnets = cloud.list(
        "network", "/subnets", "subnets", network_id=network_id
    )
    if options["subnet"]:
        subnet = unique(subnets, options["subnet"], "external pool subnet")
    else:
        matches = [
            s
            for s in subnets
            if start in ipaddress.ip_network(s["cidr"])
            and end in ipaddress.ip_network(s["cidr"])
        ]
        if len(matches) != 1:
            raise InvalidError(
                "external_ip_pool must match one external subnet; "
                "set external_ip_pool.subnet explicitly"
            )
        subnet = matches[0]
    cidr = ipaddress.ip_network(subnet["cidr"])
    if (
        subnet["network_id"] != network_id
        or start not in cidr
        or end not in cidr
    ):
        raise InvalidError(
            "external_ip_pool is outside the selected external subnet"
        )
    reserved = {str(cidr.network_address), str(cidr.broadcast_address)}
    if subnet.get("gateway_ip"):
        reserved.add(subnet["gateway_ip"])
    return {
        "network_id": network_id,
        "subnet_id": subnet["id"],
        "start": str(start),
        "end": str(end),
        "reserved": sorted(reserved),
    }


def contains(pool, address):
    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (
        candidate.version == 4
        and int(ipaddress.ip_address(pool["start"]))
        <= int(candidate)
        <= int(ipaddress.ip_address(pool["end"]))
        and str(candidate) not in pool["reserved"]
    )


def allocate(pool, inventory, create, emit, requested=None):
    """Atomically claim the selected address; retry only IP conflicts."""
    if requested is not None and not contains(pool, requested):
        raise InvalidError(
            "Requested external address is outside external_ip_pool"
        )
    # Admin inventory includes ports from other projects, floating IP ports
    # and router gateways. It is only a hint; the POST arbitrates races.
    used = {
        fixed["ip_address"]
        for port in inventory.list(
            "network", "/ports", "ports", network_id=pool["network_id"]
        )
        for fixed in port.get("fixed_ips", [])
        if fixed["subnet_id"] == pool["subnet_id"]
    }
    emit(f"SELECT external IP from {pool['start']} .. {pool['end']}")
    candidates = (
        (int(ipaddress.ip_address(requested)),)
        if requested is not None
        else range(
            int(ipaddress.ip_address(pool["start"])),
            int(ipaddress.ip_address(pool["end"])) + 1,
        )
    )
    for value in candidates:
        address = str(ipaddress.ip_address(value))
        if address in used or address in pool["reserved"]:
            continue
        emit(f"ALLOCATE external IP {address} on subnet {pool['subnet_id']}")
        try:
            return create(address, pool["subnet_id"])
        except APIError as exc:
            if exc.status != 409 or exc.error_type != "IpAddressInUse":
                # Quota, policy, unknown conflict and transport errors must
                # not be retried with another address or automatic IPAM.
                raise
            emit(f"RETRY external IP {address}: allocated concurrently")
    raise InvalidError(
        f"No free external IP in {pool['start']} .. {pool['end']}"
        + (f" (requested {requested})" if requested is not None else "")
        + "; allocation outside this pool is disabled"
    )
