"""Resolve shared TLS settings for runner and Rally connections."""

import pathlib

from .config import InvalidError


def apply_tls(cfg, rc):
    """Overlay explicit YAML settings without modifying the source OpenRC."""
    rc = dict(rc)
    options = cfg["tls"]
    insecure = options["insecure"]
    if insecure is None:
        insecure = rc.get("OS_INSECURE", "").lower() in ("1", "true", "yes")
    ca_cert = options["ca_cert"]
    if ca_cert is None:
        ca_cert = rc.get("OS_CACERT", "")
    # Rally's Keystone client gives a nonempty CA path precedence over
    # insecure. Clear it so all clients agree when verification is disabled.
    if insecure:
        ca_cert = ""
    elif ca_cert:
        path = pathlib.Path(ca_cert)
        if not path.is_absolute():
            raise InvalidError("TLS CA certificate must be an absolute path")
        try:
            with path.open("rb") as stream:
                stream.read(1)
        except OSError as exc:
            raise InvalidError(
                f"TLS CA certificate is not a readable file: {path}"
            ) from exc
    rc["OS_INSECURE"] = "true" if insecure else "false"
    rc["OS_CACERT"] = ca_cert
    return rc
