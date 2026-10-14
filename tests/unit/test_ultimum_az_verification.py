# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

import json
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

import jinja2
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
TASK = ROOT / "tasks" / "ultimum" / "az-production.yaml"
WRAPPER = ROOT / "ultimum" / "ultimum-rally-az-verification"
PLUGIN_DIR = ROOT / "ultimum" / "plugins"
PLUGINS = ",".join(str(PLUGIN_DIR / name) for name in (
    "availability_zone.py", "project_users.py"))
PROJECT = {"name": "rally-az", "domain": "Default"}
SERVICES = ("barbican", "cinder", "heat", "nova", "octavia")

# Workloads whose scenario only reads, lists or manages metadata and so
# creates no server, volume or load balancer that could be placed in a zone.
ZONELESS = (
    "Authenticate.", "Barbican", "CinderQos.", "CinderVolumeTypes.",
    "NovaAvailabilityZones.", "NovaFlavors.", "NovaHypervisors.",
    "NovaKeypair.create_", "NovaServerGroups.", "NovaServices.", "Quotas.",
    "CinderVolumes.list_types", "CinderVolumes.list_transfers",
    "NovaAggregates.list_aggregates",
)
# Heat templates with no servers or volumes.
ZONELESS_TEMPLATES = (
    "default.yaml", "random_strings.yaml", "autoscaling_policy.yaml",
    "resource_group.yaml", "resource_group_with_constraint.yaml",
    "resource_group_with_outputs.yaml",
)


def render_task(args, project=True):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TASK.parent),
        undefined=jinja2.StrictUndefined)
    if project:
        args = {"project_name": PROJECT["name"], **args}
    return yaml.safe_load(
        env.from_string(TASK.read_text()).render(**args))


def workloads(task):
    for subtask in task["subtasks"]:
        yield from subtask.get("workloads", [subtask])


