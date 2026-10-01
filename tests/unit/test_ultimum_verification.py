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
SERVICES = ("cinder", "glance", "neutron", "nova")


def render_task(path, args):
    template = jinja2.Environment(
        undefined=jinja2.StrictUndefined).from_string(path.read_text())
    return yaml.safe_load(template.render(**args))


def workloads(task):
    for subtask in task["subtasks"]:
        yield from subtask.get("workloads", [subtask])


class UltimumVerificationTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = pathlib.Path(directory.name)
        self.bin_dir = self.directory / "bin"
        self.bin_dir.mkdir()
        self.data_dir = self.directory / "data"
        extra_dir = self.data_dir / "extra"
        extra_dir.mkdir(parents=True)
        for suffix in ("img", "raw"):
            (extra_dir / f"cirros-0.6.2-x86_64-disk.{suffix}").write_text(
                "cached test image")
        self.log = self.directory / "commands.jsonl"
        rally = self.bin_dir / "rally"
        rally.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
            import json
            import os
            import sys

            args = sys.argv[1:]
            with open(os.environ["COMMAND_LOG"], "a") as stream:
                stream.write(json.dumps(args) + "\\n")
            if args[:2] == ["task", "validate"]:
                sys.exit(int(os.environ.get("VALIDATE_RC", "0")))
            if args[:2] == ["task", "start"]:
                sys.exit(int(os.environ.get("START_RC", "0")))
            if args[:2] == ["task", "list"]:
                print("test-task-uuid")
            """))
        rally.chmod(0o755)
        for command, status in (("qemu-img", 0), ("curl", 99)):
            stub = self.bin_dir / command
            stub.write_text(f"#!/bin/sh\nexit {status}\n")
            stub.chmod(0o755)

    def run_wrapper(self, service, **overrides):
        self.log.write_text("")
        env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "COMMAND_LOG": str(self.log),
            "RALLY_DATA_DIR": str(self.data_dir),
            "RALLY_TASK_DIR": str(ROOT / "tasks"),
            "CIRROS_VERSION": "0.6.2",
            **overrides,
        }
        result = subprocess.run(
            ["bash", str(ROOT / "ultimum"
                         / f"ultimum-rally-{service}-verification")],
            env=env, capture_output=True, text=True, timeout=10)
        calls = [json.loads(line)
                 for line in self.log.read_text().splitlines()]
        return result, calls

    def test_wrappers_validate_and_start_only_their_ultimum_task(self):
        for service in SERVICES:
            with self.subTest(service=service):
                result, calls = self.run_wrapper(service)
                self.assertEqual(0, result.returncode, result.stdout)
                expected_path = str(
                    ROOT / "tasks" / "ultimum" / f"{service}-production.yaml")
                task_calls = [call for call in calls
                              if call[:2] in (["task", "validate"],
                                              ["task", "start"])]
                self.assertEqual(["validate", "start"],
                                 [call[1] for call in task_calls])
                for call in task_calls:
                    self.assertEqual(expected_path, call[2])
                    self.assertEqual("--task-args", call[3])
                    task = render_task(pathlib.Path(call[2]),
                                       json.loads(call[4]))
                    self.assertEqual(2, task["version"])
                    self.assertTrue(list(workloads(task)))

    def test_task_start_failure_is_returned(self):
        for service in SERVICES:
            with self.subTest(service=service):
                result, calls = self.run_wrapper(service, START_RC="7")
                self.assertEqual(7, result.returncode, result.stdout)
                self.assertEqual(1, sum(call[:2] == ["task", "start"]
                                        for call in calls))

    def test_failed_validation_does_not_start_a_task(self):
        for service in SERVICES:
            with self.subTest(service=service):
                result, calls = self.run_wrapper(service, VALIDATE_RC="9")
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(any(call[:2] == ["task", "start"]
                                     for call in calls))

    def test_missing_task_does_not_start_another_suite(self):
        for service in SERVICES:
            with self.subTest(service=service):
                result, calls = self.run_wrapper(
                    service, RALLY_TASK_DIR=str(self.directory / "missing"))
                self.assertNotEqual(0, result.returncode)
                self.assertIn("Rally task not found", result.stdout)
                self.assertFalse(any(call[:2] == ["task", "start"]
                                     for call in calls))

    def test_glance_uses_selected_image_paths_and_web_download_url(self):
        result, calls = self.run_wrapper("glance")
        self.assertEqual(0, result.returncode, result.stdout)
        start = next(call for call in calls if call[:2] == ["task", "start"])
        args = json.loads(start[4])
        task = render_task(pathlib.Path(start[2]), args)
        web_downloads = []
        local_images = []
        for workload in workloads(task):
            params = next(iter(workload["scenario"].values()))
            if "image_location" not in params:
                continue
            location = params["image_location"]
            if params.get("import_method") == "web-download":
                web_downloads.append(location)
                self.assertTrue(location.startswith("https://"))
                self.assertIn("0.6.2", location)
            else:
                local_images.append(location)
                self.assertIn(location,
                              [args["qcow2_image"], args["raw_image"]])
                self.assertTrue(location.startswith(str(self.data_dir)))
        self.assertTrue(web_downloads)
        self.assertTrue(local_images)

    def test_optional_backup_and_floating_ip_workloads(self):
        for service, argument, value, scenario in (
            ("cinder", "cinder_backup", True,
             "CinderVolumes.create_volume_backup"),
            ("nova", "floating_network", "external-test",
             "NovaServers.boot_and_associate_floating_ip"),
        ):
            with self.subTest(service=service):
                path = (ROOT / "tasks" / "ultimum"
                        / f"{service}-production.yaml")
                default = render_task(path, {})
                enabled = render_task(path, {argument: value})
                self.assertFalse(any(scenario in w["scenario"]
                                     for w in workloads(default)))
                self.assertTrue(any(scenario in w["scenario"]
                                    for w in workloads(enabled)))
