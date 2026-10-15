# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

import functools
import importlib.util
import pathlib
from unittest import mock

from rally import exceptions

from tests.unit import test


PLUGIN = (pathlib.Path(__file__).resolve().parents[2]
          / "ultimum" / "plugins" / "availability_zone.py")


@functools.cache
def load_plugin():
    """Import the plugin with "<name>_under_test" plugin names."""
    spec = importlib.util.spec_from_file_location(
        "ultimum_availability_zone_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    # Imported here: tests.unit.test registers the [openstack] options first.
    from rally.common.plugin import plugin

    configure_plugin = plugin.configure

    def rename_plugin(name, **kwargs):
        if not name.endswith("_under_test"):
            name += "_under_test"
        return configure_plugin(name=name, **kwargs)

    with mock.patch.object(plugin, "configure", rename_plugin):
        spec.loader.exec_module(module)
    return module


def response(body=None, status=200):
    return mock.Mock(status_code=status, json=mock.Mock(return_value=body))


class PlacementTestCase(test.TestCase):
    def setUp(self):
        super().setUp()
        self.module = load_plugin()
        api = mock.patch.object(self.module.adapter, "Adapter").start()
        self.addCleanup(mock.patch.stopall)
        self.api = api.return_value
        self.placement = self.module.Placement(mock.MagicMock())

    def traits(self, *traits, generation=7):
        return response({"traits": list(traits),
                         "resource_provider_generation": generation})

    def test_provider_by_name(self):
        self.api.get.return_value = response(
            {"resource_providers": [{"uuid": "rp-1"}]})
        self.assertEqual("rp-1", self.placement.provider("node-1"))
        self.api.get.assert_called_once_with(
            "/resource_providers", params={"name": "node-1"})

    def test_missing_provider(self):
        self.api.get.return_value = response({"resource_providers": []})
        self.assertRaises(exceptions.RallyException,
                          self.placement.provider, "node-1")

    def test_add_trait_keeps_the_other_traits(self):
        self.api.get.return_value = self.traits("COMPUTE_X")
        self.api.put.return_value = response(status=200)
        self.placement.add_trait("rp-1", "CUSTOM_T")
        self.assertEqual([
            mock.call("/traits/CUSTOM_T"),
            mock.call("/resource_providers/rp-1/traits",
                      json={"traits": ["COMPUTE_X", "CUSTOM_T"],
                            "resource_provider_generation": 7},
                      raise_exc=False)], self.api.put.call_args_list)

    def test_generation_conflict_is_retried(self):
        self.api.get.side_effect = [self.traits("COMPUTE_X", generation=7),
                                    self.traits("COMPUTE_Y", generation=8)]
        self.api.put.side_effect = [response(status=409),
                                    response(status=200)]
        self.placement._update_traits("rp-1", lambda t: t | {"CUSTOM_T"})
        self.api.put.assert_called_with(
            "/resource_providers/rp-1/traits",
            json={"traits": ["COMPUTE_Y", "CUSTOM_T"],
                  "resource_provider_generation": 8},
            raise_exc=False)

    def test_remove_trait(self):
        self.api.get.return_value = self.traits("COMPUTE_X", "CUSTOM_T")
        self.api.put.return_value = response(status=200)
        self.placement.remove_trait("rp-1", "CUSTOM_T")
        self.api.put.assert_called_once_with(
            "/resource_providers/rp-1/traits",
            json={"traits": ["COMPUTE_X"],
                  "resource_provider_generation": 7},
            raise_exc=False)
        self.api.delete.assert_called_once_with("/traits/CUSTOM_T",
                                                raise_exc=False)


class AggregateBootTestCase(test.ScenarioTestCase):
    def setUp(self):
        super().setUp()
        self.module = load_plugin()
        self.placement = mock.patch.object(
            self.module, "Placement").start().return_value
        self.addCleanup(mock.patch.stopall)
        self.placement.provider.return_value = "rp-1"
        self.scenario = self.module.CreateAggregateAddHostAndBootServer(
            self.context)
        self.hypervisor = mock.Mock(hypervisor_hostname="node-1.example",
                                    service={"host": "node-1"})
        for name, value in (
                ("_admin_clients", mock.Mock()),
                ("_create_aggregate", mock.Mock()),
                ("_zone_hypervisor", mock.Mock(return_value=self.hypervisor)),
                ("_aggregate_set_metadata", mock.Mock()),
                ("_aggregate_add_host", mock.Mock()),
                ("_create_flavor", mock.Mock()),
                ("_boot_server", mock.Mock()),
                ("generate_random_name", mock.Mock(
                    return_value="s_rally_ab12_Xy"))):
            setattr(self.scenario, name, value)
        self.flavor = self.scenario._create_flavor.return_value
        self.admin_nova = self.admin_clients("nova")

    def boot(self, host):
        self.admin_nova.servers.get.return_value = mock.Mock(
            **{"OS-EXT-SRV-ATTR:host": host})
        self.scenario.run("image-id", {"ultimum_rally_az": "true"},
                          availability_zone="cz03")

    def test_server_is_steered_by_a_required_trait(self):
        self.boot("node-1")
        trait = "CUSTOM_ULTIMUM_RALLY_S_RALLY_AB12_XY"
        self.placement.provider.assert_called_once_with("node-1.example")
        self.placement.add_trait.assert_called_once_with("rp-1", trait)
        self.flavor.set_keys.assert_called_once_with({
            "aggregate_instance_extra_specs:ultimum_rally_az": "true",
            "trait:" + trait: "required"})
        self.scenario._aggregate_add_host.assert_called_once_with(
            self.scenario._create_aggregate.return_value, "node-1")
        self.scenario._boot_server.assert_called_once_with(
            "image-id", self.flavor.id, availability_zone="cz03")
        self.placement.remove_trait.assert_called_once_with("rp-1", trait)

    def test_other_host_fails_and_still_removes_the_trait(self):
        self.assertRaises(exceptions.RallyException, self.boot, "node-2")
        self.placement.remove_trait.assert_called_once()
