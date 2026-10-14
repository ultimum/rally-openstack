# ruff: noqa: E402
"""Boot every Rally server from a volume.

Clouds whose flavors have no local disk (``disk: 0``) refuse image-backed
servers ("Only volume-backed servers are allowed for flavors with zero
disk"). Every scenario and context boots its servers through
``NovaScenario._boot_server`` or ``NovaScenario._boot_servers``; this module
wraps both while ``ULTIMUM_RALLY_BOOT_FROM_VOLUME`` is ``true`` in the
environment of the Rally process:

* ``_boot_server`` and single-instance ``_boot_servers`` requests first
  create a bootable volume from the image with the configured size, volume
  type and availability zone, then boot from it with
  ``delete_on_termination``, so the volume goes away with the server.
* Multi-instance requests (``min_count`` > 1) cannot share one volume; Nova
  creates their root volumes from the image (``source_type: image``), with
  Cinder's default type and the availability zone Nova chooses.
* Requests that already carry a block device mapping (the
  ``boot_server_from_volume*`` scenarios) and images that are snapshots of
  volume-backed servers (they carry their own mapping) are left unchanged.
* Snapshotting a volume-backed server also snapshots its root volume in
  Cinder, which then keeps that volume from being deleted with the server.
  ``NovaScenario._delete_image`` therefore also deletes the volume snapshots
  the image refers to and their source volumes once nothing uses them.

Settings: ``ULTIMUM_RALLY_BOOT_VOLUME_SIZE`` (GiB, raised to what the image
needs), ``ULTIMUM_RALLY_BOOT_VOLUME_TYPE`` and ``ULTIMUM_RALLY_BOOT_VOLUME_AZ``
(empty = Cinder default).
"""

import json
import math
import os

from rally import plugins


# Rally loads --plugin-paths before the installed plugins, but the upstream
# modules imported below read the [openstack] options at import time.
plugins.load()

from rally.common import logging

from rally_openstack.common.services.storage import block
from rally_openstack.task.scenarios.nova import utils as nova_utils


LOG = logging.getLogger(__name__)

GIB = 1024 ** 3
MAPPINGS = ("block_device_mapping", "block_device_mapping_v2")

upstream_boot_server = nova_utils.NovaScenario._boot_server
upstream_boot_servers = nova_utils.NovaScenario._boot_servers
upstream_delete_image = nova_utils.NovaScenario._delete_image


def settings():
    """Boot volume settings, or None when boot from volume is disabled."""
    if os.environ.get("ULTIMUM_RALLY_BOOT_FROM_VOLUME") != "true":
        return None
    return {
        "size": int(os.environ.get("ULTIMUM_RALLY_BOOT_VOLUME_SIZE") or 1),
        "volume_type": os.environ.get("ULTIMUM_RALLY_BOOT_VOLUME_TYPE")
        or None,
        "availability_zone": os.environ.get("ULTIMUM_RALLY_BOOT_VOLUME_AZ")
        or None,
    }


def image_id_of(image):
    return getattr(image, "id", image)


def boot_image(scenario, image, kwargs):
    """Return the image to put on a volume, or None to boot unchanged."""
    if not image or any(kwargs.get(key) for key in MAPPINGS):
        return None
    image = scenario.clients("glance").images.get(image_id_of(image))
    # A snapshot of a volume-backed server: Nova boots it from its own
    # volume snapshot mapping.
    if image.get("block_device_mapping") or image.get("bdm_v2"):
        return None
    return image


def volume_size(image, size):
    needed = max(image.get("min_disk") or 0,
                 math.ceil((image.get("size") or 0) / GIB))
    return max(size, needed)


def create_boot_volume(scenario, image, config):
    cinder = block.BlockStorage(
        scenario._clients, name_generator=scenario.generate_random_name,
        atomic_inst=scenario.atomic_actions())
    return cinder, cinder.create_volume(
        volume_size(image, config["size"]), imageRef=image["id"],
        volume_type=config["volume_type"],
        availability_zone=config["availability_zone"])


