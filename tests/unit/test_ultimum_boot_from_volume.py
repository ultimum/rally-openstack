# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

import functools
import importlib.util
import json
import pathlib
from unittest import mock

from tests.unit import test


PLUGIN = (pathlib.Path(__file__).resolve().parents[2]
          / "ultimum" / "plugins" / "boot_from_volume.py")
ENABLED = {
    "ULTIMUM_RALLY_BOOT_FROM_VOLUME": "true",
    "ULTIMUM_RALLY_BOOT_VOLUME_SIZE": "2",
    "ULTIMUM_RALLY_BOOT_VOLUME_TYPE": "cz03_slow",
    "ULTIMUM_RALLY_BOOT_VOLUME_AZ": "cz03",
}
IMAGE = {"id": "image-id", "size": 50 * 1024 ** 2, "min_disk": 0}


@functools.cache
def load_plugin():
    """Import the plugin, keeping NovaScenario unpatched in this process."""
    # Imported here: tests.unit.test registers the [openstack] options first.
    from rally_openstack.task.scenarios.nova import utils as nova_utils

    original = (nova_utils.NovaScenario._boot_server,
                nova_utils.NovaScenario._boot_servers,
                nova_utils.NovaScenario._delete_image,
                nova_utils.NovaScenario._rescue_server)
    spec = importlib.util.spec_from_file_location(
        "ultimum_boot_from_volume_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (nova_utils.NovaScenario._boot_server,
     nova_utils.NovaScenario._boot_servers,
     nova_utils.NovaScenario._delete_image,
     nova_utils.NovaScenario._rescue_server) = original
    return module


class BootFromVolumeTestCase(test.TestCase):
    def setUp(self):
        super().setUp()
        self.module = load_plugin()
        self.boot = mock.patch.object(
            self.module, "upstream_boot_server").start()
        self.boot_many = mock.patch.object(
            self.module, "upstream_boot_servers").start()
        self.storage = mock.patch.object(
            self.module.block, "BlockStorage").start()
        self.addCleanup(mock.patch.stopall)
        self.cinder = self.storage.return_value
        self.cinder.create_volume.side_effect = [
            mock.Mock(id="vol-1"), mock.Mock(id="vol-2")]
        self.scenario = mock.Mock()
        self.image = dict(IMAGE)
        self.scenario.clients.return_value.images.get.return_value = (
            self.image)

    def enabled(self):
        return mock.patch.dict(self.module.os.environ, ENABLED)

    @staticmethod
    def mapping(volume_id):
        return [{"uuid": volume_id, "source_type": "volume",
                 "destination_type": "volume", "boot_index": 0,
                 "delete_on_termination": True}]

    def test_disabled_boots_unchanged(self):
        with mock.patch.dict(self.module.os.environ, clear=True):
            self.module._boot_server(self.scenario, "image-id", "flavor",
                                     availability_zone="cz03")
        self.boot.assert_called_once_with(
            self.scenario, "image-id", "flavor", False,
            availability_zone="cz03")
        self.assertFalse(self.cinder.create_volume.called)

    def test_server_boots_from_a_new_zonal_volume(self):
        with self.enabled():
            server = self.module._boot_server(
                self.scenario, "image-id", "flavor", availability_zone="cz03")
        self.assertEqual(self.boot.return_value, server)
        self.cinder.create_volume.assert_called_once_with(
            2, imageRef="image-id", volume_type="cz03_slow",
            availability_zone="cz03")
        self.boot.assert_called_once_with(
            self.scenario, None, "flavor", False,
            block_device_mapping_v2=self.mapping("vol-1"),
            availability_zone="cz03")

    def test_volume_size_covers_the_image(self):
        self.image.update(min_disk=5)
        with self.enabled():
            self.module._boot_server(self.scenario, "image-id", "flavor")
        self.assertEqual(5, self.cinder.create_volume.call_args[0][0])

    def test_existing_mapping_is_kept(self):
        mapping = {"vda": "volume-id:::0"}
        with self.enabled():
            self.module._boot_server(self.scenario, None, "flavor",
                                     block_device_mapping=mapping)
        self.boot.assert_called_once_with(
            self.scenario, None, "flavor", False,
            block_device_mapping=mapping)
        self.assertFalse(self.cinder.create_volume.called)

    def test_volume_backed_snapshot_image_is_kept(self):
        self.image.update(bdm_v2="True",
                          block_device_mapping='[{"snapshot_id": "s"}]')
        with self.enabled():
            self.module._boot_server(self.scenario, "image-id", "flavor")
        self.boot.assert_called_once_with(
            self.scenario, "image-id", "flavor", False)
        self.assertFalse(self.cinder.create_volume.called)

    def test_failed_boot_removes_the_volume(self):
        volume = mock.Mock(id="vol-1")
        self.cinder.create_volume.side_effect = None
        self.cinder.create_volume.return_value = volume
        self.boot.side_effect = RuntimeError("boot failed")
        with self.enabled():
            self.assertRaises(RuntimeError, self.module._boot_server,
                              self.scenario, "image-id", "flavor")
        self.cinder.delete_volume.assert_called_once_with(volume)

    def test_volume_left_to_cleanup_does_not_hide_the_boot_error(self):
        self.boot.side_effect = RuntimeError("boot failed")
        self.cinder.delete_volume.side_effect = Exception("in-use")
        with self.enabled():
            self.assertRaisesRegex(RuntimeError, "boot failed",
                                   self.module._boot_server,
                                   self.scenario, "image-id", "flavor")

    def test_each_single_instance_request_gets_its_own_volume(self):
        self.boot_many.side_effect = [["server-1"], ["server-2"]]
        with self.enabled():
            servers = self.module._boot_servers(
                self.scenario, "image-id", "flavor", 2,
                availability_zone="cz03")
        self.assertEqual(["server-1", "server-2"], servers)
        self.assertEqual(
            [mock.call(self.scenario, None, "flavor", 1, 1, False,
                       block_device_mapping_v2=self.mapping(volume),
                       availability_zone="cz03")
             for volume in ("vol-1", "vol-2")],
            self.boot_many.call_args_list)

    def test_multi_instance_request_lets_nova_create_volumes(self):
        with self.enabled():
            self.module._boot_servers(self.scenario, "image-id", "flavor", 1,
                                      instances_amount=5)
        self.boot_many.assert_called_once_with(
            self.scenario, None, "flavor", 1, 5, False,
            block_device_mapping_v2=[{
                "uuid": "image-id", "source_type": "image",
                "destination_type": "volume", "boot_index": 0,
                "volume_size": 2, "delete_on_termination": True}])
        self.assertFalse(self.cinder.create_volume.called)

    def test_deleting_a_server_snapshot_removes_its_volume_snapshots(self):
        self.image["block_device_mapping"] = json.dumps(
            [{"snapshot_id": "snap-1", "boot_index": 0}])
        snapshot = mock.Mock(volume_id="vol-1")
        cinder_client = self.scenario.clients.return_value
        cinder_client.volume_snapshots.get.return_value = snapshot
        self.cinder.get_volume.return_value = mock.Mock(status="available")
        image = mock.Mock(id="image-id")
        with self.enabled(), mock.patch.object(
                self.module, "upstream_delete_image") as delete_image:
            self.module._delete_image(self.scenario, image)
        delete_image.assert_called_once_with(self.scenario, image)
        cinder_client.volume_snapshots.get.assert_called_once_with("snap-1")
        self.cinder.delete_snapshot.assert_called_once_with(snapshot)
        self.cinder.get_volume.assert_called_once_with("vol-1")
        self.cinder.delete_volume.assert_called_once_with(
            self.cinder.get_volume.return_value)

    def test_volume_in_use_is_kept(self):
        self.image["block_device_mapping"] = json.dumps(
            [{"snapshot_id": "snap-1"}])
        self.cinder.get_volume.return_value = mock.Mock(status="in-use")
        with self.enabled(), mock.patch.object(
                self.module, "upstream_delete_image"):
            self.module._delete_image(self.scenario, mock.Mock(id="i"))
        self.assertFalse(self.cinder.delete_volume.called)

    def test_plain_image_deletion_is_unchanged(self):
        with self.enabled(), mock.patch.object(
                self.module, "upstream_delete_image") as delete_image:
            self.module._delete_image(self.scenario, mock.Mock(id="i"))
        self.assertTrue(delete_image.called)
        self.assertFalse(self.cinder.delete_snapshot.called)