def zones(value, keys=("availability_zone",)):
    """Yield every value stored under one of keys at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys:
                yield item
            else:
                yield from zones(item, keys)
    elif isinstance(value, list):
        for item in value:
            yield from zones(item, keys)


class UltimumAZTaskTestCase(unittest.TestCase):
    ARGS = {
        "availability_zone": "compute-az",
        "volume_availability_zone": "volume-az",
        "octavia_availability_zone": "lb-az",
        "volume_type": "zone-type",
        "cinder_backup": True,
        "floating_network": "external-net",
        "heat_template_dir": "/templates",
    }

    def test_every_created_resource_has_its_zone(self):
        task = render_task(self.ARGS)
        expected = {"compute-az", "volume-az", "lb-az"}
        checked = 0
        for workload in workloads(task):
            (name, args), = workload["scenario"].items()
            template = pathlib.Path(args.get("template_path", "")).name
            if (name.startswith(ZONELESS)
                    or template in ZONELESS_TEMPLATES):
                continue
            with self.subTest(scenario=name, args=args):
                found = set(zones(args))
                found |= set(zones(workload.get("contexts", {})))
                self.assertTrue(found)
                self.assertLessEqual(found, expected)
                checked += 1
        self.assertGreater(checked, 80)

    def test_servers_and_volumes_use_their_own_zone(self):
        task = render_task(self.ARGS)
        for workload in workloads(task):
            (name, args), = workload["scenario"].items()
            for key in ("boot_server_kwargs", "boot_server_args",
                        "create_vm_params"):
                if key in args:
                    self.assertEqual("compute-az",
                                     args[key]["availability_zone"], name)
            for key in ("create_volume_kwargs", "create_volume_params"):
                if key in args:
                    self.assertEqual("volume-az",
                                     args[key]["availability_zone"], name)
            if name.startswith("CinderVolumes.create") and "size" in args:
                if "availability_zone" in args:
                    self.assertEqual("volume-az", args["availability_zone"])
                    self.assertEqual("zone-type", args["volume_type"])
            if name.startswith("UltimumOctavia."):
                self.assertEqual("lb-az", args["availability_zone"])
            for context, config in workload.get("contexts", {}).items():
                self.assertNotIn(context, ("volumes", "servers"), name)
                if context == "volumes@ultimum":
                    self.assertEqual("volume-az", config["availability_zone"])
                    self.assertEqual("zone-type", config["type"])
                if context == "servers@ultimum":
                    self.assertEqual("compute-az",
                                     config["availability_zone"])

    def test_heat_servers_and_volumes_get_zone_parameters(self):
        task = render_task(dict(self.ARGS, services=["heat"]))
        parameters = [
            args["parameters"] for workload in workloads(task)
            for args in workload["scenario"].values()
            if "image" in args.get("parameters", {})]
        self.assertEqual(4, len(parameters))
        for params in parameters:
            self.assertEqual("compute-az", params["availability_zone"])
            if "volume_availability_zone" in params:
                self.assertEqual("volume-az",
                                 params["volume_availability_zone"])
                self.assertEqual("zone-type", params["volume_type"])
        for workload in workloads(task):
            for args in workload["scenario"].values():
                path = args.get("template_path")
                if path:
                    template = pathlib.Path(path).name
                    self.assertTrue(
                        (ROOT / "tasks" / "ultimum" / "az" / "heat"
                         / template).is_file(), template)

    def test_empty_zones_keep_service_defaults(self):
        task = render_task({"availability_zone": "",
                            "volume_availability_zone": "",
                            "octavia_availability_zone": "",
                            "volume_type": ""})
        values = set()
        for workload in workloads(task):
            values |= set(zones(workload["scenario"]))
            values |= set(zones(workload.get("contexts", {})))
            values |= set(zones(workload["scenario"], ("volume_type",)))
        self.assertLessEqual(values, {None, ""})

    def test_services_select_task_parts(self):
        titles = {}
        for service in SERVICES:
            task = render_task({"services": [service]})
            titles[service] = {s["title"] for s in task["subtasks"]}
            self.assertTrue(titles[service], service)
        task = render_task({})
        self.assertEqual(set().union(*titles.values()),
                         {s["title"] for s in task["subtasks"]})

    def test_requested_titles_are_present(self):
        titles = {s["title"] for s in render_task(self.ARGS)["subtasks"]}
        for title in (
            "BarbicanOrders.list",
            "CinderVolumes.create_snapshot_and_attach_volume tests",
            "CinderVolumes.create_and_restore_volume_backup tests",
            "HeatStacks.create_stack_and_scale tests",
            "NovaFlavors.create_and_delete_flavor tests",
            "NovaServers.boot_server_associate_and_dissociate_floating_ip "
            "tests",
            "NovaAggregates.create_aggregate_add_host_and_boot_server tests",
            "Octavia.create_and_update_loadbalanacers",
            "Octavia.create_and_show_pools",
        ):
            self.assertIn(title, titles)

    def test_every_workload_runs_in_the_configured_project(self):
        task = render_task(dict(self.ARGS, project_quota_tests=True))
        for workload in workloads(task):
            (name, _), = workload["scenario"].items()
            contexts = workload.get("contexts", {})
            with self.subTest(scenario=name):
                users = contexts["users"]
                self.assertEqual(PROJECT, users["project"])
                self.assertNotIn("tenants", users)
                self.assertGreaterEqual(users["users_per_tenant"], 1)
                self.assertNotIn("quotas", contexts)

    def test_project_is_required(self):
        self.assertRaises(jinja2.UndefinedError, render_task, {},
                          project=False)

    def test_project_quota_tests_are_opt_in(self):
        quota_writers = ("Quotas.nova_update", "Quotas.nova_update_and_delete",
                         "Quotas.cinder_update",
                         "Quotas.cinder_update_and_delete")

        def scenarios(task):
            return {name for workload in workloads(task)
                    for name in workload["scenario"]}

        default = scenarios(render_task({}))
        enabled = scenarios(render_task({"project_quota_tests": True}))
        for name in quota_writers:
            self.assertNotIn(name, default)
            self.assertIn(name, enabled)
        self.assertIn("Quotas.nova_get", default)
        self.assertIn("Quotas.cinder_get", default)

    def test_optional_workloads(self):
        default = {s["title"] for s in render_task({})["subtasks"]}
        self.assertNotIn("CinderVolumes.create_volume_backup tests", default)
        self.assertIn("NovaServers.boot_and_associate_floating_ip tests",
                      default)
        without_fip = {s["title"] for s in
                       render_task({"floating_network": ""})["subtasks"]}
        self.assertNotIn("NovaServers.boot_and_associate_floating_ip tests",
                         without_fip)


class UltimumAZWrapperTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = pathlib.Path(directory.name)
        self.bin_dir = self.directory / "bin"
        self.bin_dir.mkdir()
        self.data_dir = self.directory / "data"
        self.data_dir.mkdir()
        self.log = self.directory / "commands.jsonl"
        rally = self.bin_dir / "rally"
        rally.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
            import json
            import os
            import sys

            args = sys.argv[1:]
            with open(os.environ["COMMAND_LOG"], "a") as stream:
                stream.write(json.dumps(args) + "\\n")
            if args[:1] == ["--plugin-paths"]:
                args = args[2:]
            if args[:2] == ["task", "validate"]:
                sys.exit(int(os.environ.get("VALIDATE_RC", "0")))
            if args[:2] == ["task", "start"]:
                sys.exit(int(os.environ.get("START_RC", "0")))
            if args[:2] == ["task", "list"]:
                print("test-task-uuid")
            """))
        rally.chmod(0o755)
        # The wrapper reads ultimum.yaml with PyYAML from this interpreter.
        (self.bin_dir / "python3").symlink_to(sys.executable)

    def run_wrapper(self, **overrides):
        self.log.write_text("")
        env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "COMMAND_LOG": str(self.log),
            "RALLY_DATA_DIR": str(self.data_dir),
            "RALLY_TASK_DIR": str(ROOT / "tasks"),
            "RALLY_AZ_PLUGIN_DIR": str(PLUGIN_DIR),
            "RALLY_PROJECT_NAME": PROJECT["name"],
            **overrides,
        }
        result = subprocess.run(["bash", str(WRAPPER)], env=env,
                                capture_output=True, text=True, timeout=10)
        calls = [json.loads(line)
                 for line in self.log.read_text().splitlines()]
        return result, calls

    def task_calls(self, calls):
        return [call for call in calls if call[2:4] in (
            ["task", "validate"], ["task", "start"])]

    def test_validates_and_starts_the_az_task_with_plugins(self):
        result, calls = self.run_wrapper(
            RALLY_AVAILABILITY_ZONE="zone-a",
            RALLY_FLAVOR_NAME="m1.small",
            RALLY_VOLUME_AVAILABILITY_ZONE="zone-b",
            RALLY_SERVICES="nova,cinder")
        self.assertEqual(0, result.returncode, result.stdout)
        task_calls = self.task_calls(calls)
        self.assertEqual(["validate", "start"],
                         [call[3] for call in task_calls])
        for call in task_calls:
            self.assertEqual(["--plugin-paths", PLUGINS], call[:2])
            self.assertEqual([str(TASK), "--task-args"], call[4:6])
            args = json.loads(call[6])
            self.assertEqual(["nova", "cinder"], args["services"])
            self.assertEqual("rally-az", args["project_name"])
            self.assertEqual("Default", args["project_domain"])
            self.assertFalse(args["project_quota_tests"])
            self.assertEqual("zone-a", args["availability_zone"])
            self.assertEqual("zone-b", args["volume_availability_zone"])
            self.assertEqual("zone-a", args["octavia_availability_zone"])
            self.assertEqual("m1.small", args["flavor_name"])
            self.assertEqual(str(ROOT / "tasks" / "ultimum" / "az" / "heat"),
                             args["heat_template_dir"])
            self.assertTrue(list(workloads(render_task(args))))

    def test_volume_zone_defaults_to_compute_zone_and_can_be_cleared(self):
        _, calls = self.run_wrapper(RALLY_AVAILABILITY_ZONE="zone-a")
        args = json.loads(self.task_calls(calls)[0][6])
        self.assertEqual("zone-a", args["volume_availability_zone"])
        _, calls = self.run_wrapper(RALLY_VOLUME_AVAILABILITY_ZONE="")
        args = json.loads(self.task_calls(calls)[0][6])
        self.assertEqual("", args["volume_availability_zone"])

    def test_heat_image_prefers_prepared_cirros(self):
        _, calls = self.run_wrapper(CIRROS_VERSION="0.6.2")
        args = json.loads(self.task_calls(calls)[0][6])
        self.assertTrue(args["heat_image_location"].startswith("https://"))
        image = (self.data_dir / "extra"
                 / "cirros-0.6.2-x86_64-disk.img")
        image.parent.mkdir()
        image.write_text("cached test image")
        _, calls = self.run_wrapper(CIRROS_VERSION="0.6.2")
        args = json.loads(self.task_calls(calls)[0][6])
        self.assertEqual(str(image), args["heat_image_location"])

    def test_unknown_service_does_not_start_a_task(self):
        result, calls = self.run_wrapper(RALLY_SERVICES="nova,swift")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Unknown service 'swift'", result.stdout)
        self.assertFalse(self.task_calls(calls))

    def test_failures_are_returned(self):
        result, calls = self.run_wrapper(START_RC="7")
        self.assertEqual(7, result.returncode, result.stdout)
        result, calls = self.run_wrapper(VALIDATE_RC="9")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(["validate"],
                         [call[3] for call in self.task_calls(calls)])

    def test_missing_plugin_does_not_start_a_task(self):
        result, calls = self.run_wrapper(
            RALLY_AZ_PLUGIN_DIR=str(self.directory))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Plugin not found", result.stdout)
        self.assertFalse(self.task_calls(calls))

    def test_project_comes_from_ultimum_yaml(self):
        config = self.directory / "ultimum.yaml"
        config.write_text(textwrap.dedent("""\
            schema_version: 1
            az_validation:
              project:
                name: yaml-project
                domain: YamlDomain
            """))
        result, calls = self.run_wrapper(RALLY_PROJECT_NAME="",
                                         RALLY_ULTIMUM_CONFIG=str(config))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        args = json.loads(self.task_calls(calls)[0][6])
        self.assertEqual("yaml-project", args["project_name"])
        self.assertEqual("YamlDomain", args["project_domain"])

    def test_project_falls_back_to_identity_project(self):
        config = self.directory / "ultimum.yaml"
        config.write_text(textwrap.dedent("""\
            schema_version: 1
            create_project: false
            identity:
              project:
                name: identity-project
            """))
        _, calls = self.run_wrapper(RALLY_PROJECT_NAME="",
                                    RALLY_ULTIMUM_CONFIG=str(config))
        args = json.loads(self.task_calls(calls)[0][6])
        self.assertEqual("identity-project", args["project_name"])
        self.assertEqual("Default", args["project_domain"])

    def test_missing_project_does_not_start_a_task(self):
        result, calls = self.run_wrapper(
            RALLY_PROJECT_NAME="",
            RALLY_ULTIMUM_CONFIG=str(self.directory / "missing.yaml"))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("RALLY_PROJECT_NAME", result.stdout)
        self.assertFalse(self.task_calls(calls))