def volume_mapping(volume_id):
    return [{"uuid": volume_id, "source_type": "volume",
             "destination_type": "volume", "boot_index": 0,
             "delete_on_termination": True}]


def delete_unused(cinder, volume):
    """Remove a boot volume no server took over; never fail the caller."""
    try:
        cinder.delete_volume(volume)
    except Exception as exc:
        # Attached to a server in ERROR: the nova cleanup deletes both.
        LOG.warning("Boot volume %s was not deleted: %s" % (volume.id, exc))


def boot_from_new_volume(scenario, image, config, boot):
    cinder, volume = create_boot_volume(scenario, image, config)
    try:
        return boot(volume_mapping(volume.id))
    except Exception:
        delete_unused(cinder, volume)
        raise


def _boot_server(self, image, flavor, auto_assign_nic=False, **kwargs):
    config = settings()
    source = boot_image(self, image, kwargs) if config else None
    if source is None:
        return upstream_boot_server(self, image, flavor, auto_assign_nic,
                                    **kwargs)
    return boot_from_new_volume(
        self, source, config,
        lambda mapping: upstream_boot_server(
            self, None, flavor, auto_assign_nic,
            block_device_mapping_v2=mapping, **kwargs))


def _boot_servers(self, image_id, flavor_id, requests, instances_amount=1,
                  auto_assign_nic=False, **kwargs):
    config = settings()
    source = boot_image(self, image_id, kwargs) if config else None
    if source is None:
        return upstream_boot_servers(self, image_id, flavor_id, requests,
                                     instances_amount, auto_assign_nic,
                                     **kwargs)
    if instances_amount > 1:
        mapping = [{"uuid": source["id"], "source_type": "image",
                    "destination_type": "volume", "boot_index": 0,
                    "volume_size": volume_size(source, config["size"]),
                    "delete_on_termination": True}]
        return upstream_boot_servers(self, None, flavor_id, requests,
                                     instances_amount, auto_assign_nic,
                                     block_device_mapping_v2=mapping,
                                     **kwargs)
    servers = []
    for i in range(requests):
        servers.extend(boot_from_new_volume(
            self, source, config,
            lambda mapping: upstream_boot_servers(
                self, None, flavor_id, 1, 1, auto_assign_nic,
                block_device_mapping_v2=mapping, **kwargs)))
    return servers


def image_snapshot_ids(image):
    """IDs of the volume snapshots a volume-backed server snapshot uses."""
    mappings = image.get("block_device_mapping") or "[]"
    if isinstance(mappings, str):
        mappings = json.loads(mappings)
    return [m["snapshot_id"] for m in mappings if m.get("snapshot_id")]


def delete_image_snapshots(scenario, snapshot_ids):
    cinder = block.BlockStorage(
        scenario._clients, name_generator=scenario.generate_random_name,
        atomic_inst=scenario.atomic_actions())
    for snapshot_id in snapshot_ids:
        try:
            snapshot = scenario.clients("cinder").volume_snapshots.get(
                snapshot_id)
            cinder.delete_snapshot(snapshot)
            volume = cinder.get_volume(snapshot.volume_id)
            # The boot volume of the deleted server, kept by the snapshot.
            if volume.status == "available":
                cinder.delete_volume(volume)
        except Exception as exc:
            LOG.warning("Volume snapshot %s of a server image was not "
                        "cleaned up: %s" % (snapshot_id, exc))


def _delete_image(self, image):
    snapshot_ids = []
    if settings():
        snapshot_ids = image_snapshot_ids(
            self.clients("glance").images.get(image.id))
    upstream_delete_image(self, image)
    delete_image_snapshots(self, snapshot_ids)


nova_utils.NovaScenario._boot_server = _boot_server
nova_utils.NovaScenario._boot_servers = _boot_servers
nova_utils.NovaScenario._delete_image = _delete_image
