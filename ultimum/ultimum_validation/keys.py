"""Persistent SSH material and a matching, user-scoped Nova keypair."""

import contextlib
import hashlib
import io
import pathlib

import paramiko

from .cloud import APIError
from .config import InvalidError
from .progress import progress
from .state import project_lock
from .state import write_new


def read_private(path):
    if path.stat().st_mode & 0o077:
        raise InvalidError(f"SSH private key must have mode 0600: {path}")
    for key_type in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return key_type.from_private_key_file(str(path))
        except paramiko.PasswordRequiredException:
            raise InvalidError(
                "Encrypted SSH private keys are not supported"
            ) from None
        except paramiko.SSHException:
            continue
    raise InvalidError("Invalid or unsupported SSH private key")


def public_parts(text):
    parts = text.split()
    if len(parts) < 2:
        raise InvalidError("Invalid SSH public key")
    return parts[:2]


def local_key(cfg, create):
    private = pathlib.Path(cfg["guest"]["ssh_private_key"])
    public = pathlib.Path(cfg["guest"]["ssh_public_key"])
    if not private.is_file():
        if not create:
            raise InvalidError(
                "SSH private key missing; provide it or run prepare "
                "with create_ssh_key=true"
            )
        if public.exists():
            raise InvalidError(
                "SSH public key exists without its private key; supply the "
                "matching private key (existing material is not replaced)"
            )
        progress(f"CREATE SSH private key (RSA 3072): {private}")
        key = paramiko.RSAKey.generate(bits=3072)
        stream = io.StringIO()
        key.write_private_key(stream)
        write_new(private, stream.getvalue())
    else:
        progress(f"REUSE SSH private key: {private}")
        key = read_private(private)
    expected = [key.get_name(), key.get_base64()]
    if public.is_file():
        if public_parts(public.read_text()) != expected:
            raise InvalidError("SSH public/private keys do not match")
        progress(f"REUSE matching SSH public key: {public}")
    elif create:
        progress(f"CREATE SSH public key from private key: {public}")
        write_new(public, " ".join(expected) + " ultimum-rally\n")
    else:
        raise InvalidError(
            "SSH public key missing; provide it or run prepare "
            "with create_ssh_key=true"
        )
    return " ".join(expected)


def ensure_ssh_key(cfg, cloud, read_only=False):
    name = cfg["ssh_key"]["name"]
    create = cfg["create_ssh_key"] and not read_only
    # Different project locks may share this local private key. Serialize its
    # creation too, including recovery of a missing .pub after interruption.
    private = pathlib.Path(cfg["guest"]["ssh_private_key"])
    lock_name = (
        "ssh-key-"
        + hashlib.sha256(str(private.resolve()).encode()).hexdigest()
    )
    with (
        project_lock(cfg["execution"]["state_dir"], lock_name)
        if create
        else contextlib.nullcontext()
    ):
        public = local_key(cfg, create)
    progress(f"CHECK Nova SSH keypair {name} as configured test user")
    try:
        existing = cloud.get("compute", "/os-keypairs/" + name)["keypair"]
        progress(f"REUSE Nova SSH keypair {name}")
    except APIError as exc:
        if exc.status != 404:
            raise
        if not create:
            raise InvalidError(
                f"Nova keypair {name} missing; provide it or run prepare "
                "with create_ssh_key=true"
            ) from None
        progress(f"CREATE Nova SSH keypair {name}: import public key")
        try:
            existing = cloud.post(
                "compute",
                "/os-keypairs",
                {"keypair": {"name": name, "public_key": public}},
            )["keypair"]
        except APIError as conflict:
            if conflict.status != 409:
                raise
            # The same Keystone user may prepare another project concurrently.
            existing = cloud.get("compute", "/os-keypairs/" + name)["keypair"]
    if public_parts(existing.get("public_key", "")) != public_parts(public):
        raise InvalidError(
            f"Nova keypair {name} does not match the local SSH key; "
            "supply the matching private key or choose a different name"
        )
    progress(f"OK Nova SSH keypair {name}: matches local key, retained")
    return name
