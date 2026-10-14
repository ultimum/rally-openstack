# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

import functools
import importlib.util
import pathlib
from unittest import mock

import yaml

from tests.unit import test


ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "ultimum" / "plugins" / "heat_signal.py"
HEAT_TEMPLATES = ROOT / "tasks" / "ultimum" / "az" / "heat"
SIGNAL_URL = ("https://heat.example:8004/v1/project-id/stacks/"
              "s_rally_abc/0b1c-id/resources/scaling_policy/signal")
EC2_URL = ("https://heat.example:8000/v1/signal/arn%3Aopenstack%3Aheat%3A%3A"
           "p%3Astacks/s_rally_abc/0b1c-id/resources/scaling_policy"
           "?SignatureMethod=HmacSHA256&SignatureVersion=2"
           "&AWSAccessKeyId=key&Signature=sig%3D")


@functools.cache
def load_plugin():
    """Import the plugin, keeping HeatScenario unpatched in this process."""
    # Imported here: tests.unit.test registers the [openstack] options first.
    from rally_openstack.task.scenarios.heat import utils as heat_utils

    original = heat_utils.HeatScenario._stack_webhook
    spec = importlib.util.spec_from_file_location(
        "ultimum_heat_signal_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    heat_utils.HeatScenario._stack_webhook = original
    return module


class HeatSignalTestCase(test.TestCase):
    def setUp(self):
        super().setUp()
        self.module = load_plugin()

    def scenario(self):
        scenario = mock.Mock()
        scenario.clients.return_value.resources.signal.return_value = None
        return scenario

    def stack(self, url):
        return mock.Mock(id="0b1c-id", outputs=[
            {"output_key": "other", "output_value": "x"},
            {"output_key": "scaling_url", "output_value": url}])

    def test_heat_signal_target(self):
        self.assertEqual(("s_rally_abc/0b1c-id", "scaling_policy"),
                         self.module.heat_signal_target(SIGNAL_URL))
        self.assertIsNone(self.module.heat_signal_target(EC2_URL))
        self.assertIsNone(self.module.heat_signal_target(
            "https://example/v1/p/stacks/s/id/resources/r"))

    def test_heat_signal_url_is_sent_through_heatclient(self):
        scenario = self.scenario()
        with mock.patch.object(self.module, "upstream_stack_webhook") as up, \
                mock.patch.object(self.module.atomic, "ActionTimer"):
            self.module.stack_webhook(
                scenario, self.stack(SIGNAL_URL), "scaling_url")
        up.assert_not_called()
        scenario.clients.assert_called_once_with("heat")
        scenario.clients.return_value.resources.signal.assert_called_once_with(
            "s_rally_abc/0b1c-id", "scaling_policy")

    def test_ec2_signed_url_is_left_to_upstream(self):
        scenario = self.scenario()
        stack = self.stack(EC2_URL)
        with mock.patch.object(self.module, "upstream_stack_webhook") as up:
            self.module.stack_webhook(scenario, stack, "scaling_url")
        up.assert_called_once_with(scenario, stack, "scaling_url")
        scenario.clients.assert_not_called()

    def test_missing_output_key(self):
        self.assertRaises(
            self.module.exceptions.InvalidConfigException,
            self.module.stack_webhook,
            self.scenario(), self.stack(SIGNAL_URL), "missing")

    def test_heat_templates_use_no_ec2_signed_urls(self):
        for path in HEAT_TEMPLATES.glob("*.yaml"):
            self.assertNotIn("alarm_url", path.read_text(), path.name)
        outputs = yaml.safe_load(
            (HEAT_TEMPLATES / "autoscaling_group.yaml").read_text())["outputs"]
        self.assertEqual({"get_attr": ["scaling_policy", "signal_url"]},
                         outputs["scaling_url"]["value"])