class RescueTestCase(test.TestCase):
    def setUp(self):
        super().setUp()
        from novaclient.v2 import servers

        self.module = load_plugin()
        # Rally's _rescue_server calls server.rescue() without an image.
        self.upstream = mock.patch.object(
            self.module, "upstream_rescue_server",
            side_effect=lambda scenario, server: server.rescue()).start()
        self.addCleanup(mock.patch.stopall)
        self.manager = mock.Mock()
        self.scenario = mock.Mock()
        self.volumes = self.scenario.clients.return_value.volumes
        self.volumes.get.side_effect = lambda volume_id: {
            "data": mock.Mock(bootable="false", volume_image_metadata=None),
            "root": mock.Mock(bootable="true", volume_image_metadata={
                "image_id": "image-id"}),
        }[volume_id]
        self.server = servers.Server(self.manager, {
            "id": "server-id", "image": "",
            "os-extended-volumes:volumes_attached": [
                {"id": "data"}, {"id": "root"}]}, loaded=True)

    def rescue(self, environ):
        with mock.patch.dict(self.module.os.environ, environ, clear=True):
            self.module._rescue_server(self.scenario, self.server)

    def test_volume_backed_server_is_rescued_with_its_boot_image(self):
        self.rescue(ENABLED)
        self.manager.rescue.assert_called_once_with(
            self.server, None, "image-id")
        # The server object is left as it was.
        self.assertNotIn("rescue", vars(self.server))

    def test_image_backed_server_is_rescued_unchanged(self):
        self.server.image = {"id": "own-image"}
        self.rescue(ENABLED)
        self.manager.rescue.assert_called_once_with(self.server, None, None)
        self.volumes.get.assert_not_called()

    def test_disabled_rescue_is_unchanged(self):
        self.rescue({})
        self.manager.rescue.assert_called_once_with(self.server, None, None)
        self.volumes.get.assert_not_called()
